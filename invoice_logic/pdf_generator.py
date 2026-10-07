"""
pdf_generator.py
================
Generates client invoices as PDFs matching the INCO GROUP invoice format.
Returns PDF as bytes so it can be streamed directly to a Streamlit download button
or written to disk.
"""

import logging
from datetime import datetime, timedelta
from io import BytesIO
from pathlib import Path

from config import PHOTOS_DIR

logger = logging.getLogger(__name__)
LOGO_PATH = str(PHOTOS_DIR / "logo_smaller_2.jpg")

from PIL import Image, ImageDraw
from pypdf import PdfWriter, PdfReader
from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.lib.units import inch
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas

# ── Brand colours ─────────────────────────────────────────────────────────────
BLUE       = colors.HexColor("#1F5096")
DARK       = colors.HexColor("#1A1A1A")
LIGHT_GRAY = colors.HexColor("#F2F2F2")
MID_GRAY   = colors.HexColor("#D0D0D0")
LABEL_GRAY = colors.HexColor("#888888")

# ── Company contact info (single source of truth for FROM box and footer) ──────
_CO_NAME  = "INCO GROUP, INC."
_CO_ADDR1 = "9005 Travis Dr Ste 1"
_CO_ADDR2 = "Pharr, TX 78577"
_CO_PHONE = "(956) 702-8851"
_CO_EMAIL = "admin@incogrp.com"

# Logo existence checked once at import — avoids a stat() call per PDF.
_LOGO_EXISTS = Path(LOGO_PATH).exists()

# In-process logo cache: keyed by (path, radius_px, mtime) so it auto-invalidates if the file changes.
_img_cache: dict = {}


_LOGO_MAX_PX = 440  # max width in pixels (2.2 in × 200 DPI — good PDF quality)


def _rounded_image_reader(path: str, radius_px: int = 18) -> ImageReader:
    """Return an ImageReader of the logo with rounded corners (RGBA PNG in memory).
    Downscaled to _LOGO_MAX_PX wide before encoding to keep PDF size small.
    Cached by path + radius + mtime so the image is only processed once per file version."""
    _key = (path, radius_px, Path(path).stat().st_mtime)
    if _key not in _img_cache:
        img = Image.open(path).convert("RGBA")
        # Downscale to a PDF-appropriate resolution before embedding
        if img.width > _LOGO_MAX_PX:
            scale = _LOGO_MAX_PX / img.width
            img = img.resize(
                (int(img.width * scale), int(img.height * scale)),
                Image.LANCZOS,
            )
        mask = Image.new("L", img.size, 0)
        draw = ImageDraw.Draw(mask)
        draw.rounded_rectangle([(0, 0), (img.width - 1, img.height - 1)],
                                radius=radius_px, fill=255)
        img.putalpha(mask)
        buf = BytesIO()
        img.save(buf, format="PNG", optimize=True)
        buf.seek(0)
        _img_cache[_key] = buf.read()
    return ImageReader(BytesIO(_img_cache[_key]))


def _fmt_date(iso_date: str) -> str:
    try:
        return datetime.fromisoformat(iso_date).strftime("%m/%d/%Y")
    except Exception:
        return iso_date


def _photo_page_bytes(photos: list[bytes]) -> bytes | None:
    """
    Render worker photos onto PDF pages in a 2×2 grid (up to 4 per page).
    Title + rule sit inside the top margin. Returns PDF bytes, or None if all photos fail.
    """
    if not photos:
        return None

    buf  = BytesIO()
    W, H = letter
    c    = canvas.Canvas(buf, pagesize=letter)

    margin     = 0.50 * inch
    title_h    = 0.45 * inch   # vertical space reserved for heading + rule
    per_page   = 4
    cols, rows = 2, 2
    pad        = 0.12 * inch
    cell_w     = (W - 2 * margin) / cols
    cell_h     = (H - 2 * margin - title_h) / rows

    any_drawn = False

    for idx, photo_bytes in enumerate(photos):
        if idx % per_page == 0:
            if idx > 0:
                c.showPage()
            # Heading — drawn just below the top margin
            title_y = H - margin - 0.22 * inch
            c.setFillColor(BLUE)
            c.setFont("Helvetica-Bold", 11)
            c.drawString(margin, title_y, "Worker Photos")
            rule_y = title_y - 0.14 * inch
            c.setStrokeColor(BLUE)
            c.setLineWidth(1)
            c.line(margin, rule_y, W - margin, rule_y)

        pos  = idx % per_page
        col  = pos % cols
        row  = pos // cols
        cx   = margin + col * cell_w
        # Grid starts below the title area
        cy   = H - margin - title_h - (row + 1) * cell_h

        try:
            img    = Image.open(BytesIO(photo_bytes))
            # Cap photo resolution to reduce embedded size
            img.thumbnail((1200, 1200), Image.LANCZOS)
            img_w, img_h = img.size
            scale  = min((cell_w - 2 * pad) / img_w, (cell_h - 2 * pad) / img_h)
            draw_w = img_w * scale
            draw_h = img_h * scale
            draw_x = cx + pad + ((cell_w - 2 * pad) - draw_w) / 2
            draw_y = cy + pad + ((cell_h - 2 * pad) - draw_h) / 2

            if img.mode not in ("RGB", "L"):
                img = img.convert("RGB")
            rgb_buf = BytesIO()
            img.save(rgb_buf, format="JPEG", quality=72, optimize=True)
            rgb_buf.seek(0)
            c.drawImage(ImageReader(rgb_buf), draw_x, draw_y, width=draw_w, height=draw_h)
            any_drawn = True
        except Exception as e:
            logger.warning("_photo_page_bytes: skipping photo %d: %s", idx, e)

    if not any_drawn:
        return None

    c.save()
    buf.seek(0)
    return buf.read()


def generate_pdf(invoice: dict, provider_pdf_path: str | None = None) -> bytes:
    """
    Build a PDF invoice that matches the INCO GROUP template.
    If there are more than 10 line items the invoice automatically continues
    onto additional pages (up to 12 items per continuation page) so no item
    is ever clipped or omitted.
    If provider_pdf_path is supplied and the file exists, the original
    provider invoice pages are appended after the generated invoice page(s).

    Parameters
    ----------
    invoice : dict
        A client-invoice record from DataManager, expected keys:
        quickbooks_invoice_number, client_name, invoice_date,
        line_items, subtotal, total, po_number (optional).
    provider_pdf_path : str | None
        Local path to the original provider invoice PDF to append.

    Returns
    -------
    bytes  — raw PDF content ready for st.download_button or file write.
    """
    # Maximum rows before spilling onto a new page.
    # Page 1 has a taller header (info box) so fewer rows fit.
    _ROWS_PAGE1 = 10
    _ROWS_CONT  = 12

    buffer = BytesIO()
    W, H   = letter          # 8.5 × 11 in
    c      = canvas.Canvas(buffer, pagesize=letter)
    margin     = 0.60 * inch
    content_w  = W - 2 * margin
    r          = 10  # corner radius (points) used throughout

    # ── Shared table geometry ─────────────────────────────────────────────────
    hdr_h = 0.28 * inch
    row_h = 0.55 * inch

    # Column widths: QTY | DESCRIPTION | UNIT | RATE | AMOUNT
    col_w = [
        content_w * 0.10,
        content_w * 0.42,
        content_w * 0.12,
        content_w * 0.18,
        content_w * 0.18,
    ]
    col_x = [margin]
    for _w in col_w[:-1]:
        col_x.append(col_x[-1] + _w)

    def _draw_table_header(tbl_top_y: float) -> None:
        """Draw the dark QTY / DESCRIPTION / … header bar."""
        c.setFillColor(DARK)
        c.rect(margin, tbl_top_y - hdr_h, content_w, hdr_h, fill=1, stroke=0)
        c.setFillColor(colors.white)
        c.setFont("Helvetica-Bold", 9)
        hdrs = ["QTY", "DESCRIPTION", "UNIT", "RATE", "AMOUNT"]
        for i, (hdr, x_, w_) in enumerate(zip(hdrs, col_x, col_w)):
            y_ = tbl_top_y - hdr_h + 0.08 * inch
            if i == 0:
                c.drawCentredString(x_ + w_ / 2, y_, hdr)
            elif i == len(hdrs) - 1:
                c.drawRightString(x_ + w_ - 0.12 * inch, y_, hdr)
            else:
                c.drawString(x_ + 0.10 * inch, y_, hdr)

    def _draw_item_rows(items: list, row_top_y: float,
                        num_rows: int, colour_offset: int = 0) -> float:
        """
        Draw *num_rows* data rows starting at *row_top_y*.
        *items* holds the actual line-item dicts for this batch (may be shorter
        than num_rows — remaining rows will be blank).
        *colour_offset* keeps the alternating stripe colour consistent across pages.
        Returns the new row_top_y after all rows have been drawn.
        """
        for i in range(num_rows):
            bg = LIGHT_GRAY if (colour_offset + i) % 2 == 0 else colors.white
            c.setFillColor(bg)
            c.rect(margin, row_top_y - row_h, content_w, row_h, fill=1, stroke=0)

            if i < len(items):
                item   = items[i]
                qty    = item.get("quantity", "")
                desc   = item.get("description", "")
                unit   = item.get("unit", "ea")
                rate   = float(item.get("unit_price", 0))
                amt    = float(item.get("total", 0))
                text_y = row_top_y - 0.35 * inch

                c.setFillColor(DARK)
                c.setFont("Helvetica", 9)
                c.drawCentredString(col_x[0] + col_w[0] / 2,           text_y, str(qty))
                c.drawString       (col_x[1] + 0.10 * inch,             text_y, desc)
                c.drawString       (col_x[2] + 0.10 * inch,             text_y, unit)
                c.drawRightString  (col_x[3] + col_w[3] - 0.12 * inch,  text_y, f"${rate:,.2f}")
                c.drawRightString  (col_x[4] + col_w[4] - 0.12 * inch,  text_y, f"${amt:,.2f}")

            c.setStrokeColor(MID_GRAY)
            c.setLineWidth(0.3)
            c.rect(margin, row_top_y - row_h, content_w, row_h, fill=0, stroke=1)
            row_top_y -= row_h

        return row_top_y

    def _draw_logo_and_invoice_box(top_y: float, subtitle: str = "SERVICE") -> None:
        """Draw logo (top-left) and blue invoice box (top-right) at the given top_y."""
        logo_y = top_y - 0.90 * inch
        if _LOGO_EXISTS:
            img_reader = _rounded_image_reader(LOGO_PATH, radius_px=18)
            c.drawImage(img_reader, margin, logo_y, width=2.20 * inch, height=0.90 * inch,
                        preserveAspectRatio=True, mask="auto")
        else:
            c.setFillColor(BLUE)
            c.roundRect(margin, logo_y, 2.20 * inch, 0.90 * inch, r, fill=1, stroke=0)
            c.setFillColor(colors.white)
            c.setFont("Helvetica-Bold", 26)
            c.drawString(margin + 0.14 * inch, logo_y + 0.50 * inch, "INCO")
            c.setFont("Helvetica-Bold", 9)
            c.drawString(margin + 0.14 * inch, logo_y + 0.22 * inch, "COLD STORAGE")

        inv_w = 2.30 * inch
        inv_x = W - margin - inv_w
        inv_y = logo_y
        inv_right = inv_x + inv_w - 0.15 * inch
        qb_num = invoice.get("quickbooks_invoice_number", "")

        c.setFillColor(BLUE)
        c.roundRect(inv_x, inv_y, inv_w, 0.90 * inch, r, fill=1, stroke=0)
        c.setFillColor(colors.white)
        c.setFont("Helvetica-Bold", 11)
        c.drawString(inv_x + 0.15 * inch, inv_y + 0.63 * inch, "INVOICE")
        c.drawRightString(inv_right, inv_y + 0.63 * inch, f"#{qb_num}")
        c.setStrokeColor(colors.white)
        c.setLineWidth(0.5)
        c.line(inv_x + 0.10 * inch, inv_y + 0.47 * inch,
               inv_x + inv_w - 0.10 * inch, inv_y + 0.47 * inch)
        c.setFont("Helvetica-Bold", 9)
        prov_inv_num = invoice.get("provider_invoice_number", "")
        c.drawString(inv_x + 0.15 * inch, inv_y + 0.22 * inch, subtitle)
        c.drawRightString(inv_right, inv_y + 0.22 * inch, prov_inv_num)

    def _draw_totals_and_footer(row_top_y: float) -> None:
        """Draw the TOTAL DUE block, payment methods banner, and footer."""
        tot_x = margin + content_w * 0.55
        tot_w = content_w * 0.45
        tot_h = 0.35 * inch
        total = float(invoice.get("total", 0))

        # TOTAL DUE row
        c.setFillColor(BLUE)
        c.rect(tot_x, row_top_y - tot_h, tot_w, tot_h, fill=1, stroke=0)
        c.setStrokeColor(MID_GRAY)
        c.setLineWidth(0.3)
        c.rect(tot_x, row_top_y - tot_h, tot_w, tot_h, fill=0, stroke=1)
        txt_y = row_top_y - tot_h + 0.12 * inch
        c.setFillColor(colors.white)
        c.setFont("Helvetica-Bold", 10)
        c.drawString(tot_x + 0.15 * inch, txt_y, "TOTAL DUE")
        c.drawRightString(tot_x + tot_w - 0.15 * inch, txt_y, f"${total:,.2f}")
        t_y = row_top_y - tot_h

        # Payment methods
        pay_top = t_y - 0.20 * inch
        pay_h   = 0.35 * inch
        lbl_w   = 1.45 * inch
        c.setFillColor(BLUE)
        c.rect(margin, pay_top - pay_h, lbl_w, pay_h, fill=1, stroke=0)
        c.setFillColor(colors.white)
        c.setFont("Helvetica-Bold", 8)
        c.drawString(margin + 0.10 * inch, pay_top - pay_h + 0.12 * inch, "PAYMENT METHODS")
        c.setFillColor(LIGHT_GRAY)
        c.rect(margin + lbl_w, pay_top - pay_h, content_w - lbl_w, pay_h, fill=1, stroke=0)
        c.setStrokeColor(MID_GRAY)
        c.setLineWidth(0.5)
        c.rect(margin, pay_top - pay_h, content_w, pay_h, fill=0, stroke=1)
        c.setFillColor(DARK)
        c.setFont("Helvetica", 9)
        c.drawString(margin + lbl_w + 0.20 * inch, pay_top - pay_h + 0.12 * inch,
                     "Check  |  ACH Transfer  |  Wire Transfer")

        # Footer rule + text
        foot_y = pay_top - pay_h - 0.20 * inch
        c.setStrokeColor(BLUE)
        c.setLineWidth(1.5)
        c.line(margin, foot_y, W - margin, foot_y)
        c.setFillColor(BLUE)
        c.setFont("Helvetica-BoldOblique", 10)
        c.drawCentredString(W / 2, foot_y - 0.25 * inch,
                            "Thank you for your continued business.")
        c.setFillColor(DARK)
        c.setFont("Helvetica", 7.5)
        c.drawCentredString(
            W / 2,
            foot_y - 0.45 * inch,
            f"{_CO_NAME}  \u2022  {_CO_ADDR1}, {_CO_ADDR2}"
            f"  \u2022  {_CO_PHONE}  \u2022  {_CO_EMAIL}",
        )

    # ═════════════════════════════════════════════════════════════════════════
    # PAGE 1 — full header + info box
    # ═════════════════════════════════════════════════════════════════════════
    _draw_logo_and_invoice_box(H - margin, subtitle="SERVICE")

    rule_y = (H - margin - 0.90 * inch) - 0.14 * inch
    c.setStrokeColor(BLUE)
    c.setLineWidth(2)
    c.line(margin, rule_y, W - margin, rule_y)

    # Info box (FROM / BILL TO / dates / terms)
    box_h      = 1.05 * inch
    box_top    = rule_y - 0.12 * inch
    box_bottom = box_top - box_h

    col_widths = [
        content_w * 0.22,
        content_w * 0.35,
        content_w * 0.22,
        content_w * 0.21,
    ]
    info_col_x = [margin]
    for _cw in col_widths[:-1]:
        info_col_x.append(info_col_x[-1] + _cw)

    c.setFillColor(LIGHT_GRAY)
    c.roundRect(margin, box_bottom, content_w, box_h, r, fill=1, stroke=0)
    c.setStrokeColor(colors.white)
    c.setLineWidth(1.5)
    for i in range(1, 4):
        c.line(info_col_x[i], box_bottom, info_col_x[i], box_top)
    c.setStrokeColor(MID_GRAY)
    c.setLineWidth(0.5)
    c.roundRect(margin, box_bottom, content_w, box_h, r, fill=0, stroke=1)

    pad = 0.10 * inch

    def _label(x, y, text):
        c.setFillColor(LABEL_GRAY)
        c.setFont("Helvetica", 7)
        c.drawString(x, y, text)

    def _value(x, y, text, bold=False):
        c.setFillColor(DARK)
        c.setFont("Helvetica-Bold" if bold else "Helvetica", 9)
        c.drawString(x, y, text)

    _label(info_col_x[0] + pad, box_top - 0.18 * inch, "FROM")
    _value(info_col_x[0] + pad, box_top - 0.36 * inch, _CO_NAME,  bold=True)
    _value(info_col_x[0] + pad, box_top - 0.51 * inch, _CO_ADDR1)
    _value(info_col_x[0] + pad, box_top - 0.64 * inch, _CO_ADDR2)
    _value(info_col_x[0] + pad, box_top - 0.77 * inch, _CO_PHONE)

    _label(info_col_x[1] + pad, box_top - 0.18 * inch, "BILL TO")
    _value(info_col_x[1] + pad, box_top - 0.36 * inch, invoice.get("client_name", ""), bold=True)
    billing_address = invoice.get("billing_address", "")
    _addr_lines = billing_address.splitlines()[:3] if billing_address else []
    for _i, _line in enumerate(_addr_lines):
        _value(info_col_x[1] + pad, box_top - (0.51 + _i * 0.13) * inch, _line.strip())
    client_rfc = invoice.get("client_rfc", "").strip()
    if client_rfc:
        _rfc_y = box_top - (0.51 + len(_addr_lines) * 0.13) * inch
        _label(info_col_x[1] + pad,               _rfc_y + 0.01 * inch, "RFC")
        _value(info_col_x[1] + pad + 0.22 * inch,  _rfc_y + 0.01 * inch, client_rfc)

    inv_date_str = invoice.get("invoice_date", datetime.utcnow().date().isoformat())
    net_days     = int(invoice.get("net_days", 30))
    try:
        inv_dt  = datetime.fromisoformat(inv_date_str)
        inv_fmt = inv_dt.strftime("%m/%d/%Y")
    except Exception:
        inv_fmt = inv_date_str

    explicit_due = invoice.get("due_date", "").strip()
    if explicit_due:
        try:
            due_fmt = datetime.fromisoformat(explicit_due).strftime("%m/%d/%Y")
        except Exception:
            due_fmt = explicit_due
    else:
        try:
            due_fmt = (inv_dt + timedelta(days=net_days)).strftime("%m/%d/%Y")
        except Exception:
            due_fmt = ""

    _label(info_col_x[2] + pad, box_top - 0.18 * inch, "INVOICE DATE")
    _value(info_col_x[2] + pad, box_top - 0.36 * inch, inv_fmt)
    _label(info_col_x[2] + pad, box_top - 0.57 * inch, "DUE DATE")
    _value(info_col_x[2] + pad, box_top - 0.75 * inch, due_fmt)

    _label(info_col_x[3] + pad, box_top - 0.18 * inch, "TERMS")
    _value(info_col_x[3] + pad, box_top - 0.36 * inch, f"Net {net_days}")
    _label(info_col_x[3] + pad, box_top - 0.57 * inch, "P.O. NUMBER")
    _value(info_col_x[3] + pad, box_top - 0.75 * inch, invoice.get("po_number", ""))

    # Table
    tbl_top  = box_bottom - 0.18 * inch
    _draw_table_header(tbl_top)
    row_top  = tbl_top - hdr_h

    line_items   = invoice.get("line_items", [])
    page1_batch  = line_items[:_ROWS_PAGE1]
    num_rows_p1  = max(len(page1_batch), 5)   # always at least 5 blank rows on page 1
    colour_off   = 0
    row_top      = _draw_item_rows(page1_batch, row_top, num_rows_p1, colour_off)
    colour_off  += num_rows_p1
    remaining    = line_items[_ROWS_PAGE1:]

    # ═════════════════════════════════════════════════════════════════════════
    # CONTINUATION PAGES (only rendered when items > _ROWS_PAGE1)
    # ═════════════════════════════════════════════════════════════════════════
    while remaining:
        c.showPage()
        cont_batch = remaining[:_ROWS_CONT]
        remaining  = remaining[_ROWS_CONT:]

        # Compact header: logo + invoice box + rule + "continued" caption
        _draw_logo_and_invoice_box(H - margin, subtitle="CONTINUED")
        cont_rule_y = (H - margin - 0.90 * inch) - 0.14 * inch
        c.setStrokeColor(BLUE)
        c.setLineWidth(2)
        c.line(margin, cont_rule_y, W - margin, cont_rule_y)
        c.setFillColor(LABEL_GRAY)
        c.setFont("Helvetica-Oblique", 8)
        c.drawString(margin, cont_rule_y - 0.18 * inch, "Invoice continued from previous page")

        cont_tbl_top = cont_rule_y - 0.32 * inch
        _draw_table_header(cont_tbl_top)
        row_top  = cont_tbl_top - hdr_h
        num_cont = max(len(cont_batch), 1)
        row_top  = _draw_item_rows(cont_batch, row_top, num_cont, colour_off)
        colour_off += num_cont

    # ═════════════════════════════════════════════════════════════════════════
    # TOTALS + FOOTER (always on the last page)
    # ═════════════════════════════════════════════════════════════════════════
    _draw_totals_and_footer(row_top)

    c.save()
    buffer.seek(0)
    invoice_bytes = buffer.read()

    if not provider_pdf_path:
        return invoice_bytes

    writer = PdfWriter()
    for page in PdfReader(BytesIO(invoice_bytes)).pages:
        writer.add_page(page)

    try:
        for page in PdfReader(str(provider_pdf_path)).pages:
            writer.add_page(page)
    except Exception as e:
        logger.warning("generate_pdf: could not append provider PDF '%s': %s", provider_pdf_path, e)

    writer.compress_identical_objects(remove_identicals=True, remove_orphans=True)
    out = BytesIO()
    writer.write(out)
    out.seek(0)
    return out.read()


def append_photos_to_pdf(pdf_bytes: bytes, photo_bytes_list: list[bytes]) -> bytes:
    """
    Append worker photo pages to any existing PDF (given as bytes).
    Returns the combined PDF, or the original bytes unchanged if photos cannot be processed.
    """
    if not photo_bytes_list:
        return pdf_bytes
    photo_pdf = _photo_page_bytes(photo_bytes_list)
    if not photo_pdf:
        return pdf_bytes
    writer = PdfWriter()
    for page in PdfReader(BytesIO(pdf_bytes)).pages:
        writer.add_page(page)
    for page in PdfReader(BytesIO(photo_pdf)).pages:
        writer.add_page(page)
    writer.compress_identical_objects(remove_identicals=True, remove_orphans=True)
    out = BytesIO()
    writer.write(out)
    out.seek(0)
    return out.read()


def merge_with_provider_pdf(invoice_bytes: bytes, provider_pdf_path: str) -> bytes:
    """
    Append the original provider PDF pages after the generated invoice pages.
    Returns the combined PDF as bytes. Falls back to invoice_bytes alone if
    the provider PDF cannot be read.
    """
    writer = PdfWriter()

    # Page(s) from the generated invoice
    for page in PdfReader(BytesIO(invoice_bytes)).pages:
        writer.add_page(page)

    # Page(s) from the original provider PDF
    provider_path = Path(provider_pdf_path)
    if not provider_path.exists():
        raise FileNotFoundError(f"Provider PDF not found: {provider_pdf_path}")
    for page in PdfReader(str(provider_path)).pages:
        writer.add_page(page)

    writer.compress_identical_objects(remove_identicals=True, remove_orphans=True)
    out = BytesIO()
    writer.write(out)
    out.seek(0)
    return out.read()
