<!-- schema_version: 1.0 -->
# Daily store summary

Summarise a single trading day for the owner. Using only values present in the JSON, cover in this
order:

- **Revenue** — net and gross, order count, AOV, and the payment-method split.
- **Refunds** — refund total and refund rate; if there were none, say so.
- **Inventory flags** — any low-stock or stock-out risks (`stock_risks`, `inventory.low_stock`)
  worth acting on today.

Keep it to 2–3 short sections plus at most two recommendations, each tied to a specific number or
SKU from the data.
