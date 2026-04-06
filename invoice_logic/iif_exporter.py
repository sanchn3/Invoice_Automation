"""
iif_exporter.py
===============
Generates QuickBooks Desktop 2018 compatible IIF files.
IIF is a tab-delimited format. One TRNS block per invoice,
one SPL line per line item.
"""

import logging
from datetime import datetime
from pathlib import Path

from config import EXPORTS_DIR
from data_manager import DataManager

logger = logging.getLogger(__name__)

# IIF column headers
_TRNS_HEADER = "!TRNS\tTRNSID\tTRNSTYPE\tDATE\tACCNT\tNAME\tAMOUNT\tDOCNUM\tMEMO"
_SPL_HEADER  = "!SPL\tSPLID\tTRNSTYPE\tDATE\tACCNT\tNAME\tAMOUNT\tDOCNUM\tMEMO\tQNTY\tPRICE"
_END_HEADER  = "!ENDTRNS"


def _fmt_date(iso_date: str) -> str:
    """Convert ISO date string to MM/DD/YYYY for QuickBooks."""
    try:
        dt = datetime.fromisoformat(iso_date.replace("Z", ""))
        return dt.strftime("%m/%d/%Y")
    except Exception:
        return datetime.now().strftime("%m/%d/%Y")


def generate_iif(client_invoice_ids: list[str], dm: DataManager) -> str:
    """
    Generate an IIF file for the given client invoice IDs.

    Raises ValueError if any invoice:
    - is already exported (quickbooks_exported=True)
    - is missing a quickbooks_invoice_number

    Returns the path to the generated .iif file.
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
                f"Invoice {inv_id} does not have a QuickBooks invoice number. "
                f"Enter the QB number in the admin dashboard before exporting."
            )
        invoices_to_export.append(inv)

    lines: list[str] = [_TRNS_HEADER, _SPL_HEADER, _END_HEADER]

    for inv in invoices_to_export:
        qb_num    = inv["quickbooks_invoice_number"]
        client    = inv["client_name"]
        inv_date  = _fmt_date(inv.get("invoice_date", inv.get("created_at", "")))
        total     = float(inv.get("total", 0))
        line_items: list[dict] = inv.get("line_items", [])
        memo      = f"Service: {inv.get('service_type', '').replace('_', '-').title()}"

        # TRNS row — debit Accounts Receivable
        lines.append(
            f"TRNS\t\tINVOICE\t{inv_date}\tAccounts Receivable\t"
            f"{client}\t{total:.2f}\t{qb_num}\t{memo}"
        )

        # SPL rows — credit Services for each line item
        for item in line_items:
            qty   = item.get("quantity", 1)
            price = item.get("unit_price", 0)
            itotal = item.get("total", 0)
            desc  = item.get("description", "Service")
            lines.append(
                f"SPL\t\tINVOICE\t{inv_date}\tServices\t"
                f"{client}\t-{itotal:.2f}\t{qb_num}\t{desc}\t{qty}\t{price:.2f}"
            )

        lines.append("ENDTRNS")

    # Write file
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename  = EXPORTS_DIR / f"invoices_export_{timestamp}.iif"
    content   = "\n".join(lines) + "\n"
    filename.write_text(content, encoding="utf-8")
    logger.info("IIF exported: %s (%d invoices)", filename, len(invoices_to_export))

    # Mark as exported
    for inv in invoices_to_export:
        dm.update_client_invoice(inv["id"], {
            "quickbooks_exported": True,
            "status"             : "exported_to_qb",
        })
        # Also update the linked email log
        ci = dm.get_client_invoice_by_id(inv["id"])
        if ci and ci.get("provider_invoice_id"):
            pi = dm.get_provider_invoice_by_id(ci["provider_invoice_id"])
            if pi and pi.get("email_intake_id"):
                dm.update_email_log(pi["email_intake_id"], {"status": "exported_to_qb"})

    return str(filename)
