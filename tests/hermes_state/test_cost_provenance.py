"""Cost coverage must distinguish a priced zero from lost historical provenance."""
import sqlite3

import pytest

from hermes_state import SessionDB


def test_mixed_calls_preserve_nullable_costs_and_absolute_writes(tmp_path):
    from hermes_state_costs import cost_coverage

    db = SessionDB(db_path=tmp_path / 'state.db')
    try:
        db.create_session('s', 'cli', model='fixture')
        for kwargs in (
            {},
            {'estimated_cost_usd': 0.25, 'cost_status': 'estimated'},
            {'actual_cost_usd': 0.12, 'cost_status': 'actual'},
            {'estimated_cost_usd': 0, 'cost_status': 'estimated'},
            {'estimated_cost_usd': 0, 'cost_status': 'included', 'billing_mode': 'subscription_included'},
        ):
            db.update_token_counts('s', input_tokens=1, api_call_count=1, **kwargs)
        db.record_auxiliary_usage('s', 'compression', model='unknown', input_tokens=2)
        before = cost_coverage(db)
        assert before['actual_reported_usd'] == pytest.approx(0.12)
        assert before['estimated_reference_usd'] == pytest.approx(0.25)
        assert before['requests'] == {'actual': 1, 'estimated': 2, 'included': 1, 'unknown': 2}
        assert before['route_api_calls'] == 6
        for _ in range(2):
            db.update_token_counts('s', input_tokens=5, api_call_count=5, absolute=True)
        assert cost_coverage(db) == before
        row = db._conn.execute("SELECT actual_reported_usd, estimated_reference_usd FROM session_model_usage WHERE task='compression'").fetchone()
        assert tuple(row) == (None, None)
    finally:
        db.close()


def test_legacy_unknown_reconciliation_and_discrepancy(tmp_path):
    from hermes_state_costs import cost_coverage

    path = tmp_path / 'state.db'
    db = SessionDB(db_path=path)
    db.create_session('old', 'cli', model='fixture')
    db.update_token_counts('old', api_call_count=3, input_tokens=3)
    db.close()
    with sqlite3.connect(path) as conn:
        for column in ('cost_actual_requests', 'cost_estimated_requests', 'cost_included_requests', 'cost_unknown_requests', 'actual_reported_usd', 'estimated_reference_usd'):
            conn.execute(f'ALTER TABLE session_model_usage DROP COLUMN {column}')
    # Read-only legacy connection is intentionally not opened via schema-healing SessionDB.
    class Reader:
        def __init__(self, conn):
            self._conn = conn
        def _read_all(self, sql, params=()):
            return self._conn.execute(sql, params).fetchall()
    with sqlite3.connect(f'file:{path}?mode=ro', uri=True) as conn:
        conn.row_factory = sqlite3.Row
        old = cost_coverage(Reader(conn))
        assert old['requests']['unknown'] == 3
        assert old['actual_reported_usd'] is None
        assert old['estimated_reference_usd'] is None
    db = SessionDB(db_path=path)
    try:
        db.update_token_counts('old', api_call_count=1, estimated_cost_usd=0.5, cost_status='estimated')
        coverage = cost_coverage(db)
        assert coverage['requests'] == {'actual': 0, 'estimated': 1, 'included': 0, 'unknown': 3}
        assert coverage['estimated_reference_usd'] == 0.5
        db.update_token_counts('old', api_call_count=9, absolute=True)
        assert cost_coverage(db)['main_api_call_discrepancy'] == 5
    finally:
        db.close()


def test_aux_pricing_provenance_and_models_api_share_real_db(tmp_path, monkeypatch):
    from decimal import Decimal
    from types import SimpleNamespace
    from agent.aux_accounting import record_aux_usage, reset_accounting_context, set_accounting_context
    from agent.usage_pricing import CostResult
    from hermes_cli.web_routers import analytics

    path = tmp_path / 'state.db'
    db = SessionDB(db_path=path)
    db.create_session('s', 'cli', model='fixture')
    monkeypatch.setattr('agent.usage_pricing.estimate_usage_cost', lambda *a, **kw:
        CostResult(Decimal('0'), 'estimated', 'none', '$0'))
    token = set_accounting_context(db, 's')
    try:
        record_aux_usage(SimpleNamespace(model='fixture', usage=SimpleNamespace(prompt_tokens=3, completion_tokens=2)), 'compression')
    finally:
        reset_accounting_context(token)
        db.close()
    monkeypatch.setattr(analytics, '_open_session_db_for_profile', lambda *a, **kw: SessionDB(db_path=path, read_only=True))
    monkeypatch.setattr(analytics, '_model_capabilities', lambda *a: {})
    payload = analytics._get_models_analytics()
    assert payload['cost_coverage']['requests']['estimated'] == 1
    assert payload['cost_coverage']['estimated_reference_usd'] == 0
    assert payload['cost_coverage']['actual_reported_usd'] is None


@pytest.mark.parametrize("kind,field,amount", [
    ("estimated", "estimated_cost_usd", 0.5), ("estimated", "estimated_cost_usd", 0),
    ("actual", "actual_cost_usd", 0.5), ("actual", "actual_cost_usd", 0),
])
def test_async_coalescing_does_not_price_a_missing_amount(tmp_path, kind, field, amount):
    from hermes_state_costs import cost_coverage
    db = SessionDB(db_path=tmp_path / 'state.db')
    try:
        db.create_session('s', 'cli', model='fixture')
        db._apply_token_batch([
            ('s', {'api_call_count': 1, 'cost_status': kind, field: None}),
            ('s', {'api_call_count': 1, 'cost_status': kind, field: amount}),
        ])
        coverage = cost_coverage(db)
        assert coverage['requests']['unknown'] == 1
        assert coverage['requests'][kind] == 1
        amount_column = 'actual_reported_usd' if kind == 'actual' else 'estimated_reference_usd'
        assert coverage[amount_column] == amount
    finally:
        db.close()
