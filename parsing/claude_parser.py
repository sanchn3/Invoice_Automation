"""
claude_parser.py
================
Fallback invoice parser using the Claude API.
Called when pdfplumber-based parsing fails or produces incomplete results.
"""

import json
import logging

import anthropic
import pdfplumber

from config import ANTHROPIC_API_KEY, CLAUDE_MODEL

logger = logging.getLogger(__name__)
_client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

_EXTRACTION_PROMPT = """You are an invoice data extraction assistant.
Below is the full text of a provider invoice PDF.
Extract the following fields and return ONLY a valid JSON object — no markdown, no explanation.

Required fields:
- provider_name (string): the company sending the invoice
- client_name (string): the company being billed
- invoice_number (string): the invoice identifier
- invoice_date (string): date of the invoice (YYYY-MM-DD if possible)
- line_items (array of objects): each with keys: description, quantity, unit_price, total
- subtotal (number): subtotal before tax
- taxes (number): tax amount
- total (number): final total amount due

If a field cannot be found, use null for strings and 0 for numbers.
Return ONLY the JSON object.

Invoice text:
{text}
"""

def parse_with_claude(pdf_path: str) -> dict | None:
    """
    Extract invoice fields from a PDF using Claude as a fallback parser.
    Returns a dict with extracted fields, or None on failure.
    """
    # Extract text with pdfplumber first
    try:
        text = ""
        with pdfplumber.open(pdf_path) as pdf:
            for page in pdf.pages:
                text += (page.extract_text() or "") + "\n"
    except Exception as e:
        logger.error("Could not extract text from %s for Claude parsing: %s", pdf_path, e)
        return None

    if not text.strip():
        logger.warning("No text to send to Claude from %s", pdf_path)
        return None

    # Truncate to avoid token limits (~12k chars ≈ ~3k tokens)
    text_truncated = text[:12000]

    try:
        message = _client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=1024,
            messages=[
                {
                    "role"   : "user",
                    "content": _EXTRACTION_PROMPT.format(text=text_truncated),
                }
            ],
        )
        raw = message.content[0].text.strip()

        # Strip any accidental markdown fences
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        raw = raw.strip()

        result = json.loads(raw)

        # Normalise numeric fields
        for field in ("subtotal", "taxes", "total"):
            try:
                result[field] = float(result.get(field) or 0)
            except (TypeError, ValueError):
                result[field] = 0.0

        # Ensure line_items is a list
        if not isinstance(result.get("line_items"), list):
            result["line_items"] = []

        logger.info(
            "Claude parsed invoice: number=%s, client=%s, total=%.2f",
            result.get("invoice_number"), result.get("client_name"), result.get("total", 0),
        )
        return result

    except json.JSONDecodeError as e:
        logger.error("Claude returned invalid JSON: %s", e)
        return None
    except Exception as e:
        logger.error("Claude parsing failed for %s: %s", pdf_path, e)
        return None


_BOL_PO_PROMPT = """You are a logistics document assistant.
Extract the PO number (Purchase Order number) from the following email details.

Subject: {subject}
Body preview: {body_preview}

Rules:
- Return ONLY the PO number as a plain string (e.g. "12345" or "PO-98765").
- If no PO number can be found, return the single word: null
- No explanation, no extra text.
"""


def extract_bol_po_number(subject: str, body_preview: str) -> str:
    """
    Use Claude to extract a PO number from a BOL email subject and body.
    Returns the PO number string, or an empty string if not found.
    """
    try:
        message = _client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=50,
            messages=[
                {
                    "role"   : "user",
                    "content": _BOL_PO_PROMPT.format(
                        subject=subject,
                        body_preview=body_preview[:1000],
                    ),
                }
            ],
        )
        answer = message.content[0].text.strip()
        if answer.lower() == "null" or not answer:
            return ""
        return answer
    except Exception as e:
        logger.warning("Claude BOL PO extraction failed: %s — leaving PO blank.", e)
        return ""


