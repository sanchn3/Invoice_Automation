"""
qbo_sync.py
===========
Keeps local invoice paid/balance status in sync with QuickBooks.

sync_balances(dm, client, max_age_seconds=180)
    For every unpaid local invoice that has a qbo_invoice_id, fetch the
    current QBO balance and update:
        qbo_balance         → float (current balance from QBO)
        paid                → True when balance reaches 0
        paid_partial_amount → total paid so far (TotalAmt - Balance)
    Skips the refresh if it ran less than max_age_seconds ago.
    Returns the number of invoices whose status changed.
"""

import logging
import time

logger = logging.getLogger(__name__)

_LAST_SYNC_KEY = "_qbo_last_sync_ts"


def sync_balances(dm, client, max_age_seconds: int = 180) -> int:
    """
    Refresh QBO balances for all unpaid invoices that have a qbo_invoice_id.

    dm                : DataManager
    client            : QBOClient
    max_age_seconds   : skip if last sync was more recent than this
    Returns           : number of invoices whose local state changed
    """
    # Throttle: skip if recently synced (uses a module-level timestamp)
    global _last_sync_ts
    now = time.time()
    if hasattr(sync_balances, "_last_sync_ts"):
        if now - sync_balances._last_sync_ts < max_age_seconds:
            return 0
    sync_balances._last_sync_ts = now

    invoices = dm.get_client_invoices()
    candidates = [
        ci for ci in invoices
        if ci.get("qbo_invoice_id") and not ci.get("paid")
    ]

    if not candidates:
        return 0

    logger.info("QBO sync: checking balances for %d invoice(s)", len(candidates))
    changed = 0

    for ci in candidates:
        try:
            from .qbo_invoices import get_invoice
            qbo_inv  = get_invoice(client, ci["qbo_invoice_id"])
            balance  = float(qbo_inv.get("Balance", 0))
            total    = float(qbo_inv.get("TotalAmt", 0))
            paid_amt = round(total - balance, 2)

            updates: dict = {"qbo_balance": balance}
            if balance <= 0:
                updates["paid"]                = True
                updates["paid_partial_amount"] = total
            elif paid_amt > 0:
                updates["paid_partial_amount"] = paid_amt

            # Only write if something changed
            current_balance = ci.get("qbo_balance")
            if current_balance != balance or (balance <= 0 and not ci.get("paid")):
                dm.update_client_invoice(ci["id"], updates)
                changed += 1
                logger.debug(
                    "QBO sync: invoice %s balance %.2f → %.2f",
                    ci.get("quickbooks_invoice_number"), current_balance or 0, balance,
                )
        except Exception as exc:
            logger.warning(
                "QBO sync failed for invoice %s: %s",
                ci.get("quickbooks_invoice_number"), exc,
            )

    if changed:
        logger.info("QBO sync: updated %d invoice(s)", changed)
    return changed
