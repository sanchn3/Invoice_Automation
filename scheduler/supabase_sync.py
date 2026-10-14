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
from urllib.parse import urlparse

# Only sync to Supabase when running on Render (production).
_IS_PRODUCTION = os.environ.get("RENDER") == "true"

import httpx

from data_manager import DataManager
from utils.dns_fix import ensure_host_reachable

logger = logging.getLogger(__name__)

_TABLE   = "cold_storage_invoices"
_BATCH_SIZE = 100


def _fix_dns() -> None:
    url = os.environ.get("SUPABASE_URL", "")
    host = urlparse(url).hostname
    if host:
        ensure_host_reachable(host)


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
        # Use the invoice's own updated_at so a sync never overwrites a newer
        # accounting edit in cold_storage_invoices with a stale local timestamp.
        "updated_at"               : ci.get("updated_at") or now_iso,
    }


# ── Stale-number guard ────────────────────────────────────────────────────────

def _find_stale_local_ids(rows: list[dict]) -> set[str]:
    """
    Return the set of local_ids whose quickbooks_invoice_number already exists
    in cold_storage_invoices under a *different* local_id.

    These represent invoices whose numbers were generated from a stale local
    state (e.g. after a redeploy wiped client_invoices.json) and must not be
    synced, as they would create duplicate invoice numbers in the database.

    Logs an ERROR for every blocked invoice so operators can investigate.
    Returns an empty set on any query failure (fail open — don't block sync).
    """
    candidates = {
        r["local_id"]: r["quickbooks_invoice_number"]
        for r in rows
        if r.get("quickbooks_invoice_number") and r.get("local_id")
    }
    if not candidates:
        return set()

    inv_numbers = list(set(candidates.values()))
    # PostgREST IN filter format: in.(A,B,C)
    in_filter = "in.(" + ",".join(inv_numbers) + ")"
    try:
        resp = httpx.get(
            f"{_base_url()}/rest/v1/{_TABLE}",
            params={
                "select": "local_id,quickbooks_invoice_number",
                "quickbooks_invoice_number": in_filter,
            },
            headers=_headers(""),
            timeout=10,
        )
        if resp.status_code != 200:
            logger.warning(
                "_find_stale_local_ids: query failed (HTTP %s), skipping guard",
                resp.status_code,
            )
            return set()
        # Build a map of number → existing local_id from Supabase
        existing: dict[str, str] = {
            r["quickbooks_invoice_number"]: r["local_id"]
            for r in resp.json()
        }
    except Exception as exc:
        logger.warning("_find_stale_local_ids: %s — skipping guard", exc)
        return set()

    blocked: set[str] = set()
    for local_id, inv_num in candidates.items():
        owner = existing.get(inv_num)
        if owner and owner != local_id:
            logger.error(
                "STALE INVOICE NUMBER BLOCKED: local_id=%s carries "
                "quickbooks_invoice_number=%s which is already owned by "
                "local_id=%s in %s. Sync skipped to prevent a duplicate "
                "invoice number. Investigate how this number was re-issued.",
                local_id, inv_num, owner, _TABLE,
            )
            blocked.add(local_id)
    return blocked


def _find_newer_remote_ids(rows: list[dict]) -> set[str]:
    """
    Return the set of local_ids whose cold_storage_invoices row has a newer
    updated_at than the local copy being synced.

    Prevents a stale local copy (e.g. restored after a redeploy) from
    overwriting accounting edits that arrived after the last local update.
    Returns an empty set on any query failure (fail open — don't block sync).
    """
    candidates = {
        r["local_id"]: r.get("updated_at", "")
        for r in rows
        if r.get("local_id") and r.get("updated_at")
    }
    if not candidates:
        return set()

    in_filter = "in.(" + ",".join(candidates) + ")"
    try:
        resp = httpx.get(
            f"{_base_url()}/rest/v1/{_TABLE}",
            params={"select": "local_id,updated_at", "local_id": in_filter},
            headers=_headers(""),
            timeout=10,
        )
        if resp.status_code != 200:
            logger.warning(
                "_find_newer_remote_ids: query failed (HTTP %s), skipping guard",
                resp.status_code,
            )
            return set()
        remote: dict[str, str] = {r["local_id"]: r.get("updated_at", "") for r in resp.json()}
    except Exception as exc:
        logger.warning("_find_newer_remote_ids: %s — skipping guard", exc)
        return set()

    blocked: set[str] = set()
    for local_id, local_ts in candidates.items():
        remote_ts = remote.get(local_id, "")
        if remote_ts and local_ts and remote_ts > local_ts:
            logger.warning(
                "STALE SYNC BLOCKED: local_id=%s local updated_at=%s < remote updated_at=%s. "
                "Skipping to avoid overwriting a newer accounting edit.",
                local_id, local_ts, remote_ts,
            )
            blocked.add(local_id)
    return blocked


# ── Public API ────────────────────────────────────────────────────────────────

def sync_single_invoice(ci: dict) -> None:
    """
    Upsert a single client-invoice dict to cold_storage_invoices.
    Called immediately after any status-changing action in the UI.
    Silently logs errors so UI actions are never blocked by a sync failure.
    """
    if not _IS_PRODUCTION:
        return
    _fix_dns()
    try:
        now_iso  = datetime.utcnow().isoformat() + "Z"
        row      = _to_row(ci, now_iso)
        if _find_stale_local_ids([row]) or _find_newer_remote_ids([row]):
            return  # errors already logged inside each guard
        endpoint = f"{_base_url()}/rest/v1/{_TABLE}?on_conflict=local_id"
        resp = httpx.post(
            endpoint,
            headers=_headers(),
            content=json.dumps(row),
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
    _fix_dns()
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

    # Block rows with a duplicate invoice number or a remote row that is newer
    stale_ids       = _find_stale_local_ids(rows)
    newer_remote_ids = _find_newer_remote_ids(rows)
    blocked_ids     = stale_ids | newer_remote_ids
    if blocked_ids:
        rows = [r for r in rows if r["local_id"] not in blocked_ids]
        logger.warning(
            "supabase_sync: dropped %d invoice(s) from sync batch "
            "(%d stale-numbered, %d newer-in-remote).",
            len(blocked_ids), len(stale_ids), len(newer_remote_ids),
        )

    if not rows:
        logger.info("supabase_sync: all rows were blocked — nothing to upload.")
        return 0

    endpoint = f"{_base_url()}/rest/v1/{_TABLE}?on_conflict=local_id"
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
    _fix_dns()
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
