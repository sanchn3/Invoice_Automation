"""
auth.py
=======
Lightweight authentication for the INCO Streamlit app.

Users are stored in the Supabase `staff_users` table.
To change a password, run this in the Supabase SQL editor:
    SELECT set_staff_password('username', 'NewPassword123');

Roles map to dashboards:
  admin       → Administrator
  accounting  → Accounting
  operator    → Forklift Operator
  lead        → Lead
"""

import hashlib
import os
import secrets

import httpx

_SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
_SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")

ROLE_LABELS = {
    "admin"      : "Administrator",
    "accounting" : "Accounting",
    "operator"   : "Operator",
    "lead"       : "Lead",
}


# ── Supabase helpers ──────────────────────────────────────────────────────────

def _sb_headers() -> dict:
    return {
        "apikey"       : _SUPABASE_KEY,
        "Authorization": f"Bearer {_SUPABASE_KEY}",
        "Content-Type" : "application/json",
    }


def _load_users() -> list[dict]:
    """Fetch all staff_users rows from Supabase."""
    try:
        resp = httpx.get(
            f"{_SUPABASE_URL}/rest/v1/staff_users",
            headers=_sb_headers(),
            params={"select": "username,password_hash,salt,role"},
            timeout=10,
        )
        resp.raise_for_status()
        return resp.json()
    except Exception:
        return []


# ── Credential helpers ────────────────────────────────────────────────────────

def _hash_password(password: str, salt: str) -> str:
    return hashlib.sha256((salt + password).encode()).hexdigest()


def verify_login(username: str, password: str) -> dict | None:
    """Return the user record if credentials are valid, else None."""
    for user in _load_users():
        if user.get("username", "").lower() == username.strip().lower():
            expected = _hash_password(password, user.get("salt", ""))
            if secrets.compare_digest(expected, user.get("password_hash", "")):
                return user
    return None


def create_user(username: str, password: str, role: str) -> None:
    """Add or overwrite a user in Supabase staff_users."""
    salt = secrets.token_hex(16)
    payload = {
        "username"     : username.lower(),
        "password_hash": _hash_password(password, salt),
        "salt"         : salt,
        "role"         : role,
    }
    headers = {**_sb_headers(), "Prefer": "resolution=merge-duplicates,return=representation"}
    httpx.post(
        f"{_SUPABASE_URL}/rest/v1/staff_users",
        headers=headers,
        json=payload,
        timeout=10,
    ).raise_for_status()


# ── Session state helpers ─────────────────────────────────────────────────────

def is_authenticated() -> bool:
    import streamlit as st
    return bool(st.session_state.get("_auth_role"))


def current_role() -> str | None:
    import streamlit as st
    return st.session_state.get("_auth_role")


def login(user: dict) -> None:
    import streamlit as st
    st.session_state["_auth_role"]     = user["role"]
    st.session_state["_auth_username"] = user["username"]


def logout() -> None:
    import streamlit as st
    for key in ["_auth_role", "_auth_username"]:
        st.session_state.pop(key, None)
