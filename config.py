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

# Paths
BASE_DIR    = Path(__file__).parent
DATA_DIR    = BASE_DIR / "data"
PDFS_DIR    = BASE_DIR / "pdfs"
BOLS_DIR    = BASE_DIR / "bols"
PHOTOS_DIR  = BASE_DIR / "photos"
EXPORTS_DIR = BASE_DIR / "exports"
LOGS_DIR    = BASE_DIR / "logs"

for _dir in [DATA_DIR, PDFS_DIR, BOLS_DIR, PHOTOS_DIR, EXPORTS_DIR, LOGS_DIR]:
    _dir.mkdir(exist_ok=True)

# QuickBooks Online
QBO_CLIENT_ID     = os.getenv("QBO_CLIENT_ID", "")
QBO_CLIENT_SECRET = os.getenv("QBO_CLIENT_SECRET", "")
# Redirect URI must match exactly what is registered in your Intuit Developer app.
# Local dev: http://localhost:8501   Production: your Render app URL
QBO_REDIRECT_URI  = os.getenv("QBO_REDIRECT_URI", "http://localhost:8501")
# "sandbox" uses sandbox-quickbooks.api.intuit.com; anything else uses production
QBO_ENVIRONMENT   = os.getenv("QBO_ENVIRONMENT", "sandbox")

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
