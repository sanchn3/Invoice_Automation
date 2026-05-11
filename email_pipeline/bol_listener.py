"""
bol_listener.py
===============
Polls the "BILL OF LADING" Outlook subfolder via the O365 library.
For each new email it downloads the PDF attachment, saves it to bols/,
and creates a BOL record with status="bol_inbox" for admin validation.
"""

import logging
from datetime import datetime

from O365 import Account, FileSystemTokenBackend

from config import (
    MS_CLIENT_ID, MS_CLIENT_SECRET, MS_TENANT_ID,
    WORKER_EMAIL, OUTLOOK_BOL_FOLDER, BASE_DIR, BOLS_DIR,
)
from data_manager import DataManager
from parsing.claude_parser import extract_bol_po_number

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


def poll_bol_inbox(dm: DataManager) -> int:
    """
    Poll OUTLOOK_BOL_FOLDER for new emails.
    Saves the first PDF attachment per email and creates a BOL record.
    Returns the number of new emails processed.
    """
    logger.info("Polling BOL inbox folder: %s", OUTLOOK_BOL_FOLDER)
    count = 0

    try:
        account = _get_account()
        mailbox = account.mailbox(resource=WORKER_EMAIL)
        inbox   = mailbox.inbox_folder()

        # Find the "BILL OF LADING" folder (top-level or inbox child)
        target_folder = None
        for folder in mailbox.get_folders():
            if folder.name.lower() == OUTLOOK_BOL_FOLDER.lower():
                target_folder = folder
                break

        if target_folder is None:
            for folder in inbox.get_folders():
                if folder.name.lower() == OUTLOOK_BOL_FOLDER.lower():
                    target_folder = folder
                    break

        if target_folder is None:
            logger.error("BOL folder '%s' not found in mailbox.", OUTLOOK_BOL_FOLDER)
            return 0

        messages = target_folder.get_messages(limit=50)

        for message in messages:
            message_id = str(message.object_id)

            if dm.bol_message_id_exists(message_id):
                logger.debug("BOL already processed message_id=%s, skipping.", message_id)
                continue

            sender  = str(message.sender.address) if message.sender else ""
            subject = str(message.subject) or ""
            now     = datetime.utcnow().isoformat() + "Z"

            # Load body for Claude context
            body_preview = ""
            try:
                message.body
                body_preview = (message.body or "")[:1000]
            except Exception:
                pass

            po_number = extract_bol_po_number(subject, body_preview)

            # Download and save the first PDF attachment.
            # Use a UUID prefix so each BOL gets its own unique file even when
            # multiple emails share the same attachment filename.
            pdf_local_path = ""
            try:
                import uuid as _uuid
                message.attachments.download_attachments()
                for attachment in message.attachments:
                    name = getattr(attachment, "name", "") or ""
                    if not name.lower().endswith(".pdf"):
                        continue
                    unique_id  = _uuid.uuid4().hex[:12]
                    local_name = f"BOL_{unique_id}_{name.replace(' ', '_')}"
                    attachment.save(location=str(BOLS_DIR), custom_name=local_name)
                    pdf_local_path = str(BOLS_DIR / local_name)
                    logger.info("Saved BOL PDF: %s", pdf_local_path)
                    break  # one PDF per email is enough
            except Exception as exc:
                logger.warning("Could not save BOL attachment for %s: %s", message_id, exc)

            dm.add_bol_record({
                "po_number"       : po_number,
                "received_at"     : now,
                "sender"          : sender,
                "subject"         : subject,
                "message_id"      : message_id,
                "pdf_local_path"  : pdf_local_path,
                "status"          : "bol_inbox",
                "driver_name"     : None,
                "checkin_at"      : None,
                "checkin_notified": False,
            })

            logger.info(
                "BOL record created: PO='%s' subject='%s' from=%s",
                po_number or "(blank)", subject, sender,
            )
            count += 1

    except Exception as exc:
        logger.error("poll_bol_inbox failed: %s", exc)

    logger.info("poll_bol_inbox complete. %d new email(s) processed.", count)
    return count
