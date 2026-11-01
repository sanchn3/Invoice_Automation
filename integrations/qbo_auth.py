"""
qbo_auth.py
===========
QuickBooks Online OAuth 2.0 token lifecycle.

Handles:
- Building the authorization URL (send user to Intuit once)
- Exchanging the authorization code for access + refresh tokens
- Auto-refreshing the access token when it nears expiry (~1 hour)
- Persisting tokens via the app's DataManager so they survive restarts

Tokens are stored in data/qbo_tokens.json (local dev) by the DataManager —
the same JSON-file / Supabase pattern used for all other app data.
"""

import base64
import logging
import time
import urllib.parse

import httpx

logger = logging.getLogger(__name__)

_AUTH_ENDPOINT  = "https://appcenter.intuit.com/connect/oauth2"
_TOKEN_ENDPOINT = "https://oauth.platform.intuit.com/oauth2/v1/tokens/bearer"
_SCOPE          = "com.intuit.quickbooks.accounting"

# Refresh this many seconds before the token actually expires
_REFRESH_BUFFER = 120


class QBOAuth:
    """
    Manages QBO OAuth 2.0 tokens for one connected company (realmId).

    Usage
    -----
    1. First time: redirect user to get_authorization_url().
       Intuit redirects back with ?code=...&realmId=...&state=qbo_connect.
    2. Call exchange_code(code, realm_id) to obtain and store tokens.
    3. From then on, call get_valid_token() — it auto-refreshes as needed.
    """

    def __init__(self, client_id: str, client_secret: str, redirect_uri: str, dm):
        """
        Parameters
        ----------
        client_id / client_secret : from your Intuit Developer app
        redirect_uri              : must match exactly what is registered in the Intuit app
        dm                        : DataManager instance (used for token persistence)
        """
        self._id  = client_id
        self._sec = client_secret
        self._uri = redirect_uri
        self._dm  = dm

    # ── Public interface ──────────────────────────────────────────────────────

    def get_authorization_url(self) -> str:
        """Return the Intuit OAuth page URL to open in the browser."""
        params = {
            "client_id"    : self._id,
            "scope"        : _SCOPE,
            "redirect_uri" : self._uri,
            "response_type": "code",
            "state"        : "qbo_connect",
        }
        return f"{_AUTH_ENDPOINT}?{urllib.parse.urlencode(params)}"

    def exchange_code(self, code: str, realm_id: str) -> None:
        """Exchange the one-time authorization code for access + refresh tokens."""
        resp = self._token_post({
            "grant_type"  : "authorization_code",
            "code"        : code,
            "redirect_uri": self._uri,
        })
        self._persist(resp, realm_id)
        logger.info("QBO connected — realmId=%s", realm_id)

    def get_valid_token(self) -> tuple[str, str]:
        """
        Return (access_token, realm_id).
        Refreshes automatically if within REFRESH_BUFFER seconds of expiry.
        Raises RuntimeError if not connected.
        """
        tokens = self._dm.get_qbo_tokens()
        if not tokens or not tokens.get("refresh_token"):
            raise RuntimeError("QuickBooks is not connected.")
        if time.time() >= tokens.get("expires_at", 0) - _REFRESH_BUFFER:
            tokens = self._do_refresh(tokens)
        return tokens["access_token"], tokens["realm_id"]

    def is_connected(self) -> bool:
        """True if a refresh token exists (connection established at least once)."""
        try:
            t = self._dm.get_qbo_tokens()
            return bool(t and t.get("refresh_token"))
        except Exception:
            return False

    # ── Private helpers ───────────────────────────────────────────────────────

    def _do_refresh(self, tokens: dict) -> dict:
        logger.info("Refreshing QBO access token")
        resp = self._token_post({
            "grant_type"   : "refresh_token",
            "refresh_token": tokens["refresh_token"],
        })
        self._persist(resp, tokens["realm_id"])
        return self._dm.get_qbo_tokens()

    def _token_post(self, form: dict) -> dict:
        creds = base64.b64encode(f"{self._id}:{self._sec}".encode()).decode()
        r = httpx.post(
            _TOKEN_ENDPOINT,
            headers={
                "Authorization": f"Basic {creds}",
                "Accept"       : "application/json",
                "Content-Type" : "application/x-www-form-urlencoded",
            },
            data=form,
            timeout=15,
        )
        r.raise_for_status()
        return r.json()

    def _persist(self, resp: dict, realm_id: str) -> None:
        existing = self._dm.get_qbo_tokens() or {}
        existing.update({
            "access_token" : resp["access_token"],
            # Intuit rotates the refresh token on every refresh — always overwrite
            "refresh_token": resp.get("refresh_token", existing.get("refresh_token", "")),
            "realm_id"     : realm_id,
            "expires_at"   : time.time() + resp.get("expires_in", 3600),
        })
        self._dm.set_qbo_tokens(existing)
