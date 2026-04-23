import streamlit as st

STATUS_COLORS = {
    "received"        : "#6c757d",
    "parsed"          : "#17a2b8",
    "pending_review"  : "#ffc107",
    "pending_worker"  : "#007bff",
    "ready_to_invoice": "#fd7e14",
    "invoiced"        : "#28a745",
    "exported_to_qb"  : "#6f42c1",
    "error"           : "#dc3545",
}

STATUS_ORDER = [
    "received",
    "parsed",
    "pending_worker",
    "pending_review",
    "ready_to_invoice",
    "invoiced",
    "exported_to_qb",
]


def status_badge(status: str) -> None:
    """Render a colored pill-shaped status badge using st.markdown."""
    color = STATUS_COLORS.get(status, "#6c757d")
    label = status.replace("_", " ").title()
    st.markdown(
        f'<span style="background:{color};color:white;padding:3px 12px;'
        f'border-radius:12px;font-size:0.8em;font-weight:600;">{label}</span>',
        unsafe_allow_html=True,
    )


def status_color(status: str) -> str:
    """Return hex color string for a given status."""
    return STATUS_COLORS.get(status, "#6c757d")
