"""
csv_exporter.py
===============
Generates QuickBooks Online invoice import CSV files.
One row per line item; rows sharing an invoice number belong to the same invoice.

Columns (per QBO import spec):
  InvoiceNo, Customer, InvoiceDate, DueDate, Terms, Memo,
  Item(Product/Service), ItemDescription, ItemQuantity, ItemRate,
  ItemAmount, ServiceDate

Guarantees:
  - ItemAmount == ItemQuantity × ItemRate (checked per row; fails loudly)
  - Sum of line amounts == invoice total   (checked per invoice; fails loudly)
  - Every line item must map to a QB product (get_qb_item raises if not)
  - UTF-8, all fields quoted, no trailing blank rows
"""

import csv
import io
import logging
import uuid
from datetime import datetime, timedelta
from pathlib import Path

from config import EXPORTS_DIR
from data_manager import DataManager
from invoice_logic.qb_product_map import get_qb_item, get_qb_description

logger = logging.getLogger(__name__)

_CSV_COLUMNS = [
    "InvoiceNo",
    "Customer",
    "InvoiceDate",
    "DueDate",
    "Terms",
    "Memo",
    "Item(Product/Service)",
    "ItemDescription",
    "ItemQuantity",
    "ItemRate",
    "ItemAmount",
    "ServiceDate",
]


def _fmt_date(iso_date: str) -> str:
    """Convert an ISO date string to MM/DD/YYYY."""
    try:
        dt = datetime.fromisoformat(iso_date.replace("Z", ""))
        return dt.strftime("%m/%d/%Y")
    except Exception:
        return datetime.now().strftime("%m/%d/%Y")


def _due_date_fmt(inv_date_iso: str, net_days: int) -> str:
    """Return DueDate as MM/DD/YYYY."""
    try:
        dt = datetime.fromisoformat(inv_date_iso.replace("Z", ""))
        return (dt + timedelta(days=net_days)).strftime("%m/%d/%Y")
    except Exception:
        return ""


def build_csv_content(invoices: list[dict]) -> str:
    """
    Build and return the CSV content for a list of invoice dicts.
    Pure function — no file I/O, no state changes.

    Raises ValueError if:
      - Any line item has no QB product mapping
      - ItemQuantity × ItemRate ≠ ItemAmount for any row
      - Sum of line amounts ≠ invoice total for any invoice
    """
    output = io.StringIO()
    writer = csv.writer(output, quoting=csv.QUOTE_ALL)
    writer.writerow(_CSV_COLUMNS)

    for inv in invoices:
        qb_num     = inv.get("quickbooks_invoice_number", "")
        client     = inv.get("client_name", "")
        inv_date   = inv.get("invoice_date") or inv.get("created_at", "")
        inv_date_fmt = _fmt_date(inv_date)
        net_days   = int(inv.get("net_days", 30))
        due_fmt    = _due_date_fmt(inv_date, net_days)
        terms      = f"Net {net_days}"
        memo       = inv.get("provider_invoice_number", "")
        line_items = inv.get("line_items") or []
        total      = round(float(inv.get("total", 0)), 2)

        # ── Invoice-level total check ─────────────────────────────────────────
        line_sum = round(sum(float(item.get("total", 0)) for item in line_items), 2)
        if abs(line_sum - total) > 0.01:
            raise ValueError(
                f"Invoice {qb_num}: line items sum to ${line_sum:.2f} "
                f"but invoice total is ${total:.2f}. Fix the invoice before exporting."
            )

        for item in line_items:
            desc   = item.get("description", "")
            unit   = item.get("unit", "")
            qty    = float(item.get("quantity", 1))
            rate   = float(item.get("unit_price", 0))
            amount = round(float(item.get("total", 0)), 2)

            # ── Row-level arithmetic check ────────────────────────────────────
            expected = round(qty * rate, 2)
            if abs(expected - amount) > 0.01:
                raise ValueError(
                    f"Invoice {qb_num}, item '{desc}': "
                    f"{qty} × ${rate:.2f} = ${expected:.2f} "
                    f"but stored amount is ${amount:.2f}. Fix the invoice before exporting."
                )

            # ── QB product lookup (raises if unmapped) ────────────────────────
            qb_item = get_qb_item(desc, unit)
            item_desc = get_qb_description(desc, unit) or desc

            writer.writerow([
                qb_num,
                client,
                inv_date_fmt,
                due_fmt,
                terms,
                memo,
                qb_item,
                item_desc,
                _fmt_qty(qty),
                f"{rate:.2f}",
                f"{amount:.2f}",
                inv_date_fmt,   # ServiceDate == InvoiceDate
            ])

    return output.getvalue()


def _fmt_qty(qty: float) -> str:
    """Return an integer string when qty is whole, otherwise 2 decimal places."""
    return str(int(qty)) if qty == int(qty) else f"{qty:.2f}"


def generate_csv(
    client_invoice_ids: list[str],
    dm: DataManager,
    _timestamp: str | None = None,
) -> dict:
    """
    Build a combined CSV for all given invoice IDs, save to disk, record an
    export batch (with pre-export snapshots for undo), and mark every invoice
    as exported.

    _timestamp: YYYYMMDD_HHMMSS string.  If supplied by the caller (e.g. the
    dashboard pre-computed it for the download filename), the saved file will
    use the same timestamp so the name the user sees matches the file on disk.

    Raises ValueError if any invoice:
      - is not found
      - is already exported (quickbooks_exported=True)
      - is missing a quickbooks_invoice_number

    Returns a dict with keys:
        content   — CSV text (UTF-8 string)
        filename  — filename saved to disk
        qb_nums   — list of QB invoice numbers that were exported
        batch_id  — ID of the new export batch record
    """
    invoices_to_export: list[dict] = []

    for inv_id in client_invoice_ids:
        inv = dm.get_client_invoice_by_id(inv_id)
        if inv is None:
            raise ValueError(f"Client invoice {inv_id} not found.")
        if inv.get("quickbooks_exported"):
            raise ValueError(
                f"Invoice {inv_id} (QB# {inv.get('quickbooks_invoice_number')}) "
                f"has already been exported to QuickBooks."
            )
        if not inv.get("quickbooks_invoice_number"):
            raise ValueError(
                f"Invoice {inv_id} does not have an invoice number. "
                f"Generate the invoice in the admin dashboard before exporting."
            )
        invoices_to_export.append(inv)

    # Enrich each invoice with its provider invoice number for the Memo column
    enriched: list[dict] = []
    for inv in invoices_to_export:
        prov = dm.get_provider_invoice_by_id(inv.get("provider_invoice_id", ""))
        enriched.append({**inv, "provider_invoice_number": (prov or {}).get("invoice_number", "")})

    content = build_csv_content(enriched)

    # Capture pre-export snapshots BEFORE any state changes (used by Undo)
    pre_export_snapshots: list[dict] = []
    for inv in invoices_to_export:
        snap: dict = {
            "invoice_id"      : inv["id"],
            "fields"          : {
                "quickbooks_exported": inv.get("quickbooks_exported", False),
                "status"             : inv.get("status"),
                "ready_to_email"     : inv.get("ready_to_email", False),
            },
            "email_log_id"    : None,
            "email_log_fields": {},
        }
        pi_id = inv.get("provider_invoice_id", "")
        if pi_id:
            pi = dm.get_provider_invoice_by_id(pi_id)
            if pi and pi.get("email_intake_id"):
                el = dm.get_email_log_by_id(pi["email_intake_id"])
                if el:
                    snap["email_log_id"]     = el["id"]
                    snap["email_log_fields"] = {"status": el.get("status")}
        pre_export_snapshots.append(snap)

    EXPORTS_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = _timestamp or datetime.now().strftime("%Y%m%d_%H%M%S")
    qb_nums   = [inv.get("quickbooks_invoice_number", inv["id"]) for inv in invoices_to_export]
    filename  = f"QB_export_{timestamp}_{len(qb_nums)}inv.csv"
    filepath  = EXPORTS_DIR / filename
    with open(filepath, "w", encoding="utf-8", newline="") as _f:
        _f.write(content)
    logger.info("CSV exported: %s (%d invoice(s))", filepath, len(invoices_to_export))

    # Mark all as exported
    for inv in invoices_to_export:
        dm.update_client_invoice(inv["id"], {
            "quickbooks_exported": True,
            "status"             : "exported_to_qb",
            "ready_to_email"     : True,
        })
        ci = dm.get_client_invoice_by_id(inv["id"])
        if ci and ci.get("provider_invoice_id"):
            pi = dm.get_provider_invoice_by_id(ci["provider_invoice_id"])
            if pi and pi.get("email_intake_id"):
                dm.update_email_log(pi["email_intake_id"], {"status": "exported_to_qb"})

    # Record the export batch (includes CSV content for re-download even if file is lost)
    batch_id = str(uuid.uuid4())
    batch    = {
        "id"                  : batch_id,
        "created_at"          : datetime.now().isoformat(),
        "filename"            : filename,
        "content"             : content,
        "qb_nums"             : qb_nums,
        "invoice_ids"         : [inv["id"] for inv in invoices_to_export],
        "total"               : round(sum(float(inv.get("total", 0)) for inv in invoices_to_export), 2),
        "line_count"          : sum(len(inv.get("line_items", [])) for inv in invoices_to_export),
        "pre_export_snapshots": pre_export_snapshots,
    }
    dm.add_export_batch(batch)

    return {
        "content" : content,
        "filename": filename,
        "qb_nums" : qb_nums,
        "batch_id": batch_id,
    }
