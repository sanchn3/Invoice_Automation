"""
accounting_dashboard.py
=======================
Accounting dashboard: Invoice Review, Import to QuickBooks, and Email Clients.
"""

import streamlit as st
from datetime import datetime
from pathlib import Path

from data_manager import DataManager
from alerting.alert_manager import AlertManager
from invoice_logic.iif_exporter import generate_iif
from invoice_logic.pdf_generator import generate_pdf


@st.cache_data(show_spinner=False)
def _cached_pdf(
    ci_id: str,
    qb_num: str,
    total: float,
    provider_pdf_path: str | None,
    _ci: dict,
) -> bytes:
    """Cache keyed by invoice id + QB number + total + provider path."""
    return generate_pdf(_ci, provider_pdf_path)


def render(dm: DataManager, alert_manager: AlertManager | None = None) -> None:
    st.title("🧾 Accounting Dashboard")

    tab_review, tab_qb, tab_email = st.tabs([
        "📋 Invoice Review",
        "📤 Import to QuickBooks",
        "📧 Email Clients",
    ])

    # ──────────────────────────────────────────────────────────────────────────
    # TAB 1 — INVOICE REVIEW
    # ──────────────────────────────────────────────────────────────────────────
    with tab_review:
        st.subheader("Invoice Review")

        client_invoices   = dm.get_client_invoices()
        provider_invoices = dm.get_provider_invoices()
        prov_by_id        = {pi["id"]: pi for pi in provider_invoices}

        # Show invoices in "invoiced" state — generated but not yet exported
        reviewable = sorted(
            [ci for ci in client_invoices if ci.get("status") == "invoiced"],
            key=lambda x: x.get("created_at", ""),
            reverse=True,
        )

        if not reviewable:
            st.info("No invoices awaiting accounting review.")
        else:
            st.caption(f"{len(reviewable)} invoice(s) ready for review")

            for ci in reviewable:
                cid      = ci["id"]
                qb       = ci.get("quickbooks_invoice_number", "—")
                prov     = prov_by_id.get(ci.get("provider_invoice_id", ""), {})
                pdf_key  = f"acc_view_pdf_{cid}"
                edit_key = f"acc_edit_{cid}"
                ret_key  = f"acc_return_{cid}"

                with st.container(border=True):

                    if st.session_state.get(edit_key):
                        # ── Edit mode ─────────────────────────────────────────
                        e1, e2 = st.columns(2)
                        new_inv_date = e1.text_input(
                            "Invoice Date",
                            value=ci.get("invoice_date", ""),
                            key=f"acc_einvdate_{cid}",
                        )
                        new_due_date = e2.text_input(
                            "Due Date",
                            value=ci.get("due_date", ""),
                            placeholder="YYYY-MM-DD",
                            key=f"acc_eduedate_{cid}",
                        )
                        new_po = e1.text_input(
                            "P.O. Number",
                            value=ci.get("po_number", ""),
                            key=f"acc_epo_{cid}",
                        )

                        s1, s2 = st.columns(2)
                        if s1.button("💾 Save", key=f"acc_esave_{cid}", type="primary", width="stretch"):
                            dm.update_client_invoice(cid, {
                                "invoice_date": new_inv_date.strip(),
                                "due_date"    : new_due_date.strip(),
                                "po_number"   : new_po.strip(),
                            })
                            st.session_state.pop(edit_key, None)
                            st.rerun()
                        if s2.button("✗ Cancel", key=f"acc_ecancel_{cid}", width="stretch"):
                            st.session_state.pop(edit_key, None)
                            st.rerun()

                    else:
                        # ── View mode ─────────────────────────────────────────
                        c1, c2, c3, c4, c5, c6, c7 = st.columns([1.2, 1, 2, 1, 0.7, 0.7, 0.7])

                        c1.markdown(f"**QB #{qb}**")
                        c2.write(ci.get("invoice_date", "—"))
                        c3.write(ci.get("client_name", "—"))
                        c4.write(f"${ci.get('total', 0):,.2f}")

                        if c5.button("✏️ Edit", key=f"acc_editbtn_{cid}", width="stretch"):
                            st.session_state[edit_key] = True
                            st.rerun()

                        pdf_label = "📄 Hide" if st.session_state.get(pdf_key) else "📄 PDF"
                        if c6.button(pdf_label, key=f"acc_pdf_btn_{cid}", width="stretch"):
                            st.session_state[pdf_key] = not st.session_state.get(pdf_key, False)
                            st.rerun()

                        if st.session_state.get(ret_key):
                            c7.caption("⚠️ Sure?")
                        else:
                            if c7.button("↩", key=f"acc_retbtn_{cid}", width="stretch", help="Return to Admin"):
                                st.session_state[ret_key] = True
                                st.rerun()

                        if ci.get("po_number"):
                            st.caption(f"P.O.: {ci['po_number']}")
                        if ci.get("due_date"):
                            st.caption(f"Due: {ci['due_date']}")

                        # Return confirmation
                        if st.session_state.get(ret_key):
                            rc1, rc2 = st.columns(2)
                            if rc1.button("✅ Yes, return to Admin", key=f"acc_retyes_{cid}", type="primary", width="stretch"):
                                dm.update_client_invoice(cid, {"status": "ready_to_invoice"})
                                st.session_state.pop(ret_key, None)
                                st.success("Invoice returned to Admin dashboard.")
                                st.rerun()
                            if rc2.button("✗ Cancel", key=f"acc_retno_{cid}", width="stretch"):
                                st.session_state.pop(ret_key, None)
                                st.rerun()

                        # Line items expander
                        line_items = ci.get("line_items", [])
                        if line_items:
                            with st.expander("Line Items"):
                                for li in line_items:
                                    st.text(
                                        f"{li.get('description', '—'):<40}"
                                        f"  qty: {li.get('quantity', '')}  "
                                        f"  rate: ${li.get('unit_price', 0):,.2f}  "
                                        f"  total: ${li.get('amount', 0):,.2f}"
                                    )

                        if st.session_state.get(pdf_key):
                            from streamlit_pdf_viewer import pdf_viewer
                            _pdf_path = prov.get("pdf_local_path", "")
                            pdf_bytes = _cached_pdf(
                                cid,
                                qb,
                                float(ci.get("total", 0)),
                                _pdf_path if _pdf_path and Path(_pdf_path).exists() else None,
                                ci,
                            )
                            pdf_viewer(pdf_bytes, key=f"acc_pdfview_{cid}")

        st.markdown("---")
        st.subheader("Exported Invoices")
        exported = sorted(
            [ci for ci in client_invoices if ci.get("quickbooks_exported")],
            key=lambda x: x.get("created_at", ""),
            reverse=True,
        )
        if not exported:
            st.caption("No invoices exported yet.")
        else:
            rows = [
                {
                    "QB #"   : ci.get("quickbooks_invoice_number", "—"),
                    "Date"   : ci.get("invoice_date", "—"),
                    "Client" : ci.get("client_name", "—"),
                    "Total"  : f"${ci.get('total', 0):,.2f}",
                    "Net Days": ci.get("net_days", "—"),
                }
                for ci in exported
            ]
            st.dataframe(rows, use_container_width=True, hide_index=True)

    # ──────────────────────────────────────────────────────────────────────────
    # TAB 2 — IMPORT TO QUICKBOOKS
    # ──────────────────────────────────────────────────────────────────────────
    with tab_qb:
        st.subheader("Import to QuickBooks")

        client_invoices = dm.get_client_invoices()
        exportable = [
            ci for ci in client_invoices
            if ci.get("status") == "invoiced" and not ci.get("quickbooks_exported")
        ]

        if not exportable:
            st.info("No invoices ready to export.")
        else:
            st.caption(f"{len(exportable)} invoice(s) ready for export.")

            selected_ids: list[str] = []
            for ci in exportable:
                qb    = ci.get("quickbooks_invoice_number", "—")
                label = f"QB #{qb} — {ci.get('client_name', '—')} — ${ci.get('total', 0):,.2f}"
                prov_exp = dm.get_provider_invoice_by_id(ci.get("provider_invoice_id", ""))

                col_chk, col_pdf = st.columns([4, 1])
                with col_chk:
                    if st.checkbox(label, key=f"acc_exp_{ci['id']}"):
                        selected_ids.append(ci["id"])
                with col_pdf:
                    _pp = prov_exp.get("pdf_local_path") if prov_exp else None
                    pdf_bytes = _cached_pdf(
                        ci["id"], qb, float(ci.get("total", 0)), _pp, ci
                    )
                    fname = f"{qb}-{datetime.now().strftime('%y')}.pdf"
                    st.download_button(
                        label    ="⬇ PDF",
                        data     =pdf_bytes,
                        file_name=fname,
                        mime     ="application/pdf",
                        key      =f"acc_pdf_exp_{ci['id']}",
                    )

            if selected_ids:
                if st.button(
                    f"Export {len(selected_ids)} invoice(s) to IIF",
                    type="primary",
                ):
                    try:
                        iif_path    = generate_iif(selected_ids, dm)
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
        exported_hist = [ci for ci in dm.get_client_invoices() if ci.get("quickbooks_exported")]
        if exported_hist:
            for ci in sorted(exported_hist, key=lambda x: x.get("created_at", ""), reverse=True):
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
                    pdf_bytes = _cached_pdf(
                        ci["id"], qb, float(ci.get("total", 0)),
                        prov_hist.get("pdf_local_path") if prov_hist else None,
                        ci,
                    )
                    st.download_button(
                        label    ="⬇ PDF",
                        data     =pdf_bytes,
                        file_name=f"{qb}-{datetime.now().strftime('%y')}.pdf",
                        mime     ="application/pdf",
                        key      =f"acc_pdf_hist_{ci['id']}",
                    )
        else:
            st.caption("No invoices exported yet.")

    # ──────────────────────────────────────────────────────────────────────────
    # TAB 3 — EMAIL CLIENTS
    # ──────────────────────────────────────────────────────────────────────────
    with tab_email:
        st.subheader("Email Clients")

        client_invoices = dm.get_client_invoices()

        # Invoices eligible to email: invoiced or exported
        emailable = sorted(
            [
                ci for ci in client_invoices
                if ci.get("status") in ("invoiced",) or ci.get("quickbooks_exported")
            ],
            key=lambda x: x.get("created_at", ""),
            reverse=True,
        )

        if not emailable:
            st.info("No finalized invoices to email.")
        else:
            st.caption("Select an invoice to prepare a client email.")

            selected_ci_id = st.selectbox(
                "Invoice",
                options=[ci["id"] for ci in emailable],
                format_func=lambda cid: next(
                    (
                        f"QB #{ci.get('quickbooks_invoice_number', '—')} "
                        f"— {ci.get('client_name', '—')} "
                        f"— ${ci.get('total', 0):,.2f}"
                        for ci in emailable if ci["id"] == cid
                    ),
                    cid,
                ),
                label_visibility="collapsed",
            )

            sel = next((ci for ci in emailable if ci["id"] == selected_ci_id), None)

            if sel:
                qb          = sel.get("quickbooks_invoice_number", "—")
                client_name = sel.get("client_name", "")
                total       = sel.get("total", 0)
                inv_date    = sel.get("invoice_date", "—")
                net_days    = sel.get("net_days", 30)

                st.markdown("---")
                st.markdown("**Draft Email**")

                default_subject = f"Invoice #{qb} — INCO Logistics"
                default_body = (
                    f"Dear {client_name},\n\n"
                    f"Please find attached Invoice #{qb} dated {inv_date} "
                    f"for ${total:,.2f}, due within {net_days} days.\n\n"
                    f"Please do not hesitate to contact us if you have any questions.\n\n"
                    f"Best regards,\nINGO Logistics"
                )

                subject = st.text_input("Subject", value=default_subject, key=f"email_subj_{selected_ci_id}")
                body    = st.text_area("Body", value=default_body, height=200, key=f"email_body_{selected_ci_id}")

                # PDF attachment download
                prov_em   = dm.get_provider_invoice_by_id(sel.get("provider_invoice_id", ""))
                pdf_bytes = _cached_pdf(
                    sel["id"], qb, float(total),
                    prov_em.get("pdf_local_path") if prov_em else None,
                    sel,
                )
                pdf_fname = f"{qb}-{datetime.now().strftime('%y')}.pdf"

                dl_col, _ = st.columns([1, 3])
                dl_col.download_button(
                    label    ="⬇ Download PDF Attachment",
                    data     =pdf_bytes,
                    file_name=pdf_fname,
                    mime     ="application/pdf",
                    key      =f"acc_email_pdf_{selected_ci_id}",
                    width    ="stretch",
                )

                st.info(
                    "Copy the email body above and attach the PDF to send via your email client.",
                    icon="ℹ️",
                )
