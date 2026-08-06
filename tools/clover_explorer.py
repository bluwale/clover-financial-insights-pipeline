"""
Clover API Explorer — interactive, DEV-ONLY tool.

ORIGIN: this is the former `sandbox/sandbox.py`. It was built purely to understand the live
Clover data before designing the ETL — it is NOT part of the production pipeline and is not
imported by any layer. Kept as a convenience for manually inspecting a merchant's data.

Run from the repo root:  python tools/clover_explorer.py
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

# Allow running as a plain script: put the repo root on sys.path so top-level packages import.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from rich.console import Console
from rich.panel import Panel
from rich.syntax import Syntax
from rich.table import Table

from settings import LOCATIONS, validate
from utils.money import cents_to_str as cents
from utils.timeutils import ms_to_human as ms_to_str
from utils.clover import tender_label
from etl.clover_client import CloverClient
from etl.clover_endpoints import (
    get_categories, get_customers, get_item_groups, get_items,
    get_merchant, get_order, get_orders_sample, get_payments_sample, get_refunds,
)

console = Console()


def print_json(data, title: str = "Raw JSON") -> None:
    console.print(f"\n[dim]{title}:[/dim]")
    console.print(Syntax(json.dumps(data, indent=2), "json", theme="monokai", word_wrap=True))


def print_orders(orders: list) -> None:
    t = Table(title=f"{len(orders)} Orders", show_lines=True)
    for col, kw in (("ID", {"style": "dim", "width": 12}), ("Date", {"width": 22}),
                    ("Total", {"justify": "right", "style": "green"}), ("State", {"width": 10}),
                    ("Items", {"justify": "center", "width": 6}),
                    ("Payments", {"justify": "center", "width": 8})):
        t.add_column(col, **kw)
    for o in orders:
        state = o.get("state", "?")
        color = {"paid": "green", "open": "yellow", "voided": "red", "refunded": "magenta"}.get(state, "white")
        n_items = len(o.get("lineItems", {}).get("elements", []))
        n_pay = len(o.get("payments", {}).get("elements", []))
        t.add_row(o.get("id", "?"), ms_to_str(o.get("createdTime")), cents(o.get("total", 0)),
                  f"[{color}]{state}[/{color}]", str(n_items), str(n_pay))
    console.print(t)


def print_line_items(order: dict) -> None:
    items = order.get("lineItems", {}).get("elements", [])
    if not items:
        console.print("[yellow]No line items.[/yellow]")
        return
    t = Table(title="Line Items")
    t.add_column("Name", style="bold")
    t.add_column("Qty", justify="center", width=5)
    t.add_column("Unit Price", justify="right", style="green")
    t.add_column("Discount", justify="right", style="red")
    t.add_column("Product ID", style="dim", width=14)
    for i in items:
        pid = i.get("item", {}).get("id")
        t.add_row(i.get("name", "?"), str(i.get("quantity", 1)), cents(i.get("price", 0)),
                  cents(i.get("discountAmount", 0)) if i.get("discountAmount") else "—",
                  pid or "[dim]none (ad-hoc)[/dim]")
    console.print(t)


def print_payments(payments: list) -> None:
    t = Table(title=f"{len(payments)} Payments", show_lines=True)
    t.add_column("ID", style="dim", width=14)
    t.add_column("Date", width=22)
    t.add_column("Amount", justify="right", style="green")
    t.add_column("Tip", justify="right")
    t.add_column("Method", width=16)
    t.add_column("Order ID", style="dim", width=12)
    for p in payments:
        tender_key = p.get("tender", {}).get("labelKey", "")
        t.add_row(p.get("id", "?"), ms_to_str(p.get("createdTime")), cents(p.get("amount", 0)),
                  cents(p.get("tipAmount", 0)) if p.get("tipAmount") else "—",
                  tender_label(tender_key), p.get("order", {}).get("id", "?"))
    console.print(t)


def print_items(items: list) -> None:
    t = Table(title=f"{len(items)} Items", show_lines=True)
    for col, kw in (("ID", {"style": "dim", "width": 12}), ("Name", {"style": "bold"}),
                    ("SKU", {"style": "dim", "width": 14}), ("Price", {"justify": "right", "style": "green"}),
                    ("Stock", {"justify": "center", "width": 7}), ("Category", {"width": 16}),
                    ("Group?", {"justify": "center", "width": 7}), ("Track?", {"justify": "center", "width": 7})):
        t.add_column(col, **kw)
    for i in items:
        catlist = i.get("categories", {}).get("elements", [])
        cat = catlist[0].get("name", "—") if catlist else "—"
        group = "✅" if i.get("itemGroup") else "—"
        track = "✅" if i.get("trackStock") else "❌"
        stock = str(i.get("stockCount", "—")) if i.get("trackStock") else "—"
        t.add_row(i.get("id", "?"), i.get("name", "?"), i.get("sku") or "—",
                  cents(i.get("price", 0)), stock, cat, group, track)
    console.print(t)


MENU = """
[bold cyan]Clover API Explorer (dev tool)[/bold cyan]

  [bold]1[/bold]  Merchant info
  [bold]2[/bold]  Orders — last 7 days
  [bold]3[/bold]  Orders — last 30 days
  [bold]4[/bold]  Single order drill-down (enter ID)
  [bold]5[/bold]  Payments — last 7 days
  [bold]6[/bold]  Refunds — last 30 days
  [bold]7[/bold]  Inventory items (first 50)
  [bold]8[/bold]  Categories
  [bold]9[/bold]  Item groups / variant sets
  [bold]10[/bold] Customers (first 20)
  [bold]r[/bold]  Raw JSON for last result
  [bold]q[/bold]  Quit
"""


async def run() -> None:
    validate(require_clover=True)
    last_result = None
    # dev tool: always explores the first configured location (Location A). Swap LOCATIONS[0]
    # for LOCATIONS[1] here to point it at Location B instead.
    loc = LOCATIONS[0]
    console.print(Panel(f"[bold]Connecting to Clover ({loc['name']})...[/bold]"))

    async with CloverClient(merchant_id=loc["merchant_id"], api_token=loc["api_token"]) as client:
        merchant = await get_merchant(client)
        console.print(Panel(
            f"[green]✅ Connected[/green]\n[bold]{merchant.get('name')}[/bold]\n"
            f"ID: {merchant.get('id')} · {merchant.get('currency', '?')} · {merchant.get('timezone', '?')}",
            title="Merchant",
        ))

        while True:
            console.print(MENU)
            choice = console.input("[bold]>[/bold] ").strip().lower()

            if choice == "q":
                console.print("bye")
                break
            elif choice == "r":
                print_json(last_result) if last_result is not None else console.print("[yellow]No result yet.[/yellow]")
            elif choice == "1":
                last_result = merchant
                print_json(merchant, "Merchant Info")
            elif choice == "2":
                last_result = await get_orders_sample(client, days=7, limit=20)
                print_orders(last_result)
            elif choice == "3":
                last_result = await get_orders_sample(client, days=30, limit=50)
                print_orders(last_result)
            elif choice == "4":
                oid = console.input("Order ID: ").strip()
                if not oid:
                    continue
                order = await get_order(client, oid)
                last_result = order
                console.print(f"\n[bold]Order {order.get('id')}[/bold] · {ms_to_str(order.get('createdTime'))}")
                console.print(f"State: [cyan]{order.get('state')}[/cyan] · Total: [green]{cents(order.get('total', 0))}[/green]")
                print_line_items(order)
            elif choice == "5":
                payments = await get_payments_sample(client, days=7, limit=30)
                last_result = payments
                print_payments(payments)
                by_tender: dict = {}
                for p in payments:
                    key = tender_label(p.get("tender", {}).get("labelKey", ""))
                    by_tender[key] = by_tender.get(key, 0) + p.get("amount", 0)
                console.print("\n[bold]Tender breakdown:[/bold]")
                for k, v in sorted(by_tender.items(), key=lambda x: -x[1]):
                    console.print(f"  {k}: [green]{cents(v)}[/green]")
            elif choice == "6":
                refunds = await get_refunds(client, days=30)
                last_result = refunds
                if not refunds:
                    console.print("[green]No refunds in the last 30 days.[/green]")
                else:
                    t = Table(title=f"{len(refunds)} Refunds")
                    t.add_column("ID", style="dim", width=14)
                    t.add_column("Date", width=22)
                    t.add_column("Amount", justify="right", style="red")
                    for r in refunds:
                        t.add_row(r.get("id", "?"), ms_to_str(r.get("createdTime")), cents(r.get("amount", 0)))
                    console.print(t)
                    console.print(f"Total refunded: [red]{cents(sum(r.get('amount', 0) for r in refunds))}[/red]")
            elif choice == "7":
                items = await get_items(client, limit=50)
                last_result = items
                print_items(items)
                with_sku = sum(1 for i in items if i.get("sku"))
                with_cat = sum(1 for i in items if i.get("categories", {}).get("elements"))
                with_group = sum(1 for i in items if i.get("itemGroup"))
                tracked = sum(1 for i in items if i.get("trackStock"))
                console.print(f"\n[bold]Data quality:[/bold] SKU {with_sku}/{len(items)} · "
                              f"Category {with_cat}/{len(items)} · In group {with_group}/{len(items)} · "
                              f"Stock tracked {tracked}/{len(items)}")
            elif choice == "8":
                cats = await get_categories(client)
                last_result = cats
                t = Table(title=f"{len(cats)} Categories")
                t.add_column("ID", style="dim", width=14)
                t.add_column("Name", style="bold")
                t.add_column("Sort", justify="center", width=6)
                for c in cats:
                    t.add_row(c.get("id"), c.get("name", "?"), str(c.get("sortOrder", "—")))
                console.print(t)
            elif choice == "9":
                import re
                groups = await get_item_groups(client)
                last_result = groups
                if not groups:
                    console.print("[yellow]No item groups found. Items may not be grouped by size/colour.[/yellow]")
                else:
                    t = Table(title=f"{len(groups)} Item Groups (Variant Sets)", show_lines=True)
                    t.add_column("Group ID", style="dim", width=14)
                    t.add_column("Name", style="bold")
                    t.add_column("Variants", justify="center", width=9)
                    t.add_column("Sizes found")
                    size_re = re.compile(r"\b(XS|S|M|L|XL|XXL|2XL|3XL|\d{2})\b", re.IGNORECASE)
                    for g in groups:
                        variants = g.get("items", {}).get("elements", [])
                        sizes = sorted({m.group(0).upper() for v in variants for m in size_re.finditer(v.get("name", ""))})
                        t.add_row(g.get("id", "?"), g.get("name", "?"), str(len(variants)), ", ".join(sizes) or "—")
                    console.print(t)
            elif choice == "10":
                customers = await get_customers(client, limit=20)
                last_result = customers
                if not customers:
                    console.print("[yellow]No customers found. Staff may not be collecting customer info at POS.[/yellow]")
                else:
                    t = Table(title=f"{len(customers)} Customers")
                    t.add_column("ID", style="dim", width=14)
                    t.add_column("Name", style="bold")
                    t.add_column("Email", width=28)
                    t.add_column("Phone", width=16)
                    for c in customers:
                        name = (c.get("firstName", "") + " " + c.get("lastName", "")).strip()
                        t.add_row(c.get("id", "?"), name or "—", c.get("emailAddress", "—"), c.get("phoneNumber", "—"))
                    console.print(t)
            else:
                console.print("[yellow]Unknown option.[/yellow]")


if __name__ == "__main__":
    asyncio.run(run())
