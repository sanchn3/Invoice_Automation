"""
worker_form.py
==============
Mobile-friendly worker form. Workers select a pending job,
fill in pallet counts, extra charges, notes, and photos.
"""

import streamlit as st
from pathlib import Path
from datetime import datetime

from config import PHOTOS_DIR
from data_manager import DataManager
from alerting.alert_manager import AlertManager


def render(dm: DataManager, alert_manager: AlertManager) -> None:
    st.title("📋 Worker Job Form")
    st.caption("Fill in the details after loading is complete.")
    st.markdown("---")

    # ── Load pending jobs ─────────────────────────────────────────────────────
    all_client_invoices = dm.get_client_invoices()
    pending_jobs = [
        ci for ci in all_client_invoices
        if ci.get("status") == "pending_worker"
    ]

    if not pending_jobs:
        st.info("No pending jobs at this time. Check back later.")
        return

    # ── Job selector ──────────────────────────────────────────────────────────
    def job_label(ci: dict) -> str:
        provider_inv = dm.get_provider_invoice_by_id(ci.get("provider_invoice_id", ""))
        provider = provider_inv.get("provider_name", "Unknown") if provider_inv else "Unknown"
        inv_num  = provider_inv.get("invoice_number", "") if provider_inv else ""
        inv_part = f" — {inv_num}" if inv_num else ""
        return f"{ci.get('client_name', 'Unknown')} — {provider}{inv_part} — {ci.get('invoice_date', '')}"

    job_options = {job_label(ci): ci for ci in pending_jobs}
    selected_label = st.selectbox(
        "Select your job",
        options=list(job_options.keys()),
        help="Choose the job you just completed loading.",
    )
    selected_job = job_options[selected_label]
    job_id       = selected_job["id"]

    # ── Pre-filled info ───────────────────────────────────────────────────────
    provider_inv = dm.get_provider_invoice_by_id(selected_job.get("provider_invoice_id", ""))
    st.markdown("---")
    col1, col2, col3 = st.columns(3)
    col1.metric("Client",   selected_job.get("client_name", "—"))
    col2.metric("Provider", provider_inv.get("provider_name", "—") if provider_inv else "—")
    col3.metric("Date",     selected_job.get("invoice_date", "—"))

    pdf_path = provider_inv.get("pdf_local_path", "") if provider_inv else ""
    if pdf_path and Path(pdf_path).exists():
        pdf_key = f"worker_pdf_{job_id}"
        pdf_label = "📄 Hide Invoice PDF" if st.session_state.get(pdf_key) else "📄 View Invoice PDF"
        if st.button(pdf_label, key=f"pdftoggle_{job_id}"):
            st.session_state[pdf_key] = not st.session_state.get(pdf_key, False)
            st.rerun()
        if st.session_state.get(pdf_key):
            from streamlit_pdf_viewer import pdf_viewer
            pdf_viewer(Path(pdf_path).read_bytes(), key=f"pdfview_{job_id}")

    st.markdown("---")

    # ── Worker inputs ─────────────────────────────────────────────────────────
    st.subheader("Pallet Details")

    col_a, col_b, col_c = st.columns(3)
    pallet_count    = col_a.number_input("Total Pallets",    min_value=1,  step=1, value=1)
    damaged_pallets = col_b.number_input("Damaged Pallets",  min_value=0,  step=1, value=0)
    broken_pallets  = col_c.number_input("Broken Pallets",   min_value=0,  step=1, value=0)

    st.subheader("Extra Charges")
    extra_options = {
        "Quality Inspection": "quality_inspection",
        "Pallet Cleaning"   : "pallet_cleaning",
        "Repacking"         : "repacking",
        "Re-Inspection"     : "re_inspection",
        "Broken Pallets"    : "broken_pallets",
    }
    selected_extras: list[str] = []
    cols = st.columns(len(extra_options))
    for col, (label, key) in zip(cols, extra_options.items()):
        if col.checkbox(label):
            selected_extras.append(key)

    # Auto-add broken_pallets charge if worker entered broken pallet count
    if broken_pallets > 0 and "broken_pallets" not in selected_extras:
        selected_extras.append("broken_pallets")

    st.subheader("Notes")
    worker_notes = st.text_area(
        "Observations / Notes",
        placeholder="Describe any issues, condition of goods, special handling...",
        height=120,
    )

    st.subheader("Photos")
    uploaded_photos = st.file_uploader(
        "Upload photos (JPG / PNG)",
        type=["jpg", "jpeg", "png"],
        accept_multiple_files=True,
        help="Take photos of the load, damage, or anything noteworthy.",
    )

    st.markdown("---")

    # ── Submit ────────────────────────────────────────────────────────────────
    if st.button("✅ Submit Job", type="primary", width='stretch'):
        if pallet_count < 1:
            st.error("Pallet count must be at least 1.")
            return

        # Save photos
        photo_paths: list[str] = []
        if uploaded_photos:
            job_photo_dir = PHOTOS_DIR / job_id
            job_photo_dir.mkdir(parents=True, exist_ok=True)
            for photo in uploaded_photos:
                ts        = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
                safe_name = photo.name.replace(" ", "_")
                save_path = job_photo_dir / f"{ts}_{safe_name}"
                save_path.write_bytes(photo.getvalue())
                photo_paths.append(str(save_path))

        # Update client invoice
        dm.update_client_invoice(job_id, {
            "pallet_count"   : int(pallet_count),
            "damaged_pallets": int(damaged_pallets),
            "broken_pallets" : int(broken_pallets),
            "extra_charges"  : selected_extras,
            "worker_notes"   : worker_notes,
            "photo_paths"    : photo_paths,
            "status"         : "ready_to_invoice",
        })

        # Update linked email log
        if provider_inv and provider_inv.get("email_intake_id"):
            dm.update_email_log(
                provider_inv["email_intake_id"],
                {"status": "ready_to_invoice"},
            )

        alert_manager.worker_submitted(
            client_name=selected_job.get("client_name", ""),
            job_id=job_id,
        )

        st.success(f"Job submitted successfully! The admin has been notified.")
        st.balloons()
