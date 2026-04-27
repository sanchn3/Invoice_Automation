"""
auth.py
=======
Lightweight authentication for the INCO Streamlit app.

Users are stored in data/users.json:
  [{"username": "...", "password_hash": "...", "salt": "...", "role": "..."}]

Roles map to dashboards:
  admin       → Administrator
  accounting  → Accounting
  operator    → Forklift Operator
  lead        → Lead
"""

import hashlib
import json
import os
import secrets
from pathlib import Path

import streamlit as st

_USERS_FILE = Path(__file__).parent.parent / "data" / "users.json"

ROLE_LABELS = {
    "admin"      : "Administrator",
    "accounting" : "Accounting",
    "operator"   : "Operator",
    "lead"       : "Lead",
}


# ── Credential helpers ────────────────────────────────────────────────────────

def _hash_password(password: str, salt: str) -> str:
    return hashlib.sha256((salt + password).encode()).hexdigest()


def _load_users() -> list[dict]:
    if not _USERS_FILE.exists():
        return []
    try:
        return json.loads(_USERS_FILE.read_text(encoding="utf-8"))
    except Exception:
        return []


def verify_login(username: str, password: str) -> dict | None:
    """
    Return the user record if credentials are valid, else None.
    """
    for user in _load_users():
        if user.get("username", "").lower() == username.strip().lower():
            expected = _hash_password(password, user.get("salt", ""))
            if secrets.compare_digest(expected, user.get("password_hash", "")):
                return user
    return None


def create_user(username: str, password: str, role: str) -> None:
    """
    Add or overwrite a user in data/users.json.
    Call this once from a setup script or the Streamlit admin panel.
    """
    users = [u for u in _load_users() if u.get("username", "").lower() != username.lower()]
    salt = secrets.token_hex(16)
    users.append({
        "username"     : username.lower(),
        "password_hash": _hash_password(password, salt),
        "salt"         : salt,
        "role"         : role,
    })
    _USERS_FILE.parent.mkdir(parents=True, exist_ok=True)
    _USERS_FILE.write_text(json.dumps(users, indent=2), encoding="utf-8")


# ── Session state helpers ─────────────────────────────────────────────────────

def is_authenticated() -> bool:
    return bool(st.session_state.get("_auth_role"))


def current_role() -> str | None:
    return st.session_state.get("_auth_role")


def login(user: dict) -> None:
    st.session_state["_auth_role"]     = user["role"]
    st.session_state["_auth_username"] = user["username"]


def logout() -> None:
    for key in ["_auth_role", "_auth_username"]:
        st.session_state.pop(key, None)
