"""
supabase_sync.py
================
Syncs client invoices from the local JSON store into the Supabase
`cold_storage_invoices` table via the PostgREST REST API.

Upserts are keyed on `local_id` (the UUID from the local JSON record)
so the same invoice can be synced multiple times without creating duplicates.
"""

import json
import logging
import os
from datetime import datetime, timedelta

import httpx

from data_manager import DataManager

logger = logging.getLogger(__name__)

_TABLE   = "cold_storage_invoices"
_BATCH_SIZE = 100


# ── Helpers ───────────────────────────────────────────────────────────────────

def _base_url() -> str:
    url = os.environ.get("SUPABASE_URL", "").rstrip("/")
    if not url:
        raise RuntimeError("SUPABASE_URL must be set in .env")
    return url


def _headers(prefer: str = "resolution=merge-duplicates,return=minimal") -> dict:
    key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")
    if not key:
        raise RuntimeError("SUPABASE_SERVICE_ROLE_KEY must be set in .env")
    return {
        "apikey"       : key,
        "Authorization": f"Bearer {key}",
        "Content-Type" : "application/json",
        "Prefer"       : prefer,
    }


def _due_date(ci: dict) -> str | None:
    explicit = (ci.get("due_date") or "").strip()
    if explicit:
        return explicit
    try:
        return (
            datetime.fromisoformat(ci["invoice_date"])
            + timedelta(days=int(ci.get("net_days", 30)))
        ).date().isoformat()
    except Exception:
        return None


def _to_row(ci: dict, now_iso: str) -> dict:
    """Map a local client-invoice dict to a cold_storage_invoices row."""
    reviewed = bool(
        ci.get("ready_for_export")
        or ci.get("ready_to_email")
        or ci.get("quickbooks_exported")
        or ci.get("emailed")
    )
    return {
        "local_id"                 : ci["id"],
        "quickbooks_invoice_number": ci.get("quickbooks_invoice_number") or None,
        "client_name"              : ci.get("client_name") or None,
        "invoice_date"             : ci.get("invoice_date") or None,
        "due_date"                 : _due_date(ci),
        "net_days"                 : int(ci.get("net_days", 30)),
        "total"                    : float(ci.get("total", 0)),
        "service_type"             : ci.get("service_type") or None,
        "pipeline_status"          : ci.get("status") or None,
        "line_items"               : ci.get("line_items", []),
        "quickbooks_exported"      : bool(ci.get("quickbooks_exported")),
        "emailed"                  : bool(ci.get("emailed")),
        "reviewed"                 : reviewed,
        "paid"                     : bool(ci.get("paid")),
        "synced_at"                : now_iso,
        "updated_at"               : now_iso,
    }


# ── Public API ────────────────────────────────────────────────────────────────

def sync_single_invoice(ci: dict) -> None:
    """
    Upsert a single client-invoice dict to cold_storage_invoices.
    Called immediately after any status-changing action in the UI.
    Silently logs errors so UI actions are never blocked by a sync failure.
    """
    try:
        now_iso  = datetime.utcnow().isoformat() + "Z"
        endpoint = f"{_base_url()}/rest/v1/{_TABLE}"
        resp = httpx.post(
            endpoint,
            headers=_headers(),
            content=json.dumps(_to_row(ci, now_iso)),
            timeout=10,
        )
        if resp.status_code not in (200, 201):
            logger.warning(
                "sync_single_invoice: upsert failed (HTTP %s): %s",
                resp.status_code, resp.text,
            )
    except Exception as e:
        logger.warning("sync_single_invoice: %s", e)


def sync_invoices_to_supabase(dm: DataManager) -> int:
    """
    Bulk-upsert all processed client invoices to cold_storage_invoices.

    'Processed' = any invoice that has moved past the approval stage:
    ready_for_export | ready_to_email | quickbooks_exported | emailed | paid.

    Returns the number of rows upserted, or raises on failure.
    """
    client_invoices = dm.get_client_invoices()

    processed = [
        ci for ci in client_invoices
        if ci.get("ready_for_export")
        or ci.get("ready_to_email")
        or ci.get("quickbooks_exported")
        or ci.get("emailed")
        or ci.get("paid")
    ]

    if not processed:
        logger.info("supabase_sync: no processed invoices to upload.")
        return 0

    now_iso  = datetime.utcnow().isoformat() + "Z"
    rows     = [_to_row(ci, now_iso) for ci in processed]
    endpoint = f"{_base_url()}/rest/v1/{_TABLE}"
    headers  = _headers()
    total_upserted = 0

    with httpx.Client(timeout=30) as client:
        for i in range(0, len(rows), _BATCH_SIZE):
            batch = rows[i : i + _BATCH_SIZE]
            resp  = client.post(endpoint, headers=headers, content=json.dumps(batch))
            if resp.status_code not in (200, 201):
                raise RuntimeError(
                    f"Supabase upsert failed (HTTP {resp.status_code}): {resp.text}"
                )
            total_upserted += len(batch)

    logger.info(
        "supabase_sync: upserted %d/%d processed invoices.",
        total_upserted, len(processed),
    )
    return total_upserted


def patch_invoice_paid(local_id: str) -> None:
    """Immediately mark a single invoice as paid in cold_storage_invoices."""
    endpoint = f"{_base_url()}/rest/v1/{_TABLE}?local_id=eq.{local_id}"
    now_iso  = datetime.utcnow().isoformat() + "Z"
    payload  = {"paid": True, "updated_at": now_iso}
    with httpx.Client(timeout=15) as client:
        resp = client.patch(
            endpoint,
            headers=_headers("return=minimal"),
            content=json.dumps(payload),
        )
        if resp.status_code not in (200, 204):
            raise RuntimeError(
                f"Supabase PATCH failed (HTTP {resp.status_code}): {resp.text}"
            )
