"""
accounting_dashboard.py
=======================
Accounting dashboard: Invoice Review, Import to QuickBooks, and Email Clients.
"""

import logging
import streamlit as st
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

from config import EXPORTS_DIR
from data_manager import DataManager
from alerting.alert_manager import AlertManager
from invoice_logic.iif_exporter import build_iif_content, build_multi_iif_content
from invoice_logic.pdf_generator import generate_pdf
from utils.pdf_storage import upload_pdf_bytes as _upload_pdf_bytes, download_photo as _dl_photo

logger = logging.getLogger(__name__)


@st.cache_data(show_spinner=False)
def _cached_pdf(
    ci_id: str,
    qb_num: str,
    total: float,
    provider_pdf_path: str | None,
    invoice_date: str,
    due_date: str,
    po_number: str,
    photo_paths: tuple[str, ...],
    _ci: dict,
) -> bytes:
    """Cache keyed by all editable fields so any edit invalidates the cached PDF."""
    photo_bytes: list[bytes] = []
    for key in photo_paths:
        data = _dl_photo(key)
        if data:
            photo_bytes.append(data)
    return generate_pdf(_ci, provider_pdf_path, photo_bytes or None)


def _pdf_args(ci: dict, prov: dict | None) -> tuple:
    """Return the positional args for _cached_pdf from a client invoice + provider record."""
    _pdf_path = (prov or {}).get("pdf_local_path", "")
    return (
        ci["id"],
        ci.get("quickbooks_invoice_number", ""),
        float(ci.get("total", 0)),
        _pdf_path if _pdf_path and Path(_pdf_path).exists() else None,
        ci.get("invoice_date", ""),
        ci.get("due_date", ""),
        ci.get("po_number", ""),
        tuple(ci.get("photo_paths", [])),
        ci,
    )


def _colored_button(container, label: str, key: str, color: str, **kwargs) -> bool:
    """Render a Streamlit button with a custom background color.

    Injects a hidden <span> anchor immediately before the button so the CSS
    adjacent-sibling selector (+) can target that specific button only.
    Requires a browser that supports the :has() pseudo-class (Chrome 105+,
    Firefox 121+, Safari 15.4+ — all modern browsers).
    """
    anchor = "ca_" + "".join(c if c.isalnum() or c == "_" else "_" for c in key)
    container.markdown(
        f"<span id='{anchor}'></span>"
        f"<style>"
        # Selector A: button is inside a stColumn that contains the anchor (st.columns use)
        f"[data-testid='stColumn']:has(span#{anchor}) [data-testid='stButton']>button,"
        # Selector B: button is a direct grandchild of the stVerticalBlock that has the anchor
        #             as an immediate element-container>stMarkdown descendant (direct st use)
        f"[data-testid='stVerticalBlock']:has(>[data-testid='element-container']"
        f">[data-testid='stMarkdown'] span#{anchor})"
        f">[data-testid='element-container']>[data-testid='stButton']>button"
        f"{{background-color:{color}!important;"
        f"border-color:{color}!important;"
        f"color:white!important;}}"
        f"[data-testid='stColumn']:has(span#{anchor}) [data-testid='stButton']>button:hover,"
        f"[data-testid='stVerticalBlock']:has(>[data-testid='element-container']"
        f">[data-testid='stMarkdown'] span#{anchor})"
        f">[data-testid='element-container']>[data-testid='stButton']>button:hover"
        f"{{filter:brightness(1.12)!important;}}"
        f"</style>",
        unsafe_allow_html=True,
    )
    return container.button(label, key=key, **kwargs)


def render(dm: DataManager, alert_manager: AlertManager | None = None) -> None:
    st.title("🧾 Accounting")

    # Fetch once per render — reused across all three tabs.
    client_invoices   = dm.get_client_invoices()
    provider_invoices = dm.get_provider_invoices()
    prov_by_id        = {pi["id"]: pi for pi in provider_invoices}

    tab_review, tab_qb, tab_email, tab_processed = st.tabs([
        "📋 Invoice Review",
        "📤 Export to QuickBooks",
        "📧 Email Clients",
        "📁 Processed Invoices",
    ])

    # ──────────────────────────────────────────────────────────────────────────
    # TAB 1 — INVOICE REVIEW
    # ──────────────────────────────────────────────────────────────────────────
    with tab_review:
        st.subheader("Invoice Review")

        # Show invoices in "invoiced" state — generated but not yet exported
        reviewable = sorted(
            [ci for ci in client_invoices
             if ci.get("status") == "invoiced" and not ci.get("ready_for_export")],
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
                            # Re-upload the generated PDF since editable fields changed
                            ci_upd = dm.get_client_invoice_by_id(cid)
                            if ci_upd:
                                try:
                                    _photo_bytes = [
                                        d for k in ci_upd.get("photo_paths", [])
                                        if (d := _dl_photo(k)) is not None
                                    ]
                                    _pdf = generate_pdf(ci_upd, prov.get("pdf_local_path"), _photo_bytes or None)
                                    qb_key = ci_upd.get("quickbooks_invoice_number", cid)
                                    _upload_pdf_bytes(f"{qb_key}-invoice.pdf", _pdf)
                                except Exception as _e:
                                    logger.warning("Could not re-upload invoice PDF after edit: %s", _e)
                            st.session_state.pop(edit_key, None)
                            st.rerun()
                        if s2.button("✗ Cancel", key=f"acc_ecancel_{cid}", width="stretch"):
                            st.session_state.pop(edit_key, None)
                            st.rerun()

                    else:
                        # ── View mode ─────────────────────────────────────────
                        c1, c2, c3, c4, c5, c6 = st.columns([1.2, 1, 2, 1, 0.8, 0.8])

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

                        if ci.get("po_number"):
                            st.caption(f"P.O.: {ci['po_number']}")
                        if ci.get("due_date"):
                            st.caption(f"Due: {ci['due_date']}")

                        # Line items expander
                        line_items = ci.get("line_items", [])
                        if line_items:
                            with st.expander("Line Items"):
                                for li in line_items:
                                    st.text(
                                        f"{li.get('description', '—'):<40}"
                                        f"  qty: {li.get('quantity', '')}  "
                                        f"  rate: ${li.get('unit_price', 0):,.2f}  "
                                        f"  total: ${li.get('total', 0):,.2f}"
                                    )

                        if st.session_state.get(pdf_key):
                            from streamlit_pdf_viewer import pdf_viewer
                            pdf_bytes = _cached_pdf(*_pdf_args(ci, prov))
                            pdf_viewer(pdf_bytes, key=f"acc_pdfview_{cid}")

                        # ── Bottom action row ──────────────────────────────────
                        st.markdown("---")
                        if st.session_state.get(ret_key):
                            st.warning("⚠️ Return this invoice to the Admin dashboard?")
                            rc1, rc2 = st.columns(2)
                            if rc1.button("✅ Yes, return to Admin", key=f"acc_retyes_{cid}", type="primary", width="stretch"):
                                dm.update_client_invoice(cid, {"status": "ready_to_invoice"})
                                st.session_state.pop(ret_key, None)
                                st.rerun()
                            if rc2.button("✗ Cancel", key=f"acc_retno_{cid}", width="stretch"):
                                st.session_state.pop(ret_key, None)
                                st.rerun()
                        else:
                            b_left, b_right = st.columns(2)
                            ret_clicked = _colored_button(b_left, "↩ Return to Admin", f"acc_retbtn_{cid}", "#dc3545", width="stretch")
                            rfe_clicked = _colored_button(b_right, "✅ Ready for Export", f"rfe_{cid}", "#198754", width="stretch")
                            if ret_clicked:
                                st.session_state[ret_key] = True
                                st.rerun()
                            if rfe_clicked:
                                dm.update_client_invoice(cid, {"ready_for_export": True})
                                st.rerun()


    # ──────────────────────────────────────────────────────────────────────────
    # TAB 2 — IMPORT TO QUICKBOOKS
    # ──────────────────────────────────────────────────────────────────────────
    with tab_qb:
        st.subheader("Export to QuickBooks")

        exportable = [
            ci for ci in client_invoices
            if ci.get("ready_for_export") and not ci.get("ready_to_email")
        ]

        if not exportable:
            st.info("No invoices ready to export.")
        else:
            st.caption(f"{len(exportable)} invoice(s) ready for export.")

            selected_ids: list[str] = []
            for ci in exportable:
                qb        = ci.get("quickbooks_invoice_number", "—")
                prov_exp  = prov_by_id.get(ci.get("provider_invoice_id", ""), {})
                pdf_bytes = _cached_pdf(*_pdf_args(ci, prov_exp))
                fname     = f"{qb}-{datetime.now().strftime('%y')}.pdf"

                with st.container(border=True):
                    if ci.get("quickbooks_exported"):
                        col_info, col_pdf = st.columns([4, 1])
                        col_info.markdown(
                            f"**QB #{qb}** — {ci.get('client_name', '—')}"
                            f" — ${ci.get('total', 0):,.2f}"
                        )
                        col_info.caption("✅ Exported to QuickBooks")
                        col_pdf.download_button(
                            "⬇ PDF", pdf_bytes, fname,
                            mime="application/pdf",
                            key=f"acc_pdf_exp_{ci['id']}",
                        )
                    else:
                        col_chk, col_pdf = st.columns([4, 1])
                        with col_chk:
                            if st.checkbox(
                                f"QB #{qb} — {ci.get('client_name', '—')}"
                                f" — ${ci.get('total', 0):,.2f}",
                                key=f"acc_exp_{ci['id']}",
                            ):
                                selected_ids.append(ci["id"])
                        with col_pdf:
                            st.download_button(
                                "⬇ PDF", pdf_bytes, fname,
                                mime="application/pdf",
                                key=f"acc_pdf_exp_{ci['id']}",
                            )

                    btn_l, btn_r = st.columns(2)
                    btr_clicked = _colored_button(btn_l, "↩ Back to Review", f"btr_{ci['id']}", "#dc3545", width="stretch")
                    rte_clicked = _colored_button(btn_r, "📧 Ready to Email", f"rte_{ci['id']}", "#198754", width="stretch")
                    if btr_clicked:
                        dm.update_client_invoice(ci["id"], {"ready_for_export": False})
                        st.rerun()
                    if rte_clicked:
                        dm.update_client_invoice(ci["id"], {"ready_to_email": True})
                        st.rerun()

            if selected_ids:
                # Pre-validate and pre-build content so clicking the button
                # triggers an immediate download (no second click required).
                _iif_err: str | None = None
                _iif_content: str = ""
                _invs_to_export: list[dict] = []
                try:
                    for _iid in selected_ids:
                        _inv = dm.get_client_invoice_by_id(_iid)
                        if _inv is None:
                            raise ValueError(f"Invoice {_iid} not found.")
                        if _inv.get("quickbooks_exported"):
                            raise ValueError(
                                f"Invoice QB# {_inv.get('quickbooks_invoice_number')} "
                                f"has already been exported."
                            )
                        if not _inv.get("quickbooks_invoice_number"):
                            raise ValueError(
                                f"Invoice {_iid} is missing a QB number — "
                                f"generate it in the admin dashboard first."
                            )
                        _invs_to_export.append(_inv)
                    _iif_content = build_multi_iif_content(_invs_to_export)
                except ValueError as e:
                    _iif_err = str(e)
                except Exception as e:
                    _iif_err = f"Export preparation failed: {e}"

                if _iif_err:
                    st.error(_iif_err)
                else:
                    _iif_fname = (
                        f"invoices_export_{datetime.now().strftime('%Y%m%d_%H%M%S')}.iif"
                    )

                    def _on_export_click(
                        _invs=_invs_to_export,
                        _content=_iif_content,
                        _fname=_iif_fname,
                    ):
                        """Write IIF to disk and mark invoices as exported."""
                        try:
                            (EXPORTS_DIR / _fname).write_text(_content, encoding="utf-8")
                            logger.info("IIF exported: %s (%d invoices)", _fname, len(_invs))
                        except Exception as e:
                            logger.error("IIF file write failed: %s", e)
                        for _inv in _invs:
                            dm.update_client_invoice(_inv["id"], {
                                "quickbooks_exported": True,
                                "status"             : "exported_to_qb",
                            })
                            _ci = dm.get_client_invoice_by_id(_inv["id"])
                            if _ci and _ci.get("provider_invoice_id"):
                                _pi = dm.get_provider_invoice_by_id(_ci["provider_invoice_id"])
                                if _pi and _pi.get("email_intake_id"):
                                    dm.update_email_log(
                                        _pi["email_intake_id"], {"status": "exported_to_qb"}
                                    )

                    st.download_button(
                        label     = f"Export {len(selected_ids)} invoice(s) to IIF",
                        data      = _iif_content,
                        file_name = _iif_fname,
                        mime      = "text/plain",
                        on_click  = _on_export_click,
                        type      = "primary",
                    )

        st.markdown("---")
        st.subheader("Export History")
        exported_hist = [ci for ci in client_invoices if ci.get("quickbooks_exported")]
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
                    prov_hist = prov_by_id.get(ci.get("provider_invoice_id", ""), {})
                    pdf_bytes = _cached_pdf(*_pdf_args(ci, prov_hist))
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

        # Only show invoices not yet emailed
        emailable = [
            ci for ci in client_invoices
            if ci.get("ready_to_email") and not ci.get("emailed")
        ]

        if not emailable:
            st.info("No invoices pending email. All sent invoices are in Processed Invoices.")
        else:
            # Group by client
            by_client: dict[str, list] = defaultdict(list)
            for ci in emailable:
                by_client[ci.get("client_name", "Unknown")].append(ci)

            st.caption(f"{len(emailable)} invoice(s) across {len(by_client)} client(s)")

            for cname in sorted(by_client.keys()):
                invoices    = by_client[cname]
                prep_key    = f"email_prep_{cname}"
                client_email = dm.get_client_email(cname)

                with st.container(border=True):
                    st.markdown(f"**{cname}** — {len(invoices)} invoice(s)")
                    if client_email:
                        st.caption(f"📧 {client_email}")
                    else:
                        st.caption("⚠️ No email on file — add one in Lead → Rate Card")

                    # Invoice rows
                    for ci in invoices:
                        qb     = ci.get("quickbooks_invoice_number", "—")
                        prov_e = prov_by_id.get(ci.get("provider_invoice_id", ""), {})
                        ic1, ic2, ic3, ic4, ic5 = st.columns([1.5, 1, 1, 0.8, 1.2])
                        ic1.write(f"QB #{qb}")
                        ic2.write(ci.get("invoice_date", "—"))
                        ic3.write(f"${ci.get('total', 0):,.2f}")
                        ic4.download_button(
                            "⬇ PDF",
                            _cached_pdf(*_pdf_args(ci, prov_e)),
                            f"{qb}-{datetime.now().strftime('%y')}.pdf",
                            mime="application/pdf",
                            key=f"acc_emaildl_{ci['id']}",
                        )
                        if _colored_button(ic5, "↩ Back to Export", f"bte_{ci['id']}", "#dc3545", width="stretch"):
                            dm.update_client_invoice(ci["id"], {"ready_to_email": False})
                            st.rerun()

                    st.markdown("---")

                    if not st.session_state.get(prep_key):
                        _prep_col, _ = st.columns(2)
                        if _colored_button(_prep_col, f"📧 Prepare Email for {cname}", f"prep_{cname}", "#198754", width="stretch"):
                            st.session_state[prep_key] = True
                            st.rerun()
                    else:
                        # Draft email
                        total_sum = sum(ci.get("total", 0) for ci in invoices)
                        inv_lines = "\n".join(
                            f"  • QB #{ci.get('quickbooks_invoice_number','—')}"
                            f" — ${ci.get('total', 0):,.2f}"
                            f" — {ci.get('invoice_date','—')}"
                            for ci in invoices
                        )
                        default_subject = f"Invoices — INCO Group, Inc. — {cname}"
                        default_body = (
                            f"Dear {cname},\n\n"
                            f"Please find attached the following invoice(s):\n\n"
                            f"{inv_lines}\n\n"
                            f"Total Outstanding: ${total_sum:,.2f}\n\n"
                            f"Please do not hesitate to contact us if you have any questions.\n\n"
                            f"Best regards,\nINCO Group, Inc."
                        )
                        st.text_input(
                            "To",
                            value=client_email or "",
                            key=f"email_to_{cname}",
                        )
                        st.text_input(
                            "Subject",
                            value=default_subject,
                            key=f"email_subj_{cname}",
                        )
                        st.text_area(
                            "Body",
                            value=default_body,
                            height=180,
                            key=f"email_body_{cname}",
                        )
                        st.caption("Download PDFs above to attach to your email.")

                        mc1, mc2 = st.columns(2)
                        if mc1.button(
                            "✅ Mark as Emailed",
                            key=f"mark_emailed_{cname}",
                            type="primary",
                            width="stretch",
                        ):
                            for ci in invoices:
                                dm.update_client_invoice(ci["id"], {"emailed": True})
                            st.session_state.pop(prep_key, None)
                            st.rerun()
                        if mc2.button(
                            "✗ Cancel",
                            key=f"cancel_email_{cname}",
                            width="stretch",
                        ):
                            st.session_state.pop(prep_key, None)
                            st.rerun()

    # ──────────────────────────────────────────────────────────────────────────
    # TAB 4 — PROCESSED INVOICES
    # ──────────────────────────────────────────────────────────────────────────
    with tab_processed:
        st.subheader("Processed Invoices")

        processed = sorted(
            [
                ci for ci in client_invoices
                if ci.get("ready_for_export")
                or ci.get("ready_to_email")
                or ci.get("quickbooks_exported")
                or ci.get("emailed")
                or ci.get("paid")
            ],
            key=lambda x: x.get("invoice_date", x.get("created_at", "")),
            reverse=True,
        )

        if not processed:
            st.info("No processed invoices yet.")
        else:
            st.caption(f"{len(processed)} invoice(s)")

            # Header
            h1, h2, h3, h4, h5, h6, h7, h8 = st.columns(
                [1.2, 1.8, 1, 1, 1.2, 1.2, 0.8, 0.9]
            )
            h1.markdown("**Invoice #**")
            h2.markdown("**Client**")
            h3.markdown("**Date**")
            h4.markdown("**Due Date**")
            h5.markdown("**Reviewed**")
            h6.markdown("**QB Export**")
            h7.markdown("**Emailed**")
            h8.markdown("**Paid**")
            st.divider()

            for ci in processed:
                cid = ci["id"]
                qb  = ci.get("quickbooks_invoice_number", "—")

                # Due date: explicit override or calculated
                due = ci.get("due_date", "").strip()
                if not due:
                    try:
                        due = (
                            datetime.fromisoformat(ci.get("invoice_date", ""))
                            + timedelta(days=int(ci.get("net_days", 30)))
                        ).date().isoformat()
                    except Exception:
                        due = "—"

                reviewed = bool(
                    ci.get("ready_for_export")
                    or ci.get("ready_to_email")
                    or ci.get("quickbooks_exported")
                    or ci.get("emailed")
                )
                exported = bool(ci.get("quickbooks_exported"))
                emailed  = bool(ci.get("emailed"))
                paid     = bool(ci.get("paid"))

                c1, c2, c3, c4, c5, c6, c7, c8 = st.columns(
                    [1.2, 1.8, 1, 1, 1.2, 1.2, 0.8, 0.9]
                )
                c1.write(f"QB #{qb}")
                c2.write(ci.get("client_name", "—"))
                c3.write(ci.get("invoice_date", "—"))
                c4.write(due)
                if reviewed:
                    prov_proc = prov_by_id.get(ci.get("provider_invoice_id", ""), {})
                    c5.download_button(
                        "✅ PDF",
                        data=_cached_pdf(*_pdf_args(ci, prov_proc)),
                        file_name=f"{qb}-invoice.pdf",
                        mime="application/pdf",
                        key=f"proc_pdf_{cid}",
                    )
                else:
                    c5.write("❌")

                if exported:
                    c6.download_button(
                        "✅ IIF",
                        data=build_iif_content(ci),
                        file_name=f"{qb}-export.iif",
                        mime="text/plain",
                        key=f"proc_iif_{cid}",
                    )
                else:
                    c6.write("❌")
                c7.write("✅" if emailed  else "❌")

                paid_label = "✅ Paid" if paid else "Mark Paid"
                if c8.button(paid_label, key=f"paid_{cid}", width="stretch"):
                    dm.update_client_invoice(cid, {"paid": not paid})
                    st.rerun()
