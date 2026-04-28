import os
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

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


# Flask role names → Streamlit role names
_FLASK_ROLE_MAP = {
    "administrator": "admin",
    "lead":          "lead",
    "accounting":    "accounting",
    "operator":      "operator",
}


def _validate_flask_token(token: str) -> dict | None:
    """Validate a URLSafeTimedSerializer token issued by the Flask /api/login endpoint."""
    secret = os.environ.get("SECRET_KEY", "")
    if not secret:
        return None
    try:
        from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired
        s    = URLSafeTimedSerializer(secret)
        data = s.loads(token, max_age=300)   # expires after 5 minutes
        role = _FLASK_ROLE_MAP.get(data.get("role", ""))
        if not role:
            return None
        return {"username": data["role"], "role": role}
    except Exception:
        return None


# ── Token auto-login (from HTML login page) ───────────────────────────────────

if not auth.is_authenticated():
    token = st.query_params.get("token")
    if token:
        user = _validate_flask_token(token)
        if user:
            auth.login(user)
            st.query_params.clear()
            st.rerun()


# ── Auth gate (fallback manual login) ────────────────────────────────────────

if not auth.is_authenticated():
    st.markdown(
        "<h2 style='text-align:center;margin-top:3rem;'>📦 INCO Staff Portal</h2>",
        unsafe_allow_html=True,
    )
    st.markdown("<p style='text-align:center;color:#888;'>Sign in to continue</p>", unsafe_allow_html=True)
    st.markdown("---")

    col_l, col_c, col_r = st.columns([1, 1, 1])
    with col_c:
        with st.form("login_form"):
            username  = st.text_input("Username")
            password  = st.text_input("Password", type="password")
            submitted = st.form_submit_button("Sign In", type="primary", use_container_width=True)

        if submitted:
            user = auth.verify_login(username, password)
            if user:
                auth.login(user)
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
