"""
test_storage.py
===============
Quick sanity check for Supabase Storage connectivity.
Run from the project root after setting SUPABASE_SERVICE_ROLE_KEY in .env:

    venv/Scripts/python test_storage.py
"""
import sys
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

from utils.pdf_storage import upload_pdf, fetch_pdf_bytes

# ── 1. Find any local PDF to test with ────────────────────────────────────────
pdfs = list((Path(__file__).parent / "pdfs").glob("*.pdf"))
if not pdfs:
    print("❌  No PDFs found in pdfs/ — process an invoice first, then re-run.")
    sys.exit(1)

test_pdf = pdfs[0]
print(f"Testing with: {test_pdf.name}")

# ── 2. Upload ──────────────────────────────────────────────────────────────────
print("Uploading...")
ok = upload_pdf(str(test_pdf))
if not ok:
    print("❌  Upload failed — check logs above for details.")
    sys.exit(1)
print("✅  Upload succeeded.")

# ── 3. Download back ───────────────────────────────────────────────────────────
print("Downloading back...")
data = fetch_pdf_bytes(test_pdf.name)
if data is None:
    print("❌  Download failed — check logs above for details.")
    sys.exit(1)
if len(data) != test_pdf.stat().st_size:
    print(f"⚠️  Size mismatch: local={test_pdf.stat().st_size}  remote={len(data)}")
else:
    print(f"✅  Download succeeded ({len(data):,} bytes match).")

print("\nAll checks passed. PDFs will be accessible from Supabase Storage.")
