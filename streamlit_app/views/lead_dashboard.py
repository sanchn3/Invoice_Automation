"""
lead_dashboard.py
=================
Lead dashboard: Reports, Rate Card editor, and Settings.
"""

import streamlit as st
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

from data_manager import DataManager
from alerting.alert_manager import AlertManager
from invoice_logic.pdf_generator import generate_pdf as _generate_pdf
from invoice_logic.iif_exporter import build_iif_content


def _colored_btn(container, label: str, key: str, color: str, **kwargs) -> bool:
    anchor = "ld_" + "".join(c if c.isalnum() or c == "_" else "_" for c in key)
    container.markdown(
        f"<span id='{anchor}'></span>"
        f"<style>"
        f"[data-testid='element-container']:has(span#{anchor})"
        f" + [data-testid='element-container'] button,"
        f"[data-testid='stColumn']:has(span#{anchor}) button"
        f"{{background-color:{color}!important;"
        f"border-color:{color}!important;"
        f"color:white!important;}}"
        f"[data-testid='element-container']:has(span#{anchor})"
        f" + [data-testid='element-container'] button:hover,"
        f"[data-testid='stColumn']:has(span#{anchor}) button:hover"
        f"{{filter:brightness(1.12)!important;}}"
        f"</style>",
        unsafe_allow_html=True,
    )
    kwargs.setdefault("type", "primary")
    return container.button(label, key=key, **kwargs)


@st.cache_data(show_spinner=False)
def _cached_pdf(
    ci_id: str,
    qb_num: str,
    total: float,
    provider_pdf_path: str | None,
    invoice_date: str,
    due_date: str,
    po_number: str,
    _ci: dict,
) -> bytes:
    return _generate_pdf(_ci, provider_pdf_path)


def _pdf_args(ci: dict, prov: dict | None) -> tuple:
    _pdf_path = (prov or {}).get("pdf_local_path", "")
    return (
        ci["id"],
        ci.get("quickbooks_invoice_number", ""),
        float(ci.get("total", 0)),
        _pdf_path if _pdf_path and Path(_pdf_path).exists() else None,
        ci.get("invoice_date", ""),
        ci.get("due_date", ""),
        ci.get("po_number", ""),
        ci,
    )

def _lifecycle_html(ci: dict) -> str:
    """Return an HTML progress-stepper for the 7 invoice lifecycle stages."""
    stages = [
        ("Polled",       True),
        ("Received",     bool(ci.get("received_date"))),
        ("Approved",     bool(ci.get("quickbooks_invoice_number"))),
        ("→ Accounting", bool(ci.get("quickbooks_invoice_number"))),
        ("QB Export",    bool(ci.get("quickbooks_exported"))),
        ("Emailed",      bool(ci.get("emailed"))),
        ("Paid",         bool(ci.get("paid"))),
    ]

    GREEN = "#198754"
    BLUE  = "#0d6efd"
    GREY  = "#ced4da"
    GTXT  = "#198754"
    BTXT  = "#0d6efd"
    PTXT  = "#adb5bd"

    first_pending = next(
        (i for i, (_, done) in enumerate(stages) if not done),
        len(stages),
    )

    parts = [
        '<div style="display:flex;align-items:flex-start;'
        'width:100%;padding:6px 0 2px 0;gap:0;">'
    ]
    for i, (label, done) in enumerate(stages):
        is_active = i == first_pending
        is_last   = i == len(stages) - 1

        if done:
            bg, txt, sym, lbl_c = GREEN, "white", "✓", GTXT
        elif is_active:
            bg, txt, sym, lbl_c = BLUE, "white", str(i + 1), BTXT
        else:
            bg, txt, sym, lbl_c = GREY, "#6c757d", str(i + 1), PTXT

        parts.append(
            f'<div style="display:flex;flex-direction:column;'
            f'align-items:center;flex-shrink:0;min-width:52px;">'
            f'<div style="width:22px;height:22px;border-radius:50%;'
            f'background:{bg};display:flex;align-items:center;'
            f'justify-content:center;color:{txt};font-size:10px;'
            f'font-weight:700;">{sym}</div>'
            f'<span style="color:{lbl_c};font-size:9px;margin-top:3px;'
            f'text-align:center;line-height:1.2;">{label}</span>'
            f'</div>'
        )
        if not is_last:
            conn = GREEN if done else GREY
            parts.append(
                f'<div style="flex:1;height:2px;background:{conn};'
                f'margin-top:10px;min-width:4px;"></div>'
            )

    parts.append('</div>')
    return "".join(parts)


# Canonical client-name aliases (must stay in sync with admin_dashboard.py)
_CLIENT_ALIASES: dict[str, str] = {
    "babia ice": "BABIA ICE & PRODUCE LLC",
    "babia"    : "BABIA ICE & PRODUCE LLC",
    "babia ice & produce llc" : "BABIA ICE & PRODUCE LLC",
    "babia ice and produce llc": "BABIA ICE & PRODUCE LLC",
}


def _canonical_client(name: str) -> str:
    return _CLIENT_ALIASES.get(name.strip().lower(), name)


def render(dm: DataManager, alert_manager: AlertManager | None = None) -> None:
    st.title("📊 Lead")

    tab_report, tab_rates, tab_processed, tab_settings = st.tabs([
        "📊 Reports",
        "💲 Rate Card",
        "📁 Processed Invoices",
        "⚙️ Settings",
    ])

    # ──────────────────────────────────────────────────────────────────────────
    # TAB 1 — REPORTS
    # ──────────────────────────────────────────────────────────────────────────
    with tab_report:
        st.subheader("Reports")

        all_ci = dm.get_client_invoices()

        if not all_ci:
            st.info("No invoice data yet.")
        else:
            # ── By client ─────────────────────────────────────────────────────
            st.markdown("#### Invoices by Client")
            client_counts: dict[str, int] = defaultdict(int)
            client_totals: dict[str, float] = defaultdict(float)
            for ci in all_ci:
                client = _canonical_client(ci.get("client_name", "Unknown"))
                client_counts[client] += 1
                client_totals[client] += float(ci.get("total", 0))

            col1, col2 = st.columns(2)
            with col1:
                st.bar_chart(client_counts)
            with col2:
                for client, total in sorted(client_totals.items(), key=lambda x: -x[1]):
                    st.metric(client, f"${total:,.2f}")

            st.markdown("---")

            # ── By service type ───────────────────────────────────────────────
            st.markdown("#### By Service Type")
            svc_counts: dict[str, int] = defaultdict(int)
            for ci in all_ci:
                svc = ci.get("service_type") or "not_set"
                svc_counts[svc] += 1
            c1, c2, c3 = st.columns(3)
            c1.metric("In-Out",   svc_counts.get("in_out", 0))
            c2.metric("Transfer", svc_counts.get("transfer", 0))
            c3.metric("Not Set",  svc_counts.get("not_set", 0))

            st.markdown("---")

            # ── By week ───────────────────────────────────────────────────────
            st.markdown("#### Invoices by Week")
            week_counts: dict[str, int] = defaultdict(int)
            for ci in all_ci:
                date_str = ci.get("invoice_date", ci.get("created_at", ""))[:10]
                if date_str:
                    try:
                        dt   = datetime.fromisoformat(date_str)
                        week = dt.strftime("%Y-W%W")
                        week_counts[week] += 1
                    except ValueError:
                        pass
            if week_counts:
                st.bar_chart(dict(sorted(week_counts.items())))

            st.markdown("---")

            # ── Invoice Data Export ───────────────────────────────────────────
            st.markdown("#### Export Invoice Data")
            st.caption("Download all invoice records as an Excel spreadsheet.")

            def _build_excel(invoices: list[dict], providers: dict) -> bytes:
                import io
                import pandas as pd

                rows = []
                for ci in invoices:
                    prov = providers.get(ci.get("provider_invoice_id", ""), {})
                    inv_date = ci.get("invoice_date", "")
                    net_days = int(ci.get("net_days", 30) or 30)
                    due_date = ci.get("due_date", "")
                    if not due_date and inv_date:
                        try:
                            due_date = (
                                datetime.fromisoformat(inv_date)
                                + timedelta(days=net_days)
                            ).date().isoformat()
                        except Exception:
                            due_date = ""
                    rows.append({
                        "Date"      : inv_date,
                        "Invoice #" : ci.get("quickbooks_invoice_number") or prov.get("invoice_number", ""),
                        "Client"    : ci.get("client_name", ""),
                        "Cost ($)"  : ci.get("total", 0),
                        "PO Number" : ci.get("po_number", ""),
                        "Due Date"  : due_date,
                        "Paid"      : "Yes" if ci.get("paid") else "No",
                    })

                df  = pd.DataFrame(rows)
                buf = io.BytesIO()
                with pd.ExcelWriter(buf, engine="openpyxl") as writer:
                    df.to_excel(writer, index=False, sheet_name="Invoices")
                    ws = writer.sheets["Invoices"]
                    for col in ws.columns:
                        max_len = max(len(str(cell.value or "")) for cell in col)
                        ws.column_dimensions[col[0].column_letter].width = max_len + 4
                return buf.getvalue()

            _exp_col1, _exp_col2, _ = st.columns([1, 1, 3])

            with _exp_col1:
                if st.button("📊 Generate Excel", key="gen_excel_all"):
                    st.session_state["excel_bytes_all"] = _build_excel(
                        all_ci, {pi["id"]: pi for pi in dm.get_provider_invoices()}
                    )
                if "excel_bytes_all" in st.session_state:
                    st.download_button(
                        "⬇ Download All Invoices",
                        data=st.session_state["excel_bytes_all"],
                        file_name="all_invoices.xlsx",
                        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                        key="dl_excel_all",
                    )

            with _exp_col2:
                if st.button("📊 Generate Excel (Paid only)", key="gen_excel_paid"):
                    _paid_only = [ci for ci in all_ci if ci.get("paid")]
                    st.session_state["excel_bytes_paid"] = _build_excel(
                        _paid_only, {pi["id"]: pi for pi in dm.get_provider_invoices()}
                    )
                if "excel_bytes_paid" in st.session_state:
                    st.download_button(
                        "⬇ Download Paid Invoices",
                        data=st.session_state["excel_bytes_paid"],
                        file_name="paid_invoices.xlsx",
                        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                        key="dl_excel_paid",
                    )


    # ──────────────────────────────────────────────────────────────────────────
    # TAB 2 — RATE CARD EDITOR
    # ──────────────────────────────────────────────────────────────────────────
    with tab_rates:
        st.subheader("Rate Card")

        if _rates_msg := st.session_state.pop("rates_saved_msg", None):
            st.toast(_rates_msg, icon="✅")

        default_rates = dm.get_rate_card()
        all_client_rates = dm.get_client_rates()

        if not default_rates:
            st.error("⚠️ Rate card file not found or empty. Check that data/rate_card.json exists.")
        else:
            st.caption(f"Loaded {len(default_rates)} rate entries from file.")

        # All billing fields always shown in default rates
        billing_labels = {
            "in_out"   : "In-Out Storage (per pallet)",
            "transfer" : "Transfer per Truck",
        }

        # Non-billing labels are the same regardless of mode — reused in all loops
        _non_billing_labels = {
            "temp_recorder_hardware_fee"     : "Temp. Recorder — Hardware & Installation",
            "temp_recorder_installation_fee" : "Temp. Recorder — Installation Only",
            "quality_inspection_fee"         : "Quality Inspection",
            "pallet_cleaning_fee"            : "Pallet Cleaning",
            "repacking_fee"                  : "Repacking",
            "re_inspection_fee"              : "Re-Inspection",
            "broker_fee"                     : "Broker Fee",
            "stamps_fee"                     : "Seals",
            "overtime_fee"                   : "Hours Overtime",
            "restack_fee"                    : "Restack",
            "net_days"                       : "Net Days (payment terms)",
        }

        labels = {**billing_labels, **_non_billing_labels}

        # ── Default Rates ─────────────────────────────────────────────────────
        st.markdown("#### Default Rates")
        st.caption("Applies to all clients unless a client-specific rate is set.")

        updated: dict[str, float] = {}
        col1, col2 = st.columns(2)
        items = list(labels.items())
        for i, (key, label) in enumerate(items):
            col = col1 if i < len(items) // 2 + len(items) % 2 else col2
            if key == "net_days":
                updated[key] = col.number_input(
                    label=label,
                    value=int(default_rates.get(key, 30)),
                    min_value=1,
                    step=1,
                    key=f"rate_{key}",
                )
            else:
                updated[key] = col.number_input(
                    label=f"{label} ($)",
                    value=float(default_rates.get(key, 0)),
                    min_value=0.0,
                    step=0.25,
                    format="%.2f",
                    key=f"rate_{key}",
                )

        _cpt_col, _basis_col = st.columns(2)
        updated["cost_per_truck"] = _cpt_col.number_input(
            "Cost per Truck In & Out ($)",
            value=float(default_rates.get("cost_per_truck", 0)),
            min_value=0.0, step=0.25, format="%.2f",
            key="rate_cost_per_truck",
        )
        _cur_basis = default_rates.get("default_billing_basis", "Pallet")
        updated["default_billing_basis"] = _basis_col.selectbox(
            "Default Billing Basis",
            options=["Pallet", "Truck"],
            index=0 if _cur_basis == "Pallet" else 1,
            key="rate_default_billing_basis",
            help="Controls whether the admin dashboard shows a total pallet count input (Pallet) or skips it (Truck).",
        )

        if _colored_btn(st, "💾 Save Default Rates", key="save_default_rates", color="#198754"):
            dm.update_rate_card(updated)
            st.success("Default rates saved.")

        st.markdown("---")

        # ── Add New Client (Unified Profile) ─────────────────────────────────
        st.markdown("#### Add New Client")
        st.caption("Fill in the full client profile below and click **Save Client Profile** to register everything at once.")

        # Identity row
        _nid1, _nid2 = st.columns([2, 1])
        new_client_name     = _nid1.text_input(
            "Client Name *",
            placeholder="e.g. WALMART",
            key="new_client_name",
        )
        new_client_initials = _nid2.text_input(
            "Initials",
            placeholder="e.g. WMT",
            key="new_client_initials",
            help="Short code used on invoices (e.g. WMT, BBIA). Saved in uppercase.",
        )

        # Contact / address row
        new_client_email = st.text_input(
            "Email Address",
            placeholder="billing@client.com",
            key="new_client_email",
        )
        new_client_address = st.text_area(
            "Billing Address",
            placeholder="123 Main St\nCity, TX 78000",
            height=90,
            key="new_client_address",
        )
        new_client_rfc = st.text_input(
            "RFC",
            placeholder="e.g. ABC123456DEF",
            key="new_client_rfc",
            help="Mexican tax ID (RFC). Printed on the invoice below the billing address.",
        )

        # Rate card
        st.caption("Rate Card")
        _new_billing_mode = st.radio(
            "Billing Mode",
            options=["Pallet", "Truck"],
            horizontal=True,
            key="new_client_billing_mode",
            help="Controls which billing rates are shown for editing. Both sets of prices are always saved.",
        )
        new_cbp = (_new_billing_mode == "Pallet")

        st.caption("Pallet Rates")
        new_col1, new_col2 = st.columns(2)
        _new_in_out = new_col1.number_input(
            "In-Out Storage (per pallet) ($)",
            value=float(default_rates.get("in_out", 0)),
            min_value=0.0, step=0.25, format="%.2f",
            key="new_cr_in_out",
        )
        _new_transfer = new_col2.number_input(
            "Transfer per Truck ($)",
            value=float(default_rates.get("transfer", 0)),
            min_value=0.0, step=0.25, format="%.2f",
            key="new_cr_transfer",
        )

        st.caption("Truck Rates")
        _new_cost_per_truck = st.number_input(
            "Cost per Truck In & Out ($)",
            value=float(default_rates.get("cost_per_truck", 0)),
            min_value=0.0, step=0.25, format="%.2f",
            key="new_cr_cost_per_truck",
        )

        # Non-billing fees — always shown
        new_client_overrides: dict = {
            "charged_by_pallet": new_cbp,
            "in_out"           : _new_in_out,
            "transfer"         : _new_transfer,
            "cost_per_truck"   : _new_cost_per_truck,
        }
        _nb_new_items = list(_non_billing_labels.items())
        nb_new_col1, nb_new_col2 = st.columns(2)
        for i, (key, label) in enumerate(_nb_new_items):
            col = nb_new_col1 if i < len(_nb_new_items) // 2 + len(_nb_new_items) % 2 else nb_new_col2
            if key == "net_days":
                new_client_overrides[key] = col.number_input(
                    label=label,
                    value=int(default_rates.get(key, 30)),
                    min_value=1, step=1,
                    key=f"new_cr_{key}",
                )
            else:
                new_client_overrides[key] = col.number_input(
                    label=f"{label} ($)",
                    value=float(default_rates.get(key, 0)),
                    min_value=0.0, step=0.25, format="%.2f",
                    key=f"new_cr_{key}",
                )

        st.caption("Pallet Override — leave at 0 to disable. When set, this count is always pre-filled for this client's invoices.")
        _new_fixed_pal = st.number_input(
            "Fixed Pallet Count (optional)",
            min_value=0,
            step=1,
            value=0,
            key="new_cr_fixed_pal",
        )
        if _new_fixed_pal > 0:
            new_client_overrides["fixed_pallet_count"] = int(_new_fixed_pal)

        if _colored_btn(st, "💾 Save Client Profile", key="save_new_client", color="#198754"):
            if not new_client_name.strip():
                st.warning("Client name is required.")
            else:
                _saved_name = new_client_name.strip().upper()
                dm.set_client_rates(_saved_name, new_client_overrides)
                if new_client_address.strip():
                    dm.set_client_address(_saved_name, new_client_address.strip())
                if new_client_email.strip():
                    dm.set_client_email(_saved_name, new_client_email.strip())
                if new_client_initials.strip():
                    dm.set_client_initial(_saved_name, new_client_initials.strip())
                if new_client_rfc.strip():
                    dm.set_client_rfc(_saved_name, new_client_rfc.strip())
                st.session_state["rates_saved_msg"] = f"✅ Client profile saved for {_saved_name}."
                st.session_state.pop("new_client_save_inline", None)
                st.session_state["new_client_save_inline"] = f"Client profile saved for **{_saved_name}**."
                st.rerun()

        if _inline := st.session_state.pop("new_client_save_inline", None):
            st.success(_inline)

        st.markdown("---")

        # ── Client Details ────────────────────────────────────────────────────
        st.markdown("#### Client Details")
        st.caption("Edit initials, contact info, billing address, and rate card for each client.")

        all_addresses   = dm.get_client_addresses()
        all_emails      = dm.get_client_emails()
        all_initials_cd = dm.get_client_initials()
        all_rfcs_cd     = dm.get_client_rfcs()

        _all_cd_names = sorted(
            set(all_client_rates.keys())
            | set(all_addresses.keys())
            | set(all_emails.keys())
            | set(all_initials_cd.keys())
            | set(all_rfcs_cd.keys())
        )

        if _all_cd_names:
            for cname in _all_cd_names:
                crates = all_client_rates.get(cname, {})
                _cd_v  = st.session_state.get(f"cd_exp_v_{cname}", 0)

                with st.expander(cname, expanded=False, key=f"cd_exp_{cname}_{_cd_v}"):

                    # ── Delete button (top-right, red) ────────────────────────
                    _anchor = f"cd_delbtn_{cname}".replace(" ", "_").replace("-", "_")
                    _del_spacer, _del_btn_col = st.columns([6, 1])
                    with _del_btn_col:
                        st.markdown(
                            f"<span id='{_anchor}'></span>"
                            f"<style>"
                            f"[data-testid='element-container']:has(span#{_anchor})"
                            f" + [data-testid='element-container'] button,"
                            f"[data-testid='stColumn']:has(span#{_anchor}) button"
                            f"{{background-color:#dc3545!important;"
                            f"border-color:#dc3545!important;color:white!important;"
                            f"font-size:12px!important;padding:2px 8px!important;}}"
                            f"</style>",
                            unsafe_allow_html=True,
                        )
                        if st.button("✕ DELETE", key=f"cd_del_btn_{cname}", help=f"Delete {cname}", use_container_width=True):
                            st.session_state[f"cd_del_confirm_{cname}"] = True
                            st.rerun()

                    # Confirm-delete prompt (inside the expander)
                    if st.session_state.get(f"cd_del_confirm_{cname}"):
                        _dc1, _dc2, _dc3 = st.columns([3, 1, 1])
                        _dc1.warning(f"Delete **{cname}** and all associated data?")
                        if _dc2.button("✅ Yes", key=f"cd_del_yes_{cname}", use_container_width=True):
                            dm.delete_client_rates(cname)
                            dm.set_client_initial(cname, "")
                            dm.set_client_email(cname, "")
                            dm.set_client_address(cname, "")
                            dm.set_client_rfc(cname, "")
                            st.session_state.pop(f"cd_del_confirm_{cname}", None)
                            st.session_state["rates_saved_msg"] = f"✅ {cname} deleted."
                            st.rerun()
                        if _dc3.button("✗ Cancel", key=f"cd_del_no_{cname}", use_container_width=True):
                            st.session_state.pop(f"cd_del_confirm_{cname}", None)
                            st.rerun()

                    # ── Identity ──────────────────────────────────────────────
                    st.caption("Identity")

                    # Rename client
                    _ren_col, _ren_btn_col = st.columns([4, 1])
                    _rename_input = _ren_col.text_input(
                        "Client Name",
                        value=cname,
                        key=f"cd_rename_input_{cname}",
                        label_visibility="collapsed",
                    )
                    if _ren_btn_col.button("✏️ Rename", key=f"cd_rename_btn_{cname}", use_container_width=True):
                        _new_cname = _rename_input.strip()
                        if _new_cname and _new_cname != cname:
                            st.session_state[f"cd_rename_confirm_{cname}"] = _new_cname
                            st.rerun()

                    if st.session_state.get(f"cd_rename_confirm_{cname}"):
                        _new_cname = st.session_state[f"cd_rename_confirm_{cname}"]
                        _rc1, _rc2, _rc3 = st.columns([3, 1, 1])
                        _rc1.warning(f"Rename **{cname}** → **{_new_cname}**?")
                        if _rc2.button("✅ Yes", key=f"cd_rename_yes_{cname}", use_container_width=True):
                            dm.rename_client(cname, _new_cname)
                            st.session_state.pop(f"cd_rename_confirm_{cname}", None)
                            st.session_state["rates_saved_msg"] = f"✅ {cname} renamed to {_new_cname}."
                            st.rerun()
                        if _rc3.button("✗ Cancel", key=f"cd_rename_no_{cname}", use_container_width=True):
                            st.session_state.pop(f"cd_rename_confirm_{cname}", None)
                            st.rerun()

                    new_initials = st.text_input(
                        "Initials",
                        value=all_initials_cd.get(cname, ""),
                        placeholder="e.g. WMT",
                        key=f"cd_initials_{cname}",
                        help="Short code used on invoice IDs (e.g. WMT_2001).",
                    )

                    # ── Contact ───────────────────────────────────────────────
                    st.caption("Contact")
                    new_email = st.text_input(
                        "Email Address",
                        value=all_emails.get(cname, ""),
                        placeholder="billing@client.com",
                        key=f"cd_email_{cname}",
                    )
                    new_addr = st.text_area(
                        "Billing Address",
                        value=all_addresses.get(cname, ""),
                        placeholder="123 Main St\nCity, TX 78000",
                        height=90,
                        key=f"cd_addr_{cname}",
                    )
                    new_rfc = st.text_input(
                        "RFC",
                        value=all_rfcs_cd.get(cname, ""),
                        placeholder="e.g. ABC123456DEF",
                        key=f"cd_rfc_{cname}",
                        help="Mexican tax ID (RFC). Printed on the invoice below the billing address.",
                    )

                    # ── Rate Card ─────────────────────────────────────────────
                    st.caption("Rate Card")
                    _cd_billing_mode = st.radio(
                        "Billing Mode",
                        options=["Pallet", "Truck"],
                        index=0 if bool(crates.get("charged_by_pallet", True)) else 1,
                        horizontal=True,
                        key=f"billing_mode_{cname}",
                        help="Controls which billing rates are shown for editing. Both sets of prices are always saved.",
                    )
                    client_cbp = (_cd_billing_mode == "Pallet")

                    st.caption("Pallet Rates")
                    override_col1, override_col2 = st.columns(2)
                    _def_in_out   = float(default_rates.get("in_out", 0))
                    _def_transfer = float(default_rates.get("transfer", 0))
                    _client_in_out = override_col1.number_input(
                        "In-Out Storage (per pallet) ($)" + (" ✏️" if "in_out" in crates else ""),
                        value=float(crates.get("in_out", _def_in_out)),
                        min_value=0.0, step=0.25, format="%.2f",
                        key=f"cr_{cname}_in_out",
                        help="Default: ${:.2f}".format(_def_in_out),
                    )
                    _client_transfer = override_col2.number_input(
                        "Transfer per Truck ($)" + (" ✏️" if "transfer" in crates else ""),
                        value=float(crates.get("transfer", _def_transfer)),
                        min_value=0.0, step=0.25, format="%.2f",
                        key=f"cr_{cname}_transfer",
                        help="Default: ${:.2f}".format(_def_transfer),
                    )

                    st.caption("Truck Rates")
                    _def_cpt    = float(default_rates.get("cost_per_truck", 0))
                    _client_cpt = st.number_input(
                        "Cost per Truck In & Out ($)" + (" ✏️" if "cost_per_truck" in crates else ""),
                        value=float(crates.get("cost_per_truck", _def_cpt)),
                        min_value=0.0, step=0.25, format="%.2f",
                        key=f"cr_{cname}_cost_per_truck",
                        help="Default: ${:.2f}".format(_def_cpt),
                    )

                    new_overrides: dict = {
                        "charged_by_pallet": client_cbp,
                        "in_out"           : _client_in_out,
                        "transfer"         : _client_transfer,
                        "cost_per_truck"   : _client_cpt,
                    }

                    # Non-billing fees — always shown
                    _nb_cd_items = list(_non_billing_labels.items())
                    nb_cd_col1, nb_cd_col2 = st.columns(2)
                    for i, (key, label) in enumerate(_nb_cd_items):
                        col = nb_cd_col1 if i < len(_nb_cd_items) // 2 + len(_nb_cd_items) % 2 else nb_cd_col2
                        is_override = key in crates
                        if key == "net_days":
                            default_val = int(default_rates.get(key, 30))
                            current_val = int(crates.get(key, default_val))
                            new_val = col.number_input(
                                label=label + (" ✏️" if is_override else ""),
                                value=current_val, min_value=1, step=1,
                                key=f"cr_{cname}_{key}",
                                help=f"Default: {default_val}",
                            )
                        else:
                            default_val = float(default_rates.get(key, 0))
                            current_val = float(crates.get(key, default_val))
                            new_val = col.number_input(
                                label=f"{label} ($)" + (" ✏️" if is_override else ""),
                                value=current_val, min_value=0.0, step=0.25, format="%.2f",
                                key=f"cr_{cname}_{key}",
                                help="Default: ${:.2f}".format(default_val),
                            )
                        if new_val != default_val:
                            new_overrides[key] = new_val

                    st.caption("Pallet Override — leave at 0 to disable.")
                    _fixed_pal = st.number_input(
                        "Fixed Pallet Count (optional)",
                        min_value=0,
                        step=1,
                        value=int(crates.get("fixed_pallet_count", 0) or 0),
                        key=f"cr_{cname}_fixed_pal",
                    )
                    if _fixed_pal > 0:
                        new_overrides["fixed_pallet_count"] = int(_fixed_pal)

                    # ── Buttons ───────────────────────────────────────────────
                    btn_col1, btn_col2 = st.columns(2)
                    if _colored_btn(btn_col1, "💾 Save", key=f"save_cd_{cname}", color="#198754"):
                        dm.set_client_rates(cname, new_overrides)
                        dm.set_client_initial(cname, new_initials.strip())
                        dm.set_client_email(cname, new_email.strip())
                        dm.set_client_address(cname, new_addr.strip())
                        dm.set_client_rfc(cname, new_rfc.strip())
                        st.session_state[f"cd_exp_v_{cname}"] = _cd_v + 1
                        st.session_state["rates_saved_msg"] = f"✅ {cname} saved."
                        st.session_state[f"cd_saved_{cname}"] = f"Details saved for **{cname}**."
                        st.rerun()
                    if btn_col2.button("🗑 Delete Custom Rates", key=f"del_cr_{cname}", type="primary"):
                        dm.delete_client_rates(cname)
                        st.session_state["rates_saved_msg"] = f"✅ Custom rates removed for {cname}. Now using defaults."
                        st.rerun()

                if _cdmsg := st.session_state.pop(f"cd_saved_{cname}", None):
                    st.success(_cdmsg)
        else:
            st.info("No clients found. Add a new client above.")


    # ──────────────────────────────────────────────────────────────────────────
    # TAB 3 — SETTINGS
    # ──────────────────────────────────────────────────────────────────────────
    with tab_settings:
        st.subheader("Settings")

        # ── Client Data Management ────────────────────────────────────────────
        st.markdown("#### Client Data Management")
        st.caption("Filter invoices by client and delete all records for that client.")

        all_ci = dm.get_client_invoices()
        client_names = sorted(set(
            ci.get("client_name", "").strip()
            for ci in all_ci
            if ci.get("client_name", "").strip()
        ))

        if not client_names:
            st.info("No client data found.")
        else:
            selected_clients = st.multiselect(
                "Select client(s)",
                options=client_names,
                key="mgmt_client_sel",
                placeholder="Choose one or more clients...",
            )

            if not selected_clients:
                st.info("Select one or more clients above to view and manage their records.")
            else:
                _selected_set = set(selected_clients)
                client_records = [
                    ci for ci in all_ci
                    if ci.get("client_name", "").strip() in _selected_set
                ]
                _label_str = ", ".join(f"**{c}**" for c in selected_clients)
                st.caption(f"{len(client_records)} invoice record(s) for {_label_str}")

                if client_records:
                    _prov_by_id = {pi["id"]: pi for pi in dm.get_provider_invoices()}
                    rows = []
                    for ci in client_records:
                        qb_num = ci.get("quickbooks_invoice_number")
                        if not qb_num and ci.get("provider_invoice_id"):
                            prov   = _prov_by_id.get(ci["provider_invoice_id"])
                            qb_num = prov.get("invoice_number") if prov else None
                        rows.append({
                            "Client"    : ci.get("client_name", "—"),
                            "Invoice #" : qb_num or "—",
                            "Date"      : ci.get("invoice_date", "—"),
                            "Status"    : ci.get("status", "—"),
                            "Total"     : f"${ci.get('total', 0):,.2f}",
                        })
                    st.dataframe(rows, use_container_width=True, hide_index=True)

                _del_confirm_key = "confirm_del_clients_multi"
                _btn_label = (
                    f"🗑 Delete all records for {selected_clients[0]}"
                    if len(selected_clients) == 1
                    else f"🗑 Delete all records for {len(selected_clients)} clients"
                )
                if st.session_state.get(_del_confirm_key):
                    st.warning(
                        f"Delete ALL {len(client_records)} record(s) for "
                        f"{len(selected_clients)} client(s)? This cannot be undone."
                    )
                    c1, c2 = st.columns(2)
                    if c1.button("✅ Yes, delete", key="del_clients_yes", type="primary", width='stretch'):
                        for ci in client_records:
                            prov_id = ci.get("provider_invoice_id")
                            if prov_id:
                                prov = dm.get_provider_invoice_by_id(prov_id)
                                if prov and prov.get("email_intake_id"):
                                    try:
                                        dm.update_email_log(prov["email_intake_id"], {"status": "rejected"})
                                    except KeyError:
                                        pass
                                dm.delete_provider_invoice(prov_id)
                            dm.delete_client_invoice(ci["id"])
                        for sc in selected_clients:
                            dm.delete_client_rates(sc)
                        st.session_state.pop(_del_confirm_key, None)
                        st.success(f"All records for {len(selected_clients)} client(s) deleted.")
                        st.rerun()
                    if c2.button("✗ Cancel", key="del_clients_no", width='stretch'):
                        st.session_state.pop(_del_confirm_key, None)
                        st.rerun()
                else:
                    if st.button(_btn_label, type="primary", key="del_clients_btn"):
                        st.session_state[_del_confirm_key] = True
                        st.rerun()


    # ──────────────────────────────────────────────────────────────────────────
    # TAB 4 — PROCESSED INVOICES
    # ──────────────────────────────────────────────────────────────────────────
    with tab_processed:
        st.subheader("Processed Invoices")

        all_ci_proc   = dm.get_client_invoices()
        prov_invs_proc = dm.get_provider_invoices()
        prov_by_id_proc = {pi["id"]: pi for pi in prov_invs_proc}

        processed = sorted(
            [
                ci for ci in all_ci_proc
                if ci.get("status") in ("validated", "invoiced")
                or ci.get("ready_for_export")
                or ci.get("ready_to_email")
                or ci.get("quickbooks_exported")
                or ci.get("emailed")
                or ci.get("paid")
            ],
            key=lambda x: x.get("invoice_date", x.get("created_at", "")),
            reverse=True,
        )

        if not processed:
            st.info("No processed invoices yet.")
        else:
            # ── Filters ───────────────────────────────────────────────────────
            _all_clients = sorted({ci.get("client_name", "") for ci in processed if ci.get("client_name")})
            _all_dates   = sorted({ci.get("invoice_date", "")[:10] for ci in processed if ci.get("invoice_date")}, reverse=True)

            _fc1, _fc2, _fc3 = st.columns(3)
            _f_client = _fc1.selectbox("Client", ["All"] + _all_clients, key="lead_proc_client")
            _f_date   = _fc2.selectbox("Date",   ["All"] + _all_dates,   key="lead_proc_date")
            _f_paid   = _fc3.selectbox("Paid",   ["All", "Paid", "Unpaid"], key="lead_proc_paid")

            filtered = [
                ci for ci in processed
                if (_f_client == "All" or ci.get("client_name") == _f_client)
                and (_f_date  == "All" or ci.get("invoice_date", "")[:10] == _f_date)
                and (
                    _f_paid == "All"
                    or (_f_paid == "Paid"   and ci.get("paid"))
                    or (_f_paid == "Unpaid" and not ci.get("paid"))
                )
            ]

            st.caption(f"{len(filtered)} of {len(processed)} invoice(s)")

            h1, h2, h3, h4, h5, h6 = st.columns([1.2, 1.8, 1, 1, 1.2, 1.2])
            h1.markdown("**Invoice #**")
            h2.markdown("**Client**")
            h3.markdown("**Date**")
            h4.markdown("**Due Date**")
            h5.markdown("**PDF**")
            h6.markdown("**IIF**")
            st.divider()

            for ci in filtered:
                cid = ci["id"]
                qb  = ci.get("quickbooks_invoice_number", "—")

                due = ci.get("due_date", "").strip()
                if not due:
                    try:
                        due = (
                            datetime.fromisoformat(ci.get("invoice_date", ""))
                            + timedelta(days=int(ci.get("net_days", 30)))
                        ).date().isoformat()
                    except Exception:
                        due = "—"

                reviewed = bool(
                    ci.get("status") == "invoiced"
                    or ci.get("quickbooks_invoice_number")
                    or ci.get("ready_for_export")
                    or ci.get("ready_to_email")
                    or ci.get("quickbooks_exported")
                    or ci.get("emailed")
                )
                exported = bool(ci.get("quickbooks_exported"))

                c1, c2, c3, c4, c5, c6 = st.columns([1.2, 1.8, 1, 1, 1.2, 1.2])
                c1.write(f"QB #{qb}")
                c2.write(ci.get("client_name", "—"))
                c3.write(ci.get("invoice_date", "—"))
                c4.write(due)

                prov_proc = prov_by_id_proc.get(ci.get("provider_invoice_id", ""), {})
                if ci.get("quickbooks_invoice_number"):
                    # Full generated client invoice PDF
                    c5.download_button(
                        "⬇ PDF",
                        data=_cached_pdf(*_pdf_args(ci, prov_proc)),
                        file_name=f"{qb}-invoice.pdf",
                        mime="application/pdf",
                        key=f"lead_proc_pdf_{cid}",
                    )
                else:
                    # Validated but not yet invoiced — offer the provider's original PDF
                    _prov_pdf = prov_proc.get("pdf_local_path", "")
                    if _prov_pdf and Path(_prov_pdf).exists():
                        c5.download_button(
                            "⬇ PDF",
                            data=Path(_prov_pdf).read_bytes(),
                            file_name=Path(_prov_pdf).name,
                            mime="application/pdf",
                            key=f"lead_proc_pdf_{cid}",
                        )
                    else:
                        c5.write("—")

                if exported:
                    c6.download_button(
                        "⬇ IIF",
                        data=build_iif_content(ci),
                        file_name=f"{qb}-export.iif",
                        mime="text/plain",
                        key=f"lead_proc_iif_{cid}",
                    )
                else:
                    c6.write("—")

                # ── Lifecycle tracker ─────────────────────────────────────────
                st.markdown(_lifecycle_html(ci), unsafe_allow_html=True)
                st.divider()
