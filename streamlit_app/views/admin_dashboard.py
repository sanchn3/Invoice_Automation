"""
admin_dashboard.py
==================
Full admin dashboard with pipeline board, invoice approval,
QuickBooks export, reporting, and rate card editor.
"""

import logging
import streamlit as st
from datetime import datetime, timedelta, timezone

from pathlib import Path

from data_manager import DataManager
from invoice_logic.charge_calculator import calculate_charges
from invoice_logic.pdf_generator import generate_pdf as _generate_pdf
from email_pipeline.attachment_handler import process_pdf_from_path
from alerting.alert_manager import AlertManager
from utils.pdf_storage import (
    get_pdf_bytes as _get_pdf_bytes,
    move_to_processed as _move_to_processed,
    upload_pdf_bytes as _upload_pdf_bytes,
)

logger = logging.getLogger(__name__)

_STUCK_HOURS = 24


# Canonical client-name aliases (lowercase key → display name)
_CLIENT_ALIASES: dict[str, str] = {
    "babia ice"                : "BABIA ICE & PRODUCE LLC",
    "babia"                    : "BABIA ICE & PRODUCE LLC",
    "babia ice & produce llc"  : "BABIA ICE & PRODUCE LLC",
    "babia ice and produce llc": "BABIA ICE & PRODUCE LLC",
}


def _canonical_client(name: str) -> str:
    """Normalise variant spellings to a single display name."""
    return _CLIENT_ALIASES.get(name.strip().lower(), name)


def _generate_unique_invoice_id(dm: DataManager) -> str:
    """Return the next sequential 5-digit numeric invoice ID."""
    numeric_ids = [
        int(ci["quickbooks_invoice_number"])
        for ci in dm.get_client_invoices()
        if (ci.get("quickbooks_invoice_number") or "").isdigit()
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
    st.title("📦 Administrator")

    # ── In / Out mode toggle ──────────────────────────────────────────────────
    c_in, c_tog, c_out, _ = st.columns([0.4, 0.5, 0.5, 5])
    c_in.markdown("**In**")
    is_out = c_tog.toggle("In/Out", key="admin_mode_out", label_visibility="collapsed")
    c_out.markdown("**Out**")

    if is_out:
        from streamlit_app.views import bol_dashboard
        bol_dashboard.render(dm)
        return

    # Fetch all data once per render — reused across all three tabs.
    email_logs       = dm.get_email_logs()
    provider_invs    = dm.get_provider_invoices()
    client_invs_list = dm.get_client_invoices()
    prov_by_id       = {pi["id"]: pi for pi in provider_invs}

    tab_pipeline, tab_received, tab_approve, tab_export = st.tabs([
        "🗂 Validate",
        "📦 To Be Received",
        "✅ Approve & Invoice",
        "📤 Sent to Accounting",
    ])

    # ──────────────────────────────────────────────────────────────────────────
    # TAB 1 — PIPELINE BOARD
    # ──────────────────────────────────────────────────────────────────────────
    with tab_pipeline:
        st.subheader("Invoice Validation")

        poll_col, _ = st.columns([1, 4])
        with poll_col:
            if st.button("🔄 Poll Inbox Now", width='stretch'):
                from email_pipeline.outlook_listener import poll_inbox
                with st.spinner("Polling inbox..."):
                    new_count = poll_inbox(dm, alert_manager)
                st.success(f"Poll complete — {new_count} new email(s) processed.")
                st.rerun()

        pending_review = [log for log in email_logs if log.get("status") == "pending_review"]
        if pending_review:
            st.warning(f"⚠️ {len(pending_review)} email(s) need your review — see below.")

        # Build map: provider_invoice_id → client_invoice for quick status lookup
        _ci_by_prov_id = {
            ci.get("provider_invoice_id"): ci
            for ci in client_invs_list
            if ci.get("provider_invoice_id")
        }
        # Show only provider invoices whose linked CI is pending_validation
        active_provider_invs = [
            pi for pi in provider_invs
            if _ci_by_prov_id.get(pi["id"], {}).get("status") == "pending_validation"
        ]

        # Combine parsed invoices + pending-review emails, newest first
        all_items = (
            [("parsed",  pi)  for pi in active_provider_invs] +
            [("review",  log) for log in pending_review]
        )
        all_items.sort(key=lambda x: x[1].get("created_at", ""), reverse=True)

        if not all_items:
            st.info("No invoices pending validation.")
        else:
            st.caption(
                f"{len(active_provider_invs)} awaiting validation"
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
                pdf_exists = bool(pdf_path)
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
                            st.rerun()
                        else:
                            st.session_state[f"manual_entry_{iid}"] = True
                            st.rerun()

                # ── Manual entry fallback (shown when auto-parsing fails) ──
                if st.session_state.get(f"manual_entry_{iid}"):
                    st.warning(
                        "⚠️ Auto-parsing failed (scanned or unreadable PDF). "
                        "Fill in the invoice details manually."
                    )
                    me1, me2 = st.columns(2)
                    m_num    = me1.text_input("Invoice #",        key=f"m_num_{iid}")
                    m_date   = me2.text_input("Date (YYYY-MM-DD)", key=f"m_date_{iid}")
                    m_client = me1.text_input("Client",           key=f"m_client_{iid}")
                    m_total  = me2.number_input(
                        "Total ($)", min_value=0.0, step=0.01,
                        format="%.2f", key=f"m_total_{iid}",
                    )
                    ms1, ms2 = st.columns(2)
                    if ms1.button("💾 Create Invoice", key=f"m_save_{iid}", type="primary", width='stretch'):
                        if not m_num.strip() or not m_client.strip():
                            st.error("Invoice # and Client are required.")
                        else:
                            _now      = datetime.utcnow().isoformat() + "Z"
                            _canonical = m_client.strip().upper()
                            _prov_inv  = dm.add_provider_invoice({
                                "provider_name"  : log.get("sender", ""),
                                "client_name"    : _canonical,
                                "invoice_number" : m_num.strip(),
                                "invoice_date"   : m_date.strip() or _now[:10],
                                "line_items"     : [],
                                "subtotal"       : float(m_total),
                                "taxes"          : 0.0,
                                "total"          : float(m_total),
                                "pdf_local_path" : pdf_path,
                                "email_intake_id": iid,
                                "parsed_at"      : _now,
                                "status"         : "parsed",
                            })
                            dm.add_client_invoice({
                                "quickbooks_invoice_number": None,
                                "client_name"    : _canonical,
                                "invoice_date"   : m_date.strip() or _now[:10],
                                "service_type"   : None,
                                "temp_recorder"  : False,
                                "extra_charges"  : [],
                                "pallet_count"   : 0,
                                "damaged_pallets": 0,
                                "broken_pallets" : 0,
                                "worker_notes"   : "",
                                "photo_paths"    : [],
                                "line_items"     : [],
                                "subtotal"       : float(m_total),
                                "total"          : float(m_total),
                                "provider_invoice_id": _prov_inv["id"],
                                "quickbooks_exported": False,
                                "status"         : "pending_validation",
                            })
                            dm.update_email_log(iid, {"status": "parsed", "error_text": None})
                            st.session_state.pop(f"manual_entry_{iid}", None)
                            st.rerun()
                    if ms2.button("✗ Cancel", key=f"m_cancel_{iid}", width='stretch'):
                        st.session_state.pop(f"manual_entry_{iid}", None)
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
                    _b = _get_pdf_bytes(pdf_path)
                    if _b:
                        pdf_viewer(_b, key=f"pdfview_{iid}")
                    else:
                        st.warning("PDF not available.")

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
                            canonical = new_client.strip().upper()
                            dm.update_provider_invoice(iid, {
                                "invoice_number": new_num.strip(),
                                "invoice_date"  : new_date.strip(),
                                "client_name"   : canonical,
                                "total"         : new_total,
                                "subtotal"      : new_total,
                            })
                            # Keep the linked client invoice in sync
                            linked_ci = dm.get_client_invoice_by_provider_invoice_id(iid)
                            if linked_ci:
                                dm.update_client_invoice(linked_ci["id"], {
                                    "client_name" : canonical,
                                    "invoice_date": new_date.strip(),
                                })
                            st.session_state.pop(edit_key, None)
                            st.rerun()
                        if s2.button("✗ Cancel", key=f"ecancel_{iid}", width='stretch'):
                            st.session_state.pop(edit_key, None)
                            st.rerun()

                    else:
                        pi = item
                        pdf_path   = pi.get("pdf_local_path", "")
                        pdf_exists = bool(pdf_path)
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
                            if c7.button("🗑 TRASH", key=f"delbtn_{iid}", width='stretch'):
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
                            _b = _get_pdf_bytes(pdf_path)
                            if _b:
                                pdf_viewer(_b, key=f"pdfview_{iid}")
                            else:
                                st.warning("PDF not available.")

                        # ── Validate action ───────────────────────────────
                        st.markdown("---")
                        val_col, _ = st.columns([1, 2])
                        if val_col.button("✅ Validate", key=f"validate_{iid}", type="primary", width='stretch'):
                            linked_ci = _ci_by_prov_id.get(iid)
                            if linked_ci:
                                dm.update_client_invoice(linked_ci["id"], {"status": "to_be_received"})
                                st.success("Invoice validated — now visible in To Be Received tab.")
                                st.rerun()
                            else:
                                st.error("No linked client invoice found.")

    # ──────────────────────────────────────────────────────────────────────────
    # TAB 2 — TO BE RECEIVED
    # ──────────────────────────────────────────────────────────────────────────
    with tab_received:
        st.subheader("To Be Received")

        tbr_invoices = sorted(
            [ci for ci in client_invs_list if ci.get("status") == "to_be_received"],
            key=lambda x: x.get("created_at", ""),
            reverse=True,
        )

        if not tbr_invoices:
            st.info("No invoices awaiting receipt.")
        else:
            st.caption(f"{len(tbr_invoices)} invoice(s) — newest first")
            for ci in tbr_invoices:
                cid     = ci["id"]
                prov_pi = prov_by_id.get(ci.get("provider_invoice_id"), {})
                with st.container(border=True):
                    # Info row
                    ic1, ic2, ic3, ic4 = st.columns([1.2, 1, 2, 1])
                    ic1.markdown(f"**{prov_pi.get('invoice_number', ci.get('invoice_number', '—'))}**")
                    ic2.write(ci.get("invoice_date", "—"))
                    ic3.write(ci.get("client_name", "—"))
                    ic4.write(f"${ci.get('total', 0):,.2f}")
                    # Action buttons
                    _tbr_pdf_path   = prov_pi.get("pdf_local_path", "")
                    _tbr_pdf_exists = bool(_tbr_pdf_path)
                    _tbr_pdf_key    = f"tbr_pdf_{cid}"
                    _tbr_pdf_label  = "📄 Hide" if st.session_state.get(_tbr_pdf_key) else "📄 PDF"

                    bc1, bc2, bc3 = st.columns(3)
                    if bc1.button("↩ Return to Validation", key=f"tbr_return_{cid}", width='stretch'):
                        dm.update_client_invoice(cid, {"status": "pending_validation"})
                        st.rerun()
                    if _tbr_pdf_exists:
                        if bc2.button(_tbr_pdf_label, key=f"tbr_pdfbtn_{cid}", width='stretch'):
                            st.session_state[_tbr_pdf_key] = not st.session_state.get(_tbr_pdf_key, False)
                            st.rerun()
                    else:
                        bc2.button("📄 PDF", key=f"tbr_pdfbtn_na_{cid}", disabled=True, width='stretch')
                    if bc3.button("✅ Received", key=f"tbr_received_{cid}", width='stretch'):
                        from invoice_logic.stamp_pdf import stamp_pdf as _stamp_pdf
                        _rcv_date    = datetime.utcnow().date()
                        _stamp_error = None
                        if _tbr_pdf_exists:
                            try:
                                _stamp_pdf(_tbr_pdf_path, _rcv_date)
                            except Exception as _e:
                                _stamp_error = str(_e)
                        if _stamp_error:
                            st.error(
                                f"⚠️ PDF stamp failed — invoice NOT moved forward. "
                                f"Fix the issue and try again.\n\n`{_stamp_error}`"
                            )
                        else:
                            dm.update_client_invoice(cid, {
                                "status"       : "validated",
                                "received_date": _rcv_date.isoformat(),
                            })
                            st.rerun()

                    if st.session_state.get(_tbr_pdf_key) and _tbr_pdf_exists:
                        from streamlit_pdf_viewer import pdf_viewer
                        _b = _get_pdf_bytes(_tbr_pdf_path)
                        if _b:
                            pdf_viewer(_b, key=f"tbr_pdfview_{cid}")
                        else:
                            st.warning("PDF not available.")

    # ──────────────────────────────────────────────────────────────────────────
    # TAB 3 — APPROVE & GENERATE INVOICE
    # ──────────────────────────────────────────────────────────────────────────
    with tab_approve:
        st.subheader("Approve & Invoice")

        all_ci = sorted(
            [ci for ci in client_invs_list
             if ci.get("status") in ("validated", "ready_to_invoice")],
            key=lambda x: x.get("created_at", ""),
            reverse=True,
        )

        if not all_ci:
            st.info("No invoices awaiting processing.")
        else:
            st.caption(f"{len(all_ci)} invoice(s) — newest first")

            for ci in all_ci:
                prov    = prov_by_id.get(ci.get("provider_invoice_id", ""), {})
                status  = ci.get("status", "")
                cid     = ci["id"]

                current_svc = ci.get("service_type") or "in_out"
                svc = current_svc

                confirm_key = f"confirm_del_{cid}"

                with st.container(border=True):
                    # ── Status banner ─────────────────────────────────────
                    if status == "ready_to_invoice":
                        st.markdown(
                            '<div style="background:#d1e7dd;border:1px solid #198754;'
                            'border-radius:4px;padding:4px 10px;margin-bottom:6px;">'
                            '<span style="color:#0a3622;font-weight:600;">'
                            '✅ Ready to Invoice</span></div>',
                            unsafe_allow_html=True,
                        )

                    # ── Row 1: invoice info ───────────────────────────────
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
                        if status == "validated":
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

                    # ── Row 2: temp recorder + actions ────────────────────
                    r2a, r2c, r2d = st.columns([2.5, 0.8, 0.8])

                    with r2a:
                        _TR_TO_LBL = {"hardware_installation": "Hardware & Installation",
                                      "installation_only"    : "Installation Only"}
                        _stored = ci.get("temp_recorder", "")
                        if _stored is True:
                            _stored = "hardware_installation"
                        if _stored:
                            st.caption(f"🌡 {_TR_TO_LBL.get(_stored, _stored)}")

                    with r2c:
                        _pdf_path     = prov.get("pdf_local_path", "")
                        _pdf_exists   = bool(_pdf_path)
                        _pdf_key      = f"approve_pdf_{cid}"
                        _save_pdf_key = f"save_pdf_{cid}"
                        _has_saved    = bool(st.session_state.get(_save_pdf_key))
                        _pdf_label    = "📄 Hide" if st.session_state.get(_pdf_key) else "📄 PDF"
                        if _pdf_exists or _has_saved:
                            if st.button(_pdf_label, key=f"pdf_{cid}", width='stretch'):
                                st.session_state[_pdf_key] = not st.session_state.get(_pdf_key, False)
                                st.rerun()
                        else:
                            st.button("📄 PDF", key=f"pdf_na_{cid}", disabled=True, width='stretch')

                    with r2d:
                        if st.session_state.get(confirm_key):
                            st.caption("⚠️ Sure?")
                        else:
                            if st.button("🗑 Delete", key=f"del_{cid}", width='stretch'):
                                st.session_state[confirm_key] = True
                                st.rerun()

                    # ── Delete confirmation ───────────────────────────────
                    if st.session_state.get(confirm_key):
                        dc_yes, dc_no = st.columns(2)
                        if dc_yes.button("✅ Yes, delete", key=f"del_yes_{cid}", type="primary", width='stretch'):
                            dm.delete_client_invoice(cid)
                            st.session_state.pop(confirm_key, None)
                            st.rerun()
                        if dc_no.button("✗ Cancel", key=f"del_no_{cid}", width='stretch'):
                            st.session_state.pop(confirm_key, None)
                            st.rerun()

                    # ── Inline PDF viewer ─────────────────────────────────
                    if st.session_state.get(f"approve_pdf_{cid}"):
                        from streamlit_pdf_viewer import pdf_viewer
                        _saved = st.session_state.get(f"save_pdf_{cid}")
                        if _saved:
                            # Show the generated invoice PDF (includes saved temperature data)
                            pdf_viewer(_saved[0], key=f"approve_pdfview_{cid}")
                        elif _pdf_exists:
                            # Fall back to original provider PDF before Save is used
                            _b = _get_pdf_bytes(_pdf_path)
                            if _b:
                                pdf_viewer(_b, key=f"approve_pdfview_{cid}")
                            else:
                                st.warning("PDF not available.")
                        else:
                            st.warning("PDF not available.")

                    # ── Job details form + Generate Invoice ───────────────
                    _TR_OPTS   = ["Hardware & Installation", "Installation Only"]
                    _TR_TO_KEY = {"Hardware & Installation": "hardware_installation",
                                  "Installation Only"      : "installation_only"}
                    _TR_TO_LBL = {"hardware_installation": "Hardware & Installation",
                                  "installation_only"    : "Installation Only"}

                    st.markdown("---")
                    st.caption("Job Details")

                    _cl_rates  = dm.get_rates_for_client(ci.get("client_name", ""))
                    _cbp       = bool(_cl_rates.get("charged_by_pallet", True))
                    _fixed_pal = int(_cl_rates.get("fixed_pallet_count", 0) or 0)
                    if _cbp:
                        _pa, _pb, _pc = st.columns(3)
                        _pal_default = _fixed_pal if _fixed_pal > 0 else int(ci.get("pallet_count", 1) or 1)
                        _pal_label   = f"Total Pallets (fixed: {_fixed_pal})" if _fixed_pal > 0 else "Total Pallets"
                        _pal = _pa.number_input(_pal_label, min_value=1, step=1, value=_pal_default, key=f"val_pal_{cid}")
                        _dmg = _pb.number_input("Damaged Pallets", min_value=0, step=1, value=int(ci.get("damaged_pallets", 0) or 0), key=f"val_dmg_{cid}")
                        _brk = _pc.number_input("Broken Pallets",  min_value=0, step=1, value=int(ci.get("broken_pallets", 0) or 0), key=f"val_brk_{cid}")
                    else:
                        st.info("Billing is per truck — no pallet count required.")
                        _pal = 1
                        _pb, _pc = st.columns(2)
                        _dmg = _pb.number_input("Damaged Pallets", min_value=0, step=1, value=int(ci.get("damaged_pallets", 0) or 0), key=f"val_dmg_{cid}")
                        _brk = _pc.number_input("Broken Pallets",  min_value=0, step=1, value=int(ci.get("broken_pallets", 0) or 0), key=f"val_brk_{cid}")

                    _new_extras: list[str] = []

                    _stored_tr = ci.get("temp_recorder", "hardware_installation")
                    if _stored_tr is True:
                        _stored_tr = "hardware_installation"
                    _tr_default = _TR_TO_LBL.get(_stored_tr, "Hardware & Installation") if _stored_tr else "Hardware & Installation"
                    _tr_sel = st.radio(
                        "Temperature Recorder",
                        options=_TR_OPTS,
                        index=_TR_OPTS.index(_tr_default),
                        horizontal=True,
                        key=f"val_tr_{cid}",
                    )
                    _new_tr = _TR_TO_KEY[_tr_sel]

                    st.caption("Temperature Input")
                    _producto_caliente = st.checkbox(
                        "Producto Caliente",
                        value=bool(ci.get("producto_caliente", False)),
                        key=f"val_pc_{cid}",
                    )
                    _t1, _t2, _t3 = st.columns(3)
                    _temp1 = _t1.text_input("Temperature 1 (°F)", value=ci.get("temp_f1", ""), key=f"val_t1_{cid}")
                    _temp2 = _t2.text_input("Temperature 2 (°F)", value=ci.get("temp_f2", ""), key=f"val_t2_{cid}")
                    _temp3 = _t3.text_input("Temperature 3 (°F)", value=ci.get("temp_f3", ""), key=f"val_t3_{cid}")

                    _new_notes = st.text_area("Notes", value=ci.get("worker_notes", ""), height=80, key=f"val_notes_{cid}")

                    # ── Save / Send buttons ───────────────────────────────────
                    _save_pdf_key = f"save_pdf_{cid}"
                    _save_ok_key  = f"save_ok_{cid}"
                    _btn1, _btn2  = st.columns([1, 2])

                    if _btn1.button("💾 Save", key=f"savebtn_{cid}", width='stretch'):
                        # 1. Persist all form fields — no status change, no QB number
                        dm.update_client_invoice(cid, {
                            "pallet_count"     : int(_pal),
                            "damaged_pallets"  : int(_dmg),
                            "broken_pallets"   : int(_brk),
                            "temp_recorder"    : _new_tr,
                            "producto_caliente": _producto_caliente,
                            "temp_f1"          : _temp1.strip(),
                            "temp_f2"          : _temp2.strip(),
                            "temp_f3"          : _temp3.strip(),
                            "worker_notes"     : _new_notes.strip(),
                        })
                        # 2. Calculate charges so the PDF has complete line items
                        _sc = calculate_charges(
                            dm=dm,
                            service_type=svc,
                            pallet_count=int(_pal),
                            temp_recorder=_new_tr,
                            extra_charges=_new_extras,
                            damaged_pallets=int(_dmg),
                            broken_pallets=int(_brk),
                            client_name=ci.get("client_name", ""),
                        )
                        # 3. Build a full invoice dict for PDF generation
                        _ci_pdf = {
                            **ci,
                            "service_type"     : svc,
                            "pallet_count"     : int(_pal),
                            "damaged_pallets"  : int(_dmg),
                            "broken_pallets"   : int(_brk),
                            "temp_recorder"    : _new_tr,
                            "producto_caliente": _producto_caliente,
                            "temp_f1"          : _temp1.strip(),
                            "temp_f2"          : _temp2.strip(),
                            "temp_f3"          : _temp3.strip(),
                            "worker_notes"     : _new_notes.strip(),
                            "line_items"       : _sc["line_items"],
                            "subtotal"         : _sc["subtotal"],
                            "total"            : _sc["total"],
                        }
                        # 4. Generate PDF — temperature data rendered bottom-right
                        _save_pdf_bytes = _generate_pdf(_ci_pdf, prov.get("pdf_local_path"))
                        # 5. Re-upload if a QB invoice number already exists
                        _save_qb = ci.get("quickbooks_invoice_number")
                        if _save_qb:
                            try:
                                _upload_pdf_bytes(f"{_save_qb}-invoice.pdf", _save_pdf_bytes)
                            except Exception as _ue:
                                logger.warning("PDF re-upload on save failed: %s", _ue)
                        _pdf_stem = _save_qb or cid
                        st.session_state[_save_pdf_key] = (_save_pdf_bytes, _pdf_stem)
                        st.session_state[_save_ok_key]  = True
                        st.rerun()

                    # ── Download row (visible after Save) ─────────────────────
                    if st.session_state.get(_save_ok_key):
                        _saved_bytes, _saved_stem = st.session_state.get(
                            _save_pdf_key, (None, "invoice")
                        )
                        _dl1, _dl2 = st.columns([2, 1])
                        _dl1.success("Temperature data saved and mapped to PDF.")
                        if _saved_bytes:
                            _dl2.download_button(
                                "⬇ Download PDF",
                                data=_saved_bytes,
                                file_name=f"{_saved_stem}-invoice.pdf",
                                mime="application/pdf",
                                key=f"dl_saved_{cid}",
                            )

                    if _btn2.button("📤 Send to Accounting", key=f"gen_{cid}", type="primary", width='stretch'):
                        inv_id  = _generate_unique_invoice_id(dm)
                        charges = calculate_charges(
                            dm=dm,
                            service_type=svc,
                            pallet_count=int(_pal),
                            temp_recorder=_new_tr,
                            extra_charges=_new_extras,
                            damaged_pallets=int(_dmg),
                            broken_pallets=int(_brk),
                            client_name=ci.get("client_name", ""),
                        )
                        client_rates = dm.get_rates_for_client(ci.get("client_name", ""))
                        billing_addr = dm.get_client_address(ci.get("client_name", ""))
                        dm.update_client_invoice(cid, {
                            "quickbooks_invoice_number": inv_id,
                            "service_type"    : svc,
                            "pallet_count"    : int(_pal),
                            "damaged_pallets" : int(_dmg),
                            "broken_pallets"  : int(_brk),
                            "extra_charges"   : _new_extras,
                            "temp_recorder"      : _new_tr,
                            "producto_caliente"  : _producto_caliente,
                            "temp_f1"            : _temp1.strip(),
                            "temp_f2"            : _temp2.strip(),
                            "temp_f3"            : _temp3.strip(),
                            "worker_notes"       : _new_notes.strip(),
                            "line_items"      : charges["line_items"],
                            "subtotal"        : charges["subtotal"],
                            "total"           : charges["total"],
                            "net_days"        : int(client_rates.get("net_days", 30)),
                            "billing_address" : billing_addr,
                            "status"          : "invoiced",
                            "invoice_date"    : datetime.utcnow().date().isoformat(),
                        })
                        if prov.get("email_intake_id"):
                            dm.update_email_log(prov["email_intake_id"], {"status": "invoiced"})
                        if prov.get("pdf_local_path"):
                            _move_to_processed(prov["pdf_local_path"])
                        ci_updated = dm.get_client_invoice_by_id(cid)
                        if ci_updated:
                            try:
                                _pdf_bytes = _generate_pdf(ci_updated, prov.get("pdf_local_path"))
                                _upload_pdf_bytes(f"{inv_id}-invoice.pdf", _pdf_bytes)
                            except Exception as _e:
                                logger.warning("Could not upload generated invoice PDF: %s", _e)
                        st.session_state.pop(_save_pdf_key, None)
                        st.session_state.pop(_save_ok_key, None)
                        st.success(f"Invoice #{inv_id} generated! Total: ${charges['total']:,.2f}")
                        st.rerun()

    # ──────────────────────────────────────────────────────────────────────────
    # TAB 3 — SENT TO ACCOUNTING
    # ──────────────────────────────────────────────────────────────────────────
    with tab_export:
        st.subheader("Sent to Accounting")

        sent = sorted(
            [
                ci for ci in client_invs_list
                if ci.get("status") in ("invoiced",) or ci.get("quickbooks_exported")
            ],
            key=lambda x: x.get("created_at", ""),
            reverse=True,
        )

        if not sent:
            st.info("No invoices sent to accounting yet.")
        else:
            # ── Filters ───────────────────────────────────────────────────────
            all_clients = sorted({ci.get("client_name", "") for ci in sent if ci.get("client_name")})
            all_dates   = sorted({ci.get("invoice_date", "")[:10] for ci in sent if ci.get("invoice_date")}, reverse=True)

            f_col, f_col2 = st.columns(2)
            with f_col:
                sel_client = st.selectbox(
                    "Filter by client",
                    options=["All"] + all_clients,
                    key="acc_filter_client",
                )
            with f_col2:
                sel_date = st.selectbox(
                    "Filter by date",
                    options=["All"] + all_dates,
                    key="acc_filter_date",
                )

            filtered = [
                ci for ci in sent
                if (sel_client == "All" or ci.get("client_name") == sel_client)
                and (sel_date == "All" or (ci.get("invoice_date", "")[:10]) == sel_date)
            ]

            st.caption(f"{len(filtered)} of {len(sent)} invoice(s)")
            rows = [
                {
                    "QB #"    : ci.get("quickbooks_invoice_number") or "—",
                    "Date"    : ci.get("invoice_date", "—"),
                    "Client"  : ci.get("client_name", "—"),
                    "Total"   : f"${ci.get('total', 0):,.2f}",
                    "Net Days": str(ci.get("net_days", "—")),
                    "Status"  : "Exported to QB" if ci.get("quickbooks_exported") else "In Accounting",
                }
                for ci in filtered
            ]
            st.dataframe(rows, use_container_width=True, hide_index=True)

    # ── Button colour overrides (injected outside all tabs so the iframe
    #    doesn't taint any tab's background; MutationObserver watches the
    #    full page DOM regardless of injection point). ─────────────────────
    import streamlit.components.v1 as _components
    _components.html("""<script>
(function () {
    function applyColors() {
        window.parent.document.querySelectorAll('button').forEach(function (btn) {
            var t = btn.innerText.trim();
            if (t.indexOf('Return to Validation') !== -1) {
                btn.style.setProperty('background-color', '#dc3545', 'important');
                btn.style.setProperty('border-color',     '#dc3545', 'important');
                btn.style.setProperty('color',            '#fff',    'important');
            } else if (t === '\u2705 Received') {
                btn.style.setProperty('background-color', '#198754', 'important');
                btn.style.setProperty('border-color',     '#198754', 'important');
                btn.style.setProperty('color',            '#fff',    'important');
            }
        });
    }
    applyColors();
    new MutationObserver(applyColors).observe(
        window.parent.document.body, {childList: true, subtree: true}
    );
}());
</script>""", height=0)

