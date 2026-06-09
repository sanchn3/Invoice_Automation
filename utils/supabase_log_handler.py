"""
supabase_log_handler.py
=======================
A Python logging.Handler that writes ERROR (and above) log records
to the logs.invoice_logger table in Supabase.

Usage — call setup_supabase_logging() once at app startup:

    from utils.supabase_log_handler import setup_supabase_logging
    setup_supabase_logging()
"""

import json
import logging
import os
import traceback
from urllib.parse import urlparse

import httpx

from utils.dns_fix import ensure_host_reachable


class SupabaseLogHandler(logging.Handler):
    """Sends ERROR+ log records to logs.invoice_logger via PostgREST."""

    _ENDPOINT = None

    def __init__(self):
        super().__init__(level=logging.ERROR)

    def _endpoint(self) -> str | None:
        url = os.environ.get("SUPABASE_URL", "").rstrip("/")
        key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")
        if not url or not key:
            return None
        return f"{url}/rest/v1/invoice_logger"

    def _headers(self) -> dict:
        key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")
        return {
            "apikey"         : key,
            "Authorization"  : f"Bearer {key}",
            "Content-Type"   : "application/json",
            "Content-Profile": "logs",
            "Prefer"         : "return=minimal",
        }

    def emit(self, record: logging.LogRecord) -> None:
        url = os.environ.get("SUPABASE_URL", "")
        host = urlparse(url).hostname
        if host:
            ensure_host_reachable(host)
        endpoint = self._endpoint()
        if not endpoint:
            return
        exc_text = None
        if record.exc_info:
            exc_text = "".join(traceback.format_exception(*record.exc_info))
        payload = {
            "level"    : record.levelname,
            "logger"   : record.name,
            "message"  : self.format(record),
            "module"   : record.module,
            "func_name": record.funcName,
            "line_no"  : record.lineno,
            "exc_info" : exc_text,
        }
        try:
            httpx.post(
                endpoint,
                headers=self._headers(),
                content=json.dumps(payload),
                timeout=5,
            )
        except Exception:
            # Never let the log handler crash the app
            pass


def setup_supabase_logging() -> None:
    """
    Attach the SupabaseLogHandler to the root logger.
    Only ERROR and above are sent to Supabase.
    Call once at app startup.
    """
    root = logging.getLogger()
    # Avoid adding duplicate handlers on Streamlit reruns
    if not any(isinstance(h, SupabaseLogHandler) for h in root.handlers):
        root.addHandler(SupabaseLogHandler())
