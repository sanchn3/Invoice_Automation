"""
qb_product_map.py
=================
Single source of truth: maps every line item the charge calculator can produce
to a QuickBooks Online product path.

Table columns: internal_key | qb_name | category | sku

get_qb_item(description, unit) returns  "Category:Product name"
— the exact format QBO expects in the Item(Product/Service) import column.
No spaces around the colon; both parts are stripped of whitespace.

If a new service is added to charge_calculator.py:
  1. Add a row to QB_PRODUCTS.
  2. Add a (description, unit) → internal_key entry to _DESCRIPTION_MAP.
The export will refuse to run until every line item resolves.
"""

# ── Product catalogue ─────────────────────────────────────────────────────────
# (internal_key, qb_name, category, sku)
QB_PRODUCTS: tuple[tuple[str, str, str, str], ...] = (
    ("in_out_truck",               "In-Out Storage",                                "Cold Storage",            "STR-100"),
    ("in_out_pallet",              "In-Out Storage by Pallet",                      "Cold Storage",            "STR-120"),
    ("transfer",                   "Truck-to-Truck Transfer",                        "Handling & Labor",        "HDL-100"),
    ("extended_storage",           "Extra Storage",                                  "Cold Storage",            "STR-110"),
    ("quality_inspection",         "Quality Check Report",                           "Inspection & Compliance", "INS-100"),
    ("seals",                      "Seal",                                           "Supplies & Equipment",    "SUP-150"),
    ("american_broker_fee",        "American Broker Fee",                            "Brokerage & Customs",     "BRK-100"),
    ("mexican_broker_fee",         "Mexican Broker Fee",                             "Brokerage & Customs",     "BRK-110"),
    ("temp_recorder_hardware",     "Temperature Recorder Hardware and Installation", "Supplies & Equipment",    "SUP-110"),
    ("temp_recorder_installation", "Temperature Recorder Installation",              "Supplies & Equipment",    "SUP-100"),
    ("overtime_fee",               "Overtime",                                       "Handling & Labor",        "HDL-300"),
    ("restack_fee",                "Restack Leaning Pallets",                        "Handling & Labor",        "HDL-140"),
)

# ── internal_key → "Category:Product name" ───────────────────────────────────
# Built from QB_PRODUCTS so category is never duplicated.
_KEY_TO_PATH: dict[str, str] = {
    key: f"{category.strip()}:{name.strip()}"
    for key, name, category, _sku in QB_PRODUCTS
}

# ── (description, unit) → internal_key ───────────────────────────────────────
# description matches line_items[].description produced by charge_calculator.py.
# unit disambiguates "In-Out Storage" (pallets vs. truck).
_DESCRIPTION_MAP: dict[tuple[str, str], str] = {
    ("In-Out Storage",                           "pallets"): "in_out_pallet",
    ("In-Out Storage",                           "truck")  : "in_out_truck",
    ("Transfer (Truck-to-Truck)",                "truck")  : "transfer",
    ("Extended Storage",                         "pallet") : "extended_storage",
    ("Quality Inspection",                       "ea")     : "quality_inspection",
    ("Seal",                                     "ea")     : "seals",
    ("American Broker Fee",                      "ea")     : "american_broker_fee",
    ("Mexican Broker Fee",                       "ea")     : "mexican_broker_fee",
    ("Temp. Recorder \u2014 Hardware & Installation", "ea"): "temp_recorder_hardware",
    ("Temp. Recorder \u2014 Installation Only",  "ea")     : "temp_recorder_installation",
    ("Hours Overtime",                           "hrs")    : "overtime_fee",
    ("Restack",                                  "ea")     : "restack_fee",
}


def get_qb_item(description: str, unit: str) -> str:
    """
    Return the QuickBooks import path "Category:Product name" for a line item.

    Raises ValueError naming the unrecognised description+unit pair so the
    export fails loudly rather than writing an unmapped row.
    """
    internal_key = _DESCRIPTION_MAP.get((description.strip(), unit.strip()))
    if internal_key is None:
        raise ValueError(
            f"No QuickBooks product mapping for line item "
            f"description={description!r}, unit={unit!r}. "
            "Add it to invoice_logic/qb_product_map.py before exporting."
        )
    # _KEY_TO_PATH is always complete if QB_PRODUCTS and _DESCRIPTION_MAP are in sync
    return _KEY_TO_PATH[internal_key]
