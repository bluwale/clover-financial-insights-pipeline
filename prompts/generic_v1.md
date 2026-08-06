<!-- schema_version: 1.0 -->
# Generic report (schema-agnostic fallback)

Used when the snapshot's `schema_version` does not match a specific report template. Summarise
whatever fields are actually present in the JSON — revenue, inventory, size curve, sell-through,
anomalies — without assuming any particular key exists; skip anything absent. Follow all base rules:
explain, never compute, and cite only numbers that appear in the JSON.

Provide a short headline, one section per data area actually present, and any recommendations the
data plainly supports.
