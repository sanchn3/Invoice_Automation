"""
outlook_listener.py
===================
Polls the "Provider Invoices" Outlook subfolder via the O365 library.
Every email is logged IMMEDIATELY before any processing.
"""

import logging
from datetime import datetime

from O365 import Account, FileSystemTokenBackend

from config import (
    MS_CLIENT_ID, MS_CLIENT_SECRET, MS_TENANT_ID,
    WORKER_EMAIL, OUTLOOK_INVOICE_FOLDER, BASE_DIR,
)
from data_manager import DataManager
from email_pipeline.attachment_handler import handle_attachment, save_pdf_attachment
from email_pipeline.email_classifier import is_invoice_email
from alerting.alert_manager import AlertManager

logger = logging.getLogger(__name__)


def _get_account() -> Account:
    credentials   = (MS_CLIENT_ID, MS_CLIENT_SECRET)
    token_backend = FileSystemTokenBackend(
        token_path=str(BASE_DIR),
        token_filename="o365_token.txt",
    )
    account = Account(
        credentials,
        auth_flow_type="credentials",
        tenant_id=MS_TENANT_ID,
        token_backend=token_backend,
        main_resource=WORKER_EMAIL,
    )
    if not account.is_authenticated:
        account.authenticate()
    return account


def poll_inbox(dm: DataManager, alert_manager: AlertManager) -> int:
    """
    Poll OUTLOOK_INVOICE_FOLDER for unread emails.
    Logs every email immediately, then processes attachments.
    Returns the number of new emails processed.
    """
    logger.info("Polling inbox folder: %s", OUTLOOK_INVOICE_FOLDER)
    count = 0

    try:
        account  = _get_account()
        mailbox  = account.mailbox(resource=WORKER_EMAIL)
        inbox    = mailbox.inbox_folder()

        # Find the named subfolder
        target_folder = None
        for folder in mailbox.get_folders():
            if folder.name.lower() == OUTLOOK_INVOICE_FOLDER.lower():
                target_folder = folder
                break

        if target_folder is None:
            # Try child folders of inbox
            for folder in inbox.get_folders():
                if folder.name.lower() == OUTLOOK_INVOICE_FOLDER.lower():
                    target_folder = folder
                    break

        if target_folder is None:
            logger.error("Folder '%s' not found in mailbox.", OUTLOOK_INVOICE_FOLDER)
            return 0

        messages = target_folder.get_messages(limit=50)

        for message in messages:
            message_id = str(message.object_id)
            now        = datetime.utcnow().isoformat() + "Z"

            # Skip duplicates
            if dm.message_id_exists(message_id):
                logger.debug("Already processed message_id=%s, skipping.", message_id)
                continue

            sender  = str(message.sender.address) if message.sender else ""
            subject = str(message.subject) or ""

            # ── Log IMMEDIATELY before any processing ────────────────────────
            email_log = dm.add_email_log({
                "received_at"    : now,
                "sender"         : sender,
                "subject"        : subject,
                "message_id"     : message_id,
                "attachment_count": len(message.attachments) if message.attachments else 0,
                "pdf_filename"   : None,
                "pdf_local_path" : None,
                "status"         : "received",
                "error_text"     : None,
            })
            email_log_id = email_log["id"]
            logger.info("Logged email: subject='%s' from=%s", subject, sender)

            # ── Classify ──────────────────────────────────────────────────────
            body_preview = ""
            try:
                message.body  # trigger lazy load
                body_preview = (message.body or "")[:500]
            except Exception:
                pass

            if not is_invoice_email(subject, sender, body_preview):
                logger.info("Email classified as NOT an invoice — saving for admin review.")
                dm.update_email_log(email_log_id, {
                    "status"    : "pending_review",
                    "error_text": "Classified as non-invoice by AI classifier.",
                })
                save_pdf_attachment(message, email_log_id, dm)
                continue

            # ── Handle attachment ─────────────────────────────────────────────
            handle_attachment(message, email_log_id, dm, alert_manager)
            count += 1

    except Exception as e:
        logger.error("poll_inbox failed: %s", e)

    logger.info("poll_inbox complete. %d new email(s) processed.", count)
    return count
