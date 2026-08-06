"""Alerts check (offline): compose subject + escaped HTML from the payload and route through
send_email (which is monkeypatched — no real email)."""
from __future__ import annotations

from delivery import alerts


def _capture(monkeypatch):
    seen = {}

    def fake_send(subject, html, *, dry_run=False):
        seen.update(subject=subject, html=html, dry_run=dry_run)
        return {"dry_run": dry_run}

    monkeypatch.setattr(alerts, "send_email", fake_send)
    return seen


def test_low_stock_items_table(monkeypatch):
    seen = _capture(monkeypatch)
    out = alerts.send_alert(
        "low_stock", {"items": [{"sku": "1111100000001", "name": "Salwar Suit", "qty": 2}]}, dry_run=True
    )
    assert out["dry_run"] is True
    assert "Low-stock alert" in seen["subject"]
    assert "1111100000001" in seen["html"] and "Salwar Suit" in seen["html"]


def test_refund_spike_scalars(monkeypatch):
    seen = _capture(monkeypatch)
    alerts.send_alert("refund_spike", {"refund_total": "$500", "magnitude": "3.8x baseline"}, dry_run=True)
    assert "Refund-spike alert" in seen["subject"]
    assert "3.8x baseline" in seen["html"]


def test_payload_is_escaped(monkeypatch):
    seen = _capture(monkeypatch)
    alerts.send_alert("low_stock", {"items": [{"name": "<script>x</script>", "sku": "1"}]}, dry_run=True)
    assert "<script>x</script>" not in seen["html"]
    assert "&lt;script&gt;" in seen["html"]


def test_unknown_kind_titleized(monkeypatch):
    seen = _capture(monkeypatch)
    alerts.send_alert("weird_thing", {"a": 1}, dry_run=True)
    assert "Weird Thing" in seen["subject"]
