"""Coverage must distinguish unknown coerced zeros from recorded zero-dollar usage."""
import json
import subprocess
import sys
from pathlib import Path

from hermes_state import SessionDB


def test_coverage_reports_unknown_storage_without_repricing_or_writes(tmp_path):
    db_path = tmp_path / "state.db"
    db = SessionDB(db_path=db_path)
    for name, status, amount in [("missing", "unknown", None), ("included", "included", 0.0),
                                  ("estimated", "estimated", 0.25)]:
        db.create_session(name, source="cli", model="fixture-model")
        db.update_token_counts(name, input_tokens=100, api_call_count=1, model="fixture-model",
                               estimated_cost_usd=amount, cost_status=status, cost_source="none")
    db.close()
    before = db_path.read_bytes()
    command = [sys.executable, str(Path(__file__).resolve().parents[2] / "scripts/report_usage_coverage.py"),
               "--db", str(db_path)]
    result = subprocess.run(command, capture_output=True, text=True, check=True)
    report = json.loads(result.stdout)[0]
    for table in ("sessions", "session_model_usage"):
        counts = report["tables"][table]
        assert counts["rows"] == 3
        assert counts["unknown_or_unclassified_rows"] == 1
        assert counts["included_status_rows"] == 1
        assert counts["estimated_status_rows"] == 1
        assert counts["stored_estimate_positive_rows"] == 1
    assert report["tables"]["sessions"]["stored_actual_nonnull_rows"] == 0
    assert report["tables"]["session_model_usage"]["stored_actual_zero_rows"] == 3
    assert report["coverage_unit"] == "stored_rows_not_requests"
    assert db_path.read_bytes() == before
    assert "fixture-model" not in result.stdout
    assert "0.25" not in result.stdout  # counts, not misleading spend totals


def test_missing_database_is_not_created(tmp_path):
    path = tmp_path / "missing.db"
    result = subprocess.run([sys.executable, str(Path(__file__).resolve().parents[2] / "scripts/report_usage_coverage.py"),
                             "--db", str(path)], capture_output=True, text=True)
    assert result.returncode != 0
    assert not path.exists()
