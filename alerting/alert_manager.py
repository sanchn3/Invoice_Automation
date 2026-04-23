"""
alert_manager.py
================
Sends email alerts to admin via Microsoft Graph API.
Uses client-credentials OAuth2 flow directly with requests.
"""

import logging
import requests

from config import MS_CLIENT_ID, MS_CLIENT_SECRET, MS_TENANT_ID, ADMIN_EMAIL, WORKER_EMAIL

logger = logging.getLogger(__name__)

_TOKEN_URL = f"https://login.microsoftonline.com/{MS_TENANT_ID}/oauth2/v2.0/token"
_SEND_MAIL_URL = f"https://graph.microsoft.com/v1.0/users/{WORKER_EMAIL}/sendMail"


class AlertManager:
    def __init__(self) -> None:
        self._token: str | None = None

    def _get_token(self) -> str:
        resp = requests.post(
            _TOKEN_URL,
            data={
                "grant_type"   : "client_credentials",
                "client_id"    : MS_CLIENT_ID,
                "client_secret": MS_CLIENT_SECRET,
                "scope"        : "https://graph.microsoft.com/.default",
            },
            timeout=15,
        )
        resp.raise_for_status()
        return resp.json()["access_token"]

    def _send_email(self, subject: str, body: str) -> None:
        if not ADMIN_EMAIL or not WORKER_EMAIL:
            logger.warning("Alert skipped — ADMIN_EMAIL or WORKER_EMAIL not configured.")
            return
        try:
            token = self._get_token()
            payload = {
                "message": {
                    "subject": f"[Invoice Automation] {subject}",
                    "body"   : {"contentType": "Text", "content": body},
                    "toRecipients": [
                        {"emailAddress": {"address": ADMIN_EMAIL}}
                    ],
                },
                "saveToSentItems": False,
            }
            resp = requests.post(
                _SEND_MAIL_URL,
                json=payload,
                headers={"Authorization": f"Bearer {token}"},
                timeout=15,
            )
            resp.raise_for_status()
            logger.info("Alert sent: %s", subject)
        except Exception as e:
            logger.warning("Failed to send alert '%s': %s", subject, e)

    def new_invoice_received(self, provider: str, client: str) -> None:
        self._send_email(
            subject=f"New invoice received — {client}",
            body=(
                f"A new provider invoice has been received and parsed successfully.\n\n"
                f"Provider : {provider}\n"
                f"Client   : {client}\n\n"
                f"Log in to the admin dashboard to review and set service details."
            ),
        )

    def parsing_failed(self, subject: str, email_log_id: str) -> None:
        self._send_email(
            subject="Invoice parsing failed — manual review needed",
            body=(
                f"An invoice email could not be parsed automatically.\n\n"
                f"Email subject : {subject}\n"
                f"Log ID        : {email_log_id}\n\n"
                f"Please review this email manually in the admin dashboard."
            ),
        )

    def worker_submitted(self, client_name: str, job_id: str) -> None:
        self._send_email(
            subject=f"Worker submitted job — {client_name}",
            body=(
                f"A warehouse worker has submitted job completion details.\n\n"
                f"Client : {client_name}\n"
                f"Job ID : {job_id}\n\n"
                f"The invoice is now ready for your review and approval."
            ),
        )

    def invoice_stuck(self, invoice_id: str, status: str, hours: int) -> None:
        self._send_email(
            subject=f"Invoice stuck in '{status}' for {hours}h",
            body=(
                f"An invoice has not progressed in over {hours} hours.\n\n"
                f"Invoice ID : {invoice_id}\n"
                f"Status     : {status}\n\n"
                f"Please review this invoice in the admin dashboard."
            ),
        )

    def duplicate_detected(self, invoice_number: str, provider: str) -> None:
        self._send_email(
            subject=f"Duplicate invoice detected — {invoice_number}",
            body=(
                f"A duplicate invoice was detected and has not been processed.\n\n"
                f"Invoice number : {invoice_number}\n"
                f"Provider       : {provider}\n\n"
                f"Please verify this is not a legitimate re-send."
            ),
        )

    def unknown_provider(self, sender: str) -> None:
        self._send_email(
            subject=f"Email from unknown provider — {sender}",
            body=(
                f"An email arrived from a sender not registered as a provider.\n\n"
                f"Sender : {sender}\n\n"
                f"If this is a new provider, add them to providers.json and re-process."
            ),
        )
