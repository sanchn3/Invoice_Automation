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

# Windows font search order — first match wins
_FONT_PATHS = [
    "C:/Windows/Fonts/courbd.ttf",  # Courier New Bold  (preferred)
    "C:/Windows/Fonts/cour.ttf",    # Courier New regular fallback
    "C:/Windows/Fonts/arial.ttf",   # Arial fallback
]


def _load_font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    for path in _FONT_PATHS:
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
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

    return str(pdf_path)
