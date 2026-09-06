# Local cost coverage

`session_model_usage` records incremental main and auxiliary usage. Six additive
columns preserve cost provenance for new writes without repricing old rows:

- `cost_actual_requests`, `cost_estimated_requests`, `cost_included_requests`,
  `cost_unknown_requests`: explicitly supplied API-call counts, classified by the
  supplied status and amount. Included requires `subscription_included` billing mode.
- `actual_reported_usd`: nullable subtotal of explicit actual-status amounts.
- `estimated_reference_usd`: nullable subtotal of explicit estimated-status amounts.

An unknown amount stays NULL in these subtotals. A verified numerical zero stays
zero. The two subtotals are never added together; reported does not mean invoice
verified. Legacy `estimated_cost_usd`/`actual_cost_usd` columns retain their old
compatibility behavior, including coerced zeros, and are not cost-coverage evidence.

The existing declarative column reconciler adds these columns on the next writable
open. No table rebuild or historical backfill is introduced. The existing legacy
primary-key repair preserves the added values when it must repair an old table.
Reverting code can leave additive columns in place; do not drop or rewrite live data.

## Read and display

```sh
python scripts/report_usage_coverage.py --db /path/to/profile/state.db --requests
```

This command opens SQLite read-only, takes a consistent read snapshot, reads no
message bodies, and never initializes SessionDB. Without `--requests`, the original
stored-row coverage report remains unchanged. Repeat `--db` for separate profiles;
profiles and tables are not summed together.

The Models and Analytics APIs expose `cost_coverage`; both web pages show separate
reported and reference amounts when token analytics are already enabled. Missing
coverage renders as unavailable, not $0. API compatibility totals remain unchanged.
A frontend rebuild and a newly loaded backend are needed to expose this on an
already running installation; source delivery does not restart services.

Coverage uses session-start time, not invoice dates. `route_api_calls` is the recorded
main-plus-auxiliary denominator. Historical route-count remainder is unknown.
`provenance_count_discrepancy` reports excess classified counts, and
`main_api_call_discrepancy` reports the net session-summary versus main-route count
difference. The latter can cancel across sessions; it is not a per-session audit.
Absolute gateway total updates do not append provenance or duplicate request counts.
The async writer does not merge missing-price requests with priced neighbours.
SDK retries without identifiable usage, failed requests without usage, and auxiliary
calls without session accounting context are not inferred from tokens or elapsed time.

## Compression and cron

Compression attempt telemetry adds message-only rough token estimates before/after
assembly and the difference. These exclude system/tool schemas, are not provider
billing measurements, and are useful only with `commit_status`; an aborted attempt
is not adopted savings. The current task, protected head/tail and tool-pair behavior
remain covered by the existing compressor tests. Thresholds and models are unchanged.

Cron fire audit includes the resolved result model/provider, session ID and Hermes
`api_calls` counter. `token_totals_present` means result fields were supplied,
including explicit zero; it does not establish provider usage completeness. Session
IDs link successful fire records to the usage ledger. Failures without a result retain
NULL fields. Pre-agent failures and script-only runs remain outside this LLM audit;
no fictitious calls or costs are added for them.
