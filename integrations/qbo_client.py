"""
qbo_client.py
=============
QuickBooks Online v3 REST API — base HTTP client.

Covers:
- GET / POST / DELETE with auth headers injected automatically
- Automatic retry on 429 (rate limit) and 401 (token expired mid-call)
- intuit_tid captured from every response header and logged for support
- Full error body logged on non-2xx so QBO fault codes are traceable
- Item and deposit-account lookup with DataManager-backed cache
"""

import logging
import time
import unicodedata

import httpx

from .qbo_auth import QBOAuth, QBOAuthError

logger = logging.getLogger(__name__)

_SANDBOX_BASE = "https://sandbox-quickbooks.api.intuit.com"
_PROD_BASE    = "https://quickbooks.api.intuit.com"
_PAGE_SIZE    = 1000


def _norm(s: str) -> str:
    """Lowercase, strip, remove diacritics — for fuzzy name matching."""
    return (
        unicodedata.normalize("NFKD", (s or "").strip().lower())
        .encode("ascii", "ignore")
        .decode()
    )


class QBOClient:

    def __init__(self, auth: QBOAuth, sandbox: bool = True, dm=None):
        self._auth    = auth
        self._base    = _SANDBOX_BASE if sandbox else _PROD_BASE
        self._dm      = dm  # optional DataManager for cache; set via set_dm()

    def set_dm(self, dm) -> None:
        self._dm = dm

    # ── Internal HTTP ──────────────────────────────────────────────────────────

    def _headers(self) -> tuple[dict, str]:
        token, realm_id = self._auth.get_valid_token()
        return {"Authorization": f"Bearer {token}", "Accept": "application/json"}, realm_id

    def _log_response(self, method: str, url: str, resp: httpx.Response) -> None:
        tid = resp.headers.get("intuit_tid", "—")
        logger.debug(
            "QBO %s %s → HTTP %d  intuit_tid=%s",
            method, url, resp.status_code, tid,
        )
        if not resp.is_success:
            logger.error(
                "QBO error  intuit_tid=%s  status=%d  body=%s",
                tid, resp.status_code, resp.text[:800],
            )

    def _get(self, path: str, params: dict | None = None, _retry: bool = True) -> dict:
        headers, realm_id = self._headers()
        url = f"{self._base}/v3/company/{realm_id}/{path}"
        resp = httpx.get(url, headers=headers, params=params or {}, timeout=25)
        self._log_response("GET", url, resp)

        if resp.status_code == 429 and _retry:
            wait = float(resp.headers.get("Retry-After", "5"))
            logger.warning("QBO rate-limited; retrying in %.0fs", wait)
            time.sleep(wait)
            return self._get(path, params, _retry=False)

        if resp.status_code == 401 and _retry:
            self._auth._do_refresh(self._auth._dm.get_qbo_tokens())
            return self._get(path, params, _retry=False)

        resp.raise_for_status()
        return resp.json()

    def _post(self, path: str, body: dict, _retry: bool = True) -> dict:
        headers, realm_id = self._headers()
        headers["Content-Type"] = "application/json"
        url = f"{self._base}/v3/company/{realm_id}/{path}"
        resp = httpx.post(url, headers=headers, json=body, timeout=25)
        self._log_response("POST", url, resp)

        if resp.status_code == 429 and _retry:
            wait = float(resp.headers.get("Retry-After", "5"))
            logger.warning("QBO rate-limited; retrying in %.0fs", wait)
            time.sleep(wait)
            return self._post(path, body, _retry=False)

        if resp.status_code == 401 and _retry:
            self._auth._do_refresh(self._auth._dm.get_qbo_tokens())
            return self._post(path, body, _retry=False)

        resp.raise_for_status()
        return resp.json()

    def _delete(self, path: str, body: dict, _retry: bool = True) -> dict:
        """
        QBO deletes use POST with ?operation=delete, passing the entity with
        its current SyncToken so QBO can detect concurrent modifications.
        """
        headers, realm_id = self._headers()
        headers["Content-Type"] = "application/json"
        url = f"{self._base}/v3/company/{realm_id}/{path}?operation=delete"
        resp = httpx.post(url, headers=headers, json=body, timeout=25)
        self._log_response("DELETE", url, resp)

        if resp.status_code == 429 and _retry:
            wait = float(resp.headers.get("Retry-After", "5"))
            logger.warning("QBO rate-limited; retrying in %.0fs", wait)
            time.sleep(wait)
            return self._delete(path, body, _retry=False)

        if resp.status_code == 401 and _retry:
            self._auth._do_refresh(self._auth._dm.get_qbo_tokens())
            return self._delete(path, body, _retry=False)

        resp.raise_for_status()
        return resp.json()

    def query(self, sql: str) -> list[dict]:
        """Run a QBO SQL query and return the rows from QueryResponse."""
        data = self._get("query", {"query": sql})
        qr   = data.get("QueryResponse", {})
        # QueryResponse contains either a named key (entity type) or is empty
        for key, val in qr.items():
            if isinstance(val, list):
                return val
        return []

    # ── Customer resolution ────────────────────────────────────────────────────

    def find_customer_candidates(
        self,
        client_name: str,
        client_email: str | None,
    ) -> list[dict]:
        """
        Return matching QBO Customer records.
        []    → no match
        [one] → definitive match
        [many]→ ambiguous; caller should show a picker
        """
        customers = self._fetch_all_customers()

        if client_email:
            norm_email = client_email.strip().lower()
            hits = []
            for c in customers:
                raw = (c.get("PrimaryEmailAddr") or {}).get("Address", "")
                if any(part.strip().lower() == norm_email for part in raw.split(",")):
                    hits.append(c)
            if hits:
                return hits

        norm_name = _norm(client_name)
        return [
            c for c in customers
            if _norm(c.get("DisplayName", "")) == norm_name
            or _norm(c.get("CompanyName") or "") == norm_name
        ]

    def _fetch_all_customers(self) -> list[dict]:
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

    # ── Item lookup (with DataManager cache) ──────────────────────────────────

    def get_item_ref(self, fully_qualified_name: str) -> dict:
        """
        Return {"value": QBO_Item_Id, "name": FullyQualifiedName}.
        Caches results in DataManager so each item is only fetched once.
        Raises ValueError if the item is not found in QBO.
        """
        if self._dm:
            cache = self._dm.get_qbo_cache()
            cached_id = cache.get("item_ids", {}).get(fully_qualified_name)
            if cached_id:
                return {"value": cached_id, "name": fully_qualified_name}

        escaped = fully_qualified_name.replace("'", "\\'")
        rows = self.query(
            f"SELECT Id, FullyQualifiedName FROM Item "
            f"WHERE FullyQualifiedName = '{escaped}'"
        )
        if not rows:
            raise ValueError(
                f"QBO item not found: {fully_qualified_name!r}. "
                "Verify the product exists in QuickBooks with that exact name."
            )
        item_id = rows[0]["Id"]

        if self._dm:
            self._dm.update_qbo_cache({"item_ids": {fully_qualified_name: item_id}})

        return {"value": item_id, "name": fully_qualified_name}

    # ── Deposit account lookup (with DataManager cache) ───────────────────────

    def get_deposit_account_ref(
        self,
        account_id: str = "",
        account_name: str = "Undeposited Funds",
    ) -> dict:
        """
        Return {"value": QBO_Account_Id, "name": account_name}.
        Uses account_id directly if provided; otherwise looks up by name.
        Caches in DataManager.
        """
        if account_id:
            return {"value": account_id, "name": account_name}

        if self._dm:
            cache = self._dm.get_qbo_cache()
            dep   = cache.get("deposit_account")
            if dep:
                return dep

        escaped = account_name.replace("'", "\\'")
        rows = self.query(
            f"SELECT Id, Name FROM Account WHERE Name = '{escaped}'"
        )
        if not rows:
            raise ValueError(
                f"QBO deposit account not found: {account_name!r}. "
                "Set QBO_DEPOSIT_ACCOUNT_ID or QBO_DEPOSIT_ACCOUNT in environment."
            )
        ref = {"value": rows[0]["Id"], "name": rows[0]["Name"]}

        if self._dm:
            self._dm.update_qbo_cache({"deposit_account": ref})

        return ref

    # ── A/R Aging Detail report ───────────────────────────────────────────────

    def get_ar_aging_detail(self, customer_id: str) -> dict:
        """Fetch AgedReceivableDetail JSON for a single customer."""
        from datetime import date
        return self._get(
            "reports/AgedReceivableDetail",
            {
                "customer"    : customer_id,
                "report_date" : date.today().isoformat(),
                "minorversion": "73",
            },
        )
