"""
lead_dashboard.py
=================
Lead dashboard: Reports, Rate Card editor, and Settings.
"""

import streamlit as st
from collections import defaultdict
from datetime import datetime

from data_manager import DataManager
from alerting.alert_manager import AlertManager

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
    st.title("📊 Lead Dashboard")

    tab_report, tab_rates, tab_settings = st.tabs([
        "📊 Reports",
        "💲 Rate Card",
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

            # ── Extra charges frequency ───────────────────────────────────────
            st.markdown("#### Extra Charges Frequency")
            charge_counts: dict[str, int] = defaultdict(int)
            for ci in all_ci:
                for charge in ci.get("extra_charges", []):
                    charge_counts[charge.replace("_", " ").title()] += 1
            if charge_counts:
                st.bar_chart(charge_counts)
            else:
                st.caption("No extra charges recorded yet.")

    # ──────────────────────────────────────────────────────────────────────────
    # TAB 2 — RATE CARD EDITOR
    # ──────────────────────────────────────────────────────────────────────────
    with tab_rates:
        st.subheader("Rate Card")

        _debug_rates = dm.get_rate_card()
        if not _debug_rates:
            st.error("⚠️ Rate card file not found or empty. Check that data/rate_card.json exists.")
        else:
            st.caption(f"Loaded {len(_debug_rates)} rate entries from file.")

        labels = {
            "in_out"                         : "In-Out Storage (per pallet)",
            "transfer"                       : "Transfer (per pallet)",
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

        # ── Default Rates ─────────────────────────────────────────────────────
        st.markdown("#### Default Rates")
        st.caption("Applies to all clients unless a client-specific rate is set.")

        default_rates = dm.get_rate_card()
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

        if st.button("💾 Save Default Rates", type="primary"):
            dm.update_rate_card(updated)
            st.success("Default rates saved.")

        st.markdown("---")

        # ── Per-Client Rates ──────────────────────────────────────────────────
        st.markdown("#### Per-Client Rate Overrides")
        st.caption("Set rates for a specific client. Only fields you change here will override the defaults.")

        all_client_rates = dm.get_client_rates()

        if all_client_rates:
            st.markdown("**Clients with custom rates:**")
            for cname, crates in all_client_rates.items():
                with st.expander(cname):
                    override_col1, override_col2 = st.columns(2)
                    override_items = list(labels.items())
                    new_overrides: dict[str, float] = {}
                    for i, (key, label) in enumerate(override_items):
                        col = override_col1 if i < len(override_items) // 2 + len(override_items) % 2 else override_col2
                        default_val = float(default_rates.get(key, 0))
                        current_val = float(crates.get(key, default_val))
                        is_override = key in crates
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

                    btn_col1, btn_col2 = st.columns(2)
                    if btn_col1.button("💾 Save", key=f"save_cr_{cname}", type="primary"):
                        dm.set_client_rates(cname, new_overrides)
                        st.success(f"Rates saved for {cname}.")
                        st.rerun()
                    if btn_col2.button("🗑 Remove overrides", key=f"del_cr_{cname}"):
                        dm.delete_client_rates(cname)
                        st.success(f"Custom rates removed for {cname}. Now using defaults.")
                        st.rerun()
        else:
            st.info("No client-specific rates set yet.")

        st.markdown("---")
        st.markdown("**Add rates for a new client:**")
        new_client_name = st.text_input("Client name", placeholder="e.g. Walmart", key="new_client_name")

        if new_client_name.strip():
            new_col1, new_col2 = st.columns(2)
            new_client_overrides: dict[str, float] = {}
            new_items = list(labels.items())
            for i, (key, label) in enumerate(new_items):
                col = new_col1 if i < len(new_items) // 2 + len(new_items) % 2 else new_col2
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

            if st.button("💾 Save Client Rates", type="primary", key="save_new_client"):
                if not new_client_overrides:
                    st.warning("No rates differ from the defaults — nothing to save.")
                else:
                    dm.set_client_rates(new_client_name.strip().upper(), new_client_overrides)
                    st.success(f"Custom rates saved for {new_client_name.strip().upper()}.")
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
            selected_client = st.selectbox(
                "Select client", options=client_names, key="mgmt_client_sel"
            )
            client_records = [
                ci for ci in all_ci
                if ci.get("client_name", "").strip() == selected_client
            ]
            st.caption(f"{len(client_records)} invoice record(s) for **{selected_client}**")

            if client_records:
                rows = []
                for ci in client_records:
                    qb_num = ci.get("quickbooks_invoice_number")
                    if not qb_num and ci.get("provider_invoice_id"):
                        prov = dm.get_provider_invoice_by_id(ci["provider_invoice_id"])
                        qb_num = prov.get("invoice_number") if prov else None
                    rows.append({
                        "Invoice #" : qb_num or "—",
                        "Date"      : ci.get("invoice_date", "—"),
                        "Status"    : ci.get("status", "—"),
                        "Total"     : f"${ci.get('total', 0):,.2f}",
                    })
                st.dataframe(rows, use_container_width=True, hide_index=True)

            _del_confirm_key = f"confirm_del_client_{selected_client}"
            if st.session_state.get(_del_confirm_key):
                st.warning(f"Delete ALL {len(client_records)} record(s) for **{selected_client}**? This cannot be undone.")
                c1, c2 = st.columns(2)
                if c1.button("✅ Yes, delete", key=f"del_client_yes_{selected_client}", type="primary", width='stretch'):
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
                    dm.delete_client_rates(selected_client)
                    st.session_state.pop(_del_confirm_key, None)
                    st.success(f"All records for {selected_client} deleted.")
                    st.rerun()
                if c2.button("✗ Cancel", key=f"del_client_no_{selected_client}", width='stretch'):
                    st.session_state.pop(_del_confirm_key, None)
                    st.rerun()
            else:
                if st.button(f"🗑 Delete all records for {selected_client}", type="primary", key=f"del_client_{selected_client}"):
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
