"""
worker_form.py
==============
Mobile-friendly worker form. Workers select a pending job,
fill in pallet counts, extra charges, notes, and photos.
"""

import streamlit as st

from data_manager import DataManager
from alerting.alert_manager import AlertManager
from utils.pdf_storage import get_pdf_bytes, upload_photo as _upload_photo


def render(dm: DataManager, alert_manager: AlertManager) -> None:
    st.title("📋 Operator")
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
    if pdf_path:
        pdf_key = f"worker_pdf_{job_id}"
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

    # ── Worker inputs ─────────────────────────────────────────────────────────
    charged_by_pallet = bool(
        dm.get_rates_for_client(selected_job.get("client_name", "")).get("charged_by_pallet", True)
    )

    st.subheader("Pallet Details")

    if charged_by_pallet:
        col_a, col_b, col_c = st.columns(3)
        pallet_count    = col_a.number_input("Total Pallets",   min_value=1, step=1, value=1)
        damaged_pallets = col_b.number_input("Damaged Pallets", min_value=0, step=1, value=0)
        broken_pallets  = col_c.number_input("Broken Pallets",  min_value=0, step=1, value=0)
    else:
        st.info("Billing is per truck — no pallet count required.")
        pallet_count    = 1   # placeholder, unused in charge calculator
        col_b, col_c    = st.columns(2)
        damaged_pallets = col_b.number_input("Damaged Pallets", min_value=0, step=1, value=0)
        broken_pallets  = col_c.number_input("Broken Pallets",  min_value=0, step=1, value=0)

    st.subheader("Extra Charges")
    extra_options = {
        "Quality Inspection": "quality_inspection",
        "Pallet Cleaning"   : "pallet_cleaning",
        "Repacking"         : "repacking",
        "Re-Inspection"     : "re_inspection",
        "Overtime"          : "overtime",
    }
    selected_extras: list[str] = []
    cols = st.columns(len(extra_options))
    for col, (label, key) in zip(cols, extra_options.items()):
        if col.checkbox(label):
            selected_extras.append(key)

    _TR_OPTS   = ["Hardware & Installation", "Installation Only"]
    _TR_TO_KEY = {"Hardware & Installation": "hardware_installation",
                  "Installation Only"      : "installation_only"}
    _tr_sel = st.radio(
        "Temperature Recorder",
        options=_TR_OPTS,
        horizontal=True,
        key=f"tr_{job_id}",
    )
    temp_recorder = _TR_TO_KEY[_tr_sel]

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
        if charged_by_pallet and pallet_count < 1:
            st.error("Pallet count must be at least 1.")
            return

        # Upload photos to Supabase (keys stored; no local files written)
        photo_paths: list[str] = []
        if uploaded_photos:
            for photo in uploaded_photos:
                safe_name = photo.name.replace(" ", "_")
                key = _upload_photo(job_id, safe_name, photo.getvalue())
                if key:
                    photo_paths.append(key)

        # Update client invoice
        dm.update_client_invoice(job_id, {
            "pallet_count"   : int(pallet_count),
            "damaged_pallets": int(damaged_pallets),
            "broken_pallets" : int(broken_pallets),
            "extra_charges"  : selected_extras,
            "temp_recorder"  : temp_recorder,
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

        st.markdown(
            """
            <div id="submit-toast" style="
                position:fixed;bottom:2rem;left:50%;transform:translateX(-50%);
                background:#198754;color:#fff;padding:14px 28px;
                border-radius:8px;font-size:1rem;font-weight:600;
                box-shadow:0 4px 12px rgba(0,0,0,0.25);z-index:9999;
                animation:fadeout 0.6s ease 14.4s forwards;">
                ✅ Job submitted successfully! The admin has been notified.
            </div>
            <style>
            @keyframes fadeout { to { opacity:0; pointer-events:none; } }
            </style>
            """,
            unsafe_allow_html=True,
        )
