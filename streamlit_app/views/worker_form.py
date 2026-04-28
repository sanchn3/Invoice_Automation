"""
worker_form.py
==============
Operator view — shows active jobs and allows notes/photos to be submitted.
Pallet details and extra charges are handled by the Administrator.
"""

import streamlit as st

from data_manager import DataManager
from alerting.alert_manager import AlertManager
from invoice_logic.pdf_generator import append_photos_to_pdf as _append_photos
from utils.pdf_storage import get_pdf_bytes, overwrite_provider_pdf as _overwrite_provider_pdf


def render(dm: DataManager, alert_manager: AlertManager) -> None:
    st.title("📋 Operator")
    st.caption("View your active jobs and submit notes or photos.")
    st.markdown("---")

    # ── Load active jobs ──────────────────────────────────────────────────────
    all_client_invoices = dm.get_client_invoices()
    active_jobs = [
        ci for ci in all_client_invoices
        if ci.get("status") == "validated"
    ]

    if not active_jobs:
        st.info("No active jobs at this time. Check back later.")
        return

    # ── Job selector ──────────────────────────────────────────────────────────
    def job_label(ci: dict) -> str:
        provider_inv = dm.get_provider_invoice_by_id(ci.get("provider_invoice_id", ""))
        provider = provider_inv.get("provider_name", "Unknown") if provider_inv else "Unknown"
        inv_num  = provider_inv.get("invoice_number", "") if provider_inv else ""
        inv_part = f" — {inv_num}" if inv_num else ""
        return f"{ci.get('client_name', 'Unknown')} — {provider}{inv_part} — {ci.get('invoice_date', '')}"

    job_options = {job_label(ci): ci for ci in active_jobs}
    selected_label = st.selectbox(
        "Select your job",
        options=list(job_options.keys()),
        help="Choose the job you are working on.",
    )
    selected_job = job_options[selected_label]
    job_id       = selected_job["id"]

    # ── Job info ──────────────────────────────────────────────────────────────
    provider_inv = dm.get_provider_invoice_by_id(selected_job.get("provider_invoice_id", ""))
    st.markdown("---")
    col1, col2, col3 = st.columns(3)
    col1.metric("Client",   selected_job.get("client_name", "—"))
    col2.metric("Provider", provider_inv.get("provider_name", "—") if provider_inv else "—")
    col3.metric("Date",     selected_job.get("invoice_date", "—"))

    pdf_path = provider_inv.get("pdf_local_path", "") if provider_inv else ""
    if pdf_path:
        pdf_key   = f"worker_pdf_{job_id}"
        pdf_label = "📄 Hide Invoice PDF" if st.session_state.get(pdf_key) else "📄 View Invoice PDF"
        if st.button(pdf_label, key=f"pdftoggle_{job_id}"):
            st.session_state[pdf_key] = not st.session_state.get(pdf_key, False)
            st.rerun()
        if st.session_state.get(pdf_key):
            _bytes = get_pdf_bytes(pdf_path)
            if _bytes:
                from streamlit_pdf_viewer import pdf_viewer
                pdf_viewer(_bytes, key=f"pdfview_{job_id}")
            else:
                st.warning("PDF not available.")

    st.markdown("---")

    # ── Notes ─────────────────────────────────────────────────────────────────
    st.subheader("Notes")
    worker_notes = st.text_area(
        "Observations / Notes",
        placeholder="Describe any issues, condition of goods, special handling...",
        height=120,
    )

    # ── Photos ────────────────────────────────────────────────────────────────
    st.subheader("Photos")
    uploaded_photos = st.file_uploader(
        "Upload photos (JPG / PNG)",
        type=["jpg", "jpeg", "png"],
        accept_multiple_files=True,
        help="Take photos of the load, damage, or anything noteworthy.",
    )

    st.markdown("---")

    # ── Submit ────────────────────────────────────────────────────────────────
    if st.button("✅ Submit", type="primary", width='stretch'):
        # Bake photos into the provider PDF in memory
        if uploaded_photos and provider_inv:
            _pdf_path = provider_inv.get("pdf_local_path", "")
            if _pdf_path:
                _existing = get_pdf_bytes(_pdf_path)
                if _existing:
                    _photo_data = [p.getvalue() for p in uploaded_photos]
                    _combined   = _append_photos(_existing, _photo_data)
                    _overwrite_provider_pdf(_pdf_path, _combined)

        # Save notes only — pallet/charge details handled by Administrator
        dm.update_client_invoice(job_id, {
            "worker_notes": worker_notes,
        })

        st.markdown(
            """
            <div id="submit-toast" style="
                position:fixed;bottom:2rem;left:50%;transform:translateX(-50%);
                background:#198754;color:#fff;padding:14px 28px;
                border-radius:8px;font-size:1rem;font-weight:600;
                box-shadow:0 4px 12px rgba(0,0,0,0.25);z-index:9999;
                animation:fadeout 0.6s ease 14.4s forwards;">
                ✅ Submitted successfully!
            </div>
            <style>
            @keyframes fadeout { to { opacity:0; pointer-events:none; } }
            </style>
            """,
            unsafe_allow_html=True,
        )
