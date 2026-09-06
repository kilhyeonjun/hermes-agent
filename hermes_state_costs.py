"""Additive request-cost provenance. Legacy totals are never retroactively priced.

The nullable amounts intentionally separate provider-reported amounts from reference
estimates; neither is an independently verified invoice. Coverage counts only recorded
API responses, not speculative SDK retries or requests without identifiable usage.
"""
from __future__ import annotations

import math

COST_KINDS = ('actual', 'estimated', 'included', 'unknown')
COST_COLUMNS = tuple(f'cost_{kind}_requests' for kind in COST_KINDS)
AMOUNT_COLUMNS = ('actual_reported_usd', 'estimated_reference_usd')


def _amount(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) and value >= 0 else None


def cost_provenance(*, api_call_count, cost_status, billing_mode, actual_cost_usd, estimated_cost_usd):
    """Classify only explicitly counted responses; a zero amount alone proves nothing."""
    count = max(0, int(api_call_count or 0))
    actual, estimated = _amount(actual_cost_usd), _amount(estimated_cost_usd)
    kind = 'unknown'
    if cost_status == 'actual' and actual is not None:
        kind = 'actual'
    elif cost_status == 'estimated' and estimated is not None:
        kind = 'estimated'
    elif cost_status == 'included' and billing_mode == 'subscription_included':
        kind = 'included'
    return (
        *(count if kind == candidate else 0 for candidate in COST_KINDS),
        actual if kind == 'actual' and count else None,
        estimated if kind == 'estimated' and count else None,
    )


def cost_coverage(db, *, cutoff=0):
    """Read aggregate provenance, tolerating read-only pre-provenance databases.

    The period is session-start based, matching analytics. Historical route requests
    without counters remain unknown. Main summary versus route discrepancies are
    returned separately rather than silently guessing which count is authoritative.
    """
    columns = {row['name'] for row in db._read_all('PRAGMA table_info(session_model_usage)')}
    has_provenance = set(COST_COLUMNS + AMOUNT_COLUMNS) <= columns
    expressions = [
        '(SELECT COALESCE(SUM(api_call_count), 0) FROM sessions WHERE started_at > ?) AS session_api_calls',
        'COALESCE(SUM(u.api_call_count), 0) AS route_api_calls',
        "COALESCE(SUM(CASE WHEN u.task = '' THEN u.api_call_count ELSE 0 END), 0) AS main_route_api_calls",
    ]
    expressions.extend(f'COALESCE(SUM(u.{col}), 0) AS {col}' if has_provenance else f'0 AS {col}' for col in COST_COLUMNS)
    expressions.extend(f'SUM(u.{col}) AS {col}' if has_provenance else f'NULL AS {col}' for col in AMOUNT_COLUMNS)
    # Per-row remainder prevents unrelated rows' counter discrepancies cancelling out.
    classified = ' + '.join(f'COALESCE(u.{col}, 0)' for col in COST_COLUMNS) if has_provenance else '0'
    expressions.extend([
        f'COALESCE(SUM(MAX(0, u.api_call_count - ({classified}))), 0) AS legacy_unknown_requests',
        f'COALESCE(SUM(MAX(0, ({classified}) - u.api_call_count)), 0) AS provenance_count_discrepancy',
    ])
    row = dict(db._read_all('SELECT ' + ', '.join(expressions) +
        ' FROM session_model_usage u JOIN sessions s ON s.id=u.session_id WHERE s.started_at > ?', (cutoff, cutoff))[0])
    main = row['session_api_calls']
    counts = {kind: row.pop(f'cost_{kind}_requests') for kind in COST_KINDS}
    counts['unknown'] += row['legacy_unknown_requests']
    return {**row, 'requests': counts, 'session_api_calls': main,
            'main_api_call_discrepancy': main - row['main_route_api_calls'],
            'provenance_available': has_provenance, 'period_basis': 'session_started_at'}
