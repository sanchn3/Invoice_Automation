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
from datetime import datetime
from pathlib import Path
from typing import Any

from config import DATA_DIR

# File paths
_EMAIL_LOG_FILE        = DATA_DIR / "email_intake_log.json"
_PROVIDER_INVOICES_FILE = DATA_DIR / "provider_invoices.json"
_CLIENT_INVOICES_FILE  = DATA_DIR / "client_invoices.json"
_PROVIDERS_FILE        = DATA_DIR / "providers.json"
_RATE_CARD_FILE        = DATA_DIR / "rate_card.json"
_CLIENT_RATES_FILE     = DATA_DIR / "client_rates.json"

_lock = threading.Lock()


def _now() -> str:
    return datetime.utcnow().isoformat() + "Z"


def _new_id() -> str:
    return str(uuid.uuid4())


def _read_json(path: Path) -> Any:
    if not path.exists():
        return [] if path.name != "rate_card.json" else {}
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _write_json(path: Path, data: Any) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


class DataManager:
    # ─────────────────────────────────────────
    # EMAIL INTAKE LOG
    # ─────────────────────────────────────────

    def get_email_logs(self) -> list[dict]:
        with _lock:
            return _read_json(_EMAIL_LOG_FILE)

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

    def message_id_exists(self, message_id: str) -> bool:
        return any(
            log.get("message_id") == message_id
            for log in self.get_email_logs()
        )

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
            return record

    def delete_provider_invoice(self, id: str) -> None:
        with _lock:
            invoices = _read_json(_PROVIDER_INVOICES_FILE)
            invoices = [inv for inv in invoices if inv["id"] != id]
            _write_json(_PROVIDER_INVOICES_FILE, invoices)

    def update_provider_invoice(self, id: str, updates: dict) -> dict:
        with _lock:
            invoices = _read_json(_PROVIDER_INVOICES_FILE)
            for i, inv in enumerate(invoices):
                if inv["id"] == id:
                    invoices[i].update(updates)
                    _write_json(_PROVIDER_INVOICES_FILE, invoices)
                    return invoices[i]
            raise KeyError(f"Provider invoice {id} not found.")

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
            return record

    def update_client_invoice(self, id: str, updates: dict) -> dict:
        with _lock:
            invoices = _read_json(_CLIENT_INVOICES_FILE)
            for i, inv in enumerate(invoices):
                if inv["id"] == id:
                    invoices[i].update(updates)
                    _write_json(_CLIENT_INVOICES_FILE, invoices)
                    return invoices[i]
            raise KeyError(f"Client invoice {id} not found.")

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
            data = _read_json(_CLIENT_RATES_FILE)
            return data if isinstance(data, dict) else {}

    def get_rates_for_client(self, client_name: str) -> dict:
        """
        Returns the effective rate card for a client:
        default rate card merged with any client-specific overrides.
        """
        defaults  = self.get_rate_card()
        overrides = self.get_client_rates().get(client_name, {})
        return {**defaults, **overrides}

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
