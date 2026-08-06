"""
Scheduler — APScheduler orchestration (ProjectSummary §10).

Registers the recurring cadence and runs it in the foreground:
  * nightly ETL   — incremental Clover sync (etl.sync.run_full_sync)
  * weekly/monthly — reports.run.run_report -> email (daily summaries turned off — too
    noisy a cadence for stakeholders; weekly/monthly stay live)
  * daily         — low-stock alert check (analytics.inventory -> delivery.alerts)

All triggers use the store timezone (settings.STORE_TIMEZONE). Job bodies import their
dependencies lazily so importing this module stays cheap (and test-safe).

    python scheduler.py
"""
from __future__ import annotations

import asyncio
from functools import partial
from zoneinfo import ZoneInfo

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger

from settings import STORE_TIMEZONE
from utils.logging import get_logger

log = get_logger("scheduler")


def _nightly_sync() -> None:
    from etl.sync import run_full_sync

    log.info("nightly sync starting")
    summary = asyncio.run(run_full_sync())
    log.info("nightly sync done: %s", summary)


def _report(report_type: str) -> None:
    from reports.run import run_all_locations

    log.info("%s report starting", report_type)
    run_all_locations(report_type, send=True)


def _low_stock_check() -> None:
    from datetime import date

    from analytics import inventory
    from db.connection import get_connection
    from delivery.alerts import send_alert
    from settings import LOCATIONS, location_ready

    conn = get_connection()
    try:
        for loc in LOCATIONS:
            if not location_ready(loc):
                continue
            inv = inventory.compute(conn, date.today().isoformat(), business_id=loc["business_id"])
            low = inv.get("low_stock") or []
            if low:
                log.info("low-stock alert: %d sku(s) at %s", len(low), loc["name"])
                send_alert("low_stock", {"location": loc["name"], "items": low})
            else:
                log.info("low-stock check: nothing to alert at %s", loc["name"])
    finally:
        conn.close()


def build_scheduler() -> BlockingScheduler:
    """Register the cadence and return the (not-yet-started) scheduler. Split out for testing."""
    sched = BlockingScheduler(timezone=ZoneInfo(STORE_TIMEZONE))
    sched.add_job(_nightly_sync, CronTrigger(hour=2, minute=0), id="nightly_sync")
    sched.add_job(partial(_report, "weekly"),
                  CronTrigger(day_of_week="mon", hour=6, minute=30), id="weekly_report")
    sched.add_job(partial(_report, "monthly"),
                  CronTrigger(day=1, hour=7, minute=0), id="monthly_report")
    sched.add_job(_low_stock_check, CronTrigger(hour=8, minute=0), id="low_stock_check")
    return sched


def main() -> None:
    sched = build_scheduler()
    log.info("scheduler starting with jobs: %s", ", ".join(j.id for j in sched.get_jobs()))
    try:
        sched.start()
    except (KeyboardInterrupt, SystemExit):
        log.info("scheduler stopped")


if __name__ == "__main__":
    main()
