"""
reconciliation.py
=================
Scans email_intake_log for records stuck in the same status for 24+ hours
and sends alerts to the admin.
"""

import logging
from datetime import datetime, timedelta, timezone

from data_manager import DataManager
from alerting.alert_manager import AlertManager

logger = logging.getLogger(__name__)

STUCK_THRESHOLD_HOURS = 24
_TERMINAL_STATUSES    = {"invoiced", "exported_to_qb"}


def check_stuck_invoices(dm: DataManager, alert_manager: AlertManager) -> None:
    """
    Find email_intake_log entries that have been stuck in a non-terminal
    status for longer than STUCK_THRESHOLD_HOURS and send an admin alert.
    """
    threshold  = datetime.now(timezone.utc) - timedelta(hours=STUCK_THRESHOLD_HOURS)
    logs       = dm.get_email_logs()
    stuck: list[dict] = []

    for log in logs:
        status = log.get("status", "")
        if status in _TERMINAL_STATUSES:
            continue

        created_at_str = log.get("created_at", "")
        if not created_at_str:
            continue

        try:
            created_at = datetime.fromisoformat(created_at_str.replace("Z", "+00:00"))
        except ValueError:
            continue

        if created_at < threshold:
            hours_stuck = int((datetime.now(timezone.utc) - created_at).total_seconds() / 3600)
            stuck.append({**log, "_hours_stuck": hours_stuck})

    if not stuck:
        logger.info("Reconciliation: no stuck invoices found.")
        return

    logger.warning("Reconciliation: %d stuck invoice(s) found.", len(stuck))
    for log in stuck:
        logger.warning(
            "  Stuck: id=%s status=%s hours=%d subject=%s",
            log["id"], log["status"], log["_hours_stuck"], log.get("subject"),
        )
        alert_manager.invoice_stuck(
            invoice_id=log["id"],
            status=log["status"],
            hours=log["_hours_stuck"],
        )
