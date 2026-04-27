import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

# Microsoft Graph / O365
MS_CLIENT_ID     = os.getenv("MS_CLIENT_ID", "")
MS_CLIENT_SECRET = os.getenv("MS_CLIENT_SECRET", "")
MS_TENANT_ID     = os.getenv("MS_TENANT_ID", "")
WORKER_EMAIL     = os.getenv("WORKER_EMAIL", "")
ADMIN_EMAIL      = os.getenv("ADMIN_EMAIL", "")

# Anthropic
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")

# Supabase
SUPABASE_URL              = os.getenv("SUPABASE_URL", "")
SUPABASE_SERVICE_ROLE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")

# Outlook folder to poll
OUTLOOK_INVOICE_FOLDER = os.getenv("OUTLOOK_INVOICE_FOLDER", "Provider Invoices")

# Paths
BASE_DIR    = Path(__file__).parent
DATA_DIR    = BASE_DIR / "data"
PDFS_DIR    = BASE_DIR / "pdfs"
PHOTOS_DIR  = BASE_DIR / "photos"
EXPORTS_DIR = BASE_DIR / "exports"
LOGS_DIR    = BASE_DIR / "logs"

for _dir in [DATA_DIR, PDFS_DIR, PHOTOS_DIR, EXPORTS_DIR, LOGS_DIR]:
    _dir.mkdir(exist_ok=True)

# Claude model (override via CLAUDE_MODEL env var if needed)
CLAUDE_MODEL = os.getenv("CLAUDE_MODEL", "claude-sonnet-4-6")

REQUIRED_VARS = [
    "MS_CLIENT_ID",
    "MS_CLIENT_SECRET",
    "MS_TENANT_ID",
    "WORKER_EMAIL",
    "ADMIN_EMAIL",
    "ANTHROPIC_API_KEY",
]


def validate_config() -> list[str]:
    """Return list of missing required environment variable names."""
    return [v for v in REQUIRED_VARS if not os.getenv(v)]
