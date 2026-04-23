# INCO Invoice Automation

End-to-end invoice automation for a cold-room logistics/warehouse business.
Polls a worker's Outlook inbox for provider invoices, parses PDF attachments,
lets a worker fill in job details via a mobile form, generates client invoices
with calculated charges, and exports to QuickBooks Desktop 2018 via IIF.

---

## Architecture

```
Provider Email
     │
     ▼
outlook_listener.py  ──►  email_intake_log.json  (logged FIRST, always)
     │
     ▼
email_classifier.py  ──►  Claude: is this an invoice? (YES/NO)
     │
     ▼
attachment_handler.py  ──►  saves PDF to /pdfs/
     │
     ├──►  pdf_parser.py  (pdfplumber + provider profile)
     │         │
     │         └── failed? ──►  claude_parser.py  (AI fallback)
     │
     ▼
provider_invoices.json  +  client_invoices.json (status: pending_worker)
     │
     ▼
[Admin Dashboard]  ──►  sets service_type + temp_recorder
     │
     ▼
[Worker Form]  ──►  pallet count, extra charges, photos, notes
     │
     ▼
client_invoices.json  (status: ready_to_invoice)
     │
     ▼
[Admin Dashboard]  ──►  enters QB invoice # ──►  charge_calculator.py
     │
     ▼
client_invoices.json  (status: invoiced)
     │
     ▼
iif_exporter.py  ──►  invoices_export_{timestamp}.iif  ──►  QuickBooks Desktop
```

---

## Prerequisites

- Python 3.11+
- A Microsoft Azure App Registration with:
  - `Mail.Read` and `Mail.Send` permissions (Application type)
  - Admin consent granted
- An Anthropic API key
- QuickBooks Desktop 2018

---

## Setup

### 1. Create and activate virtual environment

```bash
cd invoice_automation
python -m venv venv

# Windows
venv\Scripts\activate

# macOS / Linux
source venv/bin/activate
```

### 2. Install dependencies

```bash
pip install -r requirements.txt
```

### 3. Configure environment variables

Edit `.env` with your actual values:

```env
MS_CLIENT_ID=your-azure-app-client-id
MS_CLIENT_SECRET=your-azure-app-secret
MS_TENANT_ID=your-azure-tenant-id
WORKER_EMAIL=worker@yourdomain.com
ADMIN_EMAIL=admin@yourdomain.com
ANTHROPIC_API_KEY=sk-ant-...
OUTLOOK_INVOICE_FOLDER=Provider Invoices
```

#### Azure App Registration (Microsoft Graph)

1. Go to [Azure Portal](https://portal.azure.com) → App registrations → New registration
2. Under **API permissions** → Add permission → Microsoft Graph → Application:
   - `Mail.Read`
   - `Mail.Send`
   - Grant admin consent
3. Under **Certificates & secrets** → New client secret → copy value to `.env`
4. Copy **Application (client) ID** → `MS_CLIENT_ID`
5. Copy **Directory (tenant) ID** → `MS_TENANT_ID`

### 4. Set up the Outlook folder

In the worker's Outlook, create a folder named **"Provider Invoices"** and configure
an Outlook rule to automatically move provider invoice emails there.

---

## Running the Application

### Email Poller (background process)

Polls the inbox every 5 minutes and processes new invoices automatically.

```bash
python main.py
```

Logs are written to `logs/poller.log`.

### Streamlit App (admin + worker UI)

```bash
streamlit run streamlit_app/app.py
```

Opens in your browser at `http://localhost:8501`

---

## How It Works

### Admin Dashboard

Accessible at the Streamlit app. Five tabs:

| Tab | Purpose |
|-----|---------|
| 🗂 Pipeline | Kanban-style board showing all invoices by status. Stuck invoices (24h+) are flagged. |
| ✅ Approve & Invoice | Set service type + temp recorder. Review worker submission. Enter QB invoice number. Generate client invoice. |
| 📤 QuickBooks Export | Select invoices to export. Downloads an IIF file. Marks invoices as exported. |
| 📊 Reports | Charts: by client, service type, week, extra charge frequency. |
| 💲 Rate Card | Edit all service rates. Changes apply immediately to new invoices. |

### Worker Form

Mobile-friendly page. Worker:
1. Selects their job from the dropdown
2. Enters pallet count, damaged/broken pallet counts
3. Checks applicable extra charges
4. Adds notes and photos
5. Submits → admin gets an email alert

### QuickBooks Export

1. Admin reviews and approves job in the **Approve & Invoice** tab
2. Admin enters the QuickBooks invoice number (from QuickBooks Desktop — **never auto-generated**)
3. Clicks **Generate Client Invoice**
4. In the **QuickBooks Export** tab, select invoices and click Export
5. Download the `.iif` file and import into QuickBooks Desktop via:
   `File → Utilities → Import → IIF Files`

---

## Adding a New Provider

Edit `data/providers.json` and add an entry:

```json
{
  "id": "generate-a-uuid-here",
  "name": "New Provider Name",
  "email_domain": "newprovider.com",
  "email_address": "invoices@newprovider.com",
  "active": true,
  "parser_profile": {
    "invoice_number_keyword": "Invoice #",
    "client_name_keyword": "Bill To",
    "date_keyword": "Invoice Date",
    "total_keyword": "Total Due"
  }
}
```

The `parser_profile` keywords tell the PDF parser where to look for each field
in that provider's specific invoice layout. If the PDF parser still fails, the
Claude fallback will handle it automatically.

---

## Updating Rates

Use the **Rate Card** tab in the admin dashboard. No code changes needed.

---

## Invoice Statuses

| Status | Meaning |
|--------|---------|
| `received` | Email logged, not yet parsed |
| `parsed` | PDF parsed, awaiting admin service setup |
| `pending_worker` | Admin set service type, waiting for worker to submit |
| `pending_review` | Parsing failed or flagged — manual review needed |
| `ready_to_invoice` | Worker submitted, admin to approve and generate invoice |
| `invoiced` | Client invoice generated with QB number |
| `exported_to_qb` | IIF exported to QuickBooks |

---

## Migrating to Supabase

All data access is isolated in `data_manager.py`. To migrate:

1. Create equivalent tables in Supabase matching the JSON structures
2. Rewrite `data_manager.py` to use `supabase-py` instead of JSON files
3. No other files need to change

---

## Project Structure

```
invoice_automation/
├── .env                          # Secrets — never commit
├── requirements.txt
├── main.py                       # Email poller entry point
├── config.py                     # Env vars + path constants
├── data_manager.py               # ALL data read/write (swap for Supabase here)
├── data/
│   ├── email_intake_log.json
│   ├── provider_invoices.json
│   ├── client_invoices.json
│   ├── providers.json
│   └── rate_card.json
├── pdfs/                         # Saved provider invoice PDFs
├── photos/                       # Worker-uploaded photos
├── exports/                      # Generated IIF files
├── logs/                         # Poller logs
├── email_pipeline/
│   ├── outlook_listener.py       # O365 inbox poller
│   ├── email_classifier.py       # Claude yes/no classifier
│   └── attachment_handler.py     # PDF save + parse trigger
├── parsing/
│   ├── pdf_parser.py             # pdfplumber parser
│   └── claude_parser.py          # Claude fallback + classifier
├── invoice_logic/
│   ├── charge_calculator.py      # Rate card × job details = total
│   └── iif_exporter.py           # QuickBooks IIF generator
├── alerting/
│   └── alert_manager.py          # Email alerts via Graph API
├── streamlit_app/
│   ├── app.py                    # Streamlit entry point
│   ├── views/
│   │   ├── admin_dashboard.py    # Admin pipeline + approval + export
│   │   └── worker_form.py        # Mobile worker job form
│   └── components/
│       ├── invoice_card.py       # Reusable invoice display card
│       └── status_badge.py       # Color-coded status pill
└── scheduler/
    └── reconciliation.py         # Stuck invoice checker
```
