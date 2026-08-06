"""
Endpoint functions — one per Clover resource we care about.

ORIGIN: migrated from the `sandbox/` Clover API explorer (exploration code, not production).
Each function takes a CloverClient and returns clean data; these are the building blocks the
ETL sync layer (etl/sync.py) will call. Sample/limit-oriented helpers below are kept as-is
from exploration and will be supplemented by full incremental-sync fetchers.
"""
from __future__ import annotations

from etl.clover_client import CloverClient
from utils.timeutils import day_window


# ── Merchant ──────────────────────────────────────────────────────────────────
async def get_merchant(client: CloverClient) -> dict:
    """Fetch merchant profile. Good first call to verify credentials."""
    return await client.get("")


# ── Orders ────────────────────────────────────────────────────────────────────
async def get_orders_sample(client: CloverClient, days: int = 7, limit: int = 10) -> list[dict]:
    """Fetch a sample of recent orders with line items + payments expanded (max 90-day window)."""
    start, end = day_window(days)
    orders: list[dict] = []
    async for order in client.paginate_window(
        "/orders", start_ms=start, end_ms=end, limit=limit,
        expand="lineItems,payments", orderBy="createdTime DESC",
    ):
        orders.append(order)
        if len(orders) >= limit:
            break
    return orders


async def get_order(client: CloverClient, order_id: str) -> dict:
    """Fetch a single order by ID with all nested data."""
    return await client.get(f"/orders/{order_id}", expand="lineItems,payments,discounts,customers")


async def get_order_line_items(client: CloverClient, order_id: str) -> list[dict]:
    """Fetch all line items for an order (use when an order may have >100 items)."""
    items: list[dict] = []
    async for item in client.paginate(f"/orders/{order_id}/line_items"):
        items.append(item)
    return items


# ── Payments ──────────────────────────────────────────────────────────────────
async def get_payments_sample(client: CloverClient, days: int = 7, limit: int = 20) -> list[dict]:
    """Fetch recent SUCCESS payments with tender expanded."""
    start, end = day_window(days)
    payments: list[dict] = []
    async for p in client.paginate_window(
        "/payments", start_ms=start, end_ms=end, limit=limit,
        expand="tender,order,refunds", filter=["result=SUCCESS"],
    ):
        payments.append(p)
        if len(payments) >= limit:
            break
    return payments


async def get_refunds(client: CloverClient, days: int = 30) -> list[dict]:
    """Fetch refunds in the last n days."""
    start, end = day_window(days)
    refunds: list[dict] = []
    async for r in client.paginate_window("/refunds", start_ms=start, end_ms=end, expand="payment"):
        refunds.append(r)
    return refunds


# ── Inventory / Items ─────────────────────────────────────────────────────────
async def get_items(client: CloverClient, limit: int = 50) -> list[dict]:
    """Fetch inventory items with categories expanded (items are not date-scoped)."""
    items: list[dict] = []
    async for item in client.paginate("/items", expand="categories,itemGroup"):
        items.append(item)
        if len(items) >= limit:
            break
    return items


async def get_all_items(client: CloverClient) -> list[dict]:
    """Fetch every item — for a full inventory sync."""
    items: list[dict] = []
    async for item in client.paginate("/items", expand="categories,itemGroup"):
        items.append(item)
    return items


async def get_item(client: CloverClient, item_id: str) -> dict:
    return await client.get(f"/items/{item_id}", expand="categories,itemGroup")


async def get_categories(client: CloverClient) -> list[dict]:
    cats: list[dict] = []
    async for c in client.paginate("/categories"):
        cats.append(c)
    return cats


async def get_item_groups(client: CloverClient) -> list[dict]:
    """Fetch item groups (size/colour variant sets) — key for size-curve analytics."""
    groups: list[dict] = []
    async for g in client.paginate("/item_groups", expand="items"):
        groups.append(g)
    return groups


# ── Customers ─────────────────────────────────────────────────────────────────
async def get_customers(client: CloverClient, limit: int = 20) -> list[dict]:
    """Fetch customer profiles (only populated if staff collect customer info at POS)."""
    customers: list[dict] = []
    async for c in client.paginate("/customers"):
        customers.append(c)
        if len(customers) >= limit:
            break
    return customers


# ══════════════════════════════════════════════════════════════════════════════
# Production sync fetchers — unbounded async generators used by etl/sync.py.
# Unlike the *_sample helpers above (kept from exploration), these stream *every*
# matching record across all pages and filter on modifiedTime for incremental sync.
# ══════════════════════════════════════════════════════════════════════════════
async def iter_orders(client: CloverClient, start_ms: int, end_ms: int):
    """Stream orders modified in [start_ms, end_ms] with line items expanded (§3.2/§3.6)."""
    async for o in client.paginate_window(
        "/orders", start_ms=start_ms, end_ms=end_ms,
        time_field="modifiedTime", expand="lineItems,lineItems.discounts",
    ):
        yield o


async def iter_payments(client: CloverClient, start_ms: int, end_ms: int):
    """Stream payments modified in the window with tender, order ref, and nested refunds."""
    async for p in client.paginate_window(
        "/payments", start_ms=start_ms, end_ms=end_ms,
        time_field="modifiedTime", expand="tender,order,refunds",
    ):
        yield p


async def iter_products(client: CloverClient, modified_since_ms: int | None = None):
    """Stream every item (categories, item group, and stock expanded). Items are not
    date-scoped, so incremental runs add a modifiedTime filter instead of a window."""
    params: dict = {"expand": "categories,itemGroup,itemStock"}
    if modified_since_ms:
        params["filter"] = [f"modifiedTime>={modified_since_ms}"]
    async for it in client.paginate("/items", **params):
        yield it


async def iter_customers(client: CloverClient, modified_since_ms: int | None = None):
    """Stream every customer (emails/phones expanded), optionally only those modified since."""
    params: dict = {"expand": "emailAddresses,phoneNumbers"}
    if modified_since_ms:
        params["filter"] = [f"modifiedTime>={modified_since_ms}"]
    async for c in client.paginate("/customers", **params):
        yield c
