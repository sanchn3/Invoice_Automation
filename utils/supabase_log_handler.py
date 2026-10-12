"""
supabase_log_handler.py
=======================
A Python logging.Handler that writes ERROR (and above) log records
to the ``logs.invoice_logger`` table in Supabase.

Prerequisite — the ``logs`` schema must be in Supabase's exposed-schemas
list (Dashboard → Settings → API → "Exposed schemas").  If the schema is
not exposed, PostgREST returns HTTP 406 on every write.  The handler
contains a circuit-breaker that disables itself after 3 consecutive
failures so it cannot generate unbounded network traffic.

Usage — call setup_supabase_logging() once at app startup:

    from utils.supabase_log_handler import setup_supabase_logging
    setup_supabase_logging()
"""

import json
import logging
import os
import sys
import traceback
from urllib.parse import urlparse

import httpx

from utils.dns_fix import ensure_host_reachable

# How many consecutive POST failures before the handler disables itself.
_CIRCUIT_BREAKER_THRESHOLD = 3


class SupabaseLogHandler(logging.Handler):
    """
    Sends ERROR+ log records to logs.invoice_logger via PostgREST.

    Circuit breaker: after _CIRCUIT_BREAKER_THRESHOLD consecutive failures
    the handler silently disables itself for the lifetime of the process so
    it cannot generate unbounded Supabase traffic when the table/schema is
    misconfigured.
    """

    def __init__(self):
        super().__init__(level=logging.ERROR)
        self._consecutive_failures: int = 0

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
            "Content-Profile": "logs",   # targets the 'logs' schema in PostgREST
            "Prefer"         : "return=minimal",
        }

    def emit(self, record: logging.LogRecord) -> None:
        # Circuit breaker: stop trying after repeated failures.
        if self._consecutive_failures >= _CIRCUIT_BREAKER_THRESHOLD:
            return

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
            resp = httpx.post(
                endpoint,
                headers=self._headers(),
                content=json.dumps(payload),
                timeout=5,
            )
            if resp.status_code in (200, 201, 204):
                self._consecutive_failures = 0   # reset on success
            else:
                self._consecutive_failures += 1
                if self._consecutive_failures >= _CIRCUIT_BREAKER_THRESHOLD:
                    print(
                        f"[supabase_log_handler] Disabled after "
                        f"{_CIRCUIT_BREAKER_THRESHOLD} consecutive failures "
                        f"(last HTTP {resp.status_code}). "
                        f"Fix: expose the 'logs' schema in Supabase → Settings → API → "
                        f"Exposed Schemas, then redeploy.",
                        file=sys.stderr,
                    )
        except Exception:
            self._consecutive_failures += 1
            # Never let the log handler crash the app.


def validate_supabase_logging() -> bool:
    """
    Probe the invoice_logger endpoint once at startup.
    Prints a plain-text warning to stderr (visible in Render logs) if the
    table or schema is not reachable.  Returns True if the probe succeeds.
    """
    url = os.environ.get("SUPABASE_URL", "").rstrip("/")
    key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")
    if not url or not key:
        return False
    try:
        resp = httpx.get(
            f"{url}/rest/v1/invoice_logger?limit=1",
            headers={
                "apikey"        : key,
                "Authorization" : f"Bearer {key}",
                "Accept-Profile": "logs",
            },
            timeout=5,
        )
        if resp.status_code == 200:
            return True
        print(
            f"[supabase_log_handler] invoice_logger probe failed (HTTP {resp.status_code}). "
            f"Error logs will NOT be written to Supabase. "
            f"Fix: go to Supabase → Settings → API → Exposed Schemas and add 'logs'.",
            file=sys.stderr,
        )
        return False
    except Exception as exc:
        print(
            f"[supabase_log_handler] invoice_logger probe exception: {exc}",
            file=sys.stderr,
        )
        return False


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
    validate_supabase_logging()
