"""
admin_dashboard.py
==================
Full admin dashboard with pipeline board, invoice approval,
QuickBooks export, reporting, and rate card editor.
"""

import streamlit as st
from datetime import datetime, timedelta, timezone
from collections import defaultdict

from pathlib import Path

from data_manager import DataManager
from invoice_logic.charge_calculator import calculate_charges
from invoice_logic.iif_exporter import generate_iif
from invoice_logic.pdf_generator import generate_pdf
from email_pipeline.attachment_handler import process_pdf_from_path
from alerting.alert_manager import AlertManager
from streamlit_app.components.status_badge import status_badge, STATUS_ORDER
from streamlit_app.components.invoice_card import invoice_card

_STUCK_HOURS = 24


def _is_stuck(log: dict) -> bool:
    terminal = {"invoiced", "exported_to_qb"}
    if log.get("status") in terminal:
        return False
    created = log.get("created_at", "")
    if not created:
        return False
    try:
        dt = datetime.fromisoformat(created.replace("Z", "+00:00"))
        return dt < datetime.now(timezone.utc) - timedelta(hours=_STUCK_HOURS)
    except ValueError:
        return False


def render(dm: DataManager, alert_manager: AlertManager | None = None) -> None:
    st.title("📦 Invoice Automation — Admin Dashboard")

    tab_pipeline, tab_approve, tab_export, tab_report, tab_rates, tab_settings = st.tabs([
        "🗂 Pipeline",
        "✅ Approve & Invoice",
        "📤 QuickBooks Export",
        "📊 Reports",
        "💲 Rate Card",
        "⚙️ Settings",
    ])

    # ──────────────────────────────────────────────────────────────────────────
    # TAB 1 — PIPELINE BOARD
    # ──────────────────────────────────────────────────────────────────────────
    with tab_pipeline:
        st.subheader("Invoice Pipeline")

        email_logs       = dm.get_email_logs()
        provider_invs    = {pi["id"]: pi for pi in dm.get_provider_invoices()}
        client_invs_list = dm.get_client_invoices()
        client_by_prov   = {ci["provider_invoice_id"]: ci for ci in client_invs_list if ci.get("provider_invoice_id")}

        # ── Pending Review section ─────────────────────────────────────────────
        pending_review = [log for log in email_logs if log.get("status") == "pending_review"]
        if pending_review:
            st.warning(f"⚠️ {len(pending_review)} email(s) need your review")
            for log in pending_review:
                with st.expander(f"🔍 {log.get('subject', 'No subject')} — from {log.get('sender', '')}"):
                    st.caption(f"Received: {log.get('received_at', '')[:16].replace('T', ' ')}")
                    reason = log.get("error_text", "Flagged for review")
                    st.info(f"Reason flagged: {reason}")

                    pdf_path = log.get("pdf_local_path")
                    if pdf_path and Path(pdf_path).exists():
                        pdf_bytes = Path(pdf_path).read_bytes()
                        st.download_button(
                            label    ="⬇ Download PDF to review",
                            data     =pdf_bytes,
                            file_name=log.get("pdf_filename", "invoice.pdf"),
                            mime     ="application/pdf",
                            key      =f"review_pdf_{log['id']}",
                        )
                    else:
                        st.caption("No PDF attachment found for this email.")

                    col_accept, col_decline = st.columns(2)
                    with col_accept:
                        if st.button("✅ Accept as Invoice", key=f"accept_{log['id']}", type="primary"):
                            if not pdf_path or not Path(pdf_path).exists():
                                st.error("No PDF available to process. Cannot accept without a PDF.")
                            else:
                                _am = alert_manager or AlertManager()
                                success = process_pdf_from_path(pdf_path, log["id"], dm, _am)
                                if success:
                                    st.success("Invoice accepted and added to the pipeline.")
                                else:
                                    st.error("Processing failed — check the PDF and try again.")
                                st.rerun()
                    with col_decline:
                        if st.button("❌ Decline", key=f"decline_{log['id']}"):
                            dm.update_email_log(log["id"], {
                                "status"    : "rejected",
                                "error_text": "Manually rejected by admin.",
                            })
                            st.rerun()

            st.markdown("---")

        if not email_logs:
            st.info("No emails received yet.")
        else:
            # Group by status
            by_status: dict[str, list[dict]] = defaultdict(list)
            for log in email_logs:
                by_status[log.get("status", "received")].append(log)

            # Sort statuses by pipeline order
            ordered_statuses = [s for s in STATUS_ORDER if s in by_status]
            for s in by_status:
                if s not in ordered_statuses:
                    ordered_statuses.append(s)

            cols = st.columns(max(len(ordered_statuses), 1))
            for col, status in zip(cols, ordered_statuses):
                with col:
                    count = len(by_status[status])
                    st.markdown(f"**{status.replace('_', ' ').title()}** ({count})")
                    for log in by_status[status]:
                        stuck = _is_stuck(log)
                        prov_inv = None
                        cli_inv  = None

                        # Find linked invoices via email_intake_id
                        for pi in provider_invs.values():
                            if pi.get("email_intake_id") == log["id"]:
                                prov_inv = pi
                                cli_inv  = client_by_prov.get(pi["id"])
                                break

                        prefix = "⚠️ " if stuck else ""
                        subject = log.get("subject", "No subject")
                        short   = f"{prefix}{subject[:30]}{'...' if len(subject) > 30 else ''}"

                        with st.expander(short):
                            if stuck:
                                st.warning(f"Stuck for {_STUCK_HOURS}+ hours!")
                            status_badge(log.get("status", ""))
                            st.caption(f"From: {log.get('sender', '')}  |  {log.get('received_at', '')[:16].replace('T', ' ')}")
                            if log.get("error_text"):
                                st.error(log["error_text"])
                            if prov_inv:
                                st.text(f"Invoice #: {prov_inv.get('invoice_number', '—')}")
                                st.text(f"Client: {prov_inv.get('client_name', '—')}")
                                st.text(f"Total: ${prov_inv.get('total', 0):,.2f}")

    # ──────────────────────────────────────────────────────────────────────────
    # TAB 2 — APPROVE & GENERATE INVOICE
    # ──────────────────────────────────────────────────────────────────────────
    with tab_approve:
        st.subheader("Set Service Details / Approve Jobs")

        client_invoices  = dm.get_client_invoices()
        provider_invoices = dm.get_provider_invoices()
        prov_by_id       = {pi["id"]: pi for pi in provider_invoices}

        # ── Section A: set service type for pending_worker invoices ───────────
        pending_worker = [ci for ci in client_invoices if ci.get("status") == "pending_worker"]

        if pending_worker:
            st.markdown("#### Set Service Type")
            st.caption("These invoices have arrived but the admin has not yet set the service type.")

            for ci in pending_worker:
                prov = prov_by_id.get(ci.get("provider_invoice_id", ""), {})
                with st.expander(f"{ci.get('client_name', 'Unknown')} — {prov.get('invoice_number', '—')}"):
                    col1, col2 = st.columns(2)
                    with col1:
                        st.metric("Provider",   prov.get("provider_name", "—"))
                        st.metric("Invoice #",  prov.get("invoice_number", "—"))
                    with col2:
                        st.metric("Client",     ci.get("client_name", "—"))
                        st.metric("Prov Total", f"${prov.get('total', 0):,.2f}")

                    service_key = f"svc_{ci['id']}"
                    temp_key    = f"tmp_{ci['id']}"

                    current_svc = ci.get("service_type") or "in_out"
                    service_type = st.radio(
                        "Service Type",
                        options=["in_out", "transfer"],
                        format_func=lambda x: "In-Out Storage" if x == "in_out" else "Transfer (Truck-to-Truck)",
                        index=0 if current_svc == "in_out" else 1,
                        key=service_key,
                        horizontal=True,
                    )
                    temp_recorder = st.checkbox(
                        "Temperature Recorder installed in outbound truck",
                        value=ci.get("temp_recorder", False),
                        key=temp_key,
                    )

                    if st.button("Save Service Details", key=f"save_{ci['id']}"):
                        dm.update_client_invoice(ci["id"], {
                            "service_type" : service_type,
                            "temp_recorder": temp_recorder,
                        })
                        st.success("Service details saved.")
                        st.rerun()
        else:
            st.info("No invoices pending service type assignment.")

        st.markdown("---")

        # ── Section B: approve ready_to_invoice jobs ──────────────────────────
        ready = [ci for ci in client_invoices if ci.get("status") == "ready_to_invoice"]

        if ready:
            st.markdown("#### Approve & Generate Client Invoice")
            st.caption("Worker has submitted job details. Enter the QuickBooks invoice number and generate.")

            for ci in ready:
                prov = prov_by_id.get(ci.get("provider_invoice_id", ""), {})
                with st.expander(f"✅ {ci.get('client_name', 'Unknown')} — Worker Submitted"):
                    st.markdown("**Worker-submitted details:**")
                    c1, c2, c3 = st.columns(3)
                    c1.metric("Pallets",          ci.get("pallet_count", 0))
                    c2.metric("Damaged Pallets",  ci.get("damaged_pallets", 0))
                    c3.metric("Broken Pallets",   ci.get("broken_pallets", 0))

                    extras = ci.get("extra_charges", [])
                    if extras:
                        st.text("Extra charges: " + ", ".join(e.replace("_", " ").title() for e in extras))
                    if ci.get("worker_notes"):
                        st.text(f"Notes: {ci['worker_notes']}")

                    photos = ci.get("photo_paths", [])
                    if photos:
                        st.markdown(f"**Photos ({len(photos)})**")
                        photo_cols = st.columns(min(len(photos), 4))
                        for ph_col, ph_path in zip(photo_cols, photos[:4]):
                            try:
                                ph_col.image(ph_path, use_container_width=True)
                            except Exception:
                                ph_col.caption(ph_path)

                    st.markdown("---")

                    # Allow setting service type here if it wasn't set earlier
                    current_svc = ci.get("service_type") or "in_out"
                    service_type_b = st.radio(
                        "Service Type",
                        options=["in_out", "transfer"],
                        format_func=lambda x: "In-Out Storage" if x == "in_out" else "Transfer (Truck-to-Truck)",
                        index=0 if current_svc == "in_out" else 1,
                        key=f"svc_b_{ci['id']}",
                        horizontal=True,
                    )
                    temp_recorder_b = st.checkbox(
                        "Temperature Recorder installed in outbound truck",
                        value=ci.get("temp_recorder", False),
                        key=f"tmp_b_{ci['id']}",
                    )

                    qb_num = st.text_input(
                        "QuickBooks Invoice Number (enter from QuickBooks Desktop)",
                        key=f"qb_{ci['id']}",
                        placeholder="e.g. 1042",
                        help="NEVER auto-generated. Enter manually from QuickBooks.",
                    )

                    if st.button("Generate Client Invoice", key=f"gen_{ci['id']}", type="primary"):
                        if not qb_num.strip():
                            st.error("Enter the QuickBooks invoice number before generating.")
                        else:
                            charges = calculate_charges(
                                dm=dm,
                                service_type=service_type_b,
                                pallet_count=int(ci.get("pallet_count", 1)),
                                temp_recorder=temp_recorder_b,
                                extra_charges=ci.get("extra_charges", []),
                                damaged_pallets=int(ci.get("damaged_pallets", 0)),
                                broken_pallets=int(ci.get("broken_pallets", 0)),
                                client_name=ci.get("client_name", ""),
                            )
                            updated_inv = {
                                "quickbooks_invoice_number": qb_num.strip(),
                                "service_type" : service_type_b,
                                "temp_recorder": temp_recorder_b,
                                "line_items"   : charges["line_items"],
                                "subtotal"     : charges["subtotal"],
                                "total"        : charges["total"],
                                "status"       : "invoiced",
                                "invoice_date" : datetime.utcnow().date().isoformat(),
                            }
                            dm.update_client_invoice(ci["id"], updated_inv)
                            if prov.get("email_intake_id"):
                                dm.update_email_log(prov["email_intake_id"], {"status": "invoiced"})
                            st.success(f"Client invoice generated! Total: ${charges['total']:,.2f}")
                            st.rerun()

                    # PDF download for already-invoiced items in this section
                    if ci.get("status") == "invoiced" and ci.get("quickbooks_invoice_number"):
                        pdf_bytes = generate_pdf(ci)
                        fname = f"INCO_Invoice_{ci.get('quickbooks_invoice_number', ci['id'])}.pdf"
                        st.download_button(
                            label    ="⬇ Download PDF Invoice",
                            data     =pdf_bytes,
                            file_name=fname,
                            mime     ="application/pdf",
                            key      =f"pdf_approve_{ci['id']}",
                        )
        else:
            st.info("No jobs ready for invoicing.")

    # ──────────────────────────────────────────────────────────────────────────
    # TAB 3 — QUICKBOOKS EXPORT
    # ──────────────────────────────────────────────────────────────────────────
    with tab_export:
        st.subheader("Export to QuickBooks")

        client_invoices = dm.get_client_invoices()
        exportable      = [
            ci for ci in client_invoices
            if ci.get("status") == "invoiced" and not ci.get("quickbooks_exported")
        ]

        if not exportable:
            st.info("No invoices ready to export.")
        else:
            st.caption(f"{len(exportable)} invoice(s) ready for export.")

            selected_ids: list[str] = []
            for ci in exportable:
                qb = ci.get("quickbooks_invoice_number", "—")
                label = f"QB #{qb} — {ci.get('client_name', '—')} — ${ci.get('total', 0):,.2f}"
                col_chk, col_pdf = st.columns([4, 1])
                with col_chk:
                    if st.checkbox(label, key=f"exp_{ci['id']}"):
                        selected_ids.append(ci["id"])
                with col_pdf:
                    pdf_bytes = generate_pdf(ci)
                    fname = f"INCO_Invoice_{qb}.pdf"
                    st.download_button(
                        label    ="⬇ PDF",
                        data     =pdf_bytes,
                        file_name=fname,
                        mime     ="application/pdf",
                        key      =f"pdf_exp_{ci['id']}",
                    )

            if selected_ids:
                if st.button(f"Export {len(selected_ids)} invoice(s) to IIF", type="primary"):
                    try:
                        iif_path = generate_iif(selected_ids, dm)
                        iif_content = open(iif_path, "r", encoding="utf-8").read()
                        st.success(f"IIF file generated: {iif_path}")
                        st.download_button(
                            label    ="⬇ Download IIF File",
                            data     =iif_content,
                            file_name=iif_path.split("\\")[-1].split("/")[-1],
                            mime     ="text/plain",
                        )
                        st.rerun()
                    except ValueError as e:
                        st.error(str(e))
                    except Exception as e:
                        st.error(f"Export failed: {e}")

        st.markdown("---")
        st.subheader("Export History")
        exported = [ci for ci in dm.get_client_invoices() if ci.get("quickbooks_exported")]
        if exported:
            for ci in sorted(exported, key=lambda x: x.get("created_at", ""), reverse=True):
                qb = ci.get("quickbooks_invoice_number", "—")
                col_info, col_pdf = st.columns([5, 1])
                with col_info:
                    st.text(
                        f"QB #{qb}  "
                        f"| {ci.get('client_name', '—')}  "
                        f"| ${ci.get('total', 0):,.2f}  "
                        f"| {ci.get('invoice_date', '—')}"
                    )
                with col_pdf:
                    pdf_bytes = generate_pdf(ci)
                    st.download_button(
                        label    ="⬇ PDF",
                        data     =pdf_bytes,
                        file_name=f"INCO_Invoice_{qb}.pdf",
                        mime     ="application/pdf",
                        key      =f"pdf_hist_{ci['id']}",
                    )
        else:
            st.caption("No invoices exported yet.")

    # ──────────────────────────────────────────────────────────────────────────
    # TAB 4 — REPORTS
    # ──────────────────────────────────────────────────────────────────────────
    with tab_report:
        st.subheader("Reports")

        all_ci = dm.get_client_invoices()

        if not all_ci:
            st.info("No invoice data yet.")
        else:
            # ── By client ─────────────────────────────────────────────────────
            st.markdown("#### Invoices by Client")
            client_counts: dict[str, int] = defaultdict(int)
            client_totals: dict[str, float] = defaultdict(float)
            for ci in all_ci:
                client = ci.get("client_name", "Unknown")
                client_counts[client] += 1
                client_totals[client] += float(ci.get("total", 0))

            col1, col2 = st.columns(2)
            with col1:
                st.bar_chart(client_counts)
            with col2:
                for client, total in sorted(client_totals.items(), key=lambda x: -x[1]):
                    st.metric(client, f"${total:,.2f}")

            st.markdown("---")

            # ── By service type ───────────────────────────────────────────────
            st.markdown("#### By Service Type")
            svc_counts: dict[str, int] = defaultdict(int)
            for ci in all_ci:
                svc = ci.get("service_type") or "not_set"
                svc_counts[svc] += 1
            c1, c2, c3 = st.columns(3)
            c1.metric("In-Out",     svc_counts.get("in_out", 0))
            c2.metric("Transfer",   svc_counts.get("transfer", 0))
            c3.metric("Not Set",    svc_counts.get("not_set", 0))

            st.markdown("---")

            # ── By week ───────────────────────────────────────────────────────
            st.markdown("#### Invoices by Week")
            week_counts: dict[str, int] = defaultdict(int)
            for ci in all_ci:
                date_str = ci.get("invoice_date", ci.get("created_at", ""))[:10]
                if date_str:
                    try:
                        dt   = datetime.fromisoformat(date_str)
                        week = dt.strftime("%Y-W%W")
                        week_counts[week] += 1
                    except ValueError:
                        pass
            if week_counts:
                st.bar_chart(dict(sorted(week_counts.items())))

            st.markdown("---")

            # ── Extra charges frequency ───────────────────────────────────────
            st.markdown("#### Extra Charges Frequency")
            charge_counts: dict[str, int] = defaultdict(int)
            for ci in all_ci:
                for charge in ci.get("extra_charges", []):
                    charge_counts[charge.replace("_", " ").title()] += 1
            if charge_counts:
                st.bar_chart(charge_counts)
            else:
                st.caption("No extra charges recorded yet.")

    # ──────────────────────────────────────────────────────────────────────────
    # TAB 5 — RATE CARD EDITOR
    # ──────────────────────────────────────────────────────────────────────────
    with tab_rates:
        st.subheader("Rate Card")

        # ── Debug: confirm data is loading ────────────────────────────────────
        _debug_rates = dm.get_rate_card()
        if not _debug_rates:
            st.error("⚠️ Rate card file not found or empty. Check that data/rate_card.json exists.")
        else:
            st.caption(f"Loaded {len(_debug_rates)} rate entries from file.")

        labels = {
            "in_out"                : "In-Out Storage (per pallet)",
            "transfer"              : "Transfer (per pallet)",
            "temp_recorder_fee"     : "Temperature Recorder",
            "quality_inspection_fee": "Quality Inspection",
            "pallet_cleaning_fee"   : "Pallet Cleaning",
            "broken_pallet_fee"     : "Broken Pallet (per pallet)",
            "repacking_fee"         : "Repacking",
            "re_inspection_fee"     : "Re-Inspection",
        }

        # ── Default Rates ─────────────────────────────────────────────────────
        st.markdown("#### Default Rates")
        st.caption("Applies to all clients unless a client-specific rate is set.")

        default_rates = dm.get_rate_card()
        updated: dict[str, float] = {}
        col1, col2 = st.columns(2)
        items = list(labels.items())
        for i, (key, label) in enumerate(items):
            col = col1 if i < len(items) // 2 + len(items) % 2 else col2
            updated[key] = col.number_input(
                label=f"{label} ($)",
                value=float(default_rates.get(key, 0)),
                min_value=0.0,
                step=0.25,
                format="%.2f",
                key=f"rate_{key}",
            )

        if st.button("💾 Save Default Rates", type="primary"):
            dm.update_rate_card(updated)
            st.success("Default rates saved.")

        st.markdown("---")

        # ── Per-Client Rates ──────────────────────────────────────────────────
        st.markdown("#### Per-Client Rate Overrides")
        st.caption("Set rates for a specific client. Only fields you change here will override the defaults.")

        all_client_rates = dm.get_client_rates()

        # Show existing client overrides
        if all_client_rates:
            st.markdown("**Clients with custom rates:**")
            for cname, crates in all_client_rates.items():
                with st.expander(cname):
                    override_col1, override_col2 = st.columns(2)
                    override_items = list(labels.items())
                    new_overrides: dict[str, float] = {}
                    for i, (key, label) in enumerate(override_items):
                        col = override_col1 if i < len(override_items) // 2 + len(override_items) % 2 else override_col2
                        default_val = float(default_rates.get(key, 0))
                        current_val = float(crates.get(key, default_val))
                        is_override = key in crates
                        new_val = col.number_input(
                            label=f"{label} ($)" + (" ✏️" if is_override else ""),
                            value=current_val,
                            min_value=0.0,
                            step=0.25,
                            format="%.2f",
                            key=f"cr_{cname}_{key}",
                            help="Default: ${:.2f}".format(default_val),
                        )
                        if new_val != default_val:
                            new_overrides[key] = new_val

                    btn_col1, btn_col2 = st.columns(2)
                    if btn_col1.button("💾 Save", key=f"save_cr_{cname}", type="primary"):
                        dm.set_client_rates(cname, new_overrides)
                        st.success(f"Rates saved for {cname}.")
                        st.rerun()
                    if btn_col2.button("🗑 Remove overrides", key=f"del_cr_{cname}"):
                        dm.delete_client_rates(cname)
                        st.success(f"Custom rates removed for {cname}. Now using defaults.")
                        st.rerun()
        else:
            st.info("No client-specific rates set yet.")

        st.markdown("---")
        st.markdown("**Add rates for a new client:**")
        new_client_name = st.text_input("Client name", placeholder="e.g. Walmart", key="new_client_name")

        if new_client_name.strip():
            new_col1, new_col2 = st.columns(2)
            new_client_overrides: dict[str, float] = {}
            new_items = list(labels.items())
            for i, (key, label) in enumerate(new_items):
                col = new_col1 if i < len(new_items) // 2 + len(new_items) % 2 else new_col2
                default_val = float(default_rates.get(key, 0))
                new_val = col.number_input(
                    label=f"{label} ($)",
                    value=default_val,
                    min_value=0.0,
                    step=0.25,
                    format="%.2f",
                    key=f"new_cr_{key}",
                )
                if new_val != default_val:
                    new_client_overrides[key] = new_val

            if st.button("💾 Save Client Rates", type="primary", key="save_new_client"):
                if not new_client_overrides:
                    st.warning("No rates differ from the defaults — nothing to save.")
                else:
                    dm.set_client_rates(new_client_name.strip(), new_client_overrides)
                    st.success(f"Custom rates saved for {new_client_name.strip()}.")
                    st.rerun()

    # ──────────────────────────────────────────────────────────────────────────
    # TAB 6 — SETTINGS
    # ──────────────────────────────────────────────────────────────────────────
    with tab_settings:
        st.subheader("Settings")

        st.markdown("#### Clear Pipeline Data")
        st.caption(
            "Removes all emails, provider invoices, and client invoices. "
            "Rate card, client rates, and provider list are kept. "
            "Use this to reset between test runs."
        )

        confirm = st.checkbox("I understand this will permanently delete all pipeline data.")
        if st.button("🗑 Clear All Pipeline Data", type="primary", disabled=not confirm):
            from config import DATA_DIR, PDFS_DIR, PHOTOS_DIR
            import json

            # Clear JSON files
            for fname in ["email_intake_log.json", "provider_invoices.json", "client_invoices.json"]:
                fpath = DATA_DIR / fname
                fpath.write_text("[]", encoding="utf-8")

            # Delete downloaded PDFs and photos
            for folder in [PDFS_DIR, PHOTOS_DIR]:
                for f in folder.iterdir():
                    try:
                        f.unlink()
                    except Exception:
                        pass

            st.success("All pipeline data cleared. The dashboard will now show a fresh state.")
            st.rerun()
