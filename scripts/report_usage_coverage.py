#!/usr/bin/env python3
"""Read-only aggregate cost-record coverage; never reprices historical usage.

Usage: python scripts/report_usage_coverage.py --db /path/to/profile/state.db
Repeat --db for independent profiles. Does not read message bodies or credentials.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path


def report_database(path: Path, *, requests: bool = False) -> dict:
    # mode=ro must also fail for absent databases; no SessionDB migrations or writers.
    conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA query_only = ON")
        conn.execute("BEGIN")  # one consistent read snapshot for both tables
        tables = {}
        for table in ("sessions", "session_model_usage"):
            row = conn.execute(f"""
                SELECT COUNT(*) AS rows,
                    COUNT(actual_cost_usd) AS stored_actual_nonnull_rows,
                    COUNT(estimated_cost_usd) AS stored_estimate_nonnull_rows,
                    COUNT(CASE WHEN actual_cost_usd = 0 THEN 1 END) AS stored_actual_zero_rows,
                    COUNT(CASE WHEN estimated_cost_usd = 0 THEN 1 END) AS stored_estimate_zero_rows,
                    COUNT(CASE WHEN estimated_cost_usd > 0 THEN 1 END) AS stored_estimate_positive_rows,
                    COUNT(CASE WHEN cost_status = 'actual' THEN 1 END) AS actual_status_rows,
                    COUNT(CASE WHEN cost_status = 'estimated' THEN 1 END) AS estimated_status_rows,
                    COUNT(CASE WHEN cost_status = 'included' THEN 1 END) AS included_status_rows,
                    COUNT(CASE WHEN cost_status IS NULL OR cost_status NOT IN
                        ('actual', 'estimated', 'included') THEN 1 END) AS unknown_or_unclassified_rows
                FROM {table}
            """).fetchone()
            tables[table] = dict(row)
        period = conn.execute("SELECT MIN(started_at), MAX(started_at) FROM sessions").fetchone()
        report = {
            "database": str(path), "coverage_unit": "stored_rows_not_requests",
            "session_started_at_unix_range": list(period), "tables": tables,
            "limitations": [
                "Zero amounts can be defaults/coercions; non-NULL is not proof of known billing.",
                "Status describes the stored row, not completeness of all calls aggregated into it.",
                "Legacy status overwrites may hide unknown calls; no invoice or spend total is inferred.",
                "Session start range is not a billing period; profiles and tables are not summed.",
            ],
        }
        if requests:
            # Pure SQL reader only: importing SessionDB would invite schema-healing side effects.
            sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
            from hermes_state_costs import cost_coverage

            class Reader:
                def _read_all(self, sql, params=()):
                    return conn.execute(sql, params).fetchall()

            report["request_cost_coverage"] = cost_coverage(Reader())
        return report
    finally:
        conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, action="append", required=True)
    parser.add_argument("--requests", action="store_true", help="Include separate request provenance and nullable reference/reported amounts")
    args = parser.parse_args()
    try:
        reports = [report_database(path, requests=args.requests) for path in args.db]
    except (sqlite3.Error, OSError) as exc:
        parser.exit(1, f"Cannot read usage coverage: {exc}\n")
    print(json.dumps(reports, indent=2))


if __name__ == "__main__":
    main()
