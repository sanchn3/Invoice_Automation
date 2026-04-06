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

_CLASSIFIER_PROMPT = """You are an email classifier for a logistics company.
Determine if the following email is a provider invoice that needs to be processed.

Email details:
Subject: {subject}
From: {sender}
Body preview: {body_preview}

Reply with a single word: YES or NO
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


def is_invoice_email(subject: str, sender: str, body_preview: str) -> bool:
    """
    Use Claude to classify whether an email is a provider invoice.
    Returns True if Claude says YES, False otherwise.
    """
    try:
        message = _client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=10,
            messages=[
                {
                    "role"   : "user",
                    "content": _CLASSIFIER_PROMPT.format(
                        subject=subject,
                        sender=sender,
                        body_preview=body_preview[:500],
                    ),
                }
            ],
        )
        answer = message.content[0].text.strip().upper()
        return answer.startswith("YES")
    except Exception as e:
        logger.warning("Claude email classifier failed: %s — defaulting to True.", e)
        return True  # Default to processing on classifier failure
