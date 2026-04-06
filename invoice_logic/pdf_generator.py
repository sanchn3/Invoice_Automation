"""
pdf_generator.py
================
Generates client invoices as PDFs matching the INCO GROUP invoice format.
Returns PDF as bytes so it can be streamed directly to a Streamlit download button
or written to disk.
"""

from datetime import datetime, timedelta
from io import BytesIO

from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.lib.units import inch
from reportlab.pdfgen import canvas

# ── Brand colours ─────────────────────────────────────────────────────────────
BLUE       = colors.HexColor("#1F5096")
DARK       = colors.HexColor("#1A1A1A")
LIGHT_GRAY = colors.HexColor("#F2F2F2")
MID_GRAY   = colors.HexColor("#D0D0D0")
LABEL_GRAY = colors.HexColor("#888888")


def _fmt_date(iso_date: str) -> str:
    try:
        return datetime.fromisoformat(iso_date).strftime("%m/%d/%Y")
    except Exception:
        return iso_date


def generate_pdf(invoice: dict) -> bytes:
    """
    Build a PDF invoice that matches the INCO GROUP template.

    Parameters
    ----------
    invoice : dict
        A client-invoice record from DataManager, expected keys:
        quickbooks_invoice_number, client_name, invoice_date,
        line_items, subtotal, total, po_number (optional).

    Returns
    -------
    bytes  — raw PDF content ready for st.download_button or file write.
    """
    buffer = BytesIO()
    W, H   = letter          # 8.5 × 11 in
    c      = canvas.Canvas(buffer, pagesize=letter)
    margin     = 0.60 * inch
    content_w  = W - 2 * margin

    # ── LOGO BOX (top-left) ───────────────────────────────────────────────────
    logo_x = margin
    logo_y = H - margin - 0.90 * inch
    logo_w = 2.20 * inch
    logo_h = 0.90 * inch

    c.setFillColor(DARK)
    c.rect(logo_x, logo_y, logo_w, logo_h, fill=1, stroke=0)

    c.setFillColor(colors.white)
    c.setFont("Helvetica-Bold", 22)
    c.drawString(logo_x + 0.12 * inch, logo_y + 0.52 * inch, "INCO")
    c.setFont("Helvetica", 9)
    c.drawString(logo_x + 0.12 * inch, logo_y + 0.28 * inch, "COLD STORAGE")

    # ── INVOICE BOX (top-right) ───────────────────────────────────────────────
    inv_w = 2.30 * inch
    inv_h = 0.90 * inch
    inv_x = W - margin - inv_w
    inv_y = logo_y

    c.setFillColor(BLUE)
    c.rect(inv_x, inv_y, inv_w, inv_h, fill=1, stroke=0)

    c.setFillColor(colors.white)
    c.setFont("Helvetica-Bold", 16)
    c.drawString(inv_x + 0.15 * inch, inv_y + 0.57 * inch, "INVOICE")

    qb_num = invoice.get("quickbooks_invoice_number", "")
    c.setFont("Helvetica-Bold", 13)
    c.drawString(inv_x + 0.15 * inch, inv_y + 0.22 * inch, f"#{qb_num}")

    # ── BLUE RULE ─────────────────────────────────────────────────────────────
    rule_y = logo_y - 0.14 * inch
    c.setStrokeColor(BLUE)
    c.setLineWidth(2)
    c.line(margin, rule_y, W - margin, rule_y)

    # ── INFO ROW: FROM | BILL TO | INVOICE DATE / DUE DATE | TERMS / P.O. ────
    box_h      = 1.05 * inch
    box_top    = rule_y - 0.12 * inch
    box_bottom = box_top - box_h

    col_widths = [
        content_w * 0.22,   # FROM
        content_w * 0.35,   # BILL TO
        content_w * 0.22,   # INVOICE DATE / DUE DATE
        content_w * 0.21,   # TERMS / P.O. NUMBER
    ]
    col_x = [margin]
    for cw in col_widths[:-1]:
        col_x.append(col_x[-1] + cw)

    # Background
    c.setFillColor(LIGHT_GRAY)
    c.rect(margin, box_bottom, content_w, box_h, fill=1, stroke=0)

    # White vertical dividers
    c.setStrokeColor(colors.white)
    c.setLineWidth(1.5)
    for i in range(1, 4):
        c.line(col_x[i], box_bottom, col_x[i], box_top)

    # Outer border
    c.setStrokeColor(MID_GRAY)
    c.setLineWidth(0.5)
    c.rect(margin, box_bottom, content_w, box_h, fill=0, stroke=1)

    def _label(x, y, text):
        c.setFillColor(LABEL_GRAY)
        c.setFont("Helvetica", 7)
        c.drawString(x, y, text)

    def _value(x, y, text, bold=False):
        c.setFillColor(DARK)
        c.setFont("Helvetica-Bold" if bold else "Helvetica", 9)
        c.drawString(x, y, text)

    pad = 0.10 * inch

    # FROM
    _label(col_x[0] + pad, box_top - 0.18 * inch, "FROM")
    _value(col_x[0] + pad, box_top - 0.36 * inch, "INCO GROUP, INC.", bold=True)
    _value(col_x[0] + pad, box_top - 0.51 * inch, "9005 Travis Dr Ste 1")
    _value(col_x[0] + pad, box_top - 0.64 * inch, "Pharr, TX 78577")
    _value(col_x[0] + pad, box_top - 0.77 * inch, "(956) 702-8851")

    # BILL TO
    _label(col_x[1] + pad, box_top - 0.18 * inch, "BILL TO")
    _value(col_x[1] + pad, box_top - 0.36 * inch, invoice.get("client_name", ""))

    # INVOICE DATE / DUE DATE
    inv_date_str = invoice.get("invoice_date", datetime.utcnow().date().isoformat())
    try:
        inv_dt  = datetime.fromisoformat(inv_date_str)
        due_dt  = inv_dt + timedelta(days=30)
        inv_fmt = inv_dt.strftime("%m/%d/%Y")
        due_fmt = due_dt.strftime("%m/%d/%Y")
    except Exception:
        inv_fmt = inv_date_str
        due_fmt = ""

    _label(col_x[2] + pad, box_top - 0.18 * inch, "INVOICE DATE")
    _value(col_x[2] + pad, box_top - 0.36 * inch, inv_fmt)
    _label(col_x[2] + pad, box_top - 0.57 * inch, "DUE DATE")
    _value(col_x[2] + pad, box_top - 0.75 * inch, due_fmt)

    # TERMS / P.O. NUMBER
    _label(col_x[3] + pad, box_top - 0.18 * inch, "TERMS")
    _value(col_x[3] + pad, box_top - 0.36 * inch, "Net 30")
    _label(col_x[3] + pad, box_top - 0.57 * inch, "P.O. NUMBER")
    _value(col_x[3] + pad, box_top - 0.75 * inch, invoice.get("po_number", ""))

    # ── LINE-ITEMS TABLE ──────────────────────────────────────────────────────
    tbl_top    = box_bottom - 0.18 * inch
    hdr_h      = 0.28 * inch
    row_h      = 0.55 * inch

    # Column widths: QTY | DESCRIPTION | UNIT | RATE | AMOUNT
    cw = [
        content_w * 0.10,
        content_w * 0.42,
        content_w * 0.12,
        content_w * 0.18,
        content_w * 0.18,
    ]
    cx = [margin]
    for w_ in cw[:-1]:
        cx.append(cx[-1] + w_)

    # Header row
    c.setFillColor(DARK)
    c.rect(margin, tbl_top - hdr_h, content_w, hdr_h, fill=1, stroke=0)
    c.setFillColor(colors.white)
    c.setFont("Helvetica-Bold", 9)
    hdrs = ["QTY", "DESCRIPTION", "UNIT", "RATE", "AMOUNT"]
    for i, (hdr, x_, w_) in enumerate(zip(hdrs, cx, cw)):
        y_ = tbl_top - hdr_h + 0.08 * inch
        if i == 0:
            c.drawCentredString(x_ + w_ / 2, y_, hdr)
        elif i == len(hdrs) - 1:
            c.drawRightString(x_ + w_ - 0.12 * inch, y_, hdr)
        else:
            c.drawString(x_ + 0.10 * inch, y_, hdr)

    # Data rows
    line_items = invoice.get("line_items", [])
    num_rows   = max(len(line_items), 5)
    row_top    = tbl_top - hdr_h

    for i in range(num_rows):
        bg = LIGHT_GRAY if i % 2 == 0 else colors.white
        c.setFillColor(bg)
        c.rect(margin, row_top - row_h, content_w, row_h, fill=1, stroke=0)

        if i < len(line_items):
            item  = line_items[i]
            qty   = item.get("quantity", "")
            desc  = item.get("description", "")
            unit  = item.get("unit", "ea")
            rate  = float(item.get("unit_price", 0))
            amt   = float(item.get("total", 0))
            text_y = row_top - 0.35 * inch

            c.setFillColor(DARK)
            c.setFont("Helvetica", 9)

            c.drawCentredString(cx[0] + cw[0] / 2,          text_y, str(qty))
            c.drawString       (cx[1] + 0.10 * inch,         text_y, desc)
            c.drawString       (cx[2] + 0.10 * inch,         text_y, unit)
            c.drawRightString  (cx[3] + cw[3] - 0.12 * inch, text_y, f"${rate:,.2f}")
            c.drawRightString  (cx[4] + cw[4] - 0.12 * inch, text_y, f"${amt:,.2f}")

        # Row border
        c.setStrokeColor(MID_GRAY)
        c.setLineWidth(0.3)
        c.rect(margin, row_top - row_h, content_w, row_h, fill=0, stroke=1)

        row_top -= row_h

    # ── TOTALS ────────────────────────────────────────────────────────────────
    tot_x = margin + content_w * 0.55
    tot_w = content_w * 0.45
    sub_h = 0.28 * inch
    tot_h = 0.35 * inch

    subtotal = float(invoice.get("subtotal", 0))
    total    = float(invoice.get("total", 0))

    def _total_row(y_top, label, value_str, highlight=False):
        h = tot_h if highlight else sub_h
        if highlight:
            c.setFillColor(BLUE)
        else:
            c.setFillColor(colors.white)
        c.rect(tot_x, y_top - h, tot_w, h, fill=1, stroke=0)
        c.setStrokeColor(MID_GRAY)
        c.setLineWidth(0.3)
        c.rect(tot_x, y_top - h, tot_w, h, fill=0, stroke=1)

        txt_y = y_top - h + (0.12 * inch if highlight else 0.08 * inch)
        c.setFillColor(colors.white if highlight else DARK)
        c.setFont("Helvetica-Bold" if highlight else "Helvetica", 10 if highlight else 9)
        c.drawString(tot_x + 0.15 * inch, txt_y, label)
        c.drawRightString(tot_x + tot_w - 0.15 * inch, txt_y, value_str)
        if highlight:
            c.setFont("Helvetica", 8)
            c.drawRightString(tot_x + tot_w - 0.15 * inch, y_top - h + 0.07 * inch - 0.10 * inch,
                              "USD")
        return y_top - h

    t_y = row_top
    t_y = _total_row(t_y, "Subtotal",  f"${subtotal:,.2f}")
    t_y = _total_row(t_y, "Tax (0%)", "$0.00")
    _total_row(t_y, "TOTAL DUE", f"${total:,.2f}", highlight=True)

    # ── PAYMENT METHODS ───────────────────────────────────────────────────────
    pay_top = t_y - (tot_h + 0.25 * inch)
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

    # ── FOOTER RULE ───────────────────────────────────────────────────────────
    foot_y = pay_top - pay_h - 0.20 * inch
    c.setStrokeColor(BLUE)
    c.setLineWidth(1.5)
    c.line(margin, foot_y, W - margin, foot_y)

    # Thank-you line
    c.setFillColor(BLUE)
    c.setFont("Helvetica-BoldOblique", 10)
    c.drawCentredString(W / 2, foot_y - 0.25 * inch, "Thank you for your continued business.")

    # Contact footer
    c.setFillColor(DARK)
    c.setFont("Helvetica", 7.5)
    c.drawCentredString(
        W / 2,
        foot_y - 0.45 * inch,
        "INCO GROUP, INC.  \u2022  9005 Travis Dr Ste 1, Pharr, TX 78577"
        "  \u2022  (956) 702-8851  \u2022  admin@incogrp.com",
    )

    c.save()
    buffer.seek(0)
    return buffer.read()
