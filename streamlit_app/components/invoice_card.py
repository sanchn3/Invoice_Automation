import streamlit as st
from streamlit_app.components.status_badge import status_badge


def invoice_card(
    email_log: dict,
    provider_invoice: dict | None = None,
    client_invoice: dict | None = None,
) -> None:
    """
    Render an invoice pipeline card as a Streamlit expander.
    Shows email log info, and if available, provider and client invoice details.
    """
    subject  = email_log.get("subject", "No subject")
    sender   = email_log.get("sender", "")
    received = email_log.get("received_at", "")[:16].replace("T", " ")
    status   = email_log.get("status", "received")

    label = f"{subject[:55]}{'...' if len(subject) > 55 else ''}"

    with st.expander(label, expanded=False):
        col1, col2 = st.columns([3, 1])
        with col1:
            st.caption(f"From: {sender}   |   Received: {received}")
        with col2:
            status_badge(status)

        if email_log.get("error_text"):
            st.error(f"Error: {email_log['error_text']}")

        if provider_invoice:
            st.markdown("---")
            st.markdown("**Provider Invoice**")
            c1, c2, c3, c4 = st.columns(4)
            c1.metric("Provider",   provider_invoice.get("provider_name", "—"))
            c2.metric("Client",     provider_invoice.get("client_name", "—"))
            c3.metric("Invoice #",  provider_invoice.get("invoice_number", "—"))
            c4.metric("Total",      f"${provider_invoice.get('total', 0):,.2f}")

            if provider_invoice.get("line_items"):
                st.markdown("**Line Items**")
                for item in provider_invoice["line_items"]:
                    st.text(
                        f"  {item.get('description', '')}  "
                        f"x{item.get('quantity', 1)}  "
                        f"@ ${item.get('unit_price', 0):.2f}  =  "
                        f"${item.get('total', 0):.2f}"
                    )

        if client_invoice:
            st.markdown("---")
            st.markdown("**Client Invoice (Our Charges)**")
            c1, c2, c3 = st.columns(3)
            svc = client_invoice.get("service_type") or "Not set"
            c1.metric("Service Type", svc.replace("_", " ").title())
            c2.metric("Pallets",      client_invoice.get("pallet_count", 0))
            c3.metric("Our Total",    f"${client_invoice.get('total', 0):,.2f}")

            qb = client_invoice.get("quickbooks_invoice_number")
            if qb:
                st.success(f"QuickBooks Invoice #: {qb}")
            else:
                st.info("QuickBooks invoice number not yet assigned.")
