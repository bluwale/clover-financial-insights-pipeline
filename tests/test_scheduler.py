"""Scheduler check (offline): build_scheduler registers the full cadence with the expected job ids
and cron triggers, without starting it (no jobs actually fire)."""
from __future__ import annotations

from scheduler import build_scheduler


def test_all_jobs_registered():
    """Daily report summaries are deliberately off (too noisy a cadence for stakeholders);
    nightly sync, weekly/monthly reports, and the low-stock check stay live."""
    sched = build_scheduler()
    jobs = {j.id: j for j in sched.get_jobs()}
    assert set(jobs) == {
        "nightly_sync", "weekly_report", "monthly_report", "low_stock_check"
    }
    assert "daily_report" not in jobs


def test_weekly_runs_monday_morning():
    sched = build_scheduler()
    weekly = {j.id: j for j in sched.get_jobs()}["weekly_report"]
    fields = {f.name: str(f) for f in weekly.trigger.fields}
    assert fields["day_of_week"] == "mon"
    assert fields["hour"] == "6"
    assert fields["minute"] == "30"
