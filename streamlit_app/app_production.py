import os
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv
load_dotenv()

import streamlit as st
import streamlit.components.v1 as _components

# ── Production sign-out destination — DO NOT CHANGE ──────────────────────────
_SIGN_OUT_URL = "https://incogrp.com/staff-login"

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

# Disable browser autocomplete on every input across the entire app.
_components.html(
    """
    <script>
    (function () {
        function off() {
            window.parent.document.querySelectorAll('input').forEach(function (el) {
                if (el.getAttribute('autocomplete') !== 'off') {
                    el.setAttribute('autocomplete', 'off');
                }
            });
        }
        off();
        new MutationObserver(off).observe(
            window.parent.document.body,
            { childList: true, subtree: true }
        );
    })();
    </script>
    """,
    height=0,
)

# Shared instances (cached across reruns)
@st.cache_resource
def get_dm() -> DataManager:
    return DataManager()

@st.cache_resource
def get_alert_manager() -> AlertManager:
    return AlertManager()


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
    auth.logout()
    _components.html(
        f'<script>window.top.location.replace("{_SIGN_OUT_URL}");</script>',
        height=1,
    )
    st.markdown(
        f'Signed out. <a href="{_SIGN_OUT_URL}" target="_top">'
        f"Return to staff login →</a>",
        unsafe_allow_html=True,
    )
    st.stop()

st.sidebar.markdown("---")
st.sidebar.caption("INCO Logistics • Invoice Automation v1.02")

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
