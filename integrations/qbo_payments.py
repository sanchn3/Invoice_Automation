"""
qbo_payments.py
===============
Payment operations against the QBO v3 REST API.

Public API
----------
get_payment_methods(client, dm)
    Return [{value: Id, name: "Check"}, ...] from QBO PaymentMethod, cached.

create_payment(client, invoice_id, customer_id, amount, txn_date,
               method_ref, payment_ref, deposit_ref)
    POST a Payment to QBO linked to the invoice. Returns the QBO Payment dict.

get_payment(client, payment_id)
    Fetch a single QBO Payment by Id.

delete_payment(client, payment_id, sync_token)
    Void/delete the QBO Payment. QBO requires the current SyncToken.

get_invoice_balance(client, qbo_invoice_id)
    Return the current Balance (float) for a QBO invoice.
"""

import logging
from datetime import date

logger = logging.getLogger(__name__)


# ── Payment methods ───────────────────────────────────────────────────────────

def get_payment_methods(client, dm=None) -> list[dict]:
    """
    Return a list of {"value": Id, "name": method_name} dicts from QBO,
    sorted alphabetically. Results are cached in DataManager.
    """
    if dm:
        cache   = dm.get_qbo_cache()
        methods = cache.get("payment_methods")
        if methods:
            return methods

    rows    = client.query("SELECT Id, Name FROM PaymentMethod WHERE Active = true")
    methods = sorted(
        [{"value": r["Id"], "name": r["Name"]} for r in rows],
        key=lambda m: m["name"],
    )

    if dm and methods:
        dm.update_qbo_cache({"payment_methods": methods})

    return methods


# ── Create payment ────────────────────────────────────────────────────────────

def create_payment(
    client,
    *,
    qbo_invoice_id: str,
    customer_id   : str,
    amount        : float,
    txn_date      : str | None = None,
    method_ref    : dict | None = None,
    payment_ref   : str = "",
    deposit_ref   : dict,
) -> dict:
    """
    POST a QBO Payment linked to a specific invoice.

    Returns the QBO Payment dict (under key "Payment").

    Parameters
    ----------
    qbo_invoice_id : QBO Invoice Id (stored on our invoice record)
    customer_id    : QBO Customer Id
    amount         : payment amount (may be less than balance for partial)
    txn_date       : ISO date string; defaults to today
    method_ref     : {"value": Id, "name": "Check"} or None
    payment_ref    : check number / transfer reference
    deposit_ref    : {"value": Id, "name": "Undeposited Funds"} — required
    """
    txn_date = txn_date or date.today().isoformat()

    body: dict = {
        "CustomerRef"       : {"value": customer_id},
        "TotalAmt"          : round(amount, 2),
        "TxnDate"           : txn_date,
        "DepositToAccountRef": deposit_ref,
        "Line"              : [
            {
                "Amount"    : round(amount, 2),
                "LinkedTxn" : [
                    {
                        "TxnId"  : qbo_invoice_id,
                        "TxnType": "Invoice",
                    }
                ],
            }
        ],
    }
    if method_ref:
        body["PaymentMethodRef"] = method_ref
    if payment_ref:
        body["PaymentRefNum"] = payment_ref

    logger.info(
        "Creating QBO payment  invoice_id=%s  amount=%.2f  method=%s",
        qbo_invoice_id, amount, (method_ref or {}).get("name", "—"),
    )
    response = client._post("payment", body)
    pmt      = response["Payment"]
    logger.info("QBO payment created  Id=%s", pmt.get("Id"))
    return pmt


# ── Fetch single payment ──────────────────────────────────────────────────────

def get_payment(client, payment_id: str) -> dict:
    """Fetch a QBO Payment by Id."""
    return client._get(f"payment/{payment_id}")["Payment"]


# ── Delete (undo) payment ──────────────────────────────────────────────────────

def delete_payment(client, payment_id: str, sync_token: str) -> None:
    """
    Delete a QBO Payment by posting with operation=delete.
    Requires the current SyncToken to prevent blind overwrites.
    """
    logger.info("Deleting QBO payment  Id=%s  SyncToken=%s", payment_id, sync_token)
    client._delete(
        "payment",
        body={"Id": payment_id, "SyncToken": sync_token},
    )
    logger.info("QBO payment deleted  Id=%s", payment_id)


# ── Balance check ──────────────────────────────────────────────────────────────

def get_invoice_balance(client, qbo_invoice_id: str) -> float:
    """
    Return the current Balance for a QBO invoice (0.0 if fully paid).
    """
    from .qbo_invoices import get_invoice
    inv = get_invoice(client, qbo_invoice_id)
    return float(inv.get("Balance", 0))
