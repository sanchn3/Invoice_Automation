"""
stamp_pdf.py
============
Overlay the INCO "RECEIVED" stamp (with date filled in) centred on the
first page of a provider invoice PDF, then save the result in-place.

Layout constants at the top can be tweaked if the date appears misaligned
after font or DPI changes.
"""
from __future__ import annotations

import io
from datetime import date
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas as rl_canvas
from pypdf import PdfReader, PdfWriter

# ── Paths ─────────────────────────────────────────────────────────────────────
_PHOTOS_DIR = Path(__file__).parent.parent / "photos"
_STAMP_PNG  = _PHOTOS_DIR / "stamp_without_date.png"

# ── Layout constants (fractions of the stamp image's own dimensions) ──────────
_DATE_CENTER_Y = 0.44   # vertical centre of the date box from top of stamp
_FONT_H_FRAC   = 0.075  # font height as fraction of stamp image height
_CHAR_SPACING  = 1.45   # tracking multiplier applied per character
_DATE_ROTATION  = 4     # degrees; positive = counter-clockwise
_DATE_X_OFFSET_PX = -31 # horizontal nudge in pixels (negative = left)
_STAMP_W_FRAC  = 0.42   # stamp width as fraction of PDF page width

_MONTHS = ["JAN", "FEB", "MAR", "APR", "MAY", "JUN",
           "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"]

# Font search order — Windows paths first, then Linux (Render/Ubuntu)
_FONT_PATHS = [
    # Windows
    "C:/Windows/Fonts/courbd.ttf",
    "C:/Windows/Fonts/cour.ttf",
    "C:/Windows/Fonts/arial.ttf",
    # Linux / Render (Ubuntu — fonts-dejavu-core is pre-installed)
    "/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationMono-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationMono-Regular.ttf",
]


def _load_font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    for path in _FONT_PATHS:
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    # load_default(size=) is supported in Pillow 9.2.0+
    try:
        return ImageFont.load_default(size=size)
    except TypeError:
        return ImageFont.load_default()


def _compose_stamp(received_date: date) -> bytes:
    """
    Write the date string into the blank box of stamp_without_date.png,
    convert the white background to transparent, and return PNG bytes.
    """
    img    = Image.open(_STAMP_PNG).convert("RGBA")
    iw, ih = img.size

    date_str = (
        f"{received_date.day:02d} "
        f"{_MONTHS[received_date.month - 1]} "
        f"{received_date.year}"
    )

    font    = _load_font(max(12, int(ih * _FONT_H_FRAC)))
    char_w  = font.getlength("0")                        # reference width (monospace)
    total_w = char_w * _CHAR_SPACING * len(date_str)

    x = (iw - total_w) / 2 + _DATE_X_OFFSET_PX
    y = ih * _DATE_CENTER_Y - (ih * _FONT_H_FRAC) / 2

    # Draw text onto a transparent layer so it can be rotated independently
    text_layer = Image.new("RGBA", (iw, ih), (0, 0, 0, 0))
    text_draw  = ImageDraw.Draw(text_layer)
    for ch in date_str:
        text_draw.text((x, y), ch, font=font, fill=(0, 0, 0, 255))
        x += char_w * _CHAR_SPACING

    # Rotate the text layer around the centre of the date box
    text_layer = text_layer.rotate(
        _DATE_ROTATION,
        resample=Image.BICUBIC,
        center=(iw / 2, ih * _DATE_CENTER_Y),
    )

    img = Image.alpha_composite(img, text_layer)

    # White / near-white → fully transparent so the PDF content shows through
    img.putdata([
        (r, g, b, 0) if (r > 220 and g > 220 and b > 220) else (r, g, b, a)
        for r, g, b, a in img.getdata()
    ])

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def stamp_pdf(pdf_path: str | Path, received_date: date) -> str:
    """
    Stamp the first page of *pdf_path* with the received stamp + date,
    overwrite the file in-place, and return *pdf_path* as a string.

    Raises FileNotFoundError if the PDF or stamp PNG is missing.
    Raises any pypdf / reportlab / PIL exception on processing failure.
    """
    pdf_path = Path(pdf_path)
    if not pdf_path.exists():
        raise FileNotFoundError(f"PDF not found: {pdf_path}")
    if not _STAMP_PNG.exists():
        raise FileNotFoundError(f"Stamp image not found: {_STAMP_PNG}")

    stamp_bytes = _compose_stamp(received_date)
    stamp_img   = Image.open(io.BytesIO(stamp_bytes))
    si_w, si_h  = stamp_img.size

    reader = PdfReader(str(pdf_path))
    page0  = reader.pages[0]
    page_w = float(page0.mediabox.width)
    page_h = float(page0.mediabox.height)

    # Scale stamp to _STAMP_W_FRAC of page width and centre it
    stamp_pt_w = page_w * _STAMP_W_FRAC
    stamp_pt_h = stamp_pt_w * (si_h / si_w)
    stamp_x    = (page_w - stamp_pt_w) / 2
    stamp_y    = (page_h - stamp_pt_h) / 2

    # Build a transparent reportlab overlay page the same size as page 0
    overlay_buf = io.BytesIO()
    c = rl_canvas.Canvas(overlay_buf, pagesize=(page_w, page_h))
    c.drawImage(
        ImageReader(io.BytesIO(stamp_bytes)),
        stamp_x, stamp_y,
        width=stamp_pt_w, height=stamp_pt_h,
        mask="auto",          # honours the PNG alpha channel
    )
    c.save()

    # Merge overlay onto page 0 only; remaining pages pass through unchanged
    overlay_buf.seek(0)
    overlay_page = PdfReader(overlay_buf).pages[0]

    writer = PdfWriter()
    for i, page in enumerate(reader.pages):
        if i == 0:
            page.merge_page(overlay_page)
        writer.add_page(page)

    # Atomic in-place replacement via temp file
    tmp = pdf_path.with_suffix(".stamping.pdf")
    try:
        with open(tmp, "wb") as fh:
            writer.write(fh)
        tmp.replace(pdf_path)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise

    # Upload stamped PDF to Supabase Storage (overwrites the pre-stamp version)
    try:
        from utils.pdf_storage import upload_pdf
        upload_pdf(str(pdf_path))
    except Exception as _e:
        import logging as _logging
        _logging.getLogger(__name__).error("stamp_pdf: Supabase upload failed: %s", _e)

    return str(pdf_path)


def _wrap_note(text: str, max_chars: int) -> list[str]:
    """Word-wrap note text into lines of at most max_chars, respecting newlines."""
    lines: list[str] = []
    for paragraph in text.splitlines():
        paragraph = paragraph.strip()
        if not paragraph:
            continue
        while len(paragraph) > max_chars:
            split_at = paragraph.rfind(" ", 0, max_chars)
            if split_at <= 0:
                split_at = max_chars
            lines.append(paragraph[:split_at])
            paragraph = paragraph[split_at:].lstrip()
        if paragraph:
            lines.append(paragraph)
    return lines


def stamp_temperature(
    pdf_bytes: bytes,
    temps: list[str],
    producto_caliente: bool,
    notes: str = "",
) -> bytes:
    """
    Overlay a PULP TEMPERATURE RECORD block onto the lower-right corner of the
    first page of a provider PDF.

    Parameters
    ----------
    pdf_bytes         : raw bytes of the original provider PDF
    temps             : list of up to 3 temperature strings (empty strings ignored)
    producto_caliente : whether to show the 'Producto Caliente' label
    notes             : optional admin notes rendered below the temperature rows

    Returns
    -------
    bytes — the modified PDF with the temperature overlay on page 1.
    """
    from reportlab.lib import colors as rl_colors
    from reportlab.lib.units import inch

    BLUE       = rl_colors.HexColor("#1F5096")
    DARK       = rl_colors.HexColor("#1A1A1A")
    LABEL_GRAY = rl_colors.HexColor("#888888")
    BG         = rl_colors.HexColor("#F2F2F2")

    reader = PdfReader(io.BytesIO(pdf_bytes))
    page0  = reader.pages[0]
    page_w = float(page0.mediabox.width)
    page_h = float(page0.mediabox.height)

    filled_temps = [t for t in temps if t and t.strip()]
    note_lines   = _wrap_note(notes, 30) if notes.strip() else []
    n_items      = len(filled_temps) + (1 if producto_caliente else 0)

    if n_items == 0 and not note_lines:
        return pdf_bytes  # nothing to stamp

    # ── Layout ────────────────────────────────────────────────────────────────
    margin   = 0.45 * inch
    box_w    = 1.85 * inch
    line_g   = 0.165 * inch
    head_h   = 0.38 * inch          # space for heading + rule
    pad      = 0.12 * inch          # top/bottom internal padding

    # Extra height needed for notes: "Notes:" label line + each note line
    notes_h  = (line_g + len(note_lines) * line_g + 0.04 * inch) if note_lines else 0
    box_h    = pad + head_h + n_items * line_g + notes_h + pad

    sec_x = page_w - margin - box_w  # left edge of the block
    sec_y = margin + box_h            # top edge of the block (ReportLab y up)

    # ── Overlay canvas ────────────────────────────────────────────────────────
    overlay_buf = io.BytesIO()
    c = rl_canvas.Canvas(overlay_buf, pagesize=(page_w, page_h))

    # Background box
    c.setFillColor(BG)
    c.setStrokeColor(BLUE)
    c.setLineWidth(0.7)
    c.roundRect(sec_x - 0.06 * inch, margin,
                box_w + 0.12 * inch, box_h, 4, fill=1, stroke=1)

    # Heading
    c.setFillColor(BLUE)
    c.setFont("Helvetica-Bold", 7)
    c.drawString(sec_x, sec_y - 0.17 * inch, "PULP TEMPERATURE")

    # Rule under heading
    c.setStrokeColor(BLUE)
    c.setLineWidth(0.5)
    c.line(sec_x, sec_y - 0.24 * inch, sec_x + box_w, sec_y - 0.24 * inch)

    item_y = sec_y - head_h

    # Temperature rows
    for i, t in enumerate(filled_temps, 1):
        c.setFillColor(LABEL_GRAY)
        c.setFont("Helvetica", 6.5)
        c.drawString(sec_x, item_y, f"Temp {i}:")
        c.setFillColor(DARK)
        c.setFont("Helvetica-Bold", 8)
        c.drawString(sec_x + 0.50 * inch, item_y, f"{t} \u00b0F")
        item_y -= line_g

    # Producto Caliente (only when active)
    if producto_caliente:
        c.setFillColor(DARK)
        c.setFont("Helvetica-Bold", 8)
        c.drawString(sec_x, item_y, "Producto Caliente")
        item_y -= line_g

    # Notes section
    if note_lines:
        item_y -= 0.04 * inch   # small gap before notes
        c.setFillColor(LABEL_GRAY)
        c.setFont("Helvetica", 6.5)
        c.drawString(sec_x, item_y, "Notes:")
        item_y -= line_g
        for line in note_lines:
            c.setFillColor(DARK)
            c.setFont("Helvetica", 7)
            c.drawString(sec_x, item_y, line)
            item_y -= line_g

    c.save()

    # ── Merge overlay onto page 0 only ────────────────────────────────────────
    overlay_buf.seek(0)
    overlay_page = PdfReader(overlay_buf).pages[0]

    writer = PdfWriter()
    for i, page in enumerate(reader.pages):
        if i == 0:
            page.merge_page(overlay_page)
        writer.add_page(page)

    out = io.BytesIO()
    writer.write(out)
    out.seek(0)
    return out.read()
