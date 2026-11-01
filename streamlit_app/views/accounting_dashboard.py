"""
accounting_dashboard.py
=======================
Accounting dashboard: Invoice Review, Import to QuickBooks, and Email Clients.
"""

import logging
import threading
import streamlit as st
import pandas as pd
from collections import defaultdict
from datetime import datetime, timedelta
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

logger = logging.getLogger(__name__)

from config import EXPORTS_DIR, QBO_CLIENT_ID, QBO_CLIENT_SECRET, QBO_REDIRECT_URI, QBO_ENVIRONMENT
from data_manager import DataManager

try:
    from integrations.qbo_auth import QBOAuth
    from integrations.qbo_client import QBOClient
    from integrations.qbo_ar_excel import build_ar_aging_xlsx, has_data, xlsx_filename
    _QBO_AVAILABLE = True
except ImportError:
    _QBO_AVAILABLE = False
from alerting.alert_manager import AlertManager
from invoice_logic.csv_exporter import generate_csv, build_csv_content
from invoice_logic.pdf_generator import generate_pdf
from scheduler.supabase_sync import patch_invoice_paid, sync_single_invoice as _sync_inv
from utils.pdf_storage import (
    upload_pdf_bytes as _upload_pdf_bytes,
    fetch_pdf_bytes as _fetch_pdf_bytes,
)
try:
    from streamlit_pdf_viewer import pdf_viewer
except ImportError:
    pdf_viewer = None


def _build_eml(to_addr: str, subject: str, body: str, attachments: list[tuple[str, bytes]]) -> bytes:
    """Build a RFC 2822 .eml file with PDF attachments.
    Opens in Outlook as a pre-filled compose window ready to send."""
    msg = MIMEMultipart()
    msg["To"]      = to_addr
    msg["Subject"] = subject
    msg["X-Unsent"] = "1"
    # Remove auto-generated headers that make Outlook treat this as a received message
    del msg["Date"]
    del msg["Message-ID"]
    msg.attach(MIMEText(body, "plain"))
    for filename, file_bytes in attachments:
        if filename.lower().endswith(".xlsx"):
            _subtype = "vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        else:
            _subtype = "pdf"
        part = MIMEApplication(file_bytes, _subtype=_subtype)
        part.add_header("Content-Disposition", "attachment", filename=filename)
        msg.attach(part)
    return msg.as_bytes()


@st.cache_data(show_spinner=False)
def _cached_pdf(
    ci_id: str,
    qb_num: str,
    total: float,
    provider_pdf_path: str | None,
    invoice_date: str,
    due_date: str,
    po_number: str,
    service_number: str,
    _ci: dict,
) -> bytes:
    """Cache keyed by all editable fields so any edit invalidates the cached PDF."""
    return generate_pdf(_ci, provider_pdf_path)


@st.cache_data(show_spinner=False, ttl=3600)
def _cached_stored_invoice_pdf(storage_key: str) -> bytes | None:
    """
    Fetch a generated invoice PDF from processed-invoices, cached for 1 hour.
    Prevents re-downloading the same attachment bytes on every Streamlit rerun
    while the email-preparation panel is open.
    """
    return _fetch_pdf_bytes(storage_key)


def _pdf_args(ci: dict, prov: dict | None) -> tuple:
    """Return the positional args for _cached_pdf from a client invoice + provider record."""
    _pdf_path = (prov or {}).get("pdf_local_path", "")
    _prov_inv_num = (prov or {}).get("invoice_number", "") or ci.get("service_number", "")
    _ci_for_pdf = {**ci, "provider_invoice_number": _prov_inv_num}
    return (
        ci["id"],
        ci.get("quickbooks_invoice_number", ""),
        float(ci.get("total", 0)),
        _pdf_path if _pdf_path and Path(_pdf_path).exists() else None,
        ci.get("invoice_date", ""),
        ci.get("due_date", ""),
        ci.get("po_number", ""),
        ci.get("service_number", ""),
        _ci_for_pdf,
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


def _do_ar_fetch(cname: str, client_email: str | None, dm, qbo_client) -> None:
    """
    Fetch the A/R Aging Detail for `cname` from QBO and store the result in
    st.session_state so the .eml builder can pick it up.

    Sets one of:
      st.session_state[f"ar_aging_{cname}"]            = (filename, xlsx_bytes)
      st.session_state[f"ar_aging_error_{cname}"]      = error-code string
      st.session_state[f"ar_aging_candidates_{cname}"] = list of QB customer dicts
    """
    ar_key      = f"ar_aging_{cname}"
    err_key     = f"ar_aging_error_{cname}"
    cand_key    = f"ar_aging_candidates_{cname}"
    try:
        # 1. Resolve QBO customer ID (cached → search → picker)
        qb_id = dm.get_qb_customer_id(cname)
        if not qb_id:
            candidates = qbo_client.find_customer_candidates(cname, client_email)
            if len(candidates) == 0:
                st.session_state[err_key] = "no_match"
                return
            elif len(candidates) > 1:
                st.session_state[cand_key] = candidates
                return
            else:
                qb_id = candidates[0]["Id"]
                dm.set_qb_customer_id(cname, qb_id)

        # 2. Pull the report
        report = qbo_client.get_ar_aging_detail(qb_id)
        if not has_data(report):
            st.session_state[err_key] = "no_data"
            return

        # 3. Build xlsx and cache it
        xlsx_bytes = build_ar_aging_xlsx(report, cname)
        fname      = xlsx_filename(cname)
        st.session_state[ar_key] = (fname, xlsx_bytes)

    except RuntimeError:
        st.session_state[err_key] = "not_connected"
    except Exception as _e:
        logger.exception("A/R Aging fetch failed for %s", cname)
        st.session_state[err_key] = str(_e)


def render(dm: DataManager, alert_manager: AlertManager | None = None) -> None:
    st.title("🧾 Accounting")

    # Fetch once per render — reused across all three tabs.
    client_invoices   = dm.get_client_invoices()
    provider_invoices = dm.get_provider_invoices()
    prov_by_id        = {pi["id"]: pi for pi in provider_invoices}

    # QuickBooks Online integration (optional — only active when credentials are set)
    _qbo_auth   = None
    _qbo_client = None
    if _QBO_AVAILABLE and QBO_CLIENT_ID and QBO_CLIENT_SECRET:
        _qbo_auth   = QBOAuth(QBO_CLIENT_ID, QBO_CLIENT_SECRET, QBO_REDIRECT_URI, dm)
        _qbo_client = QBOClient(_qbo_auth, sandbox=(QBO_ENVIRONMENT != "production"))

    tab_qb, tab_email, tab_processed = st.tabs([
        "📤 Export",
        "📧 Email",
        "📁 Processed Invoices",
    ])

    # ──────────────────────────────────────────────────────────────────────────
    # TAB 1 — EXPORT TO QUICKBOOKS  (Review + Export merged)
    # ──────────────────────────────────────────────────────────────────────────
    with tab_qb:
        st.subheader("Export to QuickBooks")

        # All pipeline invoices: Review-stage (invoiced, not yet approved) + Export-stage.
        # Exported invoices (quickbooks_exported=True) leave immediately — they belong to Email.
        all_pipeline = sorted(
            [ci for ci in client_invoices
             if (
                 (ci.get("status") == "invoiced" and not ci.get("ready_for_export"))
                 or (ci.get("ready_for_export") and not ci.get("quickbooks_exported") and not ci.get("ready_to_email"))
             )],
            key=lambda x: x.get("invoice_date", x.get("created_at", "")),
            reverse=True,
        )
        _review_ids = {ci["id"] for ci in all_pipeline if not ci.get("ready_for_export")}

        def _has_zero_rate(ci: dict) -> bool:
            return any(
                float(item.get("unit_price", 0)) == 0.0
                for item in ci.get("line_items", [])
            )

        # ── Confirmation pane (shown when a preview is staged) ─────────────
        _preview = st.session_state.get("qb_export_preview")
        if _preview:
            with st.container(border=True):
                st.markdown("#### Export summary")
                _prev_invs = [
                    dm.get_client_invoice_by_id(i)
                    for i in _preview["ids"]
                    if dm.get_client_invoice_by_id(i)
                ]
                _sum_total = sum(float(ci.get("total", 0)) for ci in _prev_invs)
                _sum_lines = sum(len(ci.get("line_items", [])) for ci in _prev_invs)
                st.write(
                    f"{len(_preview['ids'])} invoice(s) · {_sum_lines} lines · "
                    f"**${_sum_total:,.2f} total**"
                )
                for _pci in _prev_invs:
                    _n = len(_pci.get("line_items", []))
                    st.text(
                        f"  {_pci.get('quickbooks_invoice_number','—')}"
                        f"  ·  {_pci.get('client_name','—')}"
                        f"  ·  ${float(_pci.get('total',0)):,.2f}"
                        f"  ·  {_n} line{'s' if _n != 1 else ''}"
                    )

                if _preview.get("warnings"):
                    st.markdown("")
                    for _w in _preview["warnings"]:
                        st.warning(
                            f"⚠ {_w['qb_num']} · \"{_w['item']}\" → ItemRate $0.00"
                            f"  *(warning only — export will proceed)*"
                        )

                st.markdown("")

                def _do_export(
                    _ids=list(_preview["ids"]),
                    _ts=_preview["timestamp"],
                ):
                    try:
                        generate_csv(_ids, dm, _timestamp=_ts)
                    except Exception as _exc:
                        st.session_state["qb_export_error"] = str(_exc)
                    st.session_state.pop("qb_export_preview", None)

                _dl_col, _cancel_col = st.columns([3, 1])
                with _dl_col:
                    st.download_button(
                        label    =f"⬇ Confirm & Download  {_preview['filename']}",
                        data     =_preview["content"],
                        file_name=_preview["filename"],
                        mime     ="text/csv",
                        on_click =_do_export,
                        type     ="primary",
                        width    ="stretch",
                        key      ="qb_confirm_dl",
                    )
                    st.caption(
                        "Invoices are marked exported when you click. "
                        "Use Undo in Export History if the download or QuickBooks import fails."
                    )
                with _cancel_col:
                    if st.button("✗ Cancel", key="qb_cancel_preview", width="stretch"):
                        st.session_state.pop("qb_export_preview", None)
                        st.rerun()

        else:
            # ── Filter row ─────────────────────────────────────────────────
            _client_opts = ["All"] + sorted(
                {ci.get("client_name", "") for ci in all_pipeline} - {""}
            )
            _fc1, _fc2, _fc3, _fc4 = st.columns([1.4, 1.2, 1.2, 2.2])
            _f_client = _fc1.selectbox("Client", _client_opts, key="qb_f_client")
            _f_from   = _fc2.date_input("From", value=None, key="qb_f_from")
            _f_to     = _fc3.date_input("To",   value=None, key="qb_f_to")
            _f_sort   = _fc4.selectbox("Sort", ["Date (newest first)", "Review first"], key="qb_f_sort")

            _filtered = all_pipeline
            if _f_client != "All":
                _filtered = [ci for ci in _filtered if ci.get("client_name") == _f_client]
            if _f_from:
                _filtered = [ci for ci in _filtered if ci.get("invoice_date", "") >= str(_f_from)]
            if _f_to:
                _filtered = [ci for ci in _filtered if ci.get("invoice_date", "") <= str(_f_to)]
            if _f_sort == "Review first":
                _filtered = sorted(_filtered, key=lambda ci: 0 if ci["id"] in _review_ids else 1)

            if not all_pipeline:
                st.info("No invoices awaiting review or export.")
            else:
                _review_in_view = [ci for ci in _filtered if ci["id"] in _review_ids]
                _export_in_view = [ci for ci in _filtered if ci["id"] not in _review_ids]
                _r_total = sum(float(ci.get("total", 0)) for ci in _filtered)
                st.caption(
                    f"{len(_review_in_view)} review · "
                    f"{len(_export_in_view)} ready to export · "
                    f"${_r_total:,.2f}"
                )

                # Select all / Clear
                _sa1, _sa2, _ = st.columns([1, 1, 6])
                if _sa1.button("☑ Select all", key="qb_sa_all"):
                    for _ci in _filtered:
                        st.session_state[f"acc_exp_{_ci['id']}"] = True
                    st.rerun()
                if _sa2.button("☐ Clear", key="qb_sa_clear"):
                    for _ci in _filtered:
                        st.session_state[f"acc_exp_{_ci['id']}"] = False
                    st.rerun()

                # Table header
                _th_cols = st.columns([0.5, 0.35, 1.1, 1.4, 0.9, 0.9, 0.5, 0.9, 0.3])
                for _hc, _hl in zip(_th_cols, ["Stage", "☐", "Invoice #", "Client", "Date", "Due", "Lines", "Total", "⚠"]):
                    _hc.markdown(f"**{_hl}**")
                st.divider()

                _selected_ids: list[str] = []

                for _ci in _filtered:
                    _cid      = _ci["id"]
                    _qb       = _ci.get("quickbooks_invoice_number", "—")
                    is_review = _cid in _review_ids
                    _due      = _ci.get("due_date", "").strip()
                    if not _due:
                        try:
                            _due = (
                                datetime.fromisoformat(_ci.get("invoice_date", ""))
                                + timedelta(days=int(_ci.get("net_days", 30)))
                            ).date().isoformat()
                        except Exception:
                            _due = "—"

                    _tr = st.columns([0.5, 0.35, 1.1, 1.4, 0.9, 0.9, 0.5, 0.9, 0.3])
                    _tr[0].markdown("🔍" if is_review else "")
                    with _tr[1]:
                        if not is_review:
                            if st.checkbox("Select", key=f"acc_exp_{_cid}", label_visibility="collapsed"):
                                _selected_ids.append(_cid)
                    _tr[2].write(f"QB #{_qb}")
                    _tr[3].write(_ci.get("client_name", "—"))
                    _tr[4].write(_ci.get("invoice_date", "—"))
                    _tr[5].write(_due)
                    _tr[6].write(str(len(_ci.get("line_items", []))))
                    _tr[7].write(f"${float(_ci.get('total', 0)):,.2f}")
                    _tr[8].write("⚠" if _has_zero_rate(_ci) else "")

                    if is_review:
                        with st.expander(f"Details — QB #{_qb}", expanded=False):
                            _prov    = prov_by_id.get(_ci.get("provider_invoice_id", ""), {})
                            edit_key = f"acc_edit_{_cid}"
                            pdf_key  = f"acc_view_pdf_{_cid}"
                            ret_key  = f"acc_return_{_cid}"

                            if st.session_state.get(edit_key):
                                # ── Edit mode ─────────────────────────────────
                                e1, e2 = st.columns(2)
                                new_inv_date = e1.text_input(
                                    "Invoice Date",
                                    value=_ci.get("invoice_date", ""),
                                    key=f"acc_einvdate_{_cid}",
                                )
                                new_due_date = e2.text_input(
                                    "Due Date",
                                    value=_ci.get("due_date", ""),
                                    placeholder="YYYY-MM-DD",
                                    key=f"acc_eduedate_{_cid}",
                                )
                                new_po = e1.text_input(
                                    "P.O. Number",
                                    value=_ci.get("po_number", ""),
                                    key=f"acc_epo_{_cid}",
                                )
                                new_service_num = e2.text_input(
                                    "Service Number",
                                    value=_ci.get("service_number", ""),
                                    key=f"acc_esvcnum_{_cid}",
                                )
                                s1, s2 = st.columns(2)
                                if s1.button("💾 Save", key=f"acc_esave_{_cid}", type="primary", width="stretch"):
                                    dm.update_client_invoice(_cid, {
                                        "invoice_date"  : new_inv_date.strip(),
                                        "due_date"      : new_due_date.strip(),
                                        "po_number"     : new_po.strip(),
                                        "service_number": new_service_num.strip(),
                                    })
                                    # Re-generate and re-upload the stored PDF so the
                                    # email attachment reflects the updated invoice fields.
                                    _ci_saved = dm.get_client_invoice_by_id(_cid)
                                    if _ci_saved:
                                        try:
                                            _prov_s = prov_by_id.get(_ci_saved.get("provider_invoice_id", ""), {})
                                            _prov_inv_num = _prov_s.get("invoice_number", "") or _ci_saved.get("service_number", "")
                                            _ci_for_pdf = {**_ci_saved, "provider_invoice_number": _prov_inv_num}
                                            _ppath = _prov_s.get("pdf_local_path", "")
                                            _pdf_bytes = generate_pdf(
                                                _ci_for_pdf,
                                                _ppath if _ppath and Path(_ppath).exists() else None,
                                            )
                                            _qb_s = _ci_saved.get("quickbooks_invoice_number", "")
                                            if _qb_s:
                                                _upload_pdf_bytes(f"{_qb_s}-invoice.pdf", _pdf_bytes)
                                        except Exception as _e:
                                            logger.warning("Could not re-upload invoice PDF after edit: %s", _e)
                                    st.session_state.pop(edit_key, None)
                                    st.rerun()
                                if s2.button("✗ Cancel", key=f"acc_ecancel_{_cid}", width="stretch"):
                                    st.session_state.pop(edit_key, None)
                                    st.rerun()

                            else:
                                # ── View mode ─────────────────────────────────
                                c1, c2, c3, c4, c5, c6 = st.columns([1.2, 1, 2, 1, 0.8, 0.8])
                                c1.markdown(f"**QB #{_qb}**")
                                c2.write(_ci.get("invoice_date", "—"))
                                c3.write(_ci.get("client_name", "—"))
                                c4.write(f"${_ci.get('total', 0):,.2f}")
                                if c5.button("✏️ Edit", key=f"acc_editbtn_{_cid}", width="stretch"):
                                    st.session_state[edit_key] = True
                                    st.rerun()
                                pdf_label = "📄 Hide" if st.session_state.get(pdf_key) else "📄 PDF"
                                if c6.button(pdf_label, key=f"acc_pdf_btn_{_cid}", width="stretch"):
                                    st.session_state[pdf_key] = not st.session_state.get(pdf_key, False)
                                    st.rerun()
                                if _ci.get("service_number"):
                                    st.caption(f"Service #: {_ci['service_number']}")
                                if _ci.get("po_number"):
                                    st.caption(f"P.O.: {_ci['po_number']}")
                                if _ci.get("due_date"):
                                    st.caption(f"Due: {_ci['due_date']}")
                                line_items = _ci.get("line_items", [])
                                if line_items:
                                    with st.expander("Line Items"):
                                        st.dataframe(
                                            pd.DataFrame([{
                                                "Line Item" : li.get("description", "—"),
                                                "Qty"       : li.get("quantity", ""),
                                                "Rate ($)"  : f"{li.get('unit_price', 0):,.2f}",
                                                "Total ($)" : f"{li.get('total', 0):,.2f}",
                                            } for li in line_items]),
                                            width='stretch',
                                            hide_index=True,
                                        )
                                if st.session_state.get(pdf_key):
                                    pdf_bytes = _cached_pdf(*_pdf_args(_ci, _prov))
                                    pdf_viewer(pdf_bytes, key=f"acc_pdfview_{_cid}")
                                # ── Bottom action row ──────────────────────────
                                st.markdown("---")
                                if st.session_state.get(ret_key):
                                    st.warning("⚠️ Return this invoice to the Admin dashboard?")
                                    rc1, rc2 = st.columns(2)
                                    if rc1.button("✅ Yes, return to Admin", key=f"acc_retyes_{_cid}", type="primary", width="stretch"):
                                        dm.update_client_invoice(_cid, {"status": "ready_to_invoice"})
                                        st.session_state.pop(ret_key, None)
                                        st.rerun()
                                    if rc2.button("✗ Cancel", key=f"acc_retno_{_cid}", width="stretch"):
                                        st.session_state.pop(ret_key, None)
                                        st.rerun()
                                else:
                                    b_left, b_right = st.columns(2)
                                    ret_clicked = _colored_button(b_left, "↩ Return to Admin", f"acc_retbtn_{_cid}", "#dc3545", width="stretch")
                                    rfe_clicked = _colored_button(b_right, "✅ Ready for Export", f"rfe_{_cid}", "#198754", width="stretch")
                                    if ret_clicked:
                                        st.session_state[ret_key] = True
                                        st.rerun()
                                    if rfe_clicked:
                                        dm.update_client_invoice(_cid, {"ready_for_export": True})
                                        _ci_updated = dm.get_client_invoice_by_id(_cid)
                                        if _ci_updated:
                                            threading.Thread(target=_sync_inv, args=(_ci_updated,), daemon=True).start()
                                        st.rerun()

                    else:
                        with st.expander(f"Details — QB #{_qb}", expanded=False):
                            _items = _ci.get("line_items", [])
                            if _items:
                                _eh1, _eh2, _eh3, _eh4 = st.columns([3, 1, 1, 1])
                                _eh1.markdown("**Item**")
                                _eh2.markdown("**Qty**")
                                _eh3.markdown("**Rate**")
                                _eh4.markdown("**Amount**")
                                for _item in _items:
                                    _rate = float(_item.get("unit_price", 0))
                                    _ei1, _ei2, _ei3, _ei4 = st.columns([3, 1, 1, 1])
                                    _ei1.write(
                                        f"{_item.get('description','—')}"
                                        f"{'  ⚠' if _rate == 0.0 else ''}"
                                    )
                                    _ei2.write(str(_item.get("quantity", "")))
                                    _ei3.write(f"${_rate:,.2f}")
                                    _ei4.write(f"${float(_item.get('total', 0)):,.2f}")

                            _btr_conf = f"qb_btr_conf_{_cid}"
                            if st.session_state.get(_btr_conf):
                                st.warning("Return this invoice to Accounting Review?")
                                _bc1, _bc2 = st.columns(2)
                                if _bc1.button(
                                    "✅ Yes, return", key=f"qb_btr_yes_{_cid}",
                                    type="primary", width="stretch",
                                ):
                                    dm.update_client_invoice(_cid, {"ready_for_export": False})
                                    st.session_state.pop(_btr_conf, None)
                                    st.rerun()
                                if _bc2.button("✗ Cancel", key=f"qb_btr_no_{_cid}", width="stretch"):
                                    st.session_state.pop(_btr_conf, None)
                                    st.rerun()
                            else:
                                if _colored_button(st, "↩ Back to Review", f"qb_btr_{_cid}", "#dc3545"):
                                    st.session_state[_btr_conf] = True
                                    st.rerun()

                # Bottom action row — counter + Preview Export
                if _selected_ids:
                    st.markdown("")
                    _sel_total = sum(
                        float(item.get("total", 0))
                        for _sid in _selected_ids
                        for item in (dm.get_client_invoice_by_id(_sid) or {}).get("line_items", [])
                    )
                    _cnt_c, _btn_c = st.columns([2, 2])
                    _cnt_c.markdown(f"**{len(_selected_ids)} ready to export · ${_sel_total:,.2f}**")
                    with _btn_c:
                        if st.button(
                            "Preview Export ▸",
                            key="qb_preview_btn",
                            type="primary",
                            width="stretch",
                        ):
                            try:
                                _enriched_prev: list[dict] = []
                                for _inv_id in _selected_ids:
                                    _inv_p = dm.get_client_invoice_by_id(_inv_id)
                                    if _inv_p:
                                        _prov_p = dm.get_provider_invoice_by_id(
                                            _inv_p.get("provider_invoice_id", "")
                                        )
                                        _enriched_prev.append({
                                            **_inv_p,
                                            "provider_invoice_number": (
                                                _prov_p or {}
                                            ).get("invoice_number", ""),
                                        })

                                _ts_prev = datetime.now().strftime("%Y%m%d_%H%M%S")
                                _content = build_csv_content(_enriched_prev)
                                _warnings = [
                                    {
                                        "qb_num": _i.get("quickbooks_invoice_number", "?"),
                                        "item"  : _li.get("description", "?"),
                                    }
                                    for _i  in _enriched_prev
                                    for _li in _i.get("line_items", [])
                                    if float(_li.get("unit_price", 0)) == 0.0
                                ]
                                st.session_state["qb_export_preview"] = {
                                    "ids"      : _selected_ids,
                                    "content"  : _content,
                                    "filename" : f"QB_export_{_ts_prev}_{len(_selected_ids)}inv.csv",
                                    "timestamp": _ts_prev,
                                    "warnings" : _warnings,
                                }
                                st.rerun()
                            except ValueError as _ve:
                                st.error(str(_ve))
                            except Exception as _ex:
                                st.error(f"Export preview failed: {_ex}")

        # Show any error that occurred inside the on_click callback
        if st.session_state.get("qb_export_error"):
            st.error(st.session_state.pop("qb_export_error"))

        st.markdown("---")

        # ── Export History ─────────────────────────────────────────────────
        st.subheader("Export History")
        _batches = sorted(
            dm.get_export_batches(),
            key=lambda b: b.get("created_at", ""),
            reverse=True,
        )
        if not _batches:
            st.caption("No exports yet.")
        else:
            for _batch in _batches:
                with st.container(border=True):
                    _b_inv_ids = _batch.get("invoice_ids", [])
                    _b_qb_nums = _batch.get("qb_nums", [])
                    _del_conf  = f"batch_del_conf_{_batch['id']}"

                    _b_dt = _batch.get("created_at", "")
                    try:
                        _b_dt = datetime.fromisoformat(_b_dt).strftime("%m/%d/%y %H:%M")
                    except Exception:
                        _b_dt = _b_dt[:16]

                    # Re-download: file on disk first, then stored content, else None
                    _b_fpath = EXPORTS_DIR / _batch.get("filename", "__missing__")
                    if _b_fpath.exists():
                        with open(_b_fpath, "r", encoding="utf-8", newline="") as _f:
                            _b_dl = _f.read()
                    elif _batch.get("content"):
                        _b_dl = _batch["content"]
                    else:
                        _b_dl = None

                    if st.session_state.get(_del_conf):
                        st.warning(
                            f"Delete history record for {', '.join(_b_qb_nums)}? "
                            f"This only removes the record — it does not affect invoices."
                        )
                        _dc1, _dc2 = st.columns(2)
                        if _dc1.button(
                            "✅ Yes, delete",
                            key=f"batch_del_yes_{_batch['id']}",
                            type="primary",
                            width="stretch",
                        ):
                            dm.delete_export_batch(_batch["id"])
                            st.session_state.pop(_del_conf, None)
                            st.rerun()
                        if _dc2.button(
                            "✗ Cancel", key=f"batch_del_no_{_batch['id']}", width="stretch"
                        ):
                            st.session_state.pop(_del_conf, None)
                            st.rerun()
                    else:
                        # Header row: info + CSV download, delete button top-right
                        _b1, _b2, _b3, _b4, _bdel = st.columns([1.4, 2.4, 1.0, 1.0, 0.5])
                        _b1.write(_b_dt)
                        _b2.write(", ".join(_b_qb_nums))
                        _b3.write(f"${float(_batch.get('total', 0)):,.2f}")
                        if _colored_button(_bdel, "✕", f"batch_del_{_batch['id']}", "#dc3545"):
                            st.session_state[_del_conf] = True
                            st.rerun()
                        with _b4:
                            if _b_dl:
                                st.download_button(
                                    "⬇ CSV",
                                    data     =_b_dl,
                                    file_name=_batch.get("filename", "export.csv"),
                                    mime     ="text/csv",
                                    key      =f"batch_dl_{_batch['id']}",
                                )
                            else:
                                st.caption("File unavailable")

                        # Undo — only if none of the batch's invoices have been emailed
                        _can_undo = not any(
                            (dm.get_client_invoice_by_id(iid) or {}).get("emailed", False)
                            for iid in _b_inv_ids
                        )
                        _undo_conf = f"batch_undo_conf_{_batch['id']}"

                        if st.session_state.get(_undo_conf):
                            st.warning(
                                f"Undo export of {', '.join(_b_qb_nums)}? "
                                f"Invoice(s) will return to 'Ready for Export'."
                            )
                            _uc1, _uc2 = st.columns(2)
                            if _uc1.button(
                                "✅ Yes, undo",
                                key=f"batch_yes_{_batch['id']}",
                                type="primary",
                                width="stretch",
                            ):
                                try:
                                    dm.undo_export_batch(_batch["id"])
                                except Exception as _ue:
                                    st.error(f"Undo failed: {_ue}")
                                st.session_state.pop(_undo_conf, None)
                                st.rerun()
                            if _uc2.button(
                                "✗ Cancel", key=f"batch_no_{_batch['id']}", width="stretch"
                            ):
                                st.session_state.pop(_undo_conf, None)
                                st.rerun()
                        elif _can_undo:
                            if _colored_button(
                                st, "↩ Undo Export",
                                f"batch_undo_{_batch['id']}", "#dc3545",
                            ):
                                st.session_state[_undo_conf] = True
                                st.rerun()
                        else:
                            st.caption("Cannot undo — invoice(s) already emailed")

    # ──────────────────────────────────────────────────────────────────────────
    # TAB 3 — EMAIL CLIENTS
    # ──────────────────────────────────────────────────────────────────────────
    with tab_email:
        # ── QBO OAuth callback (Intuit redirects here with ?code=...&realmId=...) ──
        if _qbo_auth is not None:
            _qbo_code  = st.query_params.get("code", "")
            _qbo_realm = st.query_params.get("realmId", "")
            if _qbo_code and st.query_params.get("state") == "qbo_connect":
                try:
                    _qbo_auth.exchange_code(_qbo_code, _qbo_realm)
                    st.query_params.clear()
                    st.success("✅ QuickBooks connected! A/R Aging reports will now attach automatically.")
                except Exception as _qe:
                    st.error(f"QuickBooks connection failed: {_qe}")

        # ── Connect banner (shown only when credentials exist but not yet authorized) ──
        if _qbo_auth is not None and not _qbo_auth.is_connected():
            _auth_url = _qbo_auth.get_authorization_url()
            st.info(
                "📊 Connect QuickBooks to automatically attach A/R Aging reports to client emails.  "
                f"[Connect QuickBooks ↗]({_auth_url})"
            )

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
                        ic1, ic2, ic3, ic4 = st.columns([1.5, 1, 1, 0.8])
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

                    st.markdown("---")

                    if not st.session_state.get(prep_key):
                        _export_col, _prep_col = st.columns(2)
                        if _colored_button(_export_col, "↩ Back to Export", f"bte_{cname}", "#dc3545", width="stretch"):
                            for ci in invoices:
                                dm.update_client_invoice(ci["id"], {"ready_to_email": False})
                            st.rerun()
                        if _colored_button(_prep_col, f"📧 Prepare Email for {cname}", f"prep_{cname}", "#198754", width="stretch"):
                            st.session_state[prep_key] = True
                            st.rerun()
                    else:
                        # ── A/R Aging: auto-fetch on first open ───────────────
                        _ar_key   = f"ar_aging_{cname}"
                        _ar_err   = f"ar_aging_error_{cname}"
                        _ar_cands = f"ar_aging_candidates_{cname}"

                        if (
                            _qbo_client is not None
                            and _ar_key   not in st.session_state
                            and _ar_err   not in st.session_state
                            and _ar_cands not in st.session_state
                        ):
                            with st.spinner("📊 Fetching A/R Aging from QuickBooks…"):
                                _do_ar_fetch(cname, client_email, dm, _qbo_client)

                        # Customer disambiguation picker (shown when >1 QB match)
                        _candidates = st.session_state.get(_ar_cands, [])
                        if _candidates:
                            st.warning(
                                f"Multiple QuickBooks customers match **{cname}**. "
                                "Select the correct one:"
                            )
                            _opts = {
                                f"{c.get('DisplayName') or c.get('CompanyName', '?')}  (ID {c['Id']})": c["Id"]
                                for c in _candidates
                            }
                            _sel = st.selectbox(
                                "QuickBooks customer",
                                list(_opts.keys()),
                                key=f"ar_qb_sel_{cname}",
                            )
                            if st.button("✅ Use this customer", key=f"ar_qb_confirm_{cname}", type="primary"):
                                dm.set_qb_customer_id(cname, _opts[_sel])
                                st.session_state.pop(_ar_cands, None)
                                st.rerun()

                        # Draft email
                        total_sum = sum(ci.get("total", 0) for ci in invoices)
                        inv_lines = "\n".join(
                            f"  • Invoice #{ci.get('quickbooks_invoice_number','—')}"
                            f" — ${ci.get('total', 0):,.2f}"
                            f" — {ci.get('invoice_date','—')}"
                            for ci in invoices
                        )
                        _inv_nums = [ci.get("quickbooks_invoice_number", "") for ci in invoices]
                        _inv_label = (
                            f"Invoice #{_inv_nums[0]}"
                            if len(_inv_nums) == 1
                            else "Invoices #" + ", #".join(_inv_nums)
                        )
                        default_subject = f"{_inv_label} — INCO Group, Inc. — {cname}"
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
                        # ── A/R Aging attachment status ───────────────────────
                        if not st.session_state.get(_ar_cands):
                            _ar_error_val = st.session_state.get(_ar_err)
                            if _ar_error_val == "no_match":
                                st.caption(f"ℹ️ No QuickBooks customer matched **{cname}** — aging not attached.")
                            elif _ar_error_val == "no_data":
                                st.caption(f"ℹ️ **{cname}** has no open invoices in QuickBooks.")
                            elif _ar_error_val and _ar_error_val != "not_connected":
                                st.warning(f"⚠️ A/R Aging: {_ar_error_val}")
                            elif st.session_state.get(_ar_key):
                                _ar_fname, _ = st.session_state[_ar_key]
                                st.caption(f"📊 A/R Aging attached: **{_ar_fname}**")

                        # Build .eml with all invoice PDFs attached
                        # Use the stored generated PDF (same source as admin's Sent to Accounting download)
                        _eml_attachments = []
                        for _ci in invoices:
                            _qb  = _ci.get("quickbooks_invoice_number", "invoice")
                            _pe  = prov_by_id.get(_ci.get("provider_invoice_id", ""), {})
                            _stored = _cached_stored_invoice_pdf(f"{_qb}-invoice.pdf")
                            _eml_attachments.append((
                                f"{_qb}.pdf",
                                _stored if _stored else _cached_pdf(*_pdf_args(_ci, _pe)),
                            ))
                        # Append A/R Aging xlsx if successfully fetched
                        _ar_xlsx = st.session_state.get(_ar_key)
                        if _ar_xlsx:
                            _eml_attachments.append(_ar_xlsx)
                        _eml_bytes = _build_eml(
                            st.session_state.get(f"email_to_{cname}",   client_email or ""),
                            st.session_state.get(f"email_subj_{cname}", default_subject),
                            st.session_state.get(f"email_body_{cname}", default_body),
                            _eml_attachments,
                        )
                        _latest_qb = invoices[-1].get("quickbooks_invoice_number", "") if invoices else ""
                        _eml_name  = f"Invoice_{_latest_qb}.eml" if _latest_qb else "Invoice.eml"
                        st.download_button(
                            "📎 Download Email Draft (.eml)",
                            data=_eml_bytes,
                            file_name=_eml_name,
                            mime="message/rfc822",
                            key=f"dl_eml_{cname}",
                            width='stretch',
                        )

                        mc1, mc2 = st.columns(2)
                        if mc1.button(
                            "✅ Mark as Emailed",
                            key=f"mark_emailed_{cname}",
                            type="primary",
                            width="stretch",
                        ):
                            for ci in invoices:
                                dm.update_client_invoice(ci["id"], {"emailed": True})
                                _ci_updated = dm.get_client_invoice_by_id(ci["id"])
                                if _ci_updated:
                                    threading.Thread(target=_sync_inv, args=(_ci_updated,), daemon=True).start()
                            for _k in (prep_key, _ar_key, _ar_err, _ar_cands):
                                st.session_state.pop(_k, None)
                            st.rerun()
                        if mc2.button(
                            "✗ Cancel",
                            key=f"cancel_email_{cname}",
                            width="stretch",
                        ):
                            for _k in (prep_key, _ar_key, _ar_err, _ar_cands):
                                st.session_state.pop(_k, None)
                            st.rerun()

    # ──────────────────────────────────────────────────────────────────────────
    # TAB 4 — PROCESSED INVOICES
    # ──────────────────────────────────────────────────────────────────────────
    with tab_processed:
        st.subheader("Processed Invoices")

        processed = sorted(
            [
                ci for ci in client_invoices
                if (
                    ci.get("ready_for_export")
                    or ci.get("ready_to_email")
                    or ci.get("quickbooks_exported")
                    or ci.get("emailed")
                ) and not ci.get("paid")
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
                    _prov_csv = prov_by_id.get(ci.get("provider_invoice_id", ""), {})
                    _ci_csv   = {**ci, "provider_invoice_number": _prov_csv.get("invoice_number", "")}
                    try:
                        _csv_bytes = build_csv_content([_ci_csv])
                        c6.download_button(
                            "✅ CSV",
                            data=_csv_bytes,
                            file_name=f"{qb}-export.csv",
                            mime="text/csv",
                            key=f"proc_csv_{cid}",
                        )
                    except ValueError:
                        c6.caption("⚠ unmapped item")
                else:
                    c6.write("❌")
                c7.write("✅" if emailed  else "❌")

                if c8.button("Mark Paid", key=f"paid_{cid}", width="stretch"):
                    dm.update_client_invoice(cid, {"paid": True})
                    try:
                        patch_invoice_paid(cid)
                    except Exception as _e:
                        st.warning(f"Local record updated, but Supabase sync failed: {_e}")
                    st.rerun()
