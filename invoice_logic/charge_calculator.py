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
    "stamps"            : ("stamps_fee",               "Seal"),
}


def calculate_charges(
    dm: DataManager,
    service_type: str,
    pallet_count: int,
    temp_recorder: str | bool,
    extra_charges: list[str],
    damaged_pallets: int = 0,
    hours_overtime: int = 0,
    restack_count: int = 0,
    client_name: str = "",
) -> dict:
    """
    Calculate all charges for a client invoice.

    Parameters
    ----------
    service_type   : "transfer" or "in_out"
    pallet_count   : number of pallets
    temp_recorder  : whether a temperature recorder was installed
    extra_charges  : list of charge keys (see _EXTRA_CHARGE_MAP)
    damaged_pallets: count of damaged pallets (informational only, not billed)

    Returns
    -------
    dict with keys: line_items (list), subtotal (float), total (float)
    """
    rates = dm.get_rates_for_client(client_name) if client_name else dm.get_rate_card()
    line_items: list[dict] = []

    # ── Base service charge ──────────────────────────────────────────────────
    charged_by_pallet = bool(rates.get("charged_by_pallet", True))

    if charged_by_pallet:
        base_rate_key = "in_out" if service_type == "in_out" else "transfer"
        base_rate     = float(rates.get(base_rate_key, 0))
        base_label    = "In-Out Storage" if service_type == "in_out" else "Transfer (Truck-to-Truck)"
        line_items.append({
            "description": base_label,
            "quantity"   : pallet_count,
            "unit"       : "pallets",
            "unit_price" : base_rate,
            "total"      : round(base_rate * pallet_count, 2),
        })
    else:
        truck_cost = float(rates.get("cost_per_truck", 0))
        line_items.append({
            "description": "Truck Service",
            "quantity"   : 1,
            "unit"       : "truck",
            "unit_price" : truck_cost,
            "total"      : round(truck_cost, 2),
        })

    # ── Temperature recorder ─────────────────────────────────────────────────
    if temp_recorder:
        # backwards-compat: old bool True → hardware_installation
        _tr_type = temp_recorder if isinstance(temp_recorder, str) else "hardware_installation"
        _tr_fee_key = (
            "temp_recorder_installation_fee"
            if _tr_type == "installation_only"
            else "temp_recorder_hardware_fee"
        )
        # fallback to legacy key for old rate cards that haven't been migrated
        fee = float(rates.get(_tr_fee_key) or rates.get("temp_recorder_fee", 0))
        _tr_labels = {
            "hardware_installation": "Temp. Recorder — Hardware & Installation",
            "installation_only"    : "Temp. Recorder — Installation Only",
        }
        line_items.append({
            "description": _tr_labels.get(_tr_type, "Temperature Recorder"),
            "quantity"   : 1,
            "unit"       : "ea",
            "unit_price" : fee,
            "total"      : round(fee, 2),
        })

    # ── Extra charges ─────────────────────────────────────────────────────────
    for charge_key in extra_charges:
        if charge_key in _EXTRA_CHARGE_MAP:
            rate_key, label = _EXTRA_CHARGE_MAP[charge_key]
            fee = float(rates.get(rate_key, 0))
            line_items.append({
                "description": label,
                "quantity"   : 1,
                "unit"       : "ea",
                "unit_price" : fee,
                "total"      : round(fee, 2),
            })

    # ── Hours Overtime ────────────────────────────────────────────────────────
    if hours_overtime > 0:
        fee = float(rates.get("overtime_fee", 0))
        line_items.append({
            "description": "Hours Overtime",
            "quantity"   : hours_overtime,
            "unit"       : "hrs",
            "unit_price" : fee,
            "total"      : round(fee * hours_overtime, 2),
        })

    # ── Restack ───────────────────────────────────────────────────────────────
    if restack_count > 0:
        fee = float(rates.get("restack_fee", 0))
        line_items.append({
            "description": "Restack",
            "quantity"   : restack_count,
            "unit"       : "ea",
            "unit_price" : fee,
            "total"      : round(fee * restack_count, 2),
        })

    subtotal = round(sum(item["total"] for item in line_items), 2)

    return {
        "line_items": line_items,
        "subtotal"  : subtotal,
        "total"     : subtotal,  # no tax for now
    }
