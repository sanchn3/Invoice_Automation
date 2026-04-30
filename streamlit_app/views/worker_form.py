"""
worker_form.py
==============
Operator view — submit a lot number, notes, and photos.
"""

import json
import streamlit as st
from datetime import datetime, timezone
from pathlib import Path

from data_manager import DataManager
from alerting.alert_manager import AlertManager


_SUBMISSIONS_FILE = Path(__file__).parent.parent.parent / "data" / "operator_submissions.json"
_PHOTOS_DIR       = Path(__file__).parent.parent.parent / "photos"


def _save_submission(lot_number: str, notes: str, photo_paths: list[str]) -> None:
    _SUBMISSIONS_FILE.parent.mkdir(parents=True, exist_ok=True)
    try:
        existing = (
            json.loads(_SUBMISSIONS_FILE.read_text(encoding="utf-8"))
            if _SUBMISSIONS_FILE.exists()
            else []
        )
    except Exception:
        existing = []
    existing.append({
        "submitted_at": datetime.now(timezone.utc).isoformat(),
        "lot_number"  : lot_number,
        "notes"       : notes,
        "photo_paths" : photo_paths,
    })
    _SUBMISSIONS_FILE.write_text(
        json.dumps(existing, indent=2, ensure_ascii=False), encoding="utf-8"
    )


def render(dm: DataManager, alert_manager: AlertManager) -> None:
    st.title("📋 Operator")
    st.caption("Fill in the form below and submit when ready.")
    st.markdown("---")

    lot_number = st.text_input(
        "Lot Number",
        placeholder="Enter the lot number...",
    )

    worker_notes = st.text_area(
        "Notes",
        placeholder="Describe any issues, condition of goods, special handling...",
        height=140,
    )

    uploaded_photos = st.file_uploader(
        "Photos (JPG / PNG)",
        type=["jpg", "jpeg", "png"],
        accept_multiple_files=True,
        help="Take photos of the load, damage, or anything noteworthy.",
    )

    st.markdown("---")

    if st.button("✅ Submit", type="primary", width='stretch'):
        if not lot_number.strip():
            st.error("Please enter a lot number before submitting.")
            st.stop()

        # Save uploaded photos to disk
        saved_paths: list[str] = []
        if uploaded_photos:
            _ts        = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
            _photo_dir = _PHOTOS_DIR / f"{lot_number.strip()}_{_ts}"
            _photo_dir.mkdir(parents=True, exist_ok=True)
            for photo in uploaded_photos:
                _dest = _photo_dir / photo.name
                _dest.write_bytes(photo.getvalue())
                saved_paths.append(str(_dest))

        _save_submission(lot_number.strip(), worker_notes.strip(), saved_paths)

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
