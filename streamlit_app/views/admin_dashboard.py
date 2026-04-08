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

_STUCK_HOURS = 24

# Canonical client-name aliases (lowercase key → display name)
_CLIENT_ALIASES: dict[str, str] = {
    "babia ice": "Babia Ice",
    "babia"    : "Babia Ice",
}


def _canonical_client(name: str) -> str:
    """Normalise variant spellings to a single display name."""
    return _CLIENT_ALIASES.get(name.strip().lower(), name)


def _generate_unique_invoice_id(dm: DataManager) -> str:
    """Return the next sequential 5-digit numeric invoice ID."""
    numeric_ids = [
        int(ci["quickbooks_invoice_number"])
        for ci in dm.get_client_invoices()
        if ci.get("quickbooks_invoice_number", "").isdigit()
    ]
    next_id = (max(numeric_ids) + 1) if numeric_ids else 10001
    return str(next_id)


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

        poll_col, _ = st.columns([1, 4])
        with poll_col:
            if st.button("🔄 Poll Inbox Now", width='stretch'):
                from email_pipeline.outlook_listener import poll_inbox
                with st.spinner("Polling inbox..."):
                    new_count = poll_inbox(dm, alert_manager)
                st.success(f"Poll complete — {new_count} new email(s) processed.")
                st.rerun()

        email_logs       = dm.get_email_logs()
        provider_invs    = dm.get_provider_invoices()
        client_invs_list = dm.get_client_invoices()

        pending_review = [log for log in email_logs if log.get("status") == "pending_review"]
        if pending_review:
            st.warning(f"⚠️ {len(pending_review)} email(s) need your review — see Parsed tab below.")

        # ── Sub-tabs ───────────────────────────────────────────────────────────
        sub_parsed, sub_invoices = st.tabs(["📄 Parsed", "🧾 Invoices"])

        with sub_parsed:
            # Combine parsed invoices + pending-review emails, newest first
            all_items = (
                [("parsed",  pi)  for pi in provider_invs] +
                [("review",  log) for log in pending_review]
            )
            all_items.sort(key=lambda x: x[1].get("created_at", ""), reverse=True)

            if not all_items:
                st.info("No parsed invoices yet.")
            else:
                st.caption(
                    f"{len(provider_invs)} parsed"
                    + (f" · {len(pending_review)} pending review" if pending_review else "")
                )

            for item_type, item in all_items:
                iid     = item["id"]
                pdf_key = f"view_pdf_{iid}"
                del_key = f"del_prov_{iid}"

                # ── REVIEW item: yellow HTML card ──────────────────────────────────
                if item_type == "review":
                    log       = item
                    reason    = log.get("error_text") or "Flagged for review"
                    r_date    = log.get("received_at", "—")[:10]
                    r_sender  = log.get("sender", "—")
                    pdf_path  = log.get("pdf_local_path", "")
                    pdf_exists = bool(pdf_path and Path(pdf_path).exists())
                    pdf_label = "📄 Hide" if st.session_state.get(pdf_key) else "📄 PDF"

                    st.markdown(
                        f'<div style="background:#fff3cd;border:2px solid #ffc107;'
                        f'border-radius:8px;padding:10px 14px 10px 14px;margin-bottom:4px;">'
                        f'<p style="color:#856404;font-weight:700;font-size:0.85em;margin:0 0 6px 0;">'
                        f'⚠️ Needs Review &mdash; {reason}</p>'
                        f'<span style="font-size:0.9em;margin-right:20px;">📅 {r_date}</span>'
                        f'<span style="font-size:0.9em;">✉️ {r_sender}</span>'
                        f'</div>',
                        unsafe_allow_html=True,
                    )

                    rb1, rb2, rb3, rb4 = st.columns([0.7, 0.7, 0.7, 3.5])
                    rb1.button("✏️ Edit", key=f"ebtn_{iid}", disabled=True, width='stretch')

                    if pdf_exists:
                        if rb2.button(pdf_label, key=f"epdf_{iid}", width='stretch'):
                            st.session_state[pdf_key] = not st.session_state.get(pdf_key, False)
                            st.rerun()
                    else:
                        rb2.button("📄 PDF", key=f"epdf_na_{iid}", disabled=True, width='stretch')

                    if st.session_state.get(del_key):
                        rb3.caption("⚠️ Sure?")
                    else:
                        if rb3.button("🗑", key=f"delbtn_{iid}", width='stretch'):
                            st.session_state[del_key] = True
                            st.rerun()

                    if rb4.button("✅ Complete Review", key=f"complete_{iid}", type="primary", width='stretch'):
                        if not pdf_exists:
                            st.error("No PDF available to process.")
                        else:
                            _am = alert_manager or AlertManager()
                            success = process_pdf_from_path(pdf_path, iid, dm, _am)
                            if success:
                                st.success("Invoice processed and added to the pipeline.")
                            else:
                                st.error("Processing failed — check the PDF and try again.")
                            st.rerun()

                    if st.session_state.get(del_key):
                        dc1, dc2 = st.columns(2)
                        if dc1.button("✅ Yes, delete", key=f"delyes_{iid}", type="primary", width='stretch'):
                            dm.update_email_log(iid, {
                                "status"    : "rejected",
                                "error_text": "Manually rejected by admin.",
                            })
                            st.session_state.pop(del_key, None)
                            st.rerun()
                        if dc2.button("✗ Cancel", key=f"delno_{iid}", width='stretch'):
                            st.session_state.pop(del_key, None)
                            st.rerun()

                    if st.session_state.get(pdf_key) and pdf_exists:
                        from streamlit_pdf_viewer import pdf_viewer
                        pdf_viewer(Path(pdf_path).read_bytes(), key=f"pdfview_{iid}")

                # ── PARSED item: bordered container ────────────────────────────────
                else:
                    edit_key = f"edit_prov_{iid}"
                    with st.container(border=True):

                        if st.session_state.get(edit_key):
                            pi = item
                            e1, e2 = st.columns(2)
                            new_num    = e1.text_input("Invoice #", value=pi.get("invoice_number", ""), key=f"en_{iid}")
                            new_date   = e2.text_input("Date",      value=pi.get("invoice_date",   ""), key=f"ed_{iid}")
                            new_client = e1.text_input("Client",    value=pi.get("client_name",    ""), key=f"ec_{iid}")
                            new_total  = e2.number_input(
                                "Total ($)", value=float(pi.get("total", 0)),
                                min_value=0.0, step=0.01, format="%.2f", key=f"et_{iid}",
                            )
                            s1, s2 = st.columns(2)
                            if s1.button("💾 Save", key=f"esave_{iid}", type="primary", width='stretch'):
                                dm.update_provider_invoice(iid, {
                                    "invoice_number": new_num.strip(),
                                    "invoice_date"  : new_date.strip(),
                                    "client_name"   : new_client.strip(),
                                    "total"         : new_total,
                                    "subtotal"      : new_total,
                                })
                                st.session_state.pop(edit_key, None)
                                st.rerun()
                            if s2.button("✗ Cancel", key=f"ecancel_{iid}", width='stretch'):
                                st.session_state.pop(edit_key, None)
                                st.rerun()

                        else:
                            pi = item
                            pdf_path   = pi.get("pdf_local_path", "")
                            pdf_exists = bool(pdf_path and Path(pdf_path).exists())
                            pdf_label  = "📄 Hide" if st.session_state.get(pdf_key) else "📄 PDF"

                            c1, c2, c3, c4, c5, c6, c7 = st.columns([1.2, 1, 2, 1, 0.6, 0.6, 0.6])
                            c1.markdown(f"**{pi.get('invoice_number', '—')}**")
                            c2.write(pi.get("invoice_date", "—"))
                            c3.write(pi.get("client_name", "—"))
                            c4.write(f"${pi.get('total', 0):,.2f}")

                            if c5.button("✏️ Edit", key=f"ebtn_{iid}", width='stretch'):
                                st.session_state[edit_key] = True
                                st.rerun()

                            if pdf_exists:
                                if c6.button(pdf_label, key=f"epdf_{iid}", width='stretch'):
                                    st.session_state[pdf_key] = not st.session_state.get(pdf_key, False)
                                    st.rerun()
                            else:
                                c6.button("📄 PDF", key=f"epdf_na_{iid}", disabled=True, width='stretch')

                            if st.session_state.get(del_key):
                                c7.caption("⚠️ Sure?")
                            else:
                                if c7.button("🗑", key=f"delbtn_{iid}", width='stretch'):
                                    st.session_state[del_key] = True
                                    st.rerun()

                            if st.session_state.get(del_key):
                                dc1, dc2 = st.columns(2)
                                if dc1.button("✅ Yes, delete", key=f"delyes_{iid}", type="primary", width='stretch'):
                                    linked_ci = dm.get_client_invoice_by_provider_invoice_id(iid)
                                    if linked_ci:
                                        dm.delete_client_invoice(linked_ci["id"])
                                    dm.delete_provider_invoice(iid)
                                    st.session_state.pop(del_key, None)
                                    st.rerun()
                                if dc2.button("✗ Cancel", key=f"delno_{iid}", width='stretch'):
                                    st.session_state.pop(del_key, None)
                                    st.rerun()

                            if st.session_state.get(pdf_key) and pdf_exists:
                                from streamlit_pdf_viewer import pdf_viewer
                                pdf_viewer(Path(pdf_path).read_bytes(), key=f"pdfview_{iid}")

        with sub_invoices:
            sorted_cli = sorted(client_invs_list, key=lambda x: x.get("created_at", ""), reverse=True)
            if not sorted_cli:
                st.info("No client invoices yet.")
            else:
                st.caption(f"{len(sorted_cli)} client invoice(s)")
                rows = [
                    {
                        "Invoice #": ci.get("quickbooks_invoice_number") or "—",
                        "Date"     : ci.get("invoice_date", "—"),
                        "Client"   : ci.get("client_name", "—"),
                        "Total"    : f"${ci.get('total', 0):,.2f}",
                    }
                    for ci in sorted_cli
                ]
                st.dataframe(rows, use_container_width=True, hide_index=True)

    # ──────────────────────────────────────────────────────────────────────────
    # TAB 2 — APPROVE & GENERATE INVOICE
    # ──────────────────────────────────────────────────────────────────────────
    with tab_approve:
        st.subheader("Approve & Invoice")

        client_invoices   = dm.get_client_invoices()
        provider_invoices = dm.get_provider_invoices()
        prov_by_id        = {pi["id"]: pi for pi in provider_invoices}

        all_ci = sorted(client_invoices, key=lambda x: x.get("created_at", ""), reverse=True)

        if not all_ci:
            st.info("No invoices yet.")
        else:
            st.caption(f"{len(all_ci)} invoice(s) — newest first")

            for ci in all_ci:
                prov    = prov_by_id.get(ci.get("provider_invoice_id", ""), {})
                status  = ci.get("status", "")
                cid     = ci["id"]
                editable = status in ("pending_worker", "ready_to_invoice")

                # Pre-initialise so they're always in scope
                current_svc = ci.get("service_type") or "in_out"
                svc  = current_svc
                temp = ci.get("temp_recorder", False)

                confirm_key = f"confirm_del_{cid}"

                with st.container(border=True):
                    # ── Row 1: table fields ───────────────────────────────
                    r1a, r1b, r1c, r1d, r1e = st.columns([1.2, 0.9, 2, 1, 1.8])

                    with r1a:
                        inv_num = (
                            ci.get("quickbooks_invoice_number")
                            or prov.get("invoice_number", "—")
                        )
                        st.markdown(f"**{inv_num}**")
                    with r1b:
                        st.write(ci.get("invoice_date", "—"))
                    with r1c:
                        st.write(ci.get("client_name", "—"))
                    with r1d:
                        st.write(f"${ci.get('total', 0):,.2f}")
                    with r1e:
                        if editable:
                            svc = st.selectbox(
                                label="Service Type",
                                options=["in_out", "transfer"],
                                format_func=lambda x: "In-Out Storage" if x == "in_out" else "Transfer",
                                index=0 if current_svc == "in_out" else 1,
                                key=f"svc_{cid}",
                                label_visibility="collapsed",
                            )
                        else:
                            st.caption(
                                {"in_out": "In-Out Storage", "transfer": "Transfer"}.get(current_svc, "—")
                            )

                    # ── Row 2: actions ────────────────────────────────────
                    r2a, r2b, r2c, r2d = st.columns([2.5, 0.8, 0.8, 0.8])

                    with r2a:
                        if editable:
                            temp = st.checkbox(
                                "Temperature Recorder",
                                value=ci.get("temp_recorder", False),
                                key=f"tmp_{cid}",
                            )
                        elif ci.get("temp_recorder"):
                            st.caption("🌡 Temp Recorder used")

                    with r2b:
                        if editable:
                            if st.button("💾 Save", key=f"save_{cid}", width='stretch'):
                                dm.update_client_invoice(cid, {
                                    "service_type" : svc,
                                    "temp_recorder": temp,
                                })
                                st.success("Service details saved.")
                                st.rerun()

                    with r2c:
                        if status in ("invoiced", "exported_to_qb") and ci.get("quickbooks_invoice_number"):
                            pdf_bytes = generate_pdf(ci, prov.get("pdf_local_path"))
                            _yy = datetime.now().strftime("%y")
                            pdf_name  = f"{ci['quickbooks_invoice_number']}-{_yy}.pdf"
                        else:
                            pdf_path = prov.get("pdf_local_path", "")
                            if pdf_path and Path(pdf_path).exists():
                                pdf_bytes = Path(pdf_path).read_bytes()
                                pdf_name  = prov.get("invoice_number", "invoice") + ".pdf"
                            else:
                                pdf_bytes = None
                                pdf_name  = "invoice.pdf"

                        if pdf_bytes:
                            st.download_button(
                                "📄 PDF", pdf_bytes, pdf_name,
                                mime="application/pdf",
                                key=f"pdf_{cid}",
                                width='stretch',
                            )
                        else:
                            st.button("📄 PDF", key=f"pdf_na_{cid}", disabled=True, width='stretch')

                    with r2d:
                        if st.session_state.get(confirm_key):
                            st.caption("⚠️ Sure?")
                        else:
                            if st.button("🗑 Delete", key=f"del_{cid}", width='stretch'):
                                st.session_state[confirm_key] = True
                                st.rerun()

                    # ── Delete confirmation row ───────────────────────────
                    if st.session_state.get(confirm_key):
                        dc_yes, dc_no = st.columns(2)
                        if dc_yes.button("✅ Yes, delete", key=f"del_yes_{cid}", type="primary", width='stretch'):
                            dm.delete_client_invoice(cid)
                            st.session_state.pop(confirm_key, None)
                            st.rerun()
                        if dc_no.button("✗ Cancel", key=f"del_no_{cid}", width='stretch'):
                            st.session_state.pop(confirm_key, None)
                            st.rerun()

                    # ── Row 3: ready_to_invoice — worker details + QB gen ─
                    if status == "ready_to_invoice":
                        st.markdown("---")
                        st.caption("Worker submitted:")
                        w1, w2, w3 = st.columns(3)
                        w1.metric("Pallets",         ci.get("pallet_count", 0))
                        w2.metric("Damaged Pallets",  ci.get("damaged_pallets", 0))
                        w3.metric("Broken Pallets",   ci.get("broken_pallets", 0))

                        extras = ci.get("extra_charges", [])
                        if extras:
                            st.caption("Extra: " + ", ".join(e.replace("_", " ").title() for e in extras))
                        if ci.get("worker_notes"):
                            st.caption(f"Notes: {ci['worker_notes']}")

                        photos = ci.get("photo_paths", [])
                        if photos:
                            pcols = st.columns(min(len(photos), 4))
                            for ph_col, ph_path in zip(pcols, photos[:4]):
                                try:
                                    ph_col.image(ph_path, use_container_width=True)
                                except Exception:
                                    ph_col.caption(ph_path)

                        if st.button("🟢 Generate Invoice", key=f"gen_{cid}", type="primary", width='stretch'):
                            inv_id = _generate_unique_invoice_id(dm)
                            charges = calculate_charges(
                                dm=dm,
                                service_type=svc,
                                pallet_count=int(ci.get("pallet_count", 1)),
                                temp_recorder=temp,
                                extra_charges=ci.get("extra_charges", []),
                                damaged_pallets=int(ci.get("damaged_pallets", 0)),
                                broken_pallets=int(ci.get("broken_pallets", 0)),
                                client_name=ci.get("client_name", ""),
                            )
                            client_rates = dm.get_rates_for_client(ci.get("client_name", ""))
                            dm.update_client_invoice(cid, {
                                "quickbooks_invoice_number": inv_id,
                                "service_type" : svc,
                                "temp_recorder": temp,
                                "line_items"   : charges["line_items"],
                                "subtotal"     : charges["subtotal"],
                                "total"        : charges["total"],
                                "net_days"     : int(client_rates.get("net_days", 30)),
                                "status"       : "invoiced",
                                "invoice_date" : datetime.utcnow().date().isoformat(),
                            })
                            if prov.get("email_intake_id"):
                                dm.update_email_log(prov["email_intake_id"], {"status": "invoiced"})
                            st.success(f"Invoice #{inv_id} generated! Total: ${charges['total']:,.2f}")
                            st.rerun()

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
                    prov_exp      = dm.get_provider_invoice_by_id(ci.get("provider_invoice_id", ""))
                    pdf_bytes     = generate_pdf(ci, prov_exp.get("pdf_local_path") if prov_exp else None)
                    fname = f"{qb}-{datetime.now().strftime('%y')}.pdf"
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
                    prov_hist = dm.get_provider_invoice_by_id(ci.get("provider_invoice_id", ""))
                    pdf_bytes = generate_pdf(ci, prov_hist.get("pdf_local_path") if prov_hist else None)
                    st.download_button(
                        label    ="⬇ PDF",
                        data     =pdf_bytes,
                        file_name=f"{qb}-{datetime.now().strftime('%y')}.pdf",
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
                client = _canonical_client(ci.get("client_name", "Unknown"))
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
            "broker_fee"            : "Broker Fee",
            "net_days"              : "Net Days (payment terms)",
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
            if key == "net_days":
                updated[key] = col.number_input(
                    label=label,
                    value=int(default_rates.get(key, 30)),
                    min_value=1,
                    step=1,
                    key=f"rate_{key}",
                )
            else:
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
