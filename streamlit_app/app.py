import os
import sys
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

# Shared instances (cached across reruns)
@st.cache_resource
def get_dm() -> DataManager:
    return DataManager()

@st.cache_resource
def get_alert_manager() -> AlertManager:
    return AlertManager()


# ── Login ─────────────────────────────────────────────────────────────────────
# Flow:
#   1. incogrp.com/staff-login (Flask) validates credentials and redirects here
#      with ?token= signed by SECRET_KEY → auto-login, no form shown.
#   2. No token + on Render → show username/password form as fallback.
#   3. Running locally → dev role selector for easy profile switching.

_IS_PRODUCTION = os.environ.get("RENDER") == "true"


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


# Step 1: auto-login from incogrp.com token redirect
_sso_token = st.query_params.get("token", "")
if _sso_token and not auth.is_authenticated():
    _td = _verify_sso_token(_sso_token)
    if _td:
        _ROLE_MAP = {"administrator": "admin"}  # normalise Flask → Streamlit role names
        _role = _ROLE_MAP.get(_td["role"], _td["role"])
        auth.login({"role": _role, "username": _td.get("username", _role)})
        st.query_params.clear()
        st.rerun()

# Step 2 / 3: no valid token — show login UI
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
