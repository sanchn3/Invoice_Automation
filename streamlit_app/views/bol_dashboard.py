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


# ─── auto-refresh fragments ────────────────────────────────────────────────────

@st.fragment(run_every="4s")
def _bol_status_watcher(dm: DataManager) -> None:
    """
    Polls Supabase every 4 s. Triggers a full rerun when the set of BOL IDs
    in any active status changes — i.e. whenever the kiosk pushes a check-in.
    """
    active = frozenset(
        (r["id"], r.get("status", "")) for r in dm.get_bol_records()
        if r.get("status") not in ("printed",)
    )
    if active != st.session_state.get("_bol_active_ids"):
        st.session_state["_bol_active_ids"] = active
        st.rerun()


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
        new_po     = f1.text_input("PO Number", key="new_bol_po")
        new_date   = f2.text_input(
            "Date Received",
            value=datetime.utcnow().date().isoformat(),
            key="new_bol_date",
        )
        new_client = st.text_input(
            "Customer Name (optional — helps driver identify shipment at kiosk)",
            key="new_bol_client",
            placeholder="e.g. Walmart, Costco…",
        )
        if st.button("Add BOL", key="add_bol_btn", type="primary", width='stretch'):
            if not new_po.strip():
                st.error("PO Number is required.")
            else:
                dm.add_bol_record({
                    "po_number"       : new_po.strip().upper(),
                    "received_at"     : new_date.strip() + "T00:00:00Z",
                    "client_name"     : new_client.strip() or None,
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
            _client = bol.get("client_name") or ""
            st.caption(f"📅 Date Received: {received}" + (f"  |  🏢 {_client}" if _client else ""))

            if st.session_state.get(edit_key):
                ep1, ep2 = st.columns(2)
                edit_po     = ep1.text_input("PO Number",     value=bol.get("po_number", ""),   key=f"epo_{bid}")
                edit_date   = ep2.text_input("Date Received",  value=received,                   key=f"edt_{bid}")
                edit_client = st.text_input("Customer Name",   value=_client,                    key=f"ecli_{bid}",
                                            placeholder="e.g. Walmart, Costco…")
                es1, es2 = st.columns(2)
                if es1.button("💾 Save", key=f"esave_{bid}", type="primary", width='stretch'):
                    dm.update_bol_record(bid, {
                        "po_number"  : edit_po.strip().upper(),
                        "received_at": edit_date.strip() + "T00:00:00Z",
                        "client_name": edit_client.strip() or None,
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

                # Detect a corrupted/empty PDF (< 5 KB usually means a bad write)
                _pdf_corrupt = False
                if pdf_exists:
                    try:
                        _pdf_corrupt = Path(pdf_path).stat().st_size < 5_000
                    except OSError:
                        _pdf_corrupt = True

                pdf_label = "📄 Hide PDF" if st.session_state.get(pdf_key) else "📄 View PDF"
                if pdf_exists and not _pdf_corrupt:
                    if b2.button(pdf_label, key=f"bol_epdf_{bid}", width='stretch'):
                        st.session_state[pdf_key] = not st.session_state.get(pdf_key, False)
                        st.rerun()
                else:
                    b2.button("📄 View PDF", key=f"bol_epdf_na_{bid}", disabled=True, width='stretch')

                if _pdf_corrupt:
                    st.warning("⚠️ PDF appears corrupted or missing. Upload a replacement below.")
                if not pdf_exists or _pdf_corrupt:
                    _up = st.file_uploader(
                        "Upload BOL PDF", type="pdf",
                        key=f"bol_pdf_upload_{bid}",
                        label_visibility="collapsed",
                    )
                    if _up is not None:
                        from config import BOLS_DIR
                        _dest = BOLS_DIR / (Path(pdf_path).name if pdf_exists else f"BOL_{bol.get('po_number','unknown')}_{bid[:8]}.pdf")
                        _dest.write_bytes(_up.read())
                        dm.update_bol_record(bid, {"pdf_local_path": str(_dest)})
                        st.success(f"PDF saved: {_dest.name}")
                        st.rerun()

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
                    if not bol.get("po_number", "").strip():
                        st.error("⚠️ Cannot validate: PO Number is required. Click ✏️ Edit to add it.")
                    else:
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

        client_name = bol.get("client_name") or ""

        if status == "pending_checkin":
            st.markdown(
                f'<div style="background:#fff3cd;border:2px solid #ffc107;'
                f'border-radius:8px;padding:12px 16px;margin-bottom:8px;color:#000;">'
                f'<div style="font-weight:700;font-size:1.05em;margin-bottom:6px;color:#000;">'
                f'PO Number: {po_num}</div>'
                f'<div style="font-size:0.9em;margin-bottom:8px;color:#000;">📅 Date Received: {received}'
                + (f'&nbsp;&nbsp;|&nbsp;&nbsp;🏢 {client_name}' if client_name else '')
                + f'</div>'
                f'<div style="color:#5a4000;font-style:italic;">'
                f'⏳ The check-in of the driver is pending.</div>'
                f'</div>',
                unsafe_allow_html=True,
            )
            st.caption("Waiting for driver to check in via kiosk — this tab updates automatically.")

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


# ─── PDF Edit Mode helpers ────────────────────────────────────────────────────

@st.cache_data(show_spinner=False)
def _pdf_page_to_pil(pdf_path: str, page_index: int = 0,
                     scale: float = 2.0, file_mtime: float = 0.0):
    """
    Render one PDF page to a flat RGB PIL Image (white background).

    Results are cached by Streamlit keyed on path + mtime + scale, so repeated
    reruns inside the editor cost nothing.  Pass ``file_mtime`` from
    ``Path(pdf_path).stat().st_mtime`` so the cache auto-invalidates whenever
    the file is overwritten by a save.

    pypdfium2 5.x renders BGR (3 ch); older builds used BGRA (4 ch).  We detect
    the channel count from the buffer size and swap B↔R explicitly via numpy
    rather than trusting ``bitmap.to_pil()`` which gets the order wrong on some
    builds.
    """
    import numpy as np
    from PIL import Image as _PIL
    import pypdfium2 as pdfium  # type: ignore

    doc    = pdfium.PdfDocument(pdf_path)
    page   = doc[page_index]
    bitmap = page.render(scale=scale)
    w, h   = bitmap.width, bitmap.height

    # np.frombuffer() produces a *view* into the memoryview — no data copy.
    # doc.close() frees the underlying C memory, leaving the view dangling
    # (reads as zeros = solid black).  np.array() forces an eager copy so
    # the data survives after the document is closed.
    raw  = np.array(bitmap.buffer, dtype=np.uint8)
    doc.close()

    n_ch = raw.size // (h * w)
    arr  = raw.reshape(h, w, n_ch)

    if n_ch == 4:                              # BGRA → RGBA, flatten on white
        img = _PIL.fromarray(arr[:, :, [2, 1, 0, 3]], "RGBA")
        out = _PIL.new("RGB", img.size, (255, 255, 255))
        out.paste(img, mask=img.split()[3])
        return out
    if n_ch == 3:                              # BGR → RGB, already opaque
        return _PIL.fromarray(arr[:, :, [2, 1, 0]], "RGB")
    return _PIL.fromarray(arr.squeeze()).convert("RGB")  # grayscale / other


def _pil_to_jpeg_b64(img, quality: int = 85) -> str:
    """Convert a PIL Image to a JPEG base64 data-URI (compact for canvas embedding)."""
    from io import BytesIO
    import base64
    buf = BytesIO()
    img.convert("RGB").save(buf, format="JPEG", quality=quality)
    return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()


@st.cache_data(show_spinner=False)
def _bg_b64_for_canvas(pdf_path: str, file_mtime: float, canvas_w: int) -> str:
    """
    Cached JPEG base64 of the first PDF page scaled to canvas_w pixels wide.

    Keyed by path + mtime so it auto-invalidates when the file changes.
    Caching ensures the base64 string is identical across fragment reruns,
    which prevents Fabric.js from re-loading the background on every interaction.
    """
    from PIL import Image as _PIL
    bg_full = _pdf_page_to_pil(pdf_path, 0, 2.0, file_mtime)
    canvas_h = int(bg_full.height * canvas_w / bg_full.width)
    bg_scaled = bg_full.resize((canvas_w, canvas_h), _PIL.LANCZOS)
    return _pil_to_jpeg_b64(bg_scaled, quality=85)


@st.cache_data(show_spinner=False)
def _asset_to_b64(img_path: str, max_w: int = 300) -> str | None:
    """Convert a local image file to a base64 PNG data-URI (cached)."""
    from io import BytesIO
    import base64
    from PIL import Image as _PIL

    if not img_path or not Path(img_path).exists():
        return None
    img = _PIL.open(img_path).convert("RGBA")
    if img.width > max_w:
        img = img.resize((max_w, int(img.height * max_w / img.width)), _PIL.LANCZOS)
    buf = BytesIO()
    img.save(buf, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()


def _burn_overlay_to_pdf(pdf_path: str, annotations_rgba,
                          canvas_w: int, canvas_h: int) -> None:
    """
    Composite canvas annotations onto the original PDF page 0 and overwrite
    the file in-place.  All remaining pages are kept intact.

    ``annotations_rgba`` is the RGBA numpy array from st_canvas with the
    canvas background_image already composited by Fabric.js.  We re-render
    the PDF page from disk at 2× scale, paste the annotations on top, then
    write everything back to a PDF at the original page dimensions.

    On Windows, PdfReader keeps a file handle open; reading into BytesIO first
    lets us write back to the same path without a sharing violation.
    """
    import numpy as np
    from io import BytesIO
    from PIL import Image
    from reportlab.pdfgen import canvas as _rl_canvas
    from reportlab.lib.utils import ImageReader
    from pypdf import PdfWriter, PdfReader

    # Re-render the PDF page from disk so we always have a clean base
    # (protects against the canvas returning an all-black frame on first load).
    file_mtime = Path(pdf_path).stat().st_mtime
    bg_pil = _pdf_page_to_pil(pdf_path, 0, 2.0, file_mtime)

    # Scale the annotation layer to match the full-resolution PDF render
    ann_img = Image.fromarray(annotations_rgba.astype(np.uint8), "RGBA")
    if ann_img.size != bg_pil.size:
        ann_img = ann_img.resize(bg_pil.size, Image.LANCZOS)

    # Paste annotations (RGBA) onto the PDF background using the alpha mask
    base = bg_pil.convert("RGBA")
    base.paste(ann_img, mask=ann_img.split()[3])
    composited = base.convert("RGB")

    pdf_bytes = Path(pdf_path).read_bytes()
    reader    = PdfReader(BytesIO(pdf_bytes))
    orig_w    = float(reader.pages[0].mediabox.width)
    orig_h    = float(reader.pages[0].mediabox.height)

    img_buf = BytesIO()
    composited.save(img_buf, format="PNG")
    img_buf.seek(0)

    rl_buf = BytesIO()
    c = _rl_canvas.Canvas(rl_buf, pagesize=(orig_w, orig_h))
    c.drawImage(ImageReader(img_buf), 0, 0, width=orig_w, height=orig_h)
    c.save()
    rl_buf.seek(0)

    writer = PdfWriter()
    writer.add_page(PdfReader(rl_buf).pages[0])
    for i in range(1, len(reader.pages)):
        writer.add_page(reader.pages[i])

    out = BytesIO()
    writer.write(out)
    Path(pdf_path).write_bytes(out.getvalue())


@st.fragment
def _render_pdf_edit_mode(dm: DataManager, bol: dict) -> None:
    """
    Full-screen interactive PDF annotation editor.

    Left sidebar – tool selector + asset palette (signature, ID photos).
    Right canvas  – PDF page rendered as background; Fabric.js handles
                    drag/resize of placed images and free-text / freehand.
    Bottom bar    – Finalize & Save (burns to PDF) / Cancel.

    State keys per BOL id:
      canvas_obj_{bid}  – list of Fabric.js objects persisted across reruns
      canvas_rev_{bid}  – int revision counter; incrementing forces canvas re-init
    """
    from PIL import Image

    try:
        from streamlit_drawable_canvas import st_canvas  # type: ignore
    except ImportError:
        st.error(
            "**streamlit-drawable-canvas** is not installed.  "
            "Run `pip install streamlit-drawable-canvas` and restart the app."
        )
        if st.button("✗ Close", key=f"close_edit_import_{bol['id']}"):
            st.session_state.pop(f"pdf_edit_mode_{bol['id']}", None)
            st.rerun(scope="app")
        return

    bid      = bol["id"]
    po_num   = bol.get("po_number", "—")
    pdf_path = bol.get("pdf_local_path", "")
    obj_key  = f"canvas_obj_{bid}"
    rev_key  = f"canvas_rev_{bid}"
    init_key = f"canvas_init_{bid}"   # True after first open; prevents re-auto-placing after Clear

    # ── Render PDF page (cached by mtime — free on subsequent reruns) ────
    try:
        file_mtime = Path(pdf_path).stat().st_mtime
        bg_full    = _pdf_page_to_pil(pdf_path, 0, 2.0, file_mtime)
    except Exception as exc:
        st.error(f"Could not render PDF page: {exc}")
        if st.button("✗ Close Editor", key=f"close_edit_err_{bid}"):
            st.session_state.pop(f"pdf_edit_mode_{bid}", None)
            st.rerun(scope="app")
        return

    CANVAS_W = 720
    CANVAS_H = int(bg_full.height * CANVAS_W / bg_full.width)

    # Cached base64 JPEG for the Fabric.js backgroundImage.
    # Using the Fabric.js JSON "backgroundImage" key (not the st_canvas
    # background_image= param) bypasses the React component's broken URL
    # prefix logic that prepends the Streamlit server URL to data URIs.
    # Fabric.js loadFromJSON handles "backgroundImage" natively and waits
    # for the async image load before firing renderAll — no black flash.
    bg_b64 = _bg_b64_for_canvas(pdf_path, file_mtime, CANVAS_W)

    # ── Current canvas state ──────────────────────────────────────────────
    user_objects = st.session_state.get(obj_key, [])
    revision     = st.session_state.get(rev_key, 0)

    # Auto-place signature on FIRST open so the user can drag it immediately.
    # init_key guards against re-placing after the user explicitly clears the canvas.
    if not st.session_state.get(init_key):
        st.session_state[init_key] = True
        if not user_objects:
            _sig_b64 = _asset_to_b64(bol.get("signature_path", ""))
            if _sig_b64:
                user_objects = [{
                    "type":        "image",
                    "src":         _sig_b64,
                    "left":        CANVAS_W // 2 - 45,
                    "top":         int(CANVAS_H * 0.72),   # lower area (typical sig spot)
                    "scaleX":      0.3,
                    "scaleY":      0.3,
                    "selectable":  True,
                    "hasControls": True,
                }]
                st.session_state[obj_key] = user_objects

    st.markdown(f"#### ✏️ PDF Edit Mode — PO {po_num}")
    st.caption(
        "**Drag & Drop** — the signature is pre-placed on the PDF. "
        "Select it and drag it to the correct position.  "
        "Use the sidebar to re-add it or add ID photos.  "
        "**Add Text / Draw** — switch tools in the sidebar.  "
        "Hit **Finalize & Save** when done."
    )

    sidebar_col, canvas_col = st.columns([1, 3], gap="medium")

    # ── Left sidebar ──────────────────────────────────────────────────────
    with sidebar_col:
        st.markdown("**🛠 Tool**")
        draw_mode = st.radio(
            "tool",
            ["transform", "text", "freedraw", "rect"],
            format_func=lambda x: {
                "transform": "↕  Move / Resize",
                "text":      "T   Add Text",
                "freedraw":  "✏  Freehand",
                "rect":      "▬  Rectangle",
            }[x],
            key=f"edit_tool_{bid}",
            label_visibility="collapsed",
        )
        stroke_color = st.color_picker("Ink color",   "#1565c0", key=f"edit_col_{bid}")
        font_size    = st.slider("Font size",   10, 48, 18,   key=f"edit_fs_{bid}")
        stroke_width = st.slider("Stroke width", 1,  8,  2,  key=f"edit_sw_{bid}")

        st.markdown("---")
        st.markdown("**📎 Assets**")

        def _add_asset_btn(img_path: str, label: str, btn_key: str,
                           def_left: int, def_top: int) -> None:
            b64 = _asset_to_b64(img_path)   # cached — free after first load
            if b64 is None:
                st.caption(f"*{label}: not captured*")
                return
            st.image(img_path, caption=label, use_container_width=True)
            if st.button(f"➕ Place {label}", key=btn_key, width='stretch'):
                current = st.session_state.get(obj_key, [])
                current.append({
                    "type":       "image",
                    "src":        b64,
                    "left":       def_left,
                    "top":        def_top,
                    "scaleX":     0.3,
                    "scaleY":     0.3,
                    "selectable": True,
                    "hasControls": True,
                })
                st.session_state[obj_key] = current
                st.session_state[rev_key] = revision + 1
                # Auto-switch to Move/Resize so the user can immediately drag it
                st.session_state[f"edit_tool_{bid}"] = "transform"
                st.rerun()

        _add_asset_btn(bol.get("signature_path", ""), "Signature",  f"add_sig_{bid}",
                       CANVAS_W // 2 - 45, CANVAS_H // 2 - 25)
        _add_asset_btn(bol.get("id_front_path",  ""), "ID — Front", f"add_idf_{bid}",
                       CANVAS_W // 2 - 45, CANVAS_H // 2 + 60)
        _add_asset_btn(bol.get("id_back_path",   ""), "ID — Back",  f"add_idb_{bid}",
                       CANVAS_W // 2 - 45, CANVAS_H // 2 + 145)

        st.markdown("---")
        if st.button("🗑 Clear Canvas", key=f"clear_canvas_{bid}", width='stretch'):
            st.session_state[obj_key] = []
            st.session_state[rev_key] = revision + 1
            st.rerun()

    # ── Canvas ────────────────────────────────────────────────────────────
    with canvas_col:
        # "backgroundImage" in initial_drawing is processed by Fabric.js's
        # loadFromJSON / _setBgOverlay, which waits for the async image load
        # before firing renderAll — the PDF is always visible, never black.
        # This avoids the React component's broken URL prefix logic that
        # corrupts data URIs passed via the background_image= prop.
        canvas_result = st_canvas(
            fill_color="rgba(0,0,0,0)",
            stroke_width=stroke_width,
            stroke_color=stroke_color,
            background_color="#ffffff",
            drawing_mode=draw_mode,
            initial_drawing={
                "version":         "4.4.0",
                "backgroundImage": {
                    "type":        "image",
                    "src":         bg_b64,
                    "crossOrigin": "",
                    "left":        0,
                    "top":         0,
                    "scaleX":      1.0,
                    "scaleY":      1.0,
                    "angle":       0,
                    "opacity":     1,
                    "originX":     "left",
                    "originY":     "top",
                },
                "objects": user_objects,
            },
            height=CANVAS_H,
            width=CANVAS_W,
            key=f"canvas_{bid}_r{revision}",
            update_streamlit=True,
            display_toolbar=False,
        )
        if (canvas_result.json_data
                and isinstance(canvas_result.json_data.get("objects"), list)):
            st.session_state[obj_key] = canvas_result.json_data["objects"]

    # ── Action bar ────────────────────────────────────────────────────────
    st.markdown("---")
    save_col, cancel_col, _ = st.columns([1, 1, 3])

    if save_col.button("✅ Finalize & Save", key=f"finalize_pdf_{bid}",
                       type="primary", width='stretch'):
        if canvas_result.image_data is None:
            st.warning("Make at least one change before saving.")
        else:
            try:
                with st.spinner("Burning annotations into PDF…"):
                    _burn_overlay_to_pdf(
                        pdf_path,
                        canvas_result.image_data,
                        CANVAS_W,
                        CANVAS_H,
                    )
                dm.update_bol_record(bid, {"annotations_saved_at": _now_str()})
                for k in (f"pdf_edit_mode_{bid}", obj_key, rev_key, init_key):
                    st.session_state.pop(k, None)
                st.success("✅ PDF updated with annotations.")
                st.rerun(scope="app")
            except Exception as exc:
                st.error(f"Save failed: {exc}")

    if cancel_col.button("✗ Cancel", key=f"cancel_pdf_edit_{bid}", width='stretch'):
        for k in (f"pdf_edit_mode_{bid}", obj_key, rev_key, init_key):
            st.session_state.pop(k, None)
        st.rerun(scope="app")


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

    # Determine if any BOL is currently open in PDF edit mode.
    # Only one can be active at a time for performance.
    active_pdf_edit_id = next(
        (r["id"] for r in inspection_bols if st.session_state.get(f"pdf_edit_mode_{r['id']}")),
        None,
    )

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

            elif st.session_state.get(f"pdf_edit_mode_{bid}"):
                _render_pdf_edit_mode(dm, bol)

            else:
                # ── Action buttons row ───────────────────────────────────────
                b1, b2, b3, b4 = st.columns(4)

                # Detect corrupted/empty PDF (< 5 KB)
                _insp_pdf_corrupt = False
                if pdf_exists:
                    try:
                        _insp_pdf_corrupt = Path(pdf_path).stat().st_size < 5_000
                    except OSError:
                        _insp_pdf_corrupt = True

                pdf_label = "📄 Hide PDF" if st.session_state.get(pdf_key) else "📄 View PDF"
                if pdf_exists and not _insp_pdf_corrupt:
                    if b1.button(pdf_label, key=f"insp_pdf_{bid}", width='stretch'):
                        st.session_state[pdf_key] = not st.session_state.get(pdf_key, False)
                        st.rerun()
                else:
                    b1.button("📄 View PDF", key=f"insp_pdf_na_{bid}", disabled=True, width='stretch')

                form_label = "📝 Hide Form" if st.session_state.get(form_key) else "📝 Complete Form"
                if b2.button(form_label, key=f"insp_form_btn_{bid}", width='stretch'):
                    st.session_state[form_key] = not st.session_state.get(form_key, False)
                    st.rerun()

                another_open = active_pdf_edit_id is not None and active_pdf_edit_id != bid
                if b3.button("🖊 Edit PDF", key=f"insp_pdfedit_{bid}", width='stretch',
                             disabled=not pdf_exists or _insp_pdf_corrupt or another_open):
                    st.session_state[f"pdf_edit_mode_{bid}"] = True
                    st.rerun()

                if b4.button("✏️ Edit", key=f"insp_ebtn_{bid}", width='stretch'):
                    st.session_state[edit_key] = True
                    st.rerun()

                if _insp_pdf_corrupt:
                    st.warning("⚠️ PDF appears corrupted or missing. Upload a replacement below.")
                if not pdf_exists or _insp_pdf_corrupt:
                    _insp_up = st.file_uploader(
                        "Upload replacement BOL PDF", type="pdf",
                        key=f"insp_pdf_upload_{bid}",
                        label_visibility="collapsed",
                    )
                    if _insp_up is not None:
                        from config import BOLS_DIR
                        _dest = BOLS_DIR / (Path(pdf_path).name if pdf_exists else f"BOL_{po_num}_{bid[:8]}.pdf")
                        _dest.write_bytes(_insp_up.read())
                        dm.update_bol_record(bid, {"pdf_local_path": str(_dest)})
                        st.success(f"PDF saved: {_dest.name}")
                        st.rerun()

                # ── PDF viewer ───────────────────────────────────────────────
                if st.session_state.get(pdf_key) and pdf_exists and not _insp_pdf_corrupt:
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

                    prev = bol.get("admin_annotations") or {}

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

    # ── Global PDF-edit warning ────────────────────────────────────────────────
    # If any BOL PDF editor is open, show a sticky banner so the user knows to
    # finish or cancel before navigating away.  Also pause auto-refresh fragments
    # to prevent unexpected reruns from disrupting the canvas state.
    _editing_bols = [
        r for r in bol_records
        if st.session_state.get(f"pdf_edit_mode_{r['id']}")
    ]
    _any_editing = bool(_editing_bols)

    if _any_editing:
        _edit_pos = ", ".join(r.get("po_number", "?") for r in _editing_bols)
        st.warning(
            f"⚠️ **PDF edit in progress — PO(s): {_edit_pos}** — "
            f"Open the **BOL Inspection** tab to finish or cancel your edits "
            f"before switching pages.",
        )

    # Auto-refresh fragments are paused while editing to keep the canvas stable.
    if not _any_editing:
        _bol_status_watcher(dm)
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
