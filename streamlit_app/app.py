import os
import sys
import uuid
from datetime import datetime, timezone, timedelta
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv
load_dotenv()

import streamlit as st

from data_manager import DataManager
from alerting.alert_manager import AlertManager
from streamlit_app import auth
from streamlit_app.views import admin_dashboard, worker_form, lead_dashboard, accounting_dashboard

st.set_page_config(
    page_title="INCO Invoice Automation",
    page_icon="📦",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ── Shared instances (cached across reruns) ───────────────────────────────────
@st.cache_resource
def get_dm() -> DataManager:
    return DataManager()

@st.cache_resource
def get_alert_manager() -> AlertManager:
    return AlertManager()

# ── Server-side session store (survives page refreshes via URL token) ─────────
# Token lives in ?s=TOKEN in the URL, which persists across browser refreshes.
@st.cache_resource
def _session_store() -> dict:
    return {}

_SESSION_TTL_HOURS = 8
_IS_PRODUCTION = os.environ.get("RENDER") == "true"


def _create_session(role: str, username: str) -> str:
    token = str(uuid.uuid4())
    _session_store()[token] = {
        "role"    : role,
        "username": username,
        "expires" : (datetime.now(timezone.utc) + timedelta(hours=_SESSION_TTL_HOURS)).isoformat(),
    }
    return token


def _get_session(token: str) -> dict | None:
    store = _session_store()
    sess  = store.get(token)
    if not sess:
        return None
    if datetime.fromisoformat(sess["expires"]) < datetime.now(timezone.utc):
        store.pop(token, None)
        return None
    return sess


def _delete_session(token: str) -> None:
    _session_store().pop(token, None)


def _verify_sso_token(token: str) -> dict | None:
    """Verify the itsdangerous token issued by the incogrp.com Flask login."""
    from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired
    _secret = os.environ.get("SECRET_KEY", "")
    if not _secret:
        return None
    try:
        data = URLSafeTimedSerializer(_secret).loads(token, max_age=300)
        return data if isinstance(data, dict) and data.get("role") else None
    except (BadSignature, SignatureExpired):
        return None


# ── Restore session from URL token on refresh ─────────────────────────────────
_url_token = st.query_params.get("s", "")
if _url_token and not auth.is_authenticated():
    _sess = _get_session(_url_token)
    if _sess:
        auth.login({"role": _sess["role"], "username": _sess["username"]})


# ── SSO token from incogrp.com redirect ───────────────────────────────────────
_sso_token = st.query_params.get("token", "")
if _sso_token and not auth.is_authenticated():
    _td = _verify_sso_token(_sso_token)
    if _td:
        _ROLE_MAP = {"administrator": "admin"}
        _role  = _ROLE_MAP.get(_td["role"], _td["role"])
        _uname = _td.get("username", _role)
        auth.login({"role": _role, "username": _uname})
        _tok = _create_session(_role, _uname)
        st.query_params.clear()
        st.query_params["s"] = _tok
        st.rerun()


# ── Login UI ──────────────────────────────────────────────────────────────────
if not auth.is_authenticated():
    if _IS_PRODUCTION:
        st.markdown(
            "<h1 style='text-align:center;padding-top:3rem;'>📦 INCO Staff Portal</h1>",
            unsafe_allow_html=True,
        )
        st.markdown("<br>", unsafe_allow_html=True)
        _, col, _ = st.columns([1, 1.2, 1])
        with col:
            with st.container(border=True):
                st.markdown("#### Sign In")
                _username = st.text_input("Username", key="login_user")
                _password = st.text_input("Password", type="password", key="login_pass")
                if st.button("Sign In", type="primary", use_container_width=True):
                    _user = auth.verify_login(_username.strip(), _password)
                    if _user:
                        auth.login(_user)
                        _tok = _create_session(_user["role"], _user["username"])
                        st.query_params["s"] = _tok
                        st.rerun()
                    else:
                        st.error("Invalid username or password.")
        st.stop()
    else:
        st.title("📦 INCO — Local Dev Login")
        role_choice = st.selectbox("Sign in as", ["admin", "lead", "accounting", "operator"])
        if st.button("Sign In", type="primary"):
            auth.login({"role": role_choice, "username": role_choice})
            st.rerun()
        st.stop()


# ── Authenticated: sidebar + routing ─────────────────────────────────────────

dm            = get_dm()
alert_manager = get_alert_manager()

role     = auth.current_role()
username = st.session_state.get("_auth_username", "")

st.sidebar.title("📦 INCO")
st.sidebar.markdown(f"**{auth.ROLE_LABELS.get(role, role)}**  \n`{username}`")
st.sidebar.markdown("---")

if st.sidebar.button("🚪 Sign Out", use_container_width=True):
    auth.logout()
    _delete_session(_url_token)
    if _IS_PRODUCTION:
        import streamlit.components.v1 as _cv1
        _cv1.html(
            '<script>window.top.location.href = "https://incogrp.com/staff-login";</script>',
            height=0,
        )
        st.stop()
    else:
        st.rerun()

st.sidebar.markdown("---")
st.sidebar.caption("INCO Logistics • Invoice Automation v1.0")

# Route to the dashboard matching the user's role
if role == "admin":
    admin_dashboard.render(dm, alert_manager)
elif role == "accounting":
    accounting_dashboard.render(dm, alert_manager)
elif role == "operator":
    worker_form.render(dm, alert_manager)
elif role == "lead":
    lead_dashboard.render(dm, alert_manager)
else:
    st.error(f"Unknown role: {role!r}. Please contact your administrator.")
