"""
Clover API base client — async httpx with auth, 429 backoff, and pagination.

ORIGIN: migrated from the `sandbox/` Clover API explorer. The sandbox existed only to
understand the Clover API shape before building the ETL; this is its production home, but
it is NOT yet fully hardened (no metrics, no partial-failure recovery, no structured
per-call audit). Credentials now come from `settings`, not direct os.getenv calls.
"""
from __future__ import annotations

import asyncio
from typing import AsyncGenerator

import httpx

from settings import CLOVER_BASE_URL
from utils.logging import get_logger

log = get_logger("etl.clover")


class CloverClient:
    """One client = one Clover merchant account. merchant_id/api_token are required (not read
    from settings directly) so every call site is explicit about which location it's talking to
    — see settings.LOCATIONS."""

    def __init__(self, *, merchant_id: str, api_token: str, delay_ms: int = 200):
        self._delay = delay_ms / 1000
        self._http = httpx.AsyncClient(
            base_url=f"{CLOVER_BASE_URL}/v3/merchants/{merchant_id}",
            headers={"Authorization": f"Bearer {api_token}"},
            timeout=20.0,
        )

    async def __aenter__(self) -> "CloverClient":
        return self

    async def __aexit__(self, *_) -> None:
        await self._http.aclose()

    # ── Core GET with retry on 429 ───────────────────────────────────────────────
    async def get(self, path: str, **params) -> dict:
        """Single GET. Retries up to 5x on 429 with exponential backoff.

        Pass query params as kwargs: client.get("/orders", limit=10, expand="lineItems").
        Multiple values for one key (e.g. filter): pass as a list.
        """
        for attempt in range(5):
            r = await self._http.get(path, params=params)
            if r.status_code == 429:
                wait = 2 ** attempt
                log.warning("rate limited on %s — waiting %ss", path, wait)
                await asyncio.sleep(wait)
                continue
            if r.status_code == 401:
                raise PermissionError(
                    "Clover returned 401 Unauthorized.\n"
                    f"Request URL: {r.request.url}\n"
                    "Check CLOVER_BASE_URL matches the token environment:\n"
                    "  sandbox:    https://apisandbox.dev.clover.com\n"
                    "  production: https://api.clover.com\n"
                    "Also confirm CLOVER_MERCHANT_ID is the 13-character Clover merchantId and that "
                    "the token belongs to that merchant with the required permissions."
                )
            r.raise_for_status()
            return r.json()
        raise RuntimeError(f"Failed after 5 retries: GET {path}")

    # ── Paginate — yields every element across all pages ─────────────────────────
    async def paginate(self, path: str, limit: int = 100, **params) -> AsyncGenerator[dict, None]:
        """Async generator over all elements of a paginated endpoint."""
        offset = 0
        while True:
            data = await self.get(path, limit=limit, offset=offset, **params)
            elements = data.get("elements", [])
            for el in elements:
                yield el
            if len(elements) < limit:
                break
            offset += limit
            await asyncio.sleep(self._delay)

    # ── Paginate within a date window (required for orders/payments) ─────────────
    async def paginate_window(
        self, path: str, start_ms: int, end_ms: int, limit: int = 100,
        time_field: str = "createdTime", **params,
    ) -> AsyncGenerator[dict, None]:
        """Paginate within a time window. Clover enforces a 90-day max on /orders, /payments.

        start_ms / end_ms are Unix epoch milliseconds. ``time_field`` selects the column the
        window filters on — incremental sync uses ``modifiedTime`` so voids/refunds applied
        after creation are re-pulled (§3.2/§3.6); backfill/sampling uses ``createdTime``.
        A caller-supplied ``filter`` (str or list) is merged with the window filters rather
        than colliding with them.
        """
        extra = params.pop("filter", [])
        if isinstance(extra, str):
            extra = [extra]
        filter_params = [f"{time_field}>={start_ms}", f"{time_field}<={end_ms}", *extra]
        offset = 0
        while True:
            data = await self.get(path, limit=limit, offset=offset, filter=filter_params, **params)
            elements = data.get("elements", [])
            for el in elements:
                yield el
            if len(elements) < limit:
                break
            offset += limit
            await asyncio.sleep(self._delay)
