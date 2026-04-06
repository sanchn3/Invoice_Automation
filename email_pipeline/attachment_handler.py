"""
attachment_handler.py
=====================
Extracts PDF attachments from O365 message objects, saves them locally,
and triggers the parsing pipeline.
"""

import logging
from datetime import datetime
from pathlib import Path

from config import PDFS_DIR
from data_manager import DataManager
from parsing.pdf_parser import parse_pdf
from parsing.claude_parser import parse_with_claude
from alerting.alert_manager import AlertManager

logger = logging.getLogger(__name__)


def save_pdf_attachment(message, email_log_id: str, dm: DataManager) -> str | None:
    """
    Download and save the first PDF attachment from an O365 message.
    Updates the email log with the local path. Returns the path or None.
    Used to save PDFs for admin review before full processing.
    """
    try:
        message.attachments.download_attachments()
        for attachment in message.attachments:
            name = getattr(attachment, "name", "") or ""
            if not name.lower().endswith(".pdf"):
                continue
            short_id   = email_log_id.replace("-", "")[:12]
            safe_name  = name.replace(" ", "_")
            local_name = f"{short_id}_{safe_name}"
            local_path = PDFS_DIR / local_name
            attachment.save(location=str(PDFS_DIR), custom_name=local_name)
            pdf_path = str(local_path)
            dm.update_email_log(email_log_id, {
                "pdf_filename"   : local_name,
                "pdf_local_path" : pdf_path,
            })
            logger.info("Saved PDF for pending review: %s", local_path)
            return pdf_path
    except Exception as e:
        logger.warning("Could not save attachment for pending_review email %s: %s", email_log_id, e)
    return None


def process_pdf_from_path(
    pdf_path: str,
    email_log_id: str,
    dm: DataManager,
    alert_manager: AlertManager,
) -> bool:
    """
    Run the full parsing + invoice-creation pipeline on an already-saved PDF.
    Called when an admin manually accepts a pending_review email.
    Returns True on success, False on failure.
    """
    email_log     = dm.get_email_log_by_id(email_log_id)
    sender        = email_log.get("sender", "")
    sender_domain = sender.split("@")[-1] if "@" in sender else sender
    provider      = dm.get_provider_by_email_domain(sender_domain)

    if provider is None:
        provider_profile = None
        provider_name    = sender_domain
    else:
        provider_profile = provider.get("parser_profile")
        provider_name    = provider.get("name", sender_domain)

    parsed = parse_pdf(pdf_path, provider_profile, dm)
    if parsed is None:
        parsed = parse_with_claude(pdf_path)

    if parsed is None:
        dm.update_email_log(email_log_id, {
            "status"    : "pending_review",
            "error_text": "Parsing failed with both pdfplumber and Claude.",
        })
        alert_manager.parsing_failed(
            subject=email_log.get("subject", ""),
            email_log_id=email_log_id,
        )
        return False

    if parsed.get("provider_name"):
        provider_name = parsed["provider_name"]

    # Duplicate detection
    duplicate = next(
        (
            pi for pi in dm.get_provider_invoices()
            if pi.get("invoice_number") == parsed.get("invoice_number")
            and pi.get("provider_name") == provider_name
        ),
        None,
    )
    if duplicate:
        dm.update_email_log(email_log_id, {
            "status"    : "pending_review",
            "error_text": f"Duplicate invoice number: {parsed.get('invoice_number')}",
        })
        return False

    now = datetime.utcnow().isoformat() + "Z"
    provider_invoice = dm.add_provider_invoice({
        "provider_name"   : provider_name,
        "client_name"     : parsed.get("client_name", ""),
        "invoice_number"  : parsed.get("invoice_number", ""),
        "invoice_date"    : parsed.get("invoice_date", ""),
        "line_items"      : parsed.get("line_items", []),
        "subtotal"        : parsed.get("subtotal", 0.0),
        "taxes"           : parsed.get("taxes", 0.0),
        "total"           : parsed.get("total", 0.0),
        "pdf_local_path"  : pdf_path,
        "email_intake_id" : email_log_id,
        "parsed_at"       : now,
        "status"          : "parsed",
    })

    dm.add_client_invoice({
        "quickbooks_invoice_number": None,
        "client_name"              : parsed.get("client_name", ""),
        "invoice_date"             : parsed.get("invoice_date", now[:10]),
        "service_type"             : None,
        "temp_recorder"            : False,
        "extra_charges"            : [],
        "pallet_count"             : 0,
        "damaged_pallets"          : 0,
        "broken_pallets"           : 0,
        "worker_notes"             : "",
        "photo_paths"              : [],
        "line_items"               : [],
        "subtotal"                 : 0.0,
        "total"                    : 0.0,
        "provider_invoice_id"      : provider_invoice["id"],
        "quickbooks_exported"      : False,
        "status"                   : "pending_worker",
    })

    dm.update_email_log(email_log_id, {"status": "parsed", "error_text": None})
    alert_manager.new_invoice_received(provider=provider_name, client=parsed.get("client_name", ""))
    logger.info("Admin accepted invoice: %s → client=%s", parsed.get("invoice_number"), parsed.get("client_name"))
    return True


def handle_attachment(
    message,
    email_log_id: str,
    dm: DataManager,
    alert_manager: AlertManager,
) -> None:
    """
    Extract the first PDF attachment from an O365 message, save it locally,
    then attempt parsing. Creates provider_invoice and client_invoice records
    on success. Updates email_intake_log status throughout.
    """
    pdf_path: str | None = None
    pdf_filename: str | None = None

    # ── Save PDF attachment ───────────────────────────────────────────────────
    try:
        message.attachments.download_attachments()
        attachments = message.attachments

        for attachment in attachments:
            name = getattr(attachment, "name", "") or ""
            if not name.lower().endswith(".pdf"):
                continue

            # Build a unique local filename
            short_id     = email_log_id.replace("-", "")[:12]
            safe_name    = name.replace(" ", "_")
            local_name   = f"{short_id}_{safe_name}"
            local_path   = PDFS_DIR / local_name

            attachment.save(location=str(PDFS_DIR), custom_name=local_name)

            pdf_path     = str(local_path)
            pdf_filename = local_name
            logger.info("Saved attachment: %s", local_path)
            break  # only process the first PDF

    except Exception as e:
        logger.error("Failed to save attachment for email_log %s: %s", email_log_id, e)
        dm.update_email_log(email_log_id, {
            "status"    : "pending_review",
            "error_text": f"Attachment save failed: {e}",
        })
        alert_manager.parsing_failed(
            subject=dm.get_email_log_by_id(email_log_id).get("subject", ""),
            email_log_id=email_log_id,
        )
        return

    if not pdf_path:
        dm.update_email_log(email_log_id, {
            "status"    : "pending_review",
            "error_text": "No PDF attachment found.",
        })
        return

    # Update log with PDF location
    dm.update_email_log(email_log_id, {
        "pdf_filename"  : pdf_filename,
        "pdf_local_path": pdf_path,
        "attachment_count": 1,
    })

    # ── Identify provider ─────────────────────────────────────────────────────
    email_log  = dm.get_email_log_by_id(email_log_id)
    sender     = email_log.get("sender", "")
    sender_domain = sender.split("@")[-1] if "@" in sender else sender
    provider   = dm.get_provider_by_email_domain(sender_domain)

    if provider is None:
        logger.warning("Unknown provider email domain: %s", sender_domain)
        alert_manager.unknown_provider(sender)
        # Still attempt parsing via Claude
        provider_profile = None
        provider_name    = sender_domain
    else:
        provider_profile = provider.get("parser_profile")
        provider_name    = provider.get("name", sender_domain)

    # ── Parse PDF ─────────────────────────────────────────────────────────────
    parsed = parse_pdf(pdf_path, provider_profile, dm)

    if parsed is None:
        logger.info("pdfplumber parse failed, trying Claude fallback for %s", pdf_path)
        parsed = parse_with_claude(pdf_path)

    if parsed is None:
        logger.error("All parsing failed for %s", pdf_path)
        dm.update_email_log(email_log_id, {
            "status"    : "pending_review",
            "error_text": "Parsing failed with both pdfplumber and Claude.",
        })
        alert_manager.parsing_failed(
            subject=email_log.get("subject", ""),
            email_log_id=email_log_id,
        )
        return

    # Override provider_name from parse result if available
    if parsed.get("provider_name"):
        provider_name = parsed["provider_name"]

    # ── Duplicate detection ───────────────────────────────────────────────────
    existing_provider_invoices = dm.get_provider_invoices()
    duplicate = next(
        (
            pi for pi in existing_provider_invoices
            if pi.get("invoice_number") == parsed.get("invoice_number")
            and pi.get("provider_name") == provider_name
        ),
        None,
    )
    if duplicate:
        logger.warning("Duplicate invoice detected: %s", parsed.get("invoice_number"))
        dm.update_email_log(email_log_id, {
            "status"    : "pending_review",
            "error_text": f"Duplicate invoice number: {parsed.get('invoice_number')}",
        })
        alert_manager.duplicate_detected(
            invoice_number=parsed.get("invoice_number", ""),
            provider=provider_name,
        )
        return

    # ── Save provider invoice ─────────────────────────────────────────────────
    now = datetime.utcnow().isoformat() + "Z"
    provider_invoice = dm.add_provider_invoice({
        "provider_name"   : provider_name,
        "client_name"     : parsed.get("client_name", ""),
        "invoice_number"  : parsed.get("invoice_number", ""),
        "invoice_date"    : parsed.get("invoice_date", ""),
        "line_items"      : parsed.get("line_items", []),
        "subtotal"        : parsed.get("subtotal", 0.0),
        "taxes"           : parsed.get("taxes", 0.0),
        "total"           : parsed.get("total", 0.0),
        "pdf_local_path"  : pdf_path,
        "email_intake_id" : email_log_id,
        "parsed_at"       : now,
        "status"          : "parsed",
    })

    # ── Create pending client invoice ─────────────────────────────────────────
    dm.add_client_invoice({
        "quickbooks_invoice_number": None,
        "client_name"              : parsed.get("client_name", ""),
        "invoice_date"             : parsed.get("invoice_date", now[:10]),
        "service_type"             : None,   # set by admin
        "temp_recorder"            : False,
        "extra_charges"            : [],
        "pallet_count"             : 0,
        "damaged_pallets"          : 0,
        "broken_pallets"           : 0,
        "worker_notes"             : "",
        "photo_paths"              : [],
        "line_items"               : [],
        "subtotal"                 : 0.0,
        "total"                    : 0.0,
        "provider_invoice_id"      : provider_invoice["id"],
        "quickbooks_exported"      : False,
        "status"                   : "pending_worker",
    })

    dm.update_email_log(email_log_id, {"status": "parsed"})
    logger.info(
        "Invoice processed: %s → client=%s",
        parsed.get("invoice_number"), parsed.get("client_name"),
    )
    alert_manager.new_invoice_received(
        provider=provider_name,
        client=parsed.get("client_name", ""),
    )
