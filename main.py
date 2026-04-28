"""
main.py
=======
Entry point for the background email poller.
Runs on APScheduler — polls inbox every 5 minutes,
checks for stuck invoices every hour.

Run with:  python main.py
Streamlit app runs separately:  streamlit run streamlit_app/app.py
"""

import logging
import sys
from pathlib import Path

from apscheduler.schedulers.blocking import BlockingScheduler

from config import validate_config, LOGS_DIR
from data_manager import DataManager
from email_pipeline.outlook_listener import poll_inbox
from alerting.alert_manager import AlertManager
from scheduler.reconciliation import check_stuck_invoices
from scheduler.supabase_sync import sync_invoices_to_supabase

# ── Logging setup ─────────────────────────────────────────────────────────────
LOGS_DIR.mkdir(exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(LOGS_DIR / "poller.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger(__name__)

# O365 library logs its own HTTP errors before our exception handlers catch them.
# Raise its threshold so those internal Client Error logs don't pollute the console.
logging.getLogger("O365").setLevel(logging.CRITICAL)
logging.getLogger("O365.connection").setLevel(logging.CRITICAL)


def main() -> None:
    logger.info("=" * 55)
    logger.info("  INCO Invoice Automation — Email Poller")
    logger.info("=" * 55)

    # Validate required environment variables
    missing = validate_config()
    if missing:
        logger.error("Missing required environment variables: %s", ", ".join(missing))
        logger.error("Please fill in your .env file and restart.")
        sys.exit(1)

    dm            = DataManager()
    alert_manager = AlertManager()

    scheduler = BlockingScheduler(timezone="UTC")

    # Poll for new invoice emails every 5 minutes
    scheduler.add_job(
        func=lambda: poll_inbox(dm, alert_manager),
        trigger="interval",
        minutes=5,
        id="email_poller",
        name="Email Inbox Poller",
        replace_existing=True,
    )

    # Check for stuck invoices every hour
    scheduler.add_job(
        func=lambda: check_stuck_invoices(dm, alert_manager),
        trigger="interval",
        hours=1,
        id="reconciliation",
        name="Stuck Invoice Checker",
        replace_existing=True,
    )

    # Sync processed invoices to Supabase every Sunday at 05:00 Central
    scheduler.add_job(
        func=lambda: sync_invoices_to_supabase(dm),
        trigger="cron",
        day_of_week="sun",
        hour=5,
        minute=0,
        timezone="America/Chicago",
        id="supabase_sync",
        name="Weekly Supabase Invoice Sync",
        replace_existing=True,
    )

    logger.info("Scheduler started. Polling inbox every 5 minutes.")
    logger.info("Press Ctrl+C to stop.")

    try:
        # Run immediately on startup, then on schedule
        logger.info("Running initial poll...")
        poll_inbox(dm, alert_manager)
        check_stuck_invoices(dm, alert_manager)
        scheduler.start()
    except KeyboardInterrupt:
        logger.info("Scheduler stopped by user.")
    except Exception as e:
        logger.error("Scheduler error: %s", e)
        sys.exit(1)


if __name__ == "__main__":
    main()
