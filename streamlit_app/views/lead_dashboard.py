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

    tab_report, tab_rates, tab_settings, tab_processed = st.tabs([
        "📊 Reports",
        "💲 Rate Card",
        "⚙️ Settings",
        "📁 Processed Invoices",
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

        # ── Billing Mode Toggle ───────────────────────────────────────────────
        charged_by_pallet = st.toggle(
            "Charged by Pallet",
            value=bool(default_rates.get("charged_by_pallet", True)),
            key="rate_charged_by_pallet",
            help="ON = rates per pallet. OFF = single flat cost per truck; workers skip pallet count.",
        )

        # Billing labels change based on mode
        if charged_by_pallet:
            billing_labels = {
                "in_out"  : "In-Out Storage (per pallet)",
                "transfer": "Transfer (per pallet)",
            }
        else:
            billing_labels = {
                "cost_per_truck": "Cost per Truck",
            }

        # Non-billing labels are the same regardless of mode — reused in all loops
        _non_billing_labels = {
            "temp_recorder_hardware_fee"     : "Temp. Recorder — Hardware & Installation",
            "temp_recorder_installation_fee" : "Temp. Recorder — Installation Only",
            "quality_inspection_fee"         : "Quality Inspection",
            "pallet_cleaning_fee"            : "Pallet Cleaning",
            "broken_pallet_fee"              : "Broken Pallet (per pallet)",
            "repacking_fee"                  : "Repacking",
            "re_inspection_fee"              : "Re-Inspection",
            "broker_fee"                     : "Broker Fee",
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

        if _colored_btn(st, "💾 Save Default Rates", key="save_default_rates", color="#198754"):
            dm.update_rate_card({**updated, "charged_by_pallet": charged_by_pallet})
            st.success("Default rates saved.")

        st.markdown("---")

        # ── Add New Client Rates ──────────────────────────────────────────────
        st.markdown("**Add rates for a new client:**")
        new_client_name = st.text_input("Client name", placeholder="e.g. Walmart", key="new_client_name")

        if new_client_name.strip():
            new_cbp = st.toggle(
                "Charged by Pallet",
                value=True,
                key="new_client_cbp",
                help="ON = per pallet. OFF = flat cost per truck.",
            )
            if new_cbp:
                new_billing = {
                    "in_out"  : "In-Out Storage (per pallet)",
                    "transfer": "Transfer (per pallet)",
                }
            else:
                new_billing = {"cost_per_truck": "Cost per Truck"}

            new_client_labels = {**new_billing, **_non_billing_labels}
            new_col1, new_col2 = st.columns(2)
            new_client_overrides: dict = {"charged_by_pallet": new_cbp}
            new_items = list(new_client_labels.items())
            for i, (key, label) in enumerate(new_items):
                col = new_col1 if i < len(new_items) // 2 + len(new_items) % 2 else new_col2
                if key == "net_days":
                    default_val = int(default_rates.get(key, 30))
                    new_val = col.number_input(
                        label=label,
                        value=default_val,
                        min_value=1,
                        step=1,
                        key=f"new_cr_{key}",
                    )
                else:
                    default_val = float(default_rates.get(key, 0))
                    new_val = col.number_input(
                        label=f"{label} ($)",
                        value=default_val,
                        min_value=0.0,
                        step=0.25,
                        format="%.2f",
                        key=f"new_cr_{key}",
                    )
                if new_val != default_val:
                    new_client_overrides[key] = new_val

            st.markdown("---")
            st.caption("Pallet Override")
            _new_fixed_pal = st.number_input(
                "Fixed Pallet Count (optional)",
                min_value=0,
                step=1,
                value=0,
                key="new_cr_fixed_pal",
            )
            st.caption("Leave at 0 to disable — when set, this count is always pre-filled for this client's invoices.")
            if _new_fixed_pal > 0:
                new_client_overrides["fixed_pallet_count"] = int(_new_fixed_pal)

            if _colored_btn(st, "💾 Save Client Rates", key="save_new_client", color="#198754"):
                _saved_name = new_client_name.strip().upper()
                dm.set_client_rates(_saved_name, new_client_overrides)
                st.session_state["rates_saved_msg"] = f"✅ Rates saved for {_saved_name}."
                st.session_state["new_client_save_inline"] = f"Rates saved for **{_saved_name}**."
                st.rerun()

            if _inline := st.session_state.get("new_client_save_inline"):
                st.success(_inline)

        st.markdown("---")

        # ── Per-Client Rates ──────────────────────────────────────────────────
        st.markdown("#### Per-Client Rate Overrides")
        st.caption("Set rates for a specific client. Only fields you change here will override the defaults.")

        if all_client_rates:
            st.markdown("**Clients with custom rates:**")
            for cname, crates in all_client_rates.items():
                with st.expander(cname):
                    # Per-client billing mode toggle
                    client_cbp = st.toggle(
                        "Charged by Pallet",
                        value=bool(crates.get("charged_by_pallet", charged_by_pallet)),
                        key=f"cbp_{cname}",
                        help="ON = per pallet. OFF = flat cost per truck.",
                    )
                    if client_cbp:
                        client_billing = {
                            "in_out"  : "In-Out Storage (per pallet)",
                            "transfer": "Transfer (per pallet)",
                        }
                    else:
                        client_billing = {"cost_per_truck": "Cost per Truck"}

                    client_labels  = {**client_billing, **_non_billing_labels}
                    override_items = list(client_labels.items())
                    # Always persist the toggle state; other fields added below if they differ
                    new_overrides: dict = {"charged_by_pallet": client_cbp}

                    override_col1, override_col2 = st.columns(2)
                    for i, (key, label) in enumerate(override_items):
                        col = override_col1 if i < len(override_items) // 2 + len(override_items) % 2 else override_col2
                        is_override = key in crates
                        if key == "net_days":
                            default_val = int(default_rates.get(key, 30))
                            current_val = int(crates.get(key, default_val))
                            new_val = col.number_input(
                                label=label + (" ✏️" if is_override else ""),
                                value=current_val,
                                min_value=1,
                                step=1,
                                key=f"cr_{cname}_{key}",
                                help=f"Default: {default_val}",
                            )
                        else:
                            default_val = float(default_rates.get(key, 0))
                            current_val = float(crates.get(key, default_val))
                            new_val = col.number_input(
                                label=f"{label} ($)" + (" ✏️" if is_override else ""),
                                value=current_val,
                                min_value=0.0,
                                step=0.25,
                                format="%.2f",
                                key=f"cr_{cname}_{key}",
                                help="Default: ${:.2f}".format(default_val),
                            )
                        if new_val != default_val:
                            new_overrides[key] = new_val

                    st.markdown("---")
                    st.caption("Pallet Override")
                    _fixed_pal = st.number_input(
                        "Fixed Pallet Count (optional)",
                        min_value=0,
                        step=1,
                        value=int(crates.get("fixed_pallet_count", 0) or 0),
                        key=f"cr_{cname}_fixed_pal",
                    )
                    st.caption("Leave at 0 to disable — when set, this count is always pre-filled for this client's invoices.")
                    if _fixed_pal > 0:
                        new_overrides["fixed_pallet_count"] = int(_fixed_pal)

                    btn_col1, btn_col2 = st.columns(2)
                    if _colored_btn(btn_col1, "💾 Save", key=f"save_cr_{cname}", color="#198754"):
                        dm.set_client_rates(cname, new_overrides)
                        st.session_state["rates_saved_msg"] = f"✅ Rates saved for {cname}."
                        st.rerun()
                    if btn_col2.button("🗑 Delete Custom Rates", key=f"del_cr_{cname}", type="primary"):
                        dm.delete_client_rates(cname)
                        st.session_state["rates_saved_msg"] = f"✅ Custom rates removed for {cname}. Now using defaults."
                        st.rerun()
        else:
            st.info("No client-specific rates set yet.")

        st.markdown("---")

        # ── Client Billing Addresses ──────────────────────────────────────────
        st.markdown("#### Client Billing Addresses")
        st.caption("Address printed in the Bill To section of the generated invoice.")

        all_addresses = dm.get_client_addresses()

        if all_addresses:
            st.markdown("**Saved addresses:**")
            for cname, addr in sorted(all_addresses.items()):
                _addr_exp_v  = st.session_state.get(f"addr_exp_v_{cname}", 0)
                with st.expander(cname, expanded=False, key=f"addr_exp_{cname}_{_addr_exp_v}"):
                    new_addr = st.text_area(
                        "Address",
                        value=addr,
                        height=90,
                        key=f"addr_{cname}",
                        label_visibility="collapsed",
                    )
                    ac1, ac2 = st.columns(2)
                    if _colored_btn(ac1, "💾 Save", key=f"save_addr_{cname}", color="#198754", width="stretch"):
                        dm.set_client_address(cname, new_addr)
                        st.session_state[f"addr_exp_v_{cname}"] = _addr_exp_v + 1
                        st.session_state[f"addr_saved_{cname}"] = f"Address saved for **{cname}**."
                        st.rerun()
                    if ac2.button("🗑 Remove", key=f"del_addr_{cname}", width="stretch"):
                        dm.set_client_address(cname, "")
                        st.session_state[f"addr_exp_v_{cname}"] = _addr_exp_v + 1
                        st.rerun()
                if _amsg := st.session_state.pop(f"addr_saved_{cname}", None):
                    st.success(_amsg)
        else:
            st.info("No billing addresses saved yet.")

        st.markdown("**Add / update a billing address:**")
        clients_with_rates = sorted(all_client_rates.keys())
        if not clients_with_rates:
            st.caption("No clients with rates found. Add client rates above first.")
        else:
            addr_client = st.selectbox(
                "Client",
                options=clients_with_rates,
                key="new_addr_client",
                label_visibility="collapsed",
            )
            existing = dm.get_client_address(addr_client)
            new_addr_val = st.text_area(
                "Billing Address",
                value=existing,
                placeholder="123 Main St\nCity, TX 78000",
                height=90,
                key="new_addr_val",
            )
            if _colored_btn(st, "💾 Save Address", key="save_new_addr", color="#198754"):
                if not new_addr_val.strip():
                    st.warning("Address cannot be empty.")
                else:
                    dm.set_client_address(addr_client, new_addr_val.strip())
                    st.success(f"Billing address saved for {addr_client}.")
                    st.rerun()

        st.markdown("---")

        # ── Client Emails ─────────────────────────────────────────────────────
        st.markdown("#### Client Emails")
        st.caption("Email addresses used when sending invoices to clients.")

        all_emails = dm.get_client_emails()

        if all_emails:
            st.markdown("**Saved emails:**")
            for cname, email in sorted(all_emails.items()):
                _email_exp_v = st.session_state.get(f"email_exp_v_{cname}", 0)
                with st.expander(cname, expanded=False, key=f"email_exp_{cname}_{_email_exp_v}"):
                    new_email = st.text_input(
                        "Email",
                        value=email,
                        key=f"email_{cname}",
                        label_visibility="collapsed",
                    )
                    ec1, ec2 = st.columns(2)
                    if _colored_btn(ec1, "💾 Save", key=f"save_email_{cname}", color="#198754", width="stretch"):
                        dm.set_client_email(cname, new_email.strip())
                        st.session_state[f"email_exp_v_{cname}"] = _email_exp_v + 1
                        st.session_state[f"email_saved_{cname}"] = f"Email saved for **{cname}**."
                        st.rerun()
                    if ec2.button("🗑 Remove", key=f"del_email_{cname}", width="stretch"):
                        dm.set_client_email(cname, "")
                        st.session_state[f"email_exp_v_{cname}"] = _email_exp_v + 1
                        st.rerun()
                if _emsg := st.session_state.pop(f"email_saved_{cname}", None):
                    st.success(_emsg)
        else:
            st.info("No client emails saved yet.")

        st.markdown("**Add / update a client email:**")
        if not clients_with_rates:
            st.caption("No clients with rates found. Add client rates above first.")
        else:
            email_client = st.selectbox(
                "Client",
                options=clients_with_rates,
                key="new_email_client",
                label_visibility="collapsed",
            )
            existing_email = dm.get_client_email(email_client)
            new_email_val = st.text_input(
                "Email address",
                value=existing_email,
                placeholder="billing@client.com",
                key="new_email_val",
            )
            if _colored_btn(st, "💾 Save Email", key="save_new_email", color="#198754"):
                if not new_email_val.strip():
                    st.warning("Email cannot be empty.")
                else:
                    dm.set_client_email(email_client, new_email_val.strip())
                    st.success(f"Email saved for {email_client}.")
                    st.rerun()


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

        st.markdown("---")

        st.markdown("#### Clear Pipeline Data")
        st.caption(
            "Removes all emails, provider invoices, and client invoices. "
            "Rate card, client rates, and provider list are kept. "
            "Use this to reset between test runs."
        )

        confirm = st.checkbox("I understand this will permanently delete all pipeline data.")
        if st.button("🗑 Clear All Pipeline Data", type="primary", disabled=not confirm):
            from config import DATA_DIR, PDFS_DIR, PHOTOS_DIR

            for fname in ["email_intake_log.json", "provider_invoices.json", "client_invoices.json"]:
                fpath = DATA_DIR / fname
                fpath.write_text("[]", encoding="utf-8")

            for folder in [PDFS_DIR, PHOTOS_DIR]:
                for f in folder.iterdir():
                    try:
                        f.unlink()
                    except Exception:
                        pass

            st.success("All pipeline data cleared. The dashboard will now show a fresh state.")
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
