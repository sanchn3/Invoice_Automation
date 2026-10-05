# INCO Invoice Automation

End-to-end invoice automation for a cold-room logistics/warehouse business.
Polls a worker's Outlook inbox for provider invoices, parses PDF attachments,
lets staff fill in job details via role-based dashboards, generates client
invoices with calculated charges, and exports to QuickBooks Desktop via IIF.

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
provider_invoices.json  +  client_invoices.json  (status: pending_validation)
     │
     ▼
[Admin Dashboard — Validate tab]  ──►  review parsed invoice, validate
     │
     ▼
[Admin Dashboard — To Be Received tab]  ──►  mark physical shipment received
     │
     ▼
[Admin Dashboard — Approve & Invoice tab]  ──►  enter job details, generate invoice
     │
     ▼
client_invoices.json  (status: invoiced)
     │
     ├──►  [Accounting Dashboard]  ──►  review, edit fields, export to QuickBooks
     │
     └──►  iif_exporter.py  ──►  invoices_export_{timestamp}.iif  ──►  QuickBooks Desktop
```

---

## User Roles

| Role | Dashboard | Access |
|------|-----------|--------|
| **Lead / Worker** | Lead Dashboard | Rate card management, client setup, invoice reports, job photo review |
| **Administrator** | Admin Dashboard | Invoice validation, receiving, approval, extended storage billing |
| **Accounting** | Accounting Dashboard | Invoice review & editing, QuickBooks export, client billing |
| **BOL** | BOL Dashboard | Bill of Lading creation and management |

---

## Prerequisites

- Python 3.11+
- A Microsoft Azure App Registration with:
  - `Mail.Read` and `Mail.Send` permissions (Application type)
  - Admin consent granted
- An Anthropic API key
- A Supabase project (for invoice + client persistence across deployments)
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
SUPABASE_URL=https://your-project.supabase.co
SUPABASE_SERVICE_ROLE_KEY=your-service-role-key
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

### 4. Set up Supabase tables

Run the following SQL in your Supabase SQL editor:

```sql
-- Invoice pipeline tables
CREATE TABLE pipeline_client_invoices (
  local_id   TEXT PRIMARY KEY,
  data       JSONB NOT NULL DEFAULT '{}',
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE pipeline_provider_invoices (
  local_id   TEXT PRIMARY KEY,
  data       JSONB NOT NULL DEFAULT '{}',
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Client registry (persists rate cards, addresses, emails, RFCs, initials)
CREATE TABLE client_registry (
  client_name TEXT PRIMARY KEY,
  data        JSONB NOT NULL DEFAULT '{}',
  updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Invoice number counters (persists QB invoice numbering across redeploys)
CREATE TABLE client_invoice_counters (
  client_name TEXT PRIMARY KEY,
  counter     INTEGER NOT NULL DEFAULT 2000,
  updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Cold storage archive (for accounting/reporting)
CREATE TABLE cold_storage_invoices (
  local_id    TEXT PRIMARY KEY,
  data        JSONB NOT NULL DEFAULT '{}',
  updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
```

### 5. Set up the Outlook folder

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

### Streamlit App

```bash
streamlit run streamlit_app/app.py
```

Opens in your browser at `http://localhost:8501`

---

## How It Works

### Admin Dashboard

Accessed via the **Administrator** role. Four modes selectable via radio:

| Mode | Purpose |
|------|---------|
| **In** | Three-tab pipeline: Validate parsed invoices → mark as Received → Approve & generate client invoice |
| **Out** | Bill of Lading creation and management |
| **📷 Operation Photos** | Browse and download photos by lot number |
| **📦 Extended Storage** | Generate a direct extended-storage invoice for a client (pallets × rate, sent straight to Accounting) |

#### Invoice Pipeline (In mode)

| Tab | Status transition | What happens |
|-----|-----------------|-------------|
| 🗂 Validate | `pending_validation` | Review the parsed provider invoice, manually edit fields if needed, click Validate |
| 📦 To Be Received | `to_be_received` | Enter the physical received date; PDF is stamped automatically |
| ✅ Approve & Invoice | `validated` → `invoiced` | Enter job details (pallets, extras, temps, notes), generate the client invoice with a QB number |

### Lead Dashboard

Accessed via the **Lead** role. Three tabs:

| Tab | Purpose |
|-----|---------|
| 📊 Reports | KPI metrics, charts by client/service/week, Excel export |
| 💲 Rate Card | Edit default rates and per-client rate overrides. Set billing address, email, RFC, and initials per client. Add or delete clients. |
| 🗂 Client Data Management | Filter and bulk-delete invoice records by client |

#### Rate Card — Extended Storage

The Rate Card includes an **Extended Storage (per pallet)** field for each client.
This rate is used by the Admin's Extended Storage invoice mode to calculate the total.

### Accounting Dashboard

Accessed via the **Accounting** role. Four tabs:

| Tab | Purpose |
|-----|---------|
| 🔍 Invoice Review | Review invoiced submissions. Edit Invoice Date, Due Date, P.O. Number, and Service Number. PDF re-generates automatically on save. Mark ready for export. |
| 📥 Import to QuickBooks | Select invoices and download an IIF file for QuickBooks Desktop import |
| 📧 Send Invoices | Compose and download client invoice emails with PDF attachments |
| 📋 Invoice History | Browse all exported and paid invoices. Download PDFs and IIF files. |

### Extended Storage Billing

For clients in long-term pallet storage:

1. Lead sets the **Extended Storage (per pallet)** rate in the Rate Card for the client
2. Admin opens **📦 Extended Storage** mode, selects the client, enters a service number and pallet count
3. The invoice total is calculated as `pallets × rate` and sent directly to Accounting (`status: invoiced`)
4. Accounting can edit the service number in the Invoice Review tab; the PDF updates immediately

---

## Invoice Statuses

| Status | Meaning |
|--------|---------|
| `pending_validation` | PDF parsed, awaiting admin review |
| `to_be_received` | Validated by admin, awaiting physical receipt confirmation |
| `validated` | Received, ready for admin to approve and generate invoice |
| `ready_to_invoice` | Returned to admin from accounting for correction |
| `invoiced` | Client invoice generated with QB number, in Accounting |
| `ready_for_export` | Accounting approved, ready for QuickBooks export |
| `exported_to_qb` | IIF exported to QuickBooks |

---

## Persistence & Supabase Sync

All data is stored locally in JSON files under `data/`. On a Render redeploy the
ephemeral filesystem is wiped; Supabase is used to restore data on next startup.

| Data | Local file | Supabase table | Sync trigger |
|------|-----------|---------------|-------------|
| Client invoices | `client_invoices.json` | `pipeline_client_invoices` | Every create/update |
| Provider invoices | `provider_invoices.json` | `pipeline_provider_invoices` | Every create/update |
| Clients (rates, address, etc.) | 5 JSON files | `client_registry` | Every create/update/delete |
| Invoice counters | `client_invoice_counters.json` | `client_invoice_counters` | Every increment |
| Processed invoices | — | `cold_storage_invoices` | Scheduler batch |

All Supabase calls are fire-and-forget — failures are logged silently and the app
continues using local files.

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
in that provider's invoice layout. If parsing fails, the Claude AI fallback
handles it automatically.

---

## Project Structure

```
invoice_automation/
├── .env                          # Secrets — never commit
├── requirements.txt
├── main.py                       # Email poller entry point
├── config.py                     # Env vars + path constants
├── data_manager.py               # ALL data read/write + Supabase sync
├── data/
│   ├── email_intake_log.json
│   ├── provider_invoices.json
│   ├── client_invoices.json
│   ├── client_rates.json         # Per-client rate overrides
│   ├── client_addresses.json
│   ├── client_emails.json
│   ├── client_initials.json
│   ├── client_rfcs.json
│   ├── client_invoice_counters.json
│   ├── providers.json
│   ├── rate_card.json            # Default rates
│   └── bol_records.json
├── pdfs/                         # Saved provider invoice PDFs
├── photos/                       # Operation photos
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
│   ├── charge_calculator.py      # Rate card × job details = line items + total
│   ├── pdf_generator.py          # ReportLab client invoice PDF
│   ├── stamp_pdf.py              # Stamps received date onto provider PDF
│   └── iif_exporter.py           # QuickBooks IIF generator
├── alerting/
│   └── alert_manager.py          # Email alerts via Graph API
├── scheduler/
│   ├── supabase_sync.py          # Batch sync to cold_storage_invoices
│   └── reconciliation.py         # Stuck invoice checker
├── utils/
│   └── pdf_storage.py            # PDF upload/fetch helpers
└── streamlit_app/
    ├── app.py                    # Streamlit entry point + auth
    ├── app_production.py         # Production entry point (Render)
    └── views/
        ├── admin_dashboard.py    # Validate / Receive / Approve / Extended Storage / Photos
        ├── lead_dashboard.py     # Reports / Rate Card / Client Management
        ├── accounting_dashboard.py # Invoice Review / QB Export / Email / History
        └── bol_dashboard.py      # Bill of Lading management
```
