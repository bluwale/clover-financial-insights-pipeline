"""Step-7 check (offline): the entrypoint chains build→generate→email, dry-runs by default, sends
only with --send, and folds the headline into the subject. All collaborators are monkeypatched."""
from __future__ import annotations

from reports import run


def _wire(monkeypatch, result):
    sent = {}
    monkeypatch.setattr(
        run, "build_snapshot",
        lambda rt, business_id=None: {"report_type": rt, "schema_version": "1.0"},
    )
    monkeypatch.setattr(run, "generate_report", lambda rt, snap: result)
    monkeypatch.setattr(run, "validate", lambda **k: None)

    def fake_send(subject, html, dry_run=False):
        sent.update(subject=subject, html=html, dry_run=dry_run)
        return {"dry_run": dry_run}

    monkeypatch.setattr(run, "send_email", fake_send)
    return sent


def test_dry_run_by_default(monkeypatch):
    sent = _wire(monkeypatch, {"html": "<b>hi</b>", "headline": "Party Wear won", "llm_available": True, "flagged": []})
    out = run.run_report("weekly")
    assert sent["dry_run"] is True                 # not sent
    assert "Party Wear won" in sent["subject"]     # headline in subject
    assert sent["html"] == "<b>hi</b>"
    assert out["headline"] == "Party Wear won"


def test_send_flag_delivers(monkeypatch):
    sent = _wire(monkeypatch, {"html": "x", "headline": None, "llm_available": False, "flagged": ["$9"]})
    run.run_report("daily", send=True)
    assert sent["dry_run"] is False                # actually sends
    assert sent["subject"] == "Store daily summary — Location A"  # no headline -> base subject + location


_LOCATIONS = [
    {"business_id": "store-001", "name": "Location A", "merchant_id": "m1", "api_token": "t1", "id_prefix": ""},
    {"business_id": "store-002", "name": "Location B", "merchant_id": "m2", "api_token": "t2", "id_prefix": "b:"},
]


def _wire_multi(monkeypatch, sent_emails):
    monkeypatch.setattr(run, "LOCATIONS", _LOCATIONS)
    monkeypatch.setattr(run, "location_ready", lambda loc: True)
    monkeypatch.setattr(
        run, "build_snapshot",
        lambda rt, business_id=None: {"report_type": rt, "business_id": business_id, "revenue": {}},
    )
    monkeypatch.setattr(
        run, "generate_report",
        lambda rt, snap: {"html": "x", "headline": None, "llm_available": False, "flagged": []},
    )
    monkeypatch.setattr(run, "validate", lambda **k: None)

    def fake_send(subject, html, dry_run=False):
        sent_emails.append(subject)
        return {"dry_run": dry_run}

    monkeypatch.setattr(run, "send_email", fake_send)


def test_run_all_locations_sends_one_report_per_location_plus_comparison(monkeypatch):
    sent: list[str] = []
    _wire_multi(monkeypatch, sent)
    results = run.run_all_locations("weekly")
    assert set(results) == {"Location A", "Location B"}
    assert "Store weekly report — Location A" in sent
    assert "Store weekly report — Location B" in sent
    assert "Store weekly report — location comparison" in sent
    assert len(sent) == 3


def test_run_all_locations_skips_unready_and_omits_comparison(monkeypatch):
    """Only one location ready -> that report still sends, but no comparison email (needs 2+)."""
    sent: list[str] = []
    _wire_multi(monkeypatch, sent)
    monkeypatch.setattr(run, "location_ready", lambda loc: loc["name"] == "Location A")
    results = run.run_all_locations("weekly")
    assert set(results) == {"Location A"}
    assert sent == ["Store weekly report — Location A"]
