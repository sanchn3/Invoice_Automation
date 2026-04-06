import streamlit as st

from data_manager import DataManager
from alerting.alert_manager import AlertManager
from streamlit_app.views import admin_dashboard, worker_form

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
    options=["Admin Dashboard", "Worker Form"],
    label_visibility="collapsed",
)
st.sidebar.markdown("---")
st.sidebar.caption("INCO Logistics • Invoice Automation v1.0")

if view == "Admin Dashboard":
    admin_dashboard.render(dm)
else:
    worker_form.render(dm, alert_manager)
