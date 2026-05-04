"""
bol_dashboard.py
================
Bill of Lading Registration Automation.

Status flow:
  bol_inbox → pending_checkin → checked_in → bol_inspection → printed

Layout:
  - Pre-tab inbox section: new BOLs with Edit / View PDF / Delete / Validate
  - Tab 1 "Pending Trucker Check-In": yellow (pending) and green (checked-in) boxes
  - Tab 2 "BOL Inspection": View PDF / Edit / Print
  - Tab 3 "Printed": final table with PDF download
"""

from datetime import datetime, timezone
from pathlib import Path

import streamlit as st

from data_manager import DataManager
from utils.pdf_storage import get_pdf_bytes as _get_pdf_bytes

_CHECKIN_HOLD_SECONDS = 30


# ─── helpers ──────────────────────────────────────────────────────────────────

def _now_str() -> str:
    return datetime.utcnow().isoformat() + "Z"


def _parse_dt(s: str) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def _fire_notification(po_number: str, driver_name: str) -> None:
    """Fire a Windows toast notification. Surfaces error to UI on failure."""
    try:
        from plyer import notification  # type: ignore
        notification.notify(
            title="Driver Checked In — INCO",
            message=f"Driver {driver_name} checked in for PO {po_number}.",
            app_name="INCO Invoice Automation",
            timeout=10,
        )
    except Exception as exc:
        st.warning(f"⚠️ Windows notification failed: {exc}")


# ─── auto-refresh fragment ─────────────────────────────────────────────────────

@st.fragment(run_every="3s")
def _checkin_watcher(dm: DataManager) -> None:
    """
    Runs every 3 s while checked_in BOLs exist.
    Fires Windows notification on first run after check-in, then
    auto-transitions to bol_inspection after the hold period.
    """
    now = datetime.now(timezone.utc)
    changed = False
    for rec in dm.get_bol_records():
        if rec.get("status") != "checked_in":
            continue

        if not rec.get("checkin_notified"):
            _fire_notification(rec.get("po_number", "?"), rec.get("driver_name", "?"))
            dm.update_bol_record(rec["id"], {"checkin_notified": True})

        checkin_dt = _parse_dt(rec.get("checkin_at", ""))
        if checkin_dt and (now - checkin_dt).total_seconds() >= _CHECKIN_HOLD_SECONDS:
            dm.update_bol_record(rec["id"], {"status": "bol_inspection"})
            changed = True

    if changed:
        st.rerun()


# ─── inbox section (pre-tab) ──────────────────────────────────────────────────

def _render_inbox_section(dm: DataManager) -> None:
    poll_col, _ = st.columns([1, 4])
    with poll_col:
        if st.button("🔄 Poll BOL Inbox", width='stretch'):
            from email_pipeline.bol_listener import poll_bol_inbox
            with st.spinner("Polling BOL inbox…"):
                count = poll_bol_inbox(dm)
            if count:
                st.success(f"{count} new BOL email(s) received.")
                st.rerun()
            else:
                st.info("No new BOL emails found.")

    with st.expander("➕ Add BOL Manually"):
        f1, f2 = st.columns(2)
        new_po   = f1.text_input("PO Number", key="new_bol_po")
        new_date = f2.text_input(
            "Date Received",
            value=datetime.utcnow().date().isoformat(),
            key="new_bol_date",
        )
        if st.button("Add BOL", key="add_bol_btn", type="primary", width='stretch'):
            if not new_po.strip():
                st.error("PO Number is required.")
            else:
                dm.add_bol_record({
                    "po_number"       : new_po.strip().upper(),
                    "received_at"     : new_date.strip() + "T00:00:00Z",
                    "pdf_local_path"  : "",
                    "status"          : "bol_inbox",
                    "driver_name"     : None,
                    "checkin_at"      : None,
                    "checkin_notified": False,
                })
                st.success(f"BOL {new_po.strip().upper()} added.")
                st.rerun()

    st.markdown("---")

    inbox_bols = sorted(
        [r for r in dm.get_bol_records() if r.get("status") == "bol_inbox"],
        key=lambda x: x.get("received_at", ""),
        reverse=True,
    )

    if not inbox_bols:
        st.info("No BOLs in inbox.")
        return

    st.caption(f"{len(inbox_bols)} BOL(s) awaiting validation")

    for bol in inbox_bols:
        bid        = bol["id"]
        pdf_path   = bol.get("pdf_local_path", "")
        pdf_exists = bool(pdf_path)
        pdf_key    = f"bol_pdf_{bid}"
        del_key    = f"bol_del_{bid}"
        edit_key   = f"bol_edit_{bid}"
        received   = bol.get("received_at", "—")[:10]

        with st.container(border=True):
            st.markdown(f"**PO Number:** {bol.get('po_number', '—')}")
            st.caption(f"📅 Date Received: {received}")

            if st.session_state.get(edit_key):
                ep1, ep2 = st.columns(2)
                edit_po   = ep1.text_input("PO Number",    value=bol.get("po_number", ""), key=f"epo_{bid}")
                edit_date = ep2.text_input("Date Received", value=received,                 key=f"edt_{bid}")
                es1, es2  = st.columns(2)
                if es1.button("💾 Save", key=f"esave_{bid}", type="primary", width='stretch'):
                    dm.update_bol_record(bid, {
                        "po_number"  : edit_po.strip().upper(),
                        "received_at": edit_date.strip() + "T00:00:00Z",
                    })
                    st.session_state.pop(edit_key, None)
                    st.rerun()
                if es2.button("✗ Cancel", key=f"ecancel_{bid}", width='stretch'):
                    st.session_state.pop(edit_key, None)
                    st.rerun()

            else:
                b1, b2, b3 = st.columns(3)

                if b1.button("✏️ Edit", key=f"bol_ebtn_{bid}", width='stretch'):
                    st.session_state[edit_key] = True
                    st.rerun()

                pdf_label = "📄 Hide PDF" if st.session_state.get(pdf_key) else "📄 View PDF"
                if pdf_exists:
                    if b2.button(pdf_label, key=f"bol_epdf_{bid}", width='stretch'):
                        st.session_state[pdf_key] = not st.session_state.get(pdf_key, False)
                        st.rerun()
                else:
                    b2.button("📄 View PDF", key=f"bol_epdf_na_{bid}", disabled=True, width='stretch')

                if st.session_state.get(del_key):
                    b3.caption("⚠️ Sure?")
                else:
                    if b3.button("🗑 Delete", key=f"bol_delbtn_{bid}", width='stretch'):
                        st.session_state[del_key] = True
                        st.rerun()

                if st.session_state.get(del_key):
                    dc1, dc2 = st.columns(2)
                    if dc1.button("✅ Yes, delete", key=f"bol_delyes_{bid}", type="primary", width='stretch'):
                        dm.delete_bol_record(bid)
                        st.session_state.pop(del_key, None)
                        st.rerun()
                    if dc2.button("✗ Cancel", key=f"bol_delno_{bid}", width='stretch'):
                        st.session_state.pop(del_key, None)
                        st.rerun()

                if st.session_state.get(pdf_key) and pdf_exists:
                    from streamlit_pdf_viewer import pdf_viewer
                    _b = _get_pdf_bytes(pdf_path)
                    if _b:
                        pdf_viewer(_b, key=f"bol_pdfview_{bid}")
                    else:
                        st.warning("PDF not available.")

                st.markdown("---")
                val_col, _ = st.columns([1, 2])
                if val_col.button(
                    "✅ Validate",
                    key=f"bol_validate_{bid}",
                    type="primary",
                    width='stretch',
                ):
                    dm.update_bol_record(bid, {"status": "pending_checkin"})
                    st.success(f"BOL {bol.get('po_number')} moved to Pending Trucker Check-In.")
                    st.rerun()


# ─── Tab 1: Pending Trucker Check-In ──────────────────────────────────────────

def _render_checkin_tab(dm: DataManager) -> None:
    checkin_bols = sorted(
        [r for r in dm.get_bol_records() if r.get("status") in ("pending_checkin", "checked_in")],
        key=lambda x: x.get("received_at", ""),
        reverse=True,
    )

    if not checkin_bols:
        st.info("No BOLs pending trucker check-in.")
        return

    st.caption(f"{len(checkin_bols)} BOL(s)")
    now = datetime.now(timezone.utc)

    for bol in checkin_bols:
        bid      = bol["id"]
        po_num   = bol.get("po_number", "—")
        received = bol.get("received_at", "—")[:10]
        status   = bol.get("status")

        if status == "pending_checkin":
            st.markdown(
                f'<div style="background:#fff3cd;border:2px solid #ffc107;'
                f'border-radius:8px;padding:12px 16px;margin-bottom:8px;">'
                f'<div style="font-weight:700;font-size:1.05em;margin-bottom:6px;">'
                f'PO Number: {po_num}</div>'
                f'<div style="font-size:0.9em;margin-bottom:8px;">📅 Date Received: {received}</div>'
                f'<div style="color:#856404;font-style:italic;">'
                f'⏳ The check-in of the driver is pending.</div>'
                f'</div>',
                unsafe_allow_html=True,
            )
            # Placeholder — replace with driver check-in integration later
            name_col, btn_col = st.columns([2, 1])
            driver_input = name_col.text_input(
                "Driver name",
                key=f"driver_input_{bid}",
                placeholder="Enter driver name…",
                label_visibility="collapsed",
            )
            if btn_col.button("🚛 Mark Checked In", key=f"bol_checkin_{bid}", width='stretch'):
                if not driver_input.strip():
                    st.error("Enter the driver's name first.")
                else:
                    dm.update_bol_record(bid, {
                        "status"          : "checked_in",
                        "driver_name"     : driver_input.strip(),
                        "checkin_at"      : _now_str(),
                        "checkin_notified": False,
                    })
                    st.rerun()

        elif status == "checked_in":
            driver     = bol.get("driver_name", "—")
            checkin_dt = _parse_dt(bol.get("checkin_at", ""))
            elapsed    = (now - checkin_dt).total_seconds() if checkin_dt else 0
            remaining  = max(0, _CHECKIN_HOLD_SECONDS - int(elapsed))

            st.markdown(
                f'<div style="background:#d1e7dd;border:2px solid #198754;'
                f'border-radius:8px;padding:12px 16px;margin-bottom:8px;">'
                f'<div style="font-weight:700;font-size:1.05em;margin-bottom:6px;">'
                f'PO Number: {po_num}</div>'
                f'<div style="font-size:0.9em;margin-bottom:4px;">'
                f'📅 Date Received: {received}&nbsp;&nbsp;|&nbsp;&nbsp;'
                f'🚛 <strong>{driver}</strong></div>'
                f'<div style="color:#0a3622;font-style:italic;">✅ Driver has checked in.</div>'
                + (
                    f'<div style="color:#555;font-size:0.8em;margin-top:6px;">'
                    f'Moving to BOL Inspection in {remaining}s…</div>'
                    if remaining > 0 else ''
                )
                + '</div>',
                unsafe_allow_html=True,
            )


# ─── Tab 2: BOL Inspection ────────────────────────────────────────────────────

def _append_admin_annotations(pdf_path: str, po_num: str, driver_name: str,
                               annotations: dict) -> str:
    """
    Build an Admin Completion Notes page with reportlab and append it to the
    existing BOL PDF (in-place). Returns the (same) path on success.
    """
    from io import BytesIO
    from reportlab.lib.pagesizes import letter
    from reportlab.lib import colors
    from reportlab.lib.units import inch
    from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.enums import TA_CENTER
    from pypdf import PdfWriter, PdfReader

    BLUE    = colors.HexColor("#0d47a1")
    LT_BLUE = colors.HexColor("#e3f2fd")
    styles  = getSampleStyleSheet()

    buf = BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=letter,
                             leftMargin=0.75 * inch, rightMargin=0.75 * inch,
                             topMargin=0.75 * inch, bottomMargin=0.75 * inch)

    hdr_s = ParagraphStyle("h", parent=styles["Heading1"],
                            textColor=colors.white, fontSize=16, alignment=TA_CENTER)
    sub_s = ParagraphStyle("s", parent=styles["Normal"],
                            textColor=colors.white, fontSize=10, alignment=TA_CENTER)
    lbl_s = ParagraphStyle("l", parent=styles["Normal"], fontSize=9, textColor=colors.grey)
    val_s = ParagraphStyle("v", parent=styles["Normal"], fontSize=12, fontName="Helvetica-Bold")

    story = []

    ht = Table([
        [Paragraph("<b>ADMIN COMPLETION NOTES</b>", hdr_s)],
        [Paragraph(
            f"PO: {po_num}  |  Driver: {driver_name or '—'}  |  "
            f"{datetime.now().strftime('%Y-%m-%d %H:%M')}",
            sub_s,
        )],
    ], colWidths=[7 * inch])
    ht.setStyle(TableStyle([
        ("BACKGROUND",    (0, 0), (-1, -1), BLUE),
        ("TOPPADDING",    (0, 0), (-1, -1), 10),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 10),
        ("LEFTPADDING",   (0, 0), (-1, -1), 12),
        ("RIGHTPADDING",  (0, 0), (-1, -1), 12),
    ]))
    story.append(ht)
    story.append(Spacer(1, 0.2 * inch))

    # Field rows (skip empty)
    FIELD_LABELS = [
        ("carrier",     "Carrier / Transportista"),
        ("trailer_num", "Trailer # / Número de Remolque"),
        ("seal_num",    "Seal # / Número de Sello"),
        ("num_pallets", "# Pallets"),
        ("temp_req",    "Temp Requirement / Temperatura Requerida"),
    ]
    rows = [
        [Paragraph(label, lbl_s), Paragraph(annotations.get(key, "") or "—", val_s)]
        for key, label in FIELD_LABELS
        if annotations.get(key, "").strip()
    ]
    if rows:
        ft = Table(rows, colWidths=[3 * inch, 4 * inch])
        ft.setStyle(TableStyle([
            ("BACKGROUND",    (0, 0), (0, -1), LT_BLUE),
            ("BOX",           (0, 0), (-1, -1), 1, BLUE),
            ("INNERGRID",     (0, 0), (-1, -1), 0.5, colors.lightgrey),
            ("TOPPADDING",    (0, 0), (-1, -1), 8),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
            ("LEFTPADDING",   (0, 0), (-1, -1), 10),
        ]))
        story.append(ft)
        story.append(Spacer(1, 0.15 * inch))

    if annotations.get("notes", "").strip():
        nl = Table([[Paragraph("Special Instructions / Instrucciones Especiales", lbl_s)]],
                   colWidths=[7 * inch])
        nl.setStyle(TableStyle([
            ("BACKGROUND",    (0, 0), (-1, -1), LT_BLUE),
            ("BOX",           (0, 0), (-1, -1), 1, BLUE),
            ("TOPPADDING",    (0, 0), (-1, -1), 8),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
            ("LEFTPADDING",   (0, 0), (-1, -1), 10),
        ]))
        nv = Table([[Paragraph(annotations["notes"], val_s)]], colWidths=[7 * inch])
        nv.setStyle(TableStyle([
            ("BOX",           (0, 0), (-1, -1), 1, BLUE),
            ("TOPPADDING",    (0, 0), (-1, -1), 8),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
            ("LEFTPADDING",   (0, 0), (-1, -1), 10),
        ]))
        story.append(nl)
        story.append(nv)

    doc.build(story)
    notes_bytes = buf.getvalue()

    # Merge: existing BOL pages + new notes page
    writer = PdfWriter()
    if pdf_path and Path(pdf_path).exists():
        for page in PdfReader(pdf_path).pages:
            writer.add_page(page)
    for page in PdfReader(BytesIO(notes_bytes)).pages:
        writer.add_page(page)

    out = BytesIO()
    writer.write(out)

    save_path = pdf_path if pdf_path else str(
        Path(pdf_path).parent / f"BOL_{po_num}_annotated.pdf"
    )
    Path(save_path).write_bytes(out.getvalue())
    return save_path


def _render_inspection_tab(dm: DataManager) -> None:
    inspection_bols = sorted(
        [r for r in dm.get_bol_records() if r.get("status") == "bol_inspection"],
        key=lambda x: x.get("received_at", ""),
        reverse=True,
    )

    if not inspection_bols:
        st.info("No BOLs awaiting inspection.")
        return

    st.caption(f"{len(inspection_bols)} BOL(s)")

    for bol in inspection_bols:
        bid        = bol["id"]
        po_num     = bol.get("po_number", "—")
        received   = bol.get("received_at", "—")[:10]
        driver     = bol.get("driver_name") or "—"
        pdf_path   = bol.get("pdf_local_path", "")
        pdf_exists = bool(pdf_path)
        pdf_key    = f"bol_insp_pdf_{bid}"
        edit_key   = f"bol_insp_edit_{bid}"
        form_key   = f"bol_insp_form_{bid}"

        with st.container(border=True):
            h1, h2 = st.columns([4, 1])
            h1.markdown(f"**PO Number:** {po_num}")
            h1.caption(
                f"🚛 Driver: {driver}  |  📅 Received: {received}"
                + ("  |  ✅ Annotations saved" if bol.get("annotations_saved_at") else "")
            )

            if st.session_state.get(edit_key):
                ep1, ep2 = st.columns(2)
                edit_po   = ep1.text_input("PO Number",    value=bol.get("po_number", ""), key=f"insp_epo_{bid}")
                edit_date = ep2.text_input("Date Received", value=received,                 key=f"insp_edt_{bid}")
                es1, es2  = st.columns(2)
                if es1.button("💾 Save", key=f"insp_esave_{bid}", type="primary", width='stretch'):
                    dm.update_bol_record(bid, {
                        "po_number"  : edit_po.strip().upper(),
                        "received_at": edit_date.strip() + "T00:00:00Z",
                    })
                    st.session_state.pop(edit_key, None)
                    st.rerun()
                if es2.button("✗ Cancel", key=f"insp_ecancel_{bid}", width='stretch'):
                    st.session_state.pop(edit_key, None)
                    st.rerun()

            else:
                # ── Action buttons row ───────────────────────────────────────
                b1, b2, b3 = st.columns(3)

                pdf_label = "📄 Hide PDF" if st.session_state.get(pdf_key) else "📄 View PDF"
                if pdf_exists:
                    if b1.button(pdf_label, key=f"insp_pdf_{bid}", width='stretch'):
                        st.session_state[pdf_key] = not st.session_state.get(pdf_key, False)
                        st.rerun()
                else:
                    b1.button("📄 View PDF", key=f"insp_pdf_na_{bid}", disabled=True, width='stretch')

                form_label = "📝 Hide Form" if st.session_state.get(form_key) else "📝 Complete Form"
                if b2.button(form_label, key=f"insp_form_btn_{bid}", width='stretch'):
                    st.session_state[form_key] = not st.session_state.get(form_key, False)
                    st.rerun()

                if b3.button("✏️ Edit", key=f"insp_ebtn_{bid}", width='stretch'):
                    st.session_state[edit_key] = True
                    st.rerun()

                # ── PDF viewer ───────────────────────────────────────────────
                if st.session_state.get(pdf_key) and pdf_exists:
                    from streamlit_pdf_viewer import pdf_viewer
                    _b = _get_pdf_bytes(pdf_path)
                    if _b:
                        pdf_viewer(_b, key=f"insp_pdfview_{bid}")
                    else:
                        st.warning("PDF not available.")

                # ── Admin completion form ────────────────────────────────────
                if st.session_state.get(form_key):
                    st.markdown("---")
                    st.markdown("##### 📋 Complete Remaining BOL Fields")
                    st.caption(
                        "Fill in any remaining information and click **Save Annotations** "
                        "to append a notes page to the BOL PDF."
                    )

                    prev = bol.get("admin_annotations", {})

                    fc1, fc2 = st.columns(2)
                    carrier     = fc1.text_input("Carrier / Transportista",
                                                  key=f"f_carrier_{bid}",
                                                  value=prev.get("carrier", ""),
                                                  placeholder="e.g. XPO Logistics")
                    trailer_num = fc2.text_input("Trailer # / Número de Remolque",
                                                  key=f"f_trailer_{bid}",
                                                  value=prev.get("trailer_num", ""),
                                                  placeholder="e.g. TR-44821")
                    seal_num    = fc1.text_input("Seal # / Número de Sello",
                                                  key=f"f_seal_{bid}",
                                                  value=prev.get("seal_num", ""),
                                                  placeholder="e.g. S-1234")
                    num_pallets = fc2.text_input("# Pallets",
                                                  key=f"f_pallets_{bid}",
                                                  value=prev.get("num_pallets", ""),
                                                  placeholder="e.g. 24")
                    temp_req    = fc1.text_input("Temp Requirement / Temperatura Requerida",
                                                  key=f"f_temp_{bid}",
                                                  value=prev.get("temp_req", ""),
                                                  placeholder="e.g. 34–38°F")
                    notes       = st.text_area("Special Instructions / Instrucciones Especiales",
                                               key=f"f_notes_{bid}",
                                               value=prev.get("notes", ""),
                                               height=90)

                    save_col, dl_col, _ = st.columns([1, 1, 2])

                    if save_col.button("💾 Save Annotations", key=f"f_save_{bid}",
                                       type="primary", width='stretch'):
                        ann = {
                            "carrier":     carrier.strip(),
                            "trailer_num": trailer_num.strip(),
                            "seal_num":    seal_num.strip(),
                            "num_pallets": num_pallets.strip(),
                            "temp_req":    temp_req.strip(),
                            "notes":       notes.strip(),
                        }
                        if not any(ann.values()):
                            st.warning("Please fill in at least one field before saving.")
                        else:
                            try:
                                new_path = _append_admin_annotations(
                                    pdf_path, po_num, driver, ann
                                )
                                dm.update_bol_record(bid, {
                                    "pdf_local_path"      : new_path,
                                    "admin_annotations"   : ann,
                                    "annotations_saved_at": _now_str(),
                                })
                                st.session_state[form_key] = False
                                st.success("✅ Annotations appended to BOL PDF.")
                                st.rerun()
                            except Exception as exc:
                                st.error(f"Error saving annotations: {exc}")

                    # Download the current PDF (with or without annotations)
                    if pdf_exists:
                        _dl = _get_pdf_bytes(pdf_path)
                        if _dl:
                            dl_col.download_button(
                                "⬇ Download PDF",
                                _dl,
                                file_name=f"BOL_{po_num}.pdf",
                                mime="application/pdf",
                                key=f"insp_dl_{bid}",
                            )

                st.markdown("---")
                print_col, _ = st.columns([1, 2])
                if print_col.button(
                    "🖨 Print",
                    key=f"bol_print_{bid}",
                    type="primary",
                    width='stretch',
                ):
                    dm.update_bol_record(bid, {
                        "status"    : "printed",
                        "print_date": datetime.utcnow().date().isoformat(),
                    })
                    st.success(f"BOL {po_num} registered as printed.")
                    st.rerun()


# ─── Tab 3: Printed ───────────────────────────────────────────────────────────

def _render_printed_tab(dm: DataManager) -> None:
    printed_bols = sorted(
        [r for r in dm.get_bol_records() if r.get("status") == "printed"],
        key=lambda x: x.get("checkin_at", x.get("created_at", "")),
        reverse=True,
    )

    if not printed_bols:
        st.info("No BOLs printed yet.")
        return

    st.caption(f"{len(printed_bols)} BOL(s) printed")

    rows = []
    for bol in printed_bols:
        pdf_path   = bol.get("pdf_local_path", "")
        pdf_exists = bool(pdf_path)
        shipped    = bol.get("checkin_at", "")
        rows.append({
            "PO Number"          : bol.get("po_number", "—"),
            "Date Email Received": bol.get("received_at", "—")[:10],
            "Shipped Date"       : shipped[:10] if shipped else "—",
            "PDF Download"       : "✅" if pdf_exists else "—",
        })

    st.dataframe(rows, use_container_width=True, hide_index=True)

    # Download buttons for BOLs that have a PDF
    has_pdfs = any(
        bol.get("pdf_local_path") and Path(bol["pdf_local_path"]).exists()
        for bol in printed_bols
    )
    if has_pdfs:
        st.caption("PDF Downloads:")
        for bol in printed_bols:
            pdf_path = bol.get("pdf_local_path", "")
            if pdf_path:
                _b = _get_pdf_bytes(pdf_path)
                if _b:
                    st.download_button(
                        f"✅ {bol.get('po_number', '?')} — Download PDF",
                        _b,
                        file_name=f"BOL_{bol.get('po_number', 'unknown')}.pdf",
                        mime="application/pdf",
                        key=f"bol_dl_{bol['id']}",
                    )


# ─── main render ──────────────────────────────────────────────────────────────

def render(dm: DataManager) -> None:
    st.subheader("📋 Bill of Lading Registration Automation")

    # Make primary buttons green for this page
    st.markdown(
        """
        <style>
        button[data-testid="stBaseButton-primary"] {
            background-color: #198754 !important;
            border-color:     #198754 !important;
            color:            white   !important;
        }
        button[data-testid="stBaseButton-primary"]:hover {
            background-color: #157347 !important;
            border-color:     #146c43 !important;
        }
        </style>
        """,
        unsafe_allow_html=True,
    )

    bol_records = dm.get_bol_records()

    # Run auto-transition watcher only when needed
    if any(r.get("status") == "checked_in" for r in bol_records):
        _checkin_watcher(dm)

    tab_validation, tab_checkin, tab_inspection, tab_printed = st.tabs([
        "📋 Validation",
        "🕐 Pending Trucker Check-In",
        "🔍 BOL Inspection",
        "✅ Printed",
    ])

    with tab_validation:
        _render_inbox_section(dm)

    with tab_checkin:
        _render_checkin_tab(dm)

    with tab_inspection:
        _render_inspection_tab(dm)

    with tab_printed:
        _render_printed_tab(dm)
