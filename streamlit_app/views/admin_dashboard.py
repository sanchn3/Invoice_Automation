"""
admin_dashboard.py
==================
Full admin dashboard with pipeline board, invoice approval,
QuickBooks export, reporting, and rate card editor.
"""

import streamlit as st
from datetime import datetime, timedelta, timezone
from collections import defaultdict

from data_manager import DataManager
from invoice_logic.charge_calculator import calculate_charges
from invoice_logic.iif_exporter import generate_iif
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


def render(dm: DataManager) -> None:
    st.title("📦 Invoice Automation — Admin Dashboard")

    tab_pipeline, tab_approve, tab_export, tab_report, tab_rates = st.tabs([
        "🗂 Pipeline",
        "✅ Approve & Invoice",
        "📤 QuickBooks Export",
        "📊 Reports",
        "💲 Rate Card",
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

        if not email_logs:
            st.info("No emails received yet.")
            return

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
                    qb_num = st.text_input(
                        "QuickBooks Invoice Number (enter from QuickBooks Desktop)",
                        key=f"qb_{ci['id']}",
                        placeholder="e.g. 1042",
                        help="NEVER auto-generated. Enter manually from QuickBooks.",
                    )

                    if st.button("Generate Client Invoice", key=f"gen_{ci['id']}", type="primary"):
                        if not qb_num.strip():
                            st.error("Enter the QuickBooks invoice number before generating.")
                        elif ci.get("service_type") is None:
                            st.error("Service type has not been set. Go to the section above.")
                        else:
                            charges = calculate_charges(
                                dm=dm,
                                service_type=ci["service_type"],
                                pallet_count=int(ci.get("pallet_count", 1)),
                                temp_recorder=bool(ci.get("temp_recorder", False)),
                                extra_charges=ci.get("extra_charges", []),
                                damaged_pallets=int(ci.get("damaged_pallets", 0)),
                                broken_pallets=int(ci.get("broken_pallets", 0)),
                            )
                            dm.update_client_invoice(ci["id"], {
                                "quickbooks_invoice_number": qb_num.strip(),
                                "line_items": charges["line_items"],
                                "subtotal"  : charges["subtotal"],
                                "total"     : charges["total"],
                                "status"    : "invoiced",
                                "invoice_date": datetime.utcnow().date().isoformat(),
                            })
                            if prov.get("email_intake_id"):
                                dm.update_email_log(prov["email_intake_id"], {"status": "invoiced"})
                            st.success(f"Client invoice generated! Total: ${charges['total']:,.2f}")
                            st.rerun()
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
                if st.checkbox(label, key=f"exp_{ci['id']}"):
                    selected_ids.append(ci["id"])

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
                st.text(
                    f"QB #{ci.get('quickbooks_invoice_number', '—')}  "
                    f"| {ci.get('client_name', '—')}  "
                    f"| ${ci.get('total', 0):,.2f}  "
                    f"| {ci.get('invoice_date', '—')}"
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
        st.caption("Edit rates here. Changes take effect on the next invoice generated.")

        rates = dm.get_rate_card()

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

        updated: dict[str, float] = {}
        col1, col2 = st.columns(2)
        items = list(labels.items())
        for i, (key, label) in enumerate(items):
            col = col1 if i < len(items) // 2 + len(items) % 2 else col2
            updated[key] = col.number_input(
                label=f"{label} ($)",
                value=float(rates.get(key, 0)),
                min_value=0.0,
                step=0.25,
                format="%.2f",
                key=f"rate_{key}",
            )

        if st.button("💾 Save Rate Card", type="primary"):
            dm.update_rate_card(updated)
            st.success("Rate card updated successfully.")
