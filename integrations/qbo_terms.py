"""
qbo_terms.py
============
Look up QBO SalesTerms and cache the net-days → Term Id mapping.
"""

import logging

logger = logging.getLogger(__name__)


def get_term_ref(client, net_days: int, dm=None) -> dict | None:
    """
    Return {"value": QBO_Term_Id, "name": "Net 30"} for the given net_days,
    or None if no matching term is found in QBO.

    client : QBOClient instance
    dm     : DataManager instance for caching (optional)
    """
    # Check cache first
    cache_key = str(net_days)
    if dm:
        cache = dm.get_qbo_cache()
        cached = cache.get("term_ids", {}).get(cache_key)
        if cached:
            return cached

    # Fetch all terms from QBO
    rows = client.query("SELECT Id, Name, DueDays FROM Term WHERE Active = true")

    # Match by DueDays first, then by name containing the number
    for row in rows:
        if row.get("DueDays") == net_days:
            ref = {"value": row["Id"], "name": row["Name"]}
            if dm:
                dm.update_qbo_cache({"term_ids": {cache_key: ref}})
            return ref

    # Fallback: match "Net 30" style names
    target_name = f"Net {net_days}"
    for row in rows:
        if row.get("Name", "").strip().lower() == target_name.lower():
            ref = {"value": row["Id"], "name": row["Name"]}
            if dm:
                dm.update_qbo_cache({"term_ids": {cache_key: ref}})
            return ref

    logger.warning("No QBO term found for net_days=%d", net_days)
    return None
