"""
=====================================================
INCO - Invoice Fetcher Phase 1
=====================================================
Fetches sent invoice emails from Outlook via
Microsoft Graph API, organizes PDFs by client,
generates a CSV, and stores data in Supabase.
=====================================================
"""

import os
import csv
import base64
import logging
import requests
import msal
from datetime import datetime
from pathlib import Path
from dotenv import load_dotenv
from supabase import create_client, Client

# =====================================================
# LOGGING
# =====================================================
LOG_DIR = Path("logs")
LOG_DIR.mkdir(exist_ok=True)

_log_file = LOG_DIR / f"invoice_fetcher_{datetime.now().strftime('%Y-%m-%d_%H-%M-%S')}.log"
_formatter = logging.Formatter(
    fmt="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

_file_handler = logging.FileHandler(_log_file, encoding="utf-8")
_file_handler.setFormatter(_formatter)

_console_handler = logging.StreamHandler()
_console_handler.setFormatter(_formatter)

logging.basicConfig(level=logging.INFO, handlers=[_file_handler, _console_handler])
logger = logging.getLogger(__name__)
logger.info("Log file: %s", _log_file)

# =====================================================
# LOAD ENVIRONMENT VARIABLES
# =====================================================
load_dotenv()

CLIENT_ID     = os.getenv("CLIENT_ID")
TENANT_ID     = os.getenv("TENANT_ID")
CLIENT_SECRET = os.getenv("CLIENT_SECRET")
USER_EMAIL    = os.getenv("USER_EMAIL")
SUPABASE_URL  = os.getenv("SUPABASE_URL")
SUPABASE_KEY  = os.getenv("SUPABASE_KEY")

REQUIRED_ENV_VARS = [
    "CLIENT_ID", "TENANT_ID", "CLIENT_SECRET",
    "USER_EMAIL", "SUPABASE_URL", "SUPABASE_KEY",
]

# =====================================================
# CONFIGURATION
# =====================================================

# Well-known Graph API folder ID for Sent Items (language-independent)
SENT_FOLDER_ID = "sentitems"

# Subject keyword to filter invoice emails
SUBJECT_KEYWORD = "Invoice and balance"

# Only fetch emails sent on or after this date (ISO 8601 UTC)
FETCH_FROM_DATE = "2026-01-01T00:00:00Z"

# Client domains mapped to display names
CLIENT_DOMAINS: dict[str, str] = {
    "kavidac"        : "Kavidac",
    "babia"          : "Babia",
    "produceexports" : "Produce Exports",
}

# Local folder where PDFs will be saved
OUTPUT_BASE_FOLDER = "invoices_output"

# CSV output file
CSV_OUTPUT_FILE = "invoices_report.csv"


# =====================================================
# MICROSOFT GRAPH API AUTHENTICATION
# =====================================================
def get_access_token() -> str:
    """Obtain an OAuth2 access token from Microsoft."""
    logger.info("Authenticating with Microsoft Graph API...")

    authority = f"https://login.microsoftonline.com/{TENANT_ID}"
    app = msal.ConfidentialClientApplication(
        CLIENT_ID,
        authority=authority,
        client_credential=CLIENT_SECRET,
    )

    result = app.acquire_token_for_client(
        scopes=["https://graph.microsoft.com/.default"]
    )

    if "access_token" in result:
        logger.info("Authentication successful.")
        return result["access_token"]

    raise RuntimeError(f"Authentication failed: {result.get('error_description')}")


# =====================================================
# GET SENT FOLDER ID
# =====================================================
def get_folder_id(token: str, folder_name: str) -> str:
    """Find an Outlook mail folder ID by display name."""
    logger.info("Looking for folder: '%s'...", folder_name)

    headers = {"Authorization": f"Bearer {token}"}
    url = f"https://graph.microsoft.com/v1.0/users/{USER_EMAIL}/mailFolders"

    response = requests.get(url, headers=headers)
    response.raise_for_status()
    folders = response.json().get("value", [])

    # Search top-level folders first
    top_level_names = [f["displayName"] for f in folders]
    logger.info("Top-level folders found: %s", top_level_names)

    for folder in folders:
        if folder["displayName"].lower() == folder_name.lower():
            logger.info("Found folder '%s' (ID: %s)", folder_name, folder["id"])
            return folder["id"]

    # Fall back to child folders
    for folder in folders:
        child_url = (
            f"https://graph.microsoft.com/v1.0/users/{USER_EMAIL}"
            f"/mailFolders/{folder['id']}/childFolders"
        )
        child_response = requests.get(child_url, headers=headers)
        child_response.raise_for_status()
        children = child_response.json().get("value", [])
        if children:
            logger.info(
                "Child folders of '%s': %s",
                folder["displayName"],
                [c["displayName"] for c in children],
            )
        for child in children:
            if child["displayName"].lower() == folder_name.lower():
                logger.info("Found folder '%s' (ID: %s)", folder_name, child["id"])
                return child["id"]

    raise RuntimeError(
        f"Folder '{folder_name}' not found in mailbox. "
        f"Check the logged folder names above and update SENT_FOLDER_NAME."
    )


# =====================================================
# FETCH INVOICE EMAILS
# =====================================================
def fetch_invoice_emails(token: str, folder_id: str) -> list[dict]:
    """Fetch all emails whose subject contains the invoice keyword."""
    logger.info("Fetching emails with subject containing '%s'...", SUBJECT_KEYWORD)

    # ConsistencyLevel + $count are required by Graph API when combining
    # contains() in $filter with $orderby on message properties.
    headers = {
        "Authorization"   : f"Bearer {token}",
        "ConsistencyLevel": "eventual",
    }
    # Note: $orderby cannot be combined with $filter on mail folder messages;
    # results are sorted client-side after fetching.
    url = (
        f"https://graph.microsoft.com/v1.0/users/{USER_EMAIL}"
        f"/mailFolders/{folder_id}/messages"
        f"?$filter=contains(subject,'{SUBJECT_KEYWORD}') and sentDateTime ge {FETCH_FROM_DATE}"
        f"&$select=id,subject,sentDateTime,toRecipients,hasAttachments"
        f"&$top=100"
        f"&$count=true"
    )

    emails: list[dict] = []
    while url:
        response = requests.get(url, headers=headers)
        response.raise_for_status()
        data = response.json()
        emails.extend(data.get("value", []))
        url = data.get("@odata.nextLink")  # Handle pagination

    emails.sort(key=lambda e: e.get("sentDateTime", ""), reverse=True)
    logger.info("Found %d invoice email(s).", len(emails))
    return emails


# =====================================================
# IDENTIFY CLIENT FROM RECIPIENT EMAIL
# =====================================================
def identify_client(recipient_email: str) -> tuple[str, str]:
    """Return (client_name, domain_key) derived from the recipient's email domain."""
    if not recipient_email:
        return "Unknown", "unknown"

    domain_part = recipient_email.split("@")[-1].lower()

    for domain_key, client_name in CLIENT_DOMAINS.items():
        if domain_key in domain_part:
            return client_name, domain_key

    # Unrecognised domain — use its first label as a fallback
    domain_label = domain_part.split(".")[0]
    return domain_label.capitalize(), domain_label


# =====================================================
# DOWNLOAD PDF ATTACHMENT
# =====================================================
def download_attachment(token: str, message_id: str, client_folder: str) -> list[dict]:
    """Download PDF attachments from an email and save them to the client folder."""
    headers = {"Authorization": f"Bearer {token}"}
    url = (
        f"https://graph.microsoft.com/v1.0/users/{USER_EMAIL}"
        f"/messages/{message_id}/attachments"
    )

    response = requests.get(url, headers=headers)
    response.raise_for_status()

    saved_files: list[dict] = []

    for attachment in response.json().get("value", []):
        filename = attachment.get("name", "")
        if not filename.lower().endswith(".pdf"):
            continue

        content_bytes = attachment.get("contentBytes")
        if not content_bytes:
            logger.warning("Attachment '%s' has no content — skipping.", filename)
            continue

        folder_path = Path(OUTPUT_BASE_FOLDER) / client_folder
        folder_path.mkdir(parents=True, exist_ok=True)

        file_path = folder_path / filename
        file_path.write_bytes(base64.b64decode(content_bytes))

        saved_files.append({
            "filename"      : filename,
            "filepath"      : str(file_path),
            "invoice_number": Path(filename).stem,  # strips extension cleanly
        })
        logger.info("  Saved: %s → %s", filename, folder_path)

    return saved_files


# =====================================================
# CHECK IF EMAIL ALREADY PROCESSED
# =====================================================
def is_already_processed(supabase_client: Client, message_id: str) -> bool:
    """Return True if this email has already been processed in Supabase."""
    try:
        result = (
            supabase_client.table("processed_emails")
            .select("id")
            .eq("message_id", message_id)
            .execute()
        )
        return len(result.data) > 0
    except Exception as e:
        logger.warning("Could not check processed_emails table: %s", e)
        return False


# =====================================================
# SAVE TO SUPABASE
# =====================================================
def save_to_supabase(
    supabase_client: Client,
    invoice_data: dict,
    message_id: str,
    status: str = "success",
    error: str | None = None,
) -> None:
    """Upsert an invoice record and mark the email as processed in Supabase."""
    try:
        if status == "success":
            supabase_client.table("invoices").upsert(
                invoice_data, on_conflict="email_message_id"
            ).execute()

        supabase_client.table("processed_emails").upsert(
            {
                "message_id"     : message_id,
                "subject"        : invoice_data.get("subject"),
                "recipient_email": invoice_data.get("recipient_email"),
                "status"         : status,
                "error_message"  : error,
            },
            on_conflict="message_id",
        ).execute()

    except Exception as e:
        logger.warning("Supabase save error: %s", e)


# =====================================================
# GENERATE CSV REPORT
# =====================================================
def generate_csv(invoice_records: list[dict]) -> None:
    """Write invoice records to a CSV file."""
    if not invoice_records:
        logger.warning("No invoices to write to CSV.")
        return

    fieldnames = [
        "invoice_number",
        "client_name",
        "client_domain",
        "recipient_email",
        "subject",
        "sent_date",
        "pdf_filename",
        "payment_status",
    ]

    with open(CSV_OUTPUT_FILE, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for record in invoice_records:
            writer.writerow({k: record.get(k, "") for k in fieldnames})

    logger.info("CSV report saved: %s (%d records)", CSV_OUTPUT_FILE, len(invoice_records))


# =====================================================
# MAIN SCRIPT
# =====================================================
def main() -> None:
    print("\n" + "=" * 55)
    print("  INCO Invoice Fetcher — Phase 1")
    print("=" * 55 + "\n")

    # Validate required environment variables
    missing = [v for v in REQUIRED_ENV_VARS if not os.getenv(v)]
    if missing:
        logger.error("Missing environment variables: %s", ", ".join(missing))
        logger.error("Please check your .env file.")
        return

    # Connect to Supabase
    logger.info("Connecting to Supabase...")
    supabase_client = create_client(SUPABASE_URL, SUPABASE_KEY)
    logger.info("Supabase connected.\n")

    # Authenticate with Microsoft Graph
    token = get_access_token()

    # Retrieve invoice emails from the Sent Items folder
    emails = fetch_invoice_emails(token, SENT_FOLDER_ID)
    if not emails:
        logger.warning("No invoice emails found. Check folder name and subject keyword.")
        return

    # Process each email
    invoice_records: list[dict] = []
    skipped = errors = 0

    logger.info("Processing %d email(s)...\n", len(emails))

    for email in emails:
        message_id      = email["id"]
        subject         = email.get("subject", "")
        sent_date       = email.get("sentDateTime", "")[:10]  # YYYY-MM-DD
        recipients      = email.get("toRecipients", [])
        has_attachments = email.get("hasAttachments", False)

        recipient_email = (
            recipients[0]["emailAddress"]["address"] if recipients else ""
        )

        logger.info("Processing: %s → %s (%s)", subject, recipient_email, sent_date)

        if is_already_processed(supabase_client, message_id):
            logger.info("  Already processed — skipping.\n")
            skipped += 1
            continue

        client_name, client_domain = identify_client(recipient_email)
        logger.info("  Client: %s (%s)", client_name, client_domain)

        if not has_attachments:
            logger.warning("  No attachment found — skipping.\n")
            save_to_supabase(
                supabase_client,
                {
                    "subject"         : subject,
                    "recipient_email" : recipient_email,
                    "email_message_id": message_id,
                },
                message_id,
                status="error",
                error="No attachment found",
            )
            errors += 1
            continue

        saved_files = download_attachment(token, message_id, client_domain)
        if not saved_files:
            logger.warning("  No PDF found in attachments — skipping.\n")
            errors += 1
            continue

        pdf_info = saved_files[0]
        invoice_data = {
            "invoice_number"  : pdf_info["invoice_number"],
            "client_name"     : client_name,
            "client_domain"   : client_domain,
            "recipient_email" : recipient_email,
            "subject"         : subject,
            "sent_date"       : sent_date,
            "pdf_filename"    : pdf_info["filename"],
            "payment_status"  : "unpaid",
            "email_message_id": message_id,
        }

        save_to_supabase(supabase_client, invoice_data, message_id)
        invoice_records.append(invoice_data)
        logger.info("  Saved to Supabase.\n")

    generate_csv(invoice_records)

    print("\n" + "=" * 55)
    print("  Done!")
    print(f"  Total emails found  : {len(emails)}")
    print(f"  New invoices saved  : {len(invoice_records)}")
    print(f"  Already processed   : {skipped}")
    print(f"  Errors              : {errors}")
    print("=" * 55 + "\n")


if __name__ == "__main__":
    main()
