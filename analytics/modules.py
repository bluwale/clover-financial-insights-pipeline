"""
Module toggles — which analytics modules reach the report.

The engine always computes and persists the FULL §5.5 payload, so analytics_snapshots stays a
complete history: turning a module back on makes it reappear in reports built from snapshots
taken while it was off. The toggle therefore filters at the *report boundary*, not at compute
time.

``MODULES`` is the map from a module name (what a human toggles) to the payload keys that module
owns (what actually gets dropped). It lives here, next to the engine that produces those keys —
reports/ imports downward into analytics/, never the reverse.

Two notes for whoever wires up ``reports.generator``:
  * Filter ONCE, then feed the same filtered snapshot to both the LLM user message and
    ``renderer.render_html`` — otherwise the narrative and the data block disagree.
  * ``guardrails.verify_numbers`` must take that same filtered dict as its source pool. If it
    sees the unfiltered snapshot, a number the LLM hallucinated from a disabled module would
    still match a source value and pass the check.
"""
from __future__ import annotations

from typing import Iterable, Optional

import settings  # module, not `from settings import ...`: read the spec at call time so an
                 # override (tests, a future per-report setting) is picked up, not frozen at import
from utils.logging import get_logger

log = get_logger("analytics.modules")

# module name → the payload keys it owns. Every key the engine emits is either listed here or
# in ALWAYS_ON; a new module means a new entry (and a new test in tests/test_modules.py).
MODULES: dict[str, tuple[str, ...]] = {
    "revenue": ("revenue",),
    "inventory": ("inventory", "stock_risks", "slow_skus"),
    "size_curve": ("size_curve", "top_category"),
    "sell_through": ("new_arrival_sell_through", "underperforming_arrivals"),
    "anomalies": ("anomalies", "forecasting"),
    "operational": ("operational",),
    "calendar": ("cultural_events_upcoming",),
    "margin": ("margin", "cost_data_confidence"),
}

# Envelope keys — never filtered. The renderer's header and the prompt/schema routing in
# reports.prompts read these, so dropping them would break the report itself, not a section.
ALWAYS_ON: tuple[str, ...] = ("report_type", "schema_version", "period")


def all_modules() -> frozenset[str]:
    """Every toggleable module name."""
    return frozenset(MODULES)


def parse_spec(spec: str) -> frozenset[str]:
    """Parse a REPORT_MODULES spec string into the set of enabled module names.

    Grammar (comma-separated, whitespace tolerated, case-insensitive):
      * ``"all"`` or empty  → every module.
      * bare names          → a whitelist: ``"revenue, inventory"`` enables exactly those two.
      * ``-name`` entries   → subtracted: ``"-operational"`` means everything except operational.
      * mixed               → inclusions form the starting set, then exclusions are removed.

    Unknown names are warned about and ignored rather than raised: a typo in .env should cost a
    section of the nightly report, not the whole run. Pass an explicit set to `select()` when you
    want a typo to fail loudly instead.
    """
    entries = [e.strip().lower() for e in spec.split(",")]
    entries = [e for e in entries if e]

    if not entries or entries == ["all"]:
        return all_modules()

    include: set[str] = set()
    exclude: set[str] = set()
    for entry in entries:
        if entry == "all":
            include |= all_modules()
            continue
        target, name = (exclude, entry[1:].strip()) if entry.startswith("-") else (include, entry)
        if name not in MODULES:
            log.warning(
                "REPORT_MODULES: unknown module %r ignored; known modules: %s",
                name, ", ".join(sorted(MODULES)),
            )
            continue
        target.add(name)

    enabled = (include or all_modules()) - exclude
    if not enabled:
        log.warning("REPORT_MODULES=%r enables no modules; reports will carry data only", spec)
    return frozenset(enabled)


def resolve(modules: Optional[Iterable[str]] = None) -> frozenset[str]:
    """Normalise a caller's module selection; ``None`` falls back to the REPORT_MODULES setting.

    Raises ValueError on an unknown name — an explicit selection comes from code, where a typo is
    a bug worth surfacing (the env-var path in `parse_spec` warns and continues instead).
    """
    if modules is None:
        return parse_spec(settings.REPORT_MODULES)
    requested = frozenset(m.strip().lower() for m in modules)
    unknown = requested - all_modules()
    if unknown:
        raise ValueError(
            f"unknown module(s) {sorted(unknown)}; expected any of {sorted(MODULES)}"
        )
    return requested


def select(snapshot: dict, modules: Optional[Iterable[str]] = None) -> dict:
    """Return a shallow copy of `snapshot` carrying only the enabled modules' keys.

    ``modules=None`` uses the configured default (REPORT_MODULES, "all" out of the box, so this
    is a no-op until someone actually toggles something). ALWAYS_ON keys always survive; keys the
    registry doesn't know about are passed through untouched, so an older or hand-built snapshot
    is never silently stripped.
    """
    enabled = resolve(modules)
    dropped = {key for name, keys in MODULES.items() if name not in enabled for key in keys}
    return {k: v for k, v in snapshot.items() if k in ALWAYS_ON or k not in dropped}
