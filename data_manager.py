"""
data_manager.py
===============
Single source of truth for all data read/write operations.
All other modules call this — never read/write JSON files directly.
To migrate to Supabase later: replace only this file.
"""

import json
import threading
import uuid
from collections import OrderedDict
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx

from config import DATA_DIR, SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY

# ── Supabase helpers ──────────────────────────────────────────────────────────

import logging as _logging
_sb_logger = _logging.getLogger(__name__)

def _sb_headers(prefer: str = "return=representation") -> dict:
    return {
        "apikey":        SUPABASE_SERVICE_ROLE_KEY,
        "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}",
        "Content-Type":  "application/json",
        "Prefer":        prefer,
    }

def _sb_url(table: str) -> str:
    return f"{SUPABASE_URL}/rest/v1/{table}"


_SB_CI_TABLE = "pipeline_client_invoices"
_SB_PI_TABLE = "pipeline_provider_invoices"


def _sb_upsert_record(table: str, local_id: str, record: dict) -> None:
    """Upsert a single pipeline record to Supabase. Silently logs on failure."""
    if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
        return
    try:
        resp = httpx.post(
            f"{_sb_url(table)}?on_conflict=local_id",
            headers=_sb_headers("resolution=merge-duplicates,return=minimal"),
            content=json.dumps({"local_id": local_id, "data": record,
                                "updated_at": _now()}),
            timeout=8,
        )
        if resp.status_code not in (200, 201):
            _sb_logger.warning("_sb_upsert_record %s/%s: HTTP %s %s",
                               table, local_id, resp.status_code, resp.text[:200])
    except Exception as exc:
        _sb_logger.warning("_sb_upsert_record %s/%s: %s", table, local_id, exc)


def _sb_delete_record(table: str, local_id: str) -> None:
    """Delete a single pipeline record from Supabase. Silently logs on failure."""
    if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
        return
    try:
        resp = httpx.delete(
            f"{_sb_url(table)}?local_id=eq.{local_id}",
            headers=_sb_headers("return=minimal"),
            timeout=8,
        )
        if resp.status_code not in (200, 204):
            _sb_logger.warning("_sb_delete_record %s/%s: HTTP %s %s",
                               table, local_id, resp.status_code, resp.text[:200])
    except Exception as exc:
        _sb_logger.warning("_sb_delete_record %s/%s: %s", table, local_id, exc)


def _restore_pipeline_from_supabase() -> None:
    """
    Pull all pipeline invoice records from Supabase and write them to the
    local JSON files.  Called at startup when the files are freshly created
    (i.e. after a Render redeploy wiped the ephemeral filesystem).
    """
    if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
        return
    for file_path, table in (
        (_PROVIDER_INVOICES_FILE, _SB_PI_TABLE),
        (_CLIENT_INVOICES_FILE,   _SB_CI_TABLE),
    ):
        try:
            resp = httpx.get(
                f"{_sb_url(table)}?select=data&order=updated_at.asc",
                headers=_sb_headers(),
                timeout=15,
            )
            if resp.status_code == 200:
                rows = resp.json()
                records = [r["data"] for r in rows if isinstance(r.get("data"), dict)]
                if records:
                    _write_json(file_path, records)
                    _sb_logger.info("_restore_pipeline: restored %d records from %s",
                                    len(records), table)
            else:
                _sb_logger.warning("_restore_pipeline %s: HTTP %s", table, resp.status_code)
        except Exception as exc:
            _sb_logger.warning("_restore_pipeline %s: %s", table, exc)


def _backfill_pipeline_to_supabase() -> None:
    """
    One-time transition helper: if a pipeline Supabase table is empty but the
    local JSON file has records, push all local records up.  This handles the
    first redeploy after this feature was introduced, when the tables are new
    and empty but the running server already has invoice data on disk.
    """
    if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
        return
    for file_path, table in (
        (_PROVIDER_INVOICES_FILE, _SB_PI_TABLE),
        (_CLIENT_INVOICES_FILE,   _SB_CI_TABLE),
    ):
        try:
            # Check how many rows Supabase already has
            count_resp = httpx.get(
                f"{_sb_url(table)}?select=local_id",
                headers={**_sb_headers(), "Prefer": "count=exact"},
                timeout=10,
            )
            sb_count = int(count_resp.headers.get("content-range", "0/0").split("/")[-1] or 0)
            if sb_count > 0:
                continue  # already has data — nothing to backfill

            local_records = _read_json(file_path)
            if not local_records:
                continue

            now = _now()
            rows = [{"local_id": r["id"], "data": r, "updated_at": now}
                    for r in local_records if r.get("id")]
            resp = httpx.post(
                f"{_sb_url(table)}?on_conflict=local_id",
                headers=_sb_headers("resolution=merge-duplicates,return=minimal"),
                content=json.dumps(rows),
                timeout=30,
            )
            if resp.status_code in (200, 201):
                _sb_logger.info("_backfill_pipeline: pushed %d records to %s",
                                len(rows), table)
            else:
                _sb_logger.warning("_backfill_pipeline %s: HTTP %s %s",
                                   table, resp.status_code, resp.text[:200])
        except Exception as exc:
            _sb_logger.warning("_backfill_pipeline %s: %s", table, exc)

# File paths
_EMAIL_LOG_FILE          = DATA_DIR / "email_intake_log.json"
_PROVIDER_INVOICES_FILE  = DATA_DIR / "provider_invoices.json"
_CLIENT_INVOICES_FILE    = DATA_DIR / "client_invoices.json"
_PROVIDERS_FILE          = DATA_DIR / "providers.json"
_RATE_CARD_FILE          = DATA_DIR / "rate_card.json"
_CLIENT_RATES_FILE       = DATA_DIR / "client_rates.json"
_CLIENT_ADDRESSES_FILE   = DATA_DIR / "client_addresses.json"
_CLIENT_EMAILS_FILE      = DATA_DIR / "client_emails.json"
_CLIENT_INITIALS_FILE    = DATA_DIR / "client_initials.json"
_CLIENT_RFCS_FILE        = DATA_DIR / "client_rfcs.json"
_CLIENT_COUNTERS_FILE    = DATA_DIR / "client_invoice_counters.json"
_BOL_RECORDS_FILE        = DATA_DIR / "bol_records.json"

_lock = threading.Lock()

# Files that grow unboundedly — never cache them so their full contents are
# not held in memory between poll cycles.
_NO_CACHE_FILES = {_EMAIL_LOG_FILE, _PROVIDER_INVOICES_FILE, _CLIENT_INVOICES_FILE}

# In-memory read cache: path -> (mtime, parsed_data)
# Keyed by file mtime so the background poller's writes auto-invalidate.
# Uses an OrderedDict for LRU eviction capped at _CACHE_MAX_ENTRIES.
# All access is inside _lock, so no additional synchronisation is needed.
_CACHE_MAX_ENTRIES = 20
_file_cache: OrderedDict[Path, tuple[float, Any]] = OrderedDict()


def _now() -> str:
    return datetime.utcnow().isoformat() + "Z"


def _new_id() -> str:
    return str(uuid.uuid4())


_RATE_CARD_DEFAULTS: dict = {
    "charged_by_pallet"          : True,
    "in_out"                     : 12.0,
    "transfer"                   : 14.0,
    "cost_per_truck"             : 0.0,
    "temp_recorder_hardware_fee" : 1.0,
    "temp_recorder_installation_fee": 2.0,
    "quality_inspection_fee"     : 4.0,
    "pallet_cleaning_fee"        : 8.0,
    "broken_pallet_fee"          : 25.0,
    "repacking_fee"              : 23.0,
    "re_inspection_fee"          : 3.0,
    "broker_fee"                 : 16.0,
    "net_days"                   : 30,
}

_CLIENT_RATES_DEFAULTS: dict = {
    "PRODEMEX": {
        "charged_by_pallet"             : True,
        "in_out"                        : 16.0,
        "transfer"                      : 200.0,
        "cost_per_truck"                : 0.0,
        "temp_recorder_hardware_fee"    : 25.0,
        "temp_recorder_installation_fee": 15.0,
        "quality_inspection_fee"        : 4.0,
        "pallet_cleaning_fee"           : 8.0,
        "repacking_fee"                 : 23.0,
        "re_inspection_fee"             : 3.0,
        "broker_fee"                    : 0.0,
        "stamps_fee"                    : 5.0,
        "overtime_fee"                  : 100.0,
        "restack_fee"                   : 40.0,
        "net_days"                      : 30,
    },
}

_CLIENT_ADDRESSES_DEFAULTS: dict = {
    "PRODEMEX": (
        "Comercializadora Prodomex SA de CV\n"
        "Blvd Morelos 307\n"
        "Colonia: Zona Militar Cuartel XV\n"
        "83145 Hermosillo, Sonora, MX"
    ),
}

_CLIENT_EMAILS_DEFAULTS: dict = {
    "PRODEMEX": "jfheguertty@produceexports.mx",
}

_CLIENT_INITIALS_DEFAULTS: dict = {
    "PRODEMEX": "PDMX",
}

_CLIENT_RFCS_DEFAULTS: dict = {
    "PRODEMEX": "CPR1509284K9",
}

_JSON_DEFAULTS: dict[Path, Any] = {
    _RATE_CARD_FILE        : _RATE_CARD_DEFAULTS,
    _CLIENT_RATES_FILE     : _CLIENT_RATES_DEFAULTS,
    _CLIENT_ADDRESSES_FILE : _CLIENT_ADDRESSES_DEFAULTS,
    _CLIENT_EMAILS_FILE    : _CLIENT_EMAILS_DEFAULTS,
    _CLIENT_INITIALS_FILE  : _CLIENT_INITIALS_DEFAULTS,
    _CLIENT_RFCS_FILE      : _CLIENT_RFCS_DEFAULTS,
    _CLIENT_COUNTERS_FILE  : {},
    _BOL_RECORDS_FILE      : [],
}


def _read_json(path: Path) -> Any:
    default = _JSON_DEFAULTS.get(path, [])
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return default
    if path in _NO_CACHE_FILES:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    cached = _file_cache.get(path)
    if cached is not None and cached[0] == mtime:
        _file_cache.move_to_end(path)
        return cached[1]
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    _file_cache[path] = (mtime, data)
    _file_cache.move_to_end(path)
    if len(_file_cache) > _CACHE_MAX_ENTRIES:
        _file_cache.popitem(last=False)  # evict least recently used
    return data


def _write_json(path: Path, data: Any) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    if path in _NO_CACHE_FILES:
        return
    # Update cache immediately so the next read within the same lock
    # cycle doesn't go back to disk.
    try:
        _file_cache[path] = (path.stat().st_mtime, data)
        _file_cache.move_to_end(path)
        if len(_file_cache) > _CACHE_MAX_ENTRIES:
            _file_cache.popitem(last=False)
    except OSError:
        _file_cache.pop(path, None)


def _ensure_defaults() -> None:
    """Write default JSON files to disk if they don't exist, then restore
    pipeline invoice data from Supabase when the pipeline files are missing
    or empty (i.e. after a Render redeploy wiped the ephemeral filesystem)."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    for path, default in _JSON_DEFAULTS.items():
        if not path.exists():
            _write_json(path, default)

    # Restore from Supabase if either pipeline file is empty
    pipeline_empty = (
        not _read_json(_CLIENT_INVOICES_FILE)
        or not _read_json(_PROVIDER_INVOICES_FILE)
    )
    if pipeline_empty:
        _restore_pipeline_from_supabase()
    else:
        _backfill_pipeline_to_supabase()


_ensure_defaults()


class DataManager:
    # ─────────────────────────────────────────
    # EMAIL INTAKE LOG
    # ─────────────────────────────────────────

    def get_email_logs(self) -> list[dict]:
        with _lock:
            return _read_json(_EMAIL_LOG_FILE)

    def get_nonterminal_email_logs(self) -> list[dict]:
        """Return only logs that have not reached a terminal status.
        Use this instead of get_email_logs() when you only need active records,
        to avoid loading the full (ever-growing) log into memory."""
        _TERMINAL = {"invoiced", "exported_to_qb"}
        with _lock:
            logs = _read_json(_EMAIL_LOG_FILE)
        return [log for log in logs if log.get("status", "") not in _TERMINAL]

    def add_email_log(self, record: dict) -> dict:
        with _lock:
            logs = _read_json(_EMAIL_LOG_FILE)
            record.setdefault("id", _new_id())
            record.setdefault("created_at", _now())
            logs.append(record)
            _write_json(_EMAIL_LOG_FILE, logs)
            return record

    def update_email_log(self, id: str, updates: dict) -> dict:
        with _lock:
            logs = _read_json(_EMAIL_LOG_FILE)
            for i, log in enumerate(logs):
                if log["id"] == id:
                    logs[i].update(updates)
                    _write_json(_EMAIL_LOG_FILE, logs)
                    return logs[i]
            raise KeyError(f"Email log {id} not found.")

    def get_email_log_by_id(self, id: str) -> dict | None:
        for log in self.get_email_logs():
            if log["id"] == id:
                return log
        return None

    # ─────────────────────────────────────────
    # PROVIDER INVOICES
    # ─────────────────────────────────────────

    def get_provider_invoices(self) -> list[dict]:
        with _lock:
            return _read_json(_PROVIDER_INVOICES_FILE)

    def add_provider_invoice(self, record: dict) -> dict:
        with _lock:
            invoices = _read_json(_PROVIDER_INVOICES_FILE)
            record.setdefault("id", _new_id())
            record.setdefault("created_at", _now())
            invoices.append(record)
            _write_json(_PROVIDER_INVOICES_FILE, invoices)
        _sb_upsert_record(_SB_PI_TABLE, record["id"], record)
        return record

    def delete_provider_invoice(self, id: str) -> None:
        with _lock:
            invoices = _read_json(_PROVIDER_INVOICES_FILE)
            invoices = [inv for inv in invoices if inv["id"] != id]
            _write_json(_PROVIDER_INVOICES_FILE, invoices)
        _sb_delete_record(_SB_PI_TABLE, id)

    def update_provider_invoice(self, id: str, updates: dict) -> dict:
        with _lock:
            invoices = _read_json(_PROVIDER_INVOICES_FILE)
            for i, inv in enumerate(invoices):
                if inv["id"] == id:
                    invoices[i].update(updates)
                    _write_json(_PROVIDER_INVOICES_FILE, invoices)
                    updated = invoices[i]
                    break
            else:
                raise KeyError(f"Provider invoice {id} not found.")
        _sb_upsert_record(_SB_PI_TABLE, id, updated)
        return updated

    def get_provider_invoice_by_id(self, id: str) -> dict | None:
        for inv in self.get_provider_invoices():
            if inv["id"] == id:
                return inv
        return None

    # ─────────────────────────────────────────
    # CLIENT INVOICES
    # ─────────────────────────────────────────

    def get_client_invoices(self) -> list[dict]:
        with _lock:
            return _read_json(_CLIENT_INVOICES_FILE)

    def add_client_invoice(self, record: dict) -> dict:
        with _lock:
            invoices = _read_json(_CLIENT_INVOICES_FILE)
            record.setdefault("id", _new_id())
            record.setdefault("created_at", _now())
            invoices.append(record)
            _write_json(_CLIENT_INVOICES_FILE, invoices)
        _sb_upsert_record(_SB_CI_TABLE, record["id"], record)
        return record

    def update_client_invoice(self, id: str, updates: dict) -> dict:
        with _lock:
            invoices = _read_json(_CLIENT_INVOICES_FILE)
            for i, inv in enumerate(invoices):
                if inv["id"] == id:
                    invoices[i].update(updates)
                    _write_json(_CLIENT_INVOICES_FILE, invoices)
                    updated = invoices[i]
                    break
            else:
                raise KeyError(f"Client invoice {id} not found.")
        _sb_upsert_record(_SB_CI_TABLE, id, updated)
        return updated

    def get_client_invoice_by_id(self, id: str) -> dict | None:
        for inv in self.get_client_invoices():
            if inv["id"] == id:
                return inv
        return None

    def delete_client_invoice(self, id: str) -> None:
        with _lock:
            invoices = _read_json(_CLIENT_INVOICES_FILE)
            invoices = [inv for inv in invoices if inv["id"] != id]
            _write_json(_CLIENT_INVOICES_FILE, invoices)
        _sb_delete_record(_SB_CI_TABLE, id)

    def get_client_invoice_by_provider_invoice_id(self, provider_invoice_id: str) -> dict | None:
        for inv in self.get_client_invoices():
            if inv.get("provider_invoice_id") == provider_invoice_id:
                return inv
        return None

    # ─────────────────────────────────────────
    # PROVIDERS
    # ─────────────────────────────────────────

    def get_providers(self) -> list[dict]:
        with _lock:
            return _read_json(_PROVIDERS_FILE)

    def get_provider_by_email_domain(self, domain: str) -> dict | None:
        domain = domain.lower()
        for provider in self.get_providers():
            if provider.get("email_domain", "").lower() in domain:
                return provider
        return None

    # ─────────────────────────────────────────
    # RATE CARD
    # ─────────────────────────────────────────

    def get_rate_card(self) -> dict:
        with _lock:
            return _read_json(_RATE_CARD_FILE)

    def update_rate_card(self, updates: dict) -> dict:
        with _lock:
            card = _read_json(_RATE_CARD_FILE)
            card.update(updates)
            _write_json(_RATE_CARD_FILE, card)
            return card

    # ─────────────────────────────────────────
    # CLIENT RATES (per-client overrides)
    # ─────────────────────────────────────────

    def get_client_rates(self) -> dict:
        """Returns dict mapping client_name -> {rate_key: value} overrides."""
        with _lock:
            return _read_json(_CLIENT_RATES_FILE)

    def get_rates_for_client(self, client_name: str) -> dict:
        """
        Returns the effective rate card for a client:
        default rate card merged with any client-specific overrides.
        Single lock acquisition reads both files.
        """
        with _lock:
            defaults  = _read_json(_RATE_CARD_FILE)
            all_rates = _read_json(_CLIENT_RATES_FILE)
        return {**defaults, **all_rates.get(client_name, {})}

    def set_client_rates(self, client_name: str, rates: dict) -> None:
        """Save per-client rate overrides. Pass an empty dict to remove overrides."""
        with _lock:
            all_rates = _read_json(_CLIENT_RATES_FILE)
            if not isinstance(all_rates, dict):
                all_rates = {}
            if rates:
                all_rates[client_name] = rates
            else:
                all_rates.pop(client_name, None)
            _write_json(_CLIENT_RATES_FILE, all_rates)

    def delete_client_rates(self, client_name: str) -> None:
        with _lock:
            all_rates = _read_json(_CLIENT_RATES_FILE)
            if isinstance(all_rates, dict):
                all_rates.pop(client_name, None)
                _write_json(_CLIENT_RATES_FILE, all_rates)

    def rename_client(self, old_name: str, new_name: str) -> None:
        """Rename a client across all data files atomically."""
        if not new_name or old_name == new_name:
            return
        with _lock:
            for fpath in (_CLIENT_RATES_FILE, _CLIENT_ADDRESSES_FILE,
                          _CLIENT_EMAILS_FILE, _CLIENT_RFCS_FILE,
                          _CLIENT_INITIALS_FILE, _CLIENT_COUNTERS_FILE):
                data = _read_json(fpath)
                if isinstance(data, dict) and old_name in data:
                    data[new_name] = data.pop(old_name)
                    _write_json(fpath, data)

            for fpath in (_CLIENT_INVOICES_FILE, _PROVIDER_INVOICES_FILE):
                records = _read_json(fpath)
                if isinstance(records, list):
                    changed = False
                    for rec in records:
                        if rec.get("client_name") == old_name:
                            rec["client_name"] = new_name
                            changed = True
                    if changed:
                        _write_json(fpath, records)

    # ─────────────────────────────────────────
    # CLIENT BILLING ADDRESSES
    # ─────────────────────────────────────────

    def get_client_addresses(self) -> dict:
        """Returns dict mapping client_name -> billing address string."""
        with _lock:
            return _read_json(_CLIENT_ADDRESSES_FILE)

    def get_client_address(self, client_name: str) -> str:
        """Returns the billing address for a client, or empty string."""
        return self.get_client_addresses().get(client_name, "")

    def set_client_address(self, client_name: str, address: str) -> None:
        """Save or remove a billing address for a client."""
        with _lock:
            all_addrs = _read_json(_CLIENT_ADDRESSES_FILE)
            if not isinstance(all_addrs, dict):
                all_addrs = {}
            if address.strip():
                all_addrs[client_name] = address.strip()
            else:
                all_addrs.pop(client_name, None)
            _write_json(_CLIENT_ADDRESSES_FILE, all_addrs)

    # ─────────────────────────────────────────
    # CLIENT EMAILS
    # ─────────────────────────────────────────

    def get_client_emails(self) -> dict:
        """Returns dict mapping client_name -> email address string."""
        with _lock:
            return _read_json(_CLIENT_EMAILS_FILE)

    def get_client_email(self, client_name: str) -> str:
        """Returns the email for a client, or empty string."""
        return self.get_client_emails().get(client_name, "")

    def set_client_email(self, client_name: str, email: str) -> None:
        """Save or remove an email for a client."""
        with _lock:
            all_emails = _read_json(_CLIENT_EMAILS_FILE)
            if not isinstance(all_emails, dict):
                all_emails = {}
            if email.strip():
                all_emails[client_name] = email.strip()
            else:
                all_emails.pop(client_name, None)
            _write_json(_CLIENT_EMAILS_FILE, all_emails)

    # ─────────────────────────────────────────
    # CLIENT RFCs
    # ─────────────────────────────────────────

    def get_client_rfcs(self) -> dict:
        """Returns dict mapping client_name -> RFC string."""
        with _lock:
            return _read_json(_CLIENT_RFCS_FILE)

    def get_client_rfc(self, client_name: str) -> str:
        """Returns the RFC for a client, or empty string."""
        return self.get_client_rfcs().get(client_name, "")

    def set_client_rfc(self, client_name: str, rfc: str) -> None:
        """Save or remove an RFC for a client."""
        with _lock:
            all_rfcs = _read_json(_CLIENT_RFCS_FILE)
            if not isinstance(all_rfcs, dict):
                all_rfcs = {}
            if rfc.strip():
                all_rfcs[client_name] = rfc.strip().upper()
            else:
                all_rfcs.pop(client_name, None)
            _write_json(_CLIENT_RFCS_FILE, all_rfcs)

    # ─────────────────────────────────────────
    # CLIENT INITIALS
    # ─────────────────────────────────────────

    def get_client_initials(self) -> dict:
        """Returns dict mapping client_name -> initials string."""
        with _lock:
            return _read_json(_CLIENT_INITIALS_FILE)

    def get_client_initial(self, client_name: str) -> str:
        """Returns the initials for a client, or empty string."""
        return self.get_client_initials().get(client_name, "")

    def set_client_initial(self, client_name: str, initials: str) -> None:
        """Save or remove initials for a client."""
        with _lock:
            all_initials = _read_json(_CLIENT_INITIALS_FILE)
            if not isinstance(all_initials, dict):
                all_initials = {}
            if initials.strip():
                all_initials[client_name] = initials.strip().upper()
            else:
                all_initials.pop(client_name, None)
            _write_json(_CLIENT_INITIALS_FILE, all_initials)

    # ─────────────────────────────────────────
    # PER-CLIENT INVOICE COUNTERS
    # ─────────────────────────────────────────

    def _max_issued_number(self, client_name: str) -> int:
        """
        Scan client_invoices.json for the highest numeric invoice number
        already issued to client_name. Returns 2000 if none found.
        Strips any prefix (e.g. 'WMT_2005' → 2005) before comparing.
        """
        invoices = _read_json(_CLIENT_INVOICES_FILE)
        max_num = 2000
        for inv in invoices:
            if inv.get("client_name") != client_name:
                continue
            qb = inv.get("quickbooks_invoice_number") or ""
            # Strip optional prefix (e.g. "WMT_" or "MKY_")
            numeric_part = qb.split("_")[-1] if "_" in qb else qb
            try:
                max_num = max(max_num, int(numeric_part))
            except (ValueError, AttributeError):
                pass
        return max_num

    def _sb_get_counter(self, client_name: str) -> int:
        """
        Fetch last_issued for client_name from Supabase client_invoice_counters.
        Returns 2000 on any error so callers always get a safe floor.
        """
        try:
            resp = httpx.get(
                _sb_url("client_invoice_counters"),
                headers=_sb_headers(""),
                params={"client_name": f"eq.{client_name}", "select": "last_issued", "limit": "1"},
                timeout=5,
            )
            resp.raise_for_status()
            rows = resp.json()
            return int(rows[0]["last_issued"]) if rows else 2000
        except Exception:
            return 2000

    def _sb_set_counter(self, client_name: str, value: int) -> None:
        """
        Upsert last_issued for client_name in Supabase. Silently ignores errors
        so a transient network issue never blocks invoice creation.
        """
        try:
            httpx.post(
                _sb_url("client_invoice_counters"),
                headers={**_sb_headers(), "Prefer": "resolution=merge-duplicates,return=minimal"},
                json={"client_name": client_name, "last_issued": value, "updated_at": _now()},
                timeout=5,
            )
        except Exception:
            pass

    def next_client_invoice_number(self, client_name: str) -> str:
        """
        Atomically increment and return the next invoice ID for client_name.

        Takes the max of three floors:
          - local JSON counter (fast, same-process safety)
          - max number already in client_invoices.json (self-healing scan)
          - Supabase counter (survives redeploys)
        Each client starts at 2000; the first call returns 2001.
        Format: "<INITIALS>_<NUMBER>" when initials exist, else just "<NUMBER>".
        Example: "WMT_2001", "WMT_2002" ... or "2001" if no initials set.
        """
        sb_floor = self._sb_get_counter(client_name)
        with _lock:
            counters = _read_json(_CLIENT_COUNTERS_FILE)
            if not isinstance(counters, dict):
                counters = {}
            stored   = int(counters.get(client_name, 2000))
            current  = max(stored, self._max_issued_number(client_name), sb_floor)
            next_num = current + 1
            counters[client_name] = next_num
            _write_json(_CLIENT_COUNTERS_FILE, counters)

            initials = _read_json(_CLIENT_INITIALS_FILE)
            prefix = (initials.get(client_name, "") if isinstance(initials, dict) else "").strip().upper()

        self._sb_set_counter(client_name, next_num)
        return f"{prefix}_{next_num}" if prefix else str(next_num)

    def peek_client_invoice_number(self, client_name: str) -> str:
        """
        Return what the next invoice ID *would* be without incrementing the counter.
        Useful for previewing the ID before the user confirms.
        """
        sb_floor = self._sb_get_counter(client_name)
        with _lock:
            counters = _read_json(_CLIENT_COUNTERS_FILE)
            if not isinstance(counters, dict):
                counters = {}
            stored   = int(counters.get(client_name, 2000))
            current  = max(stored, self._max_issued_number(client_name), sb_floor)
            next_num = current + 1

            initials = _read_json(_CLIENT_INITIALS_FILE)
            prefix = (initials.get(client_name, "") if isinstance(initials, dict) else "").strip().upper()

        return f"{prefix}_{next_num}" if prefix else str(next_num)

    # ─────────────────────────────────────────
    # BILL OF LADING RECORDS  (Supabase)
    # ─────────────────────────────────────────

    def bol_message_id_exists(self, message_id: str) -> bool:
        resp = httpx.get(
            _sb_url("bol_records"),
            headers=_sb_headers(""),
            params={"message_id": f"eq.{message_id}", "select": "id", "limit": "1"},
            timeout=10,
        )
        resp.raise_for_status()
        return len(resp.json()) > 0

    def get_bol_records(self) -> list[dict]:
        resp = httpx.get(
            _sb_url("bol_records"),
            headers=_sb_headers(""),
            params={"order": "created_at.desc"},
            timeout=10,
        )
        resp.raise_for_status()
        return resp.json()

    def add_bol_record(self, record: dict) -> dict:
        record.setdefault("id", _new_id())
        record.setdefault("created_at", _now())
        resp = httpx.post(
            _sb_url("bol_records"),
            headers=_sb_headers(),
            json=record,
            timeout=10,
        )
        resp.raise_for_status()
        return resp.json()[0]

    def update_bol_record(self, id: str, updates: dict) -> dict:
        resp = httpx.patch(
            _sb_url("bol_records"),
            headers=_sb_headers(),
            params={"id": f"eq.{id}"},
            json=updates,
            timeout=10,
        )
        resp.raise_for_status()
        rows = resp.json()
        if not rows:
            raise KeyError(f"BOL record {id} not found.")
        return rows[0]

    def delete_bol_record(self, id: str) -> None:
        resp = httpx.delete(
            _sb_url("bol_records"),
            headers=_sb_headers(""),
            params={"id": f"eq.{id}"},
            timeout=10,
        )
        resp.raise_for_status()
