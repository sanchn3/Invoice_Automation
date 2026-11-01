"""
qbo_client.py
=============
QuickBooks Online REST API wrapper.

Covers:
- Paginated customer fetch + matching (email-first, then normalized name)
- A/R Aging Detail report for a single customer
- Automatic retry on HTTP 429 (rate limit) and 401 (token just expired mid-call)
"""

import logging
import time
import unicodedata
from datetime import date

import httpx

from .qbo_auth import QBOAuth

logger = logging.getLogger(__name__)

_SANDBOX_BASE = "https://sandbox-quickbooks.api.intuit.com"
_PROD_BASE    = "https://quickbooks.api.intuit.com"
_PAGE_SIZE    = 1000


def _norm(s: str) -> str:
    """Lowercase, strip, and remove diacritics for fuzzy name matching."""
    return (
        unicodedata.normalize("NFKD", (s or "").strip().lower())
        .encode("ascii", "ignore")
        .decode()
    )


class QBOClient:

    def __init__(self, auth: QBOAuth, sandbox: bool = True):
        self._auth = auth
        self._base = _SANDBOX_BASE if sandbox else _PROD_BASE

    # ── Internal HTTP ─────────────────────────────────────────────────────────

    def _headers_and_realm(self) -> tuple[dict, str]:
        token, realm_id = self._auth.get_valid_token()
        return {"Authorization": f"Bearer {token}", "Accept": "application/json"}, realm_id

    def _get(self, path: str, params: dict | None = None, _retry: bool = True) -> dict:
        headers, realm_id = self._headers_and_realm()
        url = f"{self._base}/v3/company/{realm_id}/{path}"
        resp = httpx.get(url, headers=headers, params=params or {}, timeout=25)

        if resp.status_code == 429 and _retry:
            wait = float(resp.headers.get("Retry-After", "5"))
            logger.warning("QBO rate-limited; retrying in %.0fs", wait)
            time.sleep(wait)
            return self._get(path, params, _retry=False)

        if resp.status_code == 401 and _retry:
            # Token expired between validity check and the actual call — refresh once
            self._auth._do_refresh(self._auth._dm.get_qbo_tokens())
            return self._get(path, params, _retry=False)

        resp.raise_for_status()
        return resp.json()

    # ── Customer resolution ───────────────────────────────────────────────────

    def find_customer_candidates(
        self,
        client_name: str,
        client_email: str | None,
    ) -> list[dict]:
        """
        Return matching QBO Customer records.

        Returns
        -------
        []        → no match found
        [one]     → definitive single match
        [many]    → ambiguous; caller should show a picker
        """
        customers = self._fetch_all_customers()

        # Pass 1: email match (case-insensitive; QB may store comma-separated emails)
        if client_email:
            norm_email = client_email.strip().lower()
            hits = []
            for c in customers:
                raw = (c.get("PrimaryEmailAddr") or {}).get("Address", "")
                if any(part.strip().lower() == norm_email for part in raw.split(",")):
                    hits.append(c)
            if hits:
                return hits

        # Pass 2: normalized name match on DisplayName or CompanyName
        norm_name = _norm(client_name)
        return [
            c for c in customers
            if _norm(c.get("DisplayName", "")) == norm_name
            or _norm(c.get("CompanyName") or "") == norm_name
        ]

    def _fetch_all_customers(self) -> list[dict]:
        """Load all active QBO customers with pagination."""
        all_customers: list[dict] = []
        start = 1
        while True:
            q = (
                f"select Id, DisplayName, CompanyName, PrimaryEmailAddr "
                f"from Customer where Active = true "
                f"STARTPOSITION {start} MAXRESULTS {_PAGE_SIZE}"
            )
            data = self._get("query", {"query": q})
            page = data.get("QueryResponse", {}).get("Customer", [])
            all_customers.extend(page)
            if len(page) < _PAGE_SIZE:
                break
            start += _PAGE_SIZE
        logger.debug("Loaded %d active QBO customers", len(all_customers))
        return all_customers

    # ── A/R Aging Detail report ───────────────────────────────────────────────

    def get_ar_aging_detail(self, customer_id: str) -> dict:
        """
        Fetch the raw AgedReceivableDetail JSON for a single customer.
        report_date defaults to today so the caller always gets current data.
        """
        return self._get(
            "reports/AgedReceivableDetail",
            {
                "customer"    : customer_id,
                "report_date" : date.today().isoformat(),
                "minorversion": "73",
            },
        )
