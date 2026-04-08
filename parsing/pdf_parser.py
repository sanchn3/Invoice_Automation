"""
pdf_parser.py
=============
Parses provider invoice PDFs using pdfplumber + provider-specific
keyword profiles. Returns None if required fields cannot be found,
triggering the Claude fallback.
"""

import logging
import re
from pathlib import Path
from typing import Any

import pdfplumber

logger = logging.getLogger(__name__)

_REQUIRED_FIELDS = {"invoice_number", "client_name", "total"}


def _extract_text(pdf_path: str) -> str:
    """Extract all text from a PDF file."""
    text = ""
    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            page_text = page.extract_text() or ""
            text += page_text + "\n"
    return text


def _find_value_after_keyword(text: str, keyword: str) -> str | None:
    """
    Find the value on the same line or the next non-empty line after a keyword.
    Returns the cleaned string value or None.
    """
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if keyword.lower() in line.lower():
            # Try to get value after keyword on same line
            remainder = line[line.lower().index(keyword.lower()) + len(keyword):].strip(" :").strip()
            if remainder:
                return remainder
            # Try next non-empty line
            for j in range(i + 1, min(i + 4, len(lines))):
                candidate = lines[j].strip()
                if candidate:
                    return candidate
    return None


def _extract_tables(pdf_path: str) -> list[list[list[str | None]]]:
    """Extract all tables from the PDF."""
    tables = []
    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            page_tables = page.extract_tables()
            if page_tables:
                tables.extend(page_tables)
    return tables


def _parse_amount(value: str) -> float:
    """Parse a currency string like '$1,234.56' to float."""
    if not value:
        return 0.0
    cleaned = re.sub(r"[^\d.]", "", value.replace(",", ""))
    try:
        return float(cleaned)
    except ValueError:
        return 0.0


def _parse_line_items(tables: list) -> list[dict]:
    """
    Attempt to extract line items from PDF tables.
    Looks for rows with at least a description and a numeric amount.
    """
    line_items: list[dict] = []

    for table in tables:
        if not table or len(table) < 2:
            continue

        # Try to detect header row
        header = [str(cell).lower().strip() if cell else "" for cell in table[0]]
        desc_col  = next((i for i, h in enumerate(header) if "desc" in h or "item" in h or "service" in h), None)
        qty_col   = next((i for i, h in enumerate(header) if "qty" in h or "quant" in h), None)
        price_col = next((i for i, h in enumerate(header) if "price" in h or "rate" in h or "unit" in h), None)
        total_col = next((i for i, h in enumerate(header) if "total" in h or "amount" in h or "ext" in h), None)

        if desc_col is None:
            continue

        for row in table[1:]:
            if not row or not row[desc_col]:
                continue
            desc = str(row[desc_col]).strip()
            if not desc or desc.lower() in ("", "description", "item"):
                continue

            qty   = float(str(row[qty_col]).strip() or "1") if qty_col is not None and row[qty_col] else 1
            price = _parse_amount(str(row[price_col])) if price_col is not None and row[price_col] else 0.0
            total = _parse_amount(str(row[total_col])) if total_col is not None and row[total_col] else round(qty * price, 2)

            if desc:
                line_items.append({
                    "description": desc,
                    "quantity"   : qty,
                    "unit_price" : price,
                    "total"      : total,
                })

    return line_items


def parse_pdf(pdf_path: str, provider_profile: dict | None, dm: Any) -> dict | None:
    """
    Parse a provider invoice PDF using the provider's keyword profile.

    Returns a dict with extracted fields, or None if parsing fails.
    """
    try:
        text   = _extract_text(pdf_path)
        tables = _extract_tables(pdf_path)
    except Exception as e:
        logger.error("pdfplumber failed to open %s: %s", pdf_path, e)
        return None

    if not text.strip():
        logger.warning("PDF appears to have no extractable text: %s", pdf_path)
        return None

    profile = provider_profile or {}

    # Keyword mappings (profile keys → fallback keywords)
    invoice_num_kw = profile.get("invoice_number_keyword", "Invoice")
    client_kw      = profile.get("client_name_keyword", "Bill To")
    date_kw        = profile.get("date_keyword", "Date")
    total_kw       = profile.get("total_keyword", "Total")

    invoice_number = _find_value_after_keyword(text, invoice_num_kw)
    client_name    = _find_value_after_keyword(text, client_kw)
    invoice_date   = _find_value_after_keyword(text, date_kw)
    total_str      = _find_value_after_keyword(text, total_kw)

    # Subtotal / taxes — best-effort
    subtotal_str = _find_value_after_keyword(text, "Subtotal")
    tax_str      = _find_value_after_keyword(text, "Tax") or _find_value_after_keyword(text, "HST") or "0"

    total    = _parse_amount(total_str or "0")
    subtotal = _parse_amount(subtotal_str or "0") or total
    taxes    = _parse_amount(tax_str or "0")

    # Provider name — try to infer from first meaningful line
    provider_name = ""
    for line in text.splitlines()[:5]:
        line = line.strip()
        if len(line) > 3 and not any(kw in line.lower() for kw in ("invoice", "date", "bill")):
            provider_name = line
            break

    result = {
        "provider_name"  : provider_name,
        "client_name"    : (client_name or "").upper(),
        "invoice_number" : invoice_number or "",
        "invoice_date"   : invoice_date or "",
        "line_items"     : _parse_line_items(tables),
        "subtotal"       : subtotal,
        "taxes"          : taxes,
        "total"          : total,
    }

    # Validate required fields
    missing = [f for f in _REQUIRED_FIELDS if not result.get(f)]
    if missing:
        logger.warning(
            "PDF parser could not extract required fields %s from %s — falling back to Claude.",
            missing, pdf_path,
        )
        return None

    logger.info(
        "PDF parsed: invoice=%s, client=%s, total=%.2f",
        result["invoice_number"], result["client_name"], result["total"],
    )
    return result
