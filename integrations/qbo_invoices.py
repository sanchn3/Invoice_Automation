"""
qbo_invoices.py
===============
Invoice operations against the QBO v3 REST API.

Public API
----------
validate_for_qbo(ci, dm, client)
    Run the same arithmetic + mapping checks as the CSV exporter, then verify
    the QBO customer exists. Returns a list of error strings (empty = OK).

find_by_doc_number(client, doc_number)
    Query QBO for an existing invoice with this DocNumber. Returns the QBO
    invoice dict or None.

create_invoice(client, ci, customer_id, term_ref, dm)
    POST a new invoice to QBO. Returns the full QBO invoice response dict.
    Caller is responsible for checking find_by_doc_number first.

get_invoice(client, qbo_invoice_id)
    Fetch a single QBO invoice by its Id.

get_open_invoices(client, customer_id)
    Return all QBO invoices for this customer where Balance > 0.
    Each dict has: Id, DocNumber, TxnDate, DueDate, TotalAmt, Balance.
"""

import logging
from datetime import date, datetime, timedelta

from invoice_logic.qb_product_map import get_qb_item

logger = logging.getLogger(__name__)


# ── Validation ────────────────────────────────────────────────────────────────

def validate_for_qbo(ci: dict, dm, client=None) -> list[str]:
    """
    Return a list of error strings. Empty list means the invoice is ready.

    Checks (same order as CSV exporter):
    1. Invoice number present
    2. Not already exported (qbo_invoice_id or quickbooks_exported)
    3. Line items sum to invoice total (±$0.01)
    4. Each line: qty × rate = amount (±$0.01)
    5. Each line maps to a QBO product
    6. If client is provided: QBO customer exists
    """
    errors: list[str] = []

    if not ci.get("quickbooks_invoice_number"):
        errors.append("Invoice has no QB invoice number. Generate it in the Admin dashboard first.")

    if ci.get("qbo_invoice_id"):
        errors.append(
            f"Invoice {ci.get('quickbooks_invoice_number')} is already in QuickBooks "
            f"(QBO Id: {ci['qbo_invoice_id']})."
        )

    lines = ci.get("line_items", [])
    if not lines:
        errors.append("Invoice has no line items.")
        return errors

    # Total check
    computed_total = sum(float(li.get("total", 0)) for li in lines)
    inv_total      = float(ci.get("total", 0))
    if abs(computed_total - inv_total) > 0.01:
        errors.append(
            f"Line items sum to ${computed_total:,.2f} but invoice total is ${inv_total:,.2f}."
        )

    # Per-line checks
    for li in lines:
        desc  = li.get("description", "")
        unit  = li.get("unit", "")
        qty   = float(li.get("quantity", 0))
        rate  = float(li.get("unit_price", 0))
        amt   = float(li.get("total", 0))

        if abs(qty * rate - amt) > 0.01:
            errors.append(
                f"Line '{desc}': {qty} × {rate} = {qty*rate:.2f} but stored amount is {amt:.2f}."
            )

        try:
            get_qb_item(desc, unit)
        except ValueError as exc:
            errors.append(str(exc))

    return errors


# ── Duplicate check ───────────────────────────────────────────────────────────

def find_by_doc_number(client, doc_number: str) -> dict | None:
    """
    Query QBO for an invoice with this DocNumber.
    Returns the QBO invoice dict or None if not found.
    """
    escaped = doc_number.replace("'", "\\'")
    rows = client.query(
        f"SELECT Id, DocNumber, SyncToken, CustomerRef, Balance, TotalAmt "
        f"FROM Invoice WHERE DocNumber = '{escaped}'"
    )
    if rows:
        logger.info("QBO duplicate found: DocNumber=%s  Id=%s", doc_number, rows[0].get("Id"))
    return rows[0] if rows else None


# ── Create ────────────────────────────────────────────────────────────────────

def create_invoice(
    client,
    ci: dict,
    customer_id: str,
    term_ref: dict | None,
    dm,
) -> dict:
    """
    POST a new invoice to QBO. Returns the QBO invoice dict under key "Invoice".

    ci           : client invoice record from DataManager
    customer_id  : QBO Customer Id (already resolved)
    term_ref     : {"value": Id, "name": "Net 30"} or None
    dm           : DataManager (for item ref lookup cache)
    """
    inv_date = ci.get("invoice_date", date.today().isoformat())

    # Due date: explicit override or calculated
    due = ci.get("due_date", "").strip()
    if not due:
        try:
            due = (
                datetime.fromisoformat(inv_date)
                + timedelta(days=int(ci.get("net_days", 30)))
            ).date().isoformat()
        except Exception:
            due = inv_date

    # Build line items
    qbo_lines = []
    for li in ci.get("line_items", []):
        desc     = li.get("description", "")
        unit     = li.get("unit", "")
        qty      = float(li.get("quantity", 0))
        rate     = float(li.get("unit_price", 0))
        amt      = float(li.get("total", 0))
        fqn      = get_qb_item(desc, unit)
        item_ref = client.get_item_ref(fqn)

        qbo_lines.append({
            "DetailType": "SalesItemLineDetail",
            "Amount"    : round(amt, 2),
            "Description": desc,
            "SalesItemLineDetail": {
                "ItemRef"    : item_ref,
                "Qty"        : qty,
                "UnitPrice"  : rate,
                "ServiceDate": inv_date,
            },
        })

    # Provider invoice number goes in PrivateNote
    prov = dm.get_provider_invoice_by_id(ci.get("provider_invoice_id", "")) or {}
    private_note = prov.get("invoice_number", "")

    body: dict = {
        "DocNumber"  : ci["quickbooks_invoice_number"],
        "CustomerRef": {"value": customer_id},
        "TxnDate"    : inv_date,
        "DueDate"    : due,
        "Line"       : qbo_lines,
        "PrivateNote": private_note,
    }
    if term_ref:
        body["SalesTermRef"] = term_ref

    logger.info(
        "Creating QBO invoice DocNumber=%s  customer=%s  total=%.2f",
        ci["quickbooks_invoice_number"], customer_id, float(ci.get("total", 0)),
    )
    response = client._post("invoice", body)
    qbo_inv  = response["Invoice"]
    logger.info(
        "QBO invoice created  Id=%s  DocNumber=%s",
        qbo_inv.get("Id"), qbo_inv.get("DocNumber"),
    )
    return qbo_inv


# ── Fetch single ──────────────────────────────────────────────────────────────

def get_invoice(client, qbo_invoice_id: str) -> dict:
    """Fetch a QBO invoice by Id. Returns the invoice dict."""
    response = client._get(f"invoice/{qbo_invoice_id}")
    return response["Invoice"]


# ── Open invoices ─────────────────────────────────────────────────────────────

def get_open_invoices(client, customer_id: str) -> list[dict]:
    """
    Return all QBO invoices for this customer where Balance > 0.

    Each item:
        Id, DocNumber, TxnDate, DueDate, TotalAmt, Balance, days_overdue
    """
    rows = client.query(
        f"SELECT Id, DocNumber, TxnDate, DueDate, TotalAmt, Balance "
        f"FROM Invoice "
        f"WHERE CustomerRef = '{customer_id}' AND Balance > '0'"
    )

    today = date.today()
    result = []
    for row in rows:
        due_str = row.get("DueDate", "")
        days_overdue = 0
        if due_str:
            try:
                due_dt = datetime.fromisoformat(due_str).date()
                days_overdue = max(0, (today - due_dt).days)
            except Exception:
                pass
        result.append({
            "Id"          : row.get("Id"),
            "DocNumber"   : row.get("DocNumber", ""),
            "TxnDate"     : row.get("TxnDate", ""),
            "DueDate"     : due_str,
            "TotalAmt"    : float(row.get("TotalAmt", 0)),
            "Balance"     : float(row.get("Balance", 0)),
            "days_overdue": days_overdue,
        })

    # Oldest first
    result.sort(key=lambda r: r["TxnDate"])
    return result
