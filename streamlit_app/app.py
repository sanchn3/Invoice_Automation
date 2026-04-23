import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import streamlit as st

from data_manager import DataManager
from alerting.alert_manager import AlertManager
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

dm            = get_dm()
alert_manager = get_alert_manager()

# Sidebar navigation
st.sidebar.title("📦 Invoice Automation")
st.sidebar.markdown("---")
view = st.sidebar.radio(
    "Navigate",
    options=["Forklift Operator", "Administrator", "Lead", "Accounting"],
    label_visibility="collapsed",
)
st.sidebar.markdown("---")
st.sidebar.caption("INCO Logistics • Invoice Automation v1.0")

if view == "Forklift Operator":
    worker_form.render(dm, alert_manager)
elif view == "Lead":
    lead_dashboard.render(dm, alert_manager)
elif view == "Accounting":
    accounting_dashboard.render(dm, alert_manager)
else:
    admin_dashboard.render(dm, alert_manager)
