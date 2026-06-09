"""
Quick integration smoke-tests for:
  1. sync_single_invoice  → cold_storage_invoices
  2. SupabaseLogHandler   → logs.invoice_logger
  3. patch_invoice_paid   → cold_storage_invoices

Run from the repo root:
    python -m tests.test_supabase_integration
"""

import json
import logging
import os
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

# Allow importing from project root
sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv
load_dotenv()

import httpx

# ── helpers ───────────────────────────────────────────────────────────────────

_SUPABASE_URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
_SERVICE_KEY  = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")

def _headers(prefer: str = "") -> dict:
    h = {
        "apikey"       : _SERVICE_KEY,
        "Authorization": f"Bearer {_SERVICE_KEY}",
        "Content-Type" : "application/json",
    }
    if prefer:
        h["Prefer"] = prefer
    return h


def _check_env():
    missing = [v for v in ("SUPABASE_URL", "SUPABASE_SERVICE_ROLE_KEY") if not os.environ.get(v)]
    if missing:
        print(f"SKIP  — missing env vars: {missing}")
        sys.exit(0)


# ── Test 1: sync_single_invoice ───────────────────────────────────────────────

def test_sync_single_invoice() -> str:
    """Upsert a synthetic invoice; return its local_id."""
    from scheduler.supabase_sync import sync_single_invoice

    test_id = f"test-{uuid.uuid4()}"
    ci = {
        "id"                       : test_id,
        "quickbooks_invoice_number": "TEST-9999",
        "client_name"              : "_TEST_CLIENT_",
        "invoice_date"             : datetime.now(timezone.utc).date().isoformat(),
        "net_days"                 : 30,
        "total"                    : 123.45,
        "service_type"             : "cold_storage",
        "status"                   : "approved",
        "line_items"               : [{"description": "Test item", "amount": 123.45}],
        "ready_for_export"         : False,
        "ready_to_email"           : False,
        "quickbooks_exported"      : False,
        "emailed"                  : False,
        "paid"                     : False,
    }

    sync_single_invoice(ci)

    # Verify it's in Supabase
    url  = f"{_SUPABASE_URL}/rest/v1/cold_storage_invoices?local_id=eq.{test_id}&select=local_id,client_name,total"
    resp = httpx.get(url, headers=_headers("return=representation"), timeout=10)
    rows = resp.json()

    assert resp.status_code == 200, f"GET failed: {resp.status_code} {resp.text}"
    assert len(rows) == 1, f"Expected 1 row, got {rows}"
    assert rows[0]["client_name"] == "_TEST_CLIENT_"
    assert abs(rows[0]["total"] - 123.45) < 0.01

    print(f"PASS  test_sync_single_invoice  (local_id={test_id})")
    return test_id


# ── Test 2: patch_invoice_paid ────────────────────────────────────────────────

def test_patch_invoice_paid(test_id: str):
    from scheduler.supabase_sync import patch_invoice_paid

    patch_invoice_paid(test_id)

    url  = f"{_SUPABASE_URL}/rest/v1/cold_storage_invoices?local_id=eq.{test_id}&select=paid"
    resp = httpx.get(url, headers=_headers(), timeout=10)
    rows = resp.json()

    assert resp.status_code == 200, f"GET failed: {resp.status_code} {resp.text}"
    assert len(rows) == 1 and rows[0]["paid"] is True, f"paid not set: {rows}"

    print(f"PASS  test_patch_invoice_paid   (local_id={test_id})")


# ── Test 3: SupabaseLogHandler ────────────────────────────────────────────────

def test_supabase_log_handler():
    from utils.supabase_log_handler import SupabaseLogHandler

    marker = f"TEST-ERROR-{uuid.uuid4()}"

    handler = SupabaseLogHandler()
    root    = logging.getLogger()
    root.addHandler(handler)
    root.setLevel(logging.DEBUG)

    try:
        logging.getLogger("test_integration").error("Integration test error: %s", marker)
    finally:
        root.removeHandler(handler)

    # Give PostgREST a moment, then query
    import time; time.sleep(1)

    url  = (
        f"{_SUPABASE_URL}/rest/v1/invoice_logger"
        f"?message=like.*{marker}*&select=level,message,logger"
    )
    hdrs = {**_headers(), "Accept-Profile": "logs"}
    resp = httpx.get(url, headers=hdrs, timeout=10)
    rows = resp.json()

    assert resp.status_code == 200, f"GET failed: {resp.status_code} {resp.text}"
    assert len(rows) >= 1, f"Log row not found for marker {marker}. Got: {rows}"
    assert rows[0]["level"] == "ERROR"

    print(f"PASS  test_supabase_log_handler (marker={marker})")


# ── Cleanup ───────────────────────────────────────────────────────────────────

def cleanup(test_id: str):
    """Delete the synthetic test row so the table stays clean."""
    url  = f"{_SUPABASE_URL}/rest/v1/cold_storage_invoices?local_id=eq.{test_id}"
    resp = httpx.delete(url, headers=_headers("return=minimal"), timeout=10)
    if resp.status_code in (200, 204):
        print(f"CLEAN cold_storage_invoices test row deleted.")
    else:
        print(f"WARN  cleanup failed: {resp.status_code} {resp.text}")


# ── Main ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    _check_env()

    passed = 0
    failed = 0
    test_id = None

    # Test 1
    try:
        test_id = test_sync_single_invoice()
        passed += 1
    except Exception as exc:
        print(f"FAIL  test_sync_single_invoice: {exc}")
        failed += 1

    # Test 2 (depends on test_id from test 1)
    if test_id:
        try:
            test_patch_invoice_paid(test_id)
            passed += 1
        except Exception as exc:
            print(f"FAIL  test_patch_invoice_paid: {exc}")
            failed += 1

    # Test 3
    try:
        test_supabase_log_handler()
        passed += 1
    except Exception as exc:
        print(f"FAIL  test_supabase_log_handler: {exc}")
        failed += 1

    if test_id:
        cleanup(test_id)

    print(f"\n{'='*50}")
    print(f"Results: {passed} passed, {failed} failed")
    sys.exit(0 if failed == 0 else 1)
