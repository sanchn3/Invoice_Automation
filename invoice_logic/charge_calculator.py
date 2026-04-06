"""
charge_calculator.py
====================
Computes client invoice charges from the rate card.
Reads rate_card.json via DataManager — never directly.
"""

from data_manager import DataManager

# Maps extra_charge keys to rate_card keys and human labels
_EXTRA_CHARGE_MAP = {
    "quality_inspection": ("quality_inspection_fee", "Quality Inspection"),
    "pallet_cleaning"   : ("pallet_cleaning_fee",    "Pallet Cleaning"),
    "re_inspection"     : ("re_inspection_fee",       "Re-Inspection"),
    "repacking"         : ("repacking_fee",            "Repacking"),
}


def calculate_charges(
    dm: DataManager,
    service_type: str,
    pallet_count: int,
    temp_recorder: bool,
    extra_charges: list[str],
    damaged_pallets: int = 0,
    broken_pallets: int = 0,
) -> dict:
    """
    Calculate all charges for a client invoice.

    Parameters
    ----------
    service_type   : "transfer" or "in_out"
    pallet_count   : number of pallets
    temp_recorder  : whether a temperature recorder was installed
    extra_charges  : list of charge keys (see _EXTRA_CHARGE_MAP + "broken_pallets")
    damaged_pallets: count of damaged pallets (informational only, not billed)
    broken_pallets : count of broken pallets (billed per unit)

    Returns
    -------
    dict with keys: line_items (list), subtotal (float), total (float)
    """
    rates = dm.get_rate_card()
    line_items: list[dict] = []

    # ── Base service charge ──────────────────────────────────────────────────
    base_rate_key = "in_out" if service_type == "in_out" else "transfer"
    base_rate     = float(rates.get(base_rate_key, 0))
    base_label    = "In-Out Storage" if service_type == "in_out" else "Transfer (Truck-to-Truck)"
    base_total    = base_rate * pallet_count

    line_items.append({
        "description": base_label,
        "quantity"   : pallet_count,
        "unit_price" : base_rate,
        "total"      : round(base_total, 2),
    })

    # ── Temperature recorder ─────────────────────────────────────────────────
    if temp_recorder:
        fee = float(rates.get("temp_recorder_fee", 0))
        line_items.append({
            "description": "Temperature Recorder",
            "quantity"   : 1,
            "unit_price" : fee,
            "total"      : round(fee, 2),
        })

    # ── Extra charges ─────────────────────────────────────────────────────────
    for charge_key in extra_charges:
        if charge_key == "broken_pallets":
            if broken_pallets > 0:
                fee = float(rates.get("broken_pallet_fee", 0))
                line_items.append({
                    "description": "Broken Pallets",
                    "quantity"   : broken_pallets,
                    "unit_price" : fee,
                    "total"      : round(fee * broken_pallets, 2),
                })
        elif charge_key in _EXTRA_CHARGE_MAP:
            rate_key, label = _EXTRA_CHARGE_MAP[charge_key]
            fee = float(rates.get(rate_key, 0))
            line_items.append({
                "description": label,
                "quantity"   : 1,
                "unit_price" : fee,
                "total"      : round(fee, 2),
            })

    subtotal = round(sum(item["total"] for item in line_items), 2)

    return {
        "line_items": line_items,
        "subtotal"  : subtotal,
        "total"     : subtotal,  # no tax for now
    }
