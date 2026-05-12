import os
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv
load_dotenv()

import streamlit as st
from streamlit_cookies_manager import EncryptedCookieManager

from data_manager import DataManager
from alerting.alert_manager import AlertManager
from streamlit_app import auth
from streamlit_app.views import admin_dashboard, worker_form, lead_dashboard, accounting_dashboard

st.set_page_config(
    page_title="INCO Staff Portal",
    page_icon="📦",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ── Persistent cookie session ─────────────────────────────────────────────────

_cookies = EncryptedCookieManager(
    prefix="inco_",
    password=os.environ.get("SECRET_KEY", "dev-fallback-key"),
)
if not _cookies.ready():
    st.stop()

# Shared instances (cached across reruns)
@st.cache_resource
def get_dm() -> DataManager:
    return DataManager()

@st.cache_resource
def get_alert_manager() -> AlertManager:
    return AlertManager()


def _save_cookie(user: dict) -> None:
    _cookies["role"]     = user["role"]
    _cookies["username"] = user["username"]
    _cookies.save()


def _clear_cookie() -> None:
    _cookies["role"]     = ""
    _cookies["username"] = ""
    _cookies.save()


# ── Restore session from cookie on refresh ────────────────────────────────────

if not auth.is_authenticated():
    _saved_role = _cookies.get("role", "")
    _saved_user = _cookies.get("username", "")
    if _saved_role and _saved_user:
        auth.login({"role": _saved_role, "username": _saved_user})


# ── Login form ────────────────────────────────────────────────────────────────

if not auth.is_authenticated():
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
            if st.button("Sign In", type="primary", use_container_width=True, key="login_btn"):
                _user = auth.verify_login(_username.strip(), _password)
                if _user:
                    auth.login(_user)
                    _save_cookie(_user)
                    st.rerun()
                else:
                    st.error("Invalid username or password.")
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
    _clear_cookie()
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
