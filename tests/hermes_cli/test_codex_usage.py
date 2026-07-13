import io
import json
import urllib.error

from hermes_cli.codex_usage import (
    annotate_usage_trends,
    apply_alert_policy,
    collect,
    compute_recommendation,
    load_history,
    render_alert,
    render_compact,
    render_credential_insights,
    render_text,
    save_history_snapshot,
    summarize_window,
    usage_bar,
)


def test_display_label_never_echoes_unknown_account_metadata():
    from hermes_cli.codex_usage import display_label

    secret_label = "private-seat-owner@example.invalid"

    assert display_label(secret_label) == "unknown"


def test_usage_policy_missing_is_empty_but_corrupt_or_unknown_is_invalid(
    monkeypatch, tmp_path
):
    import hermes_cli.codex_usage as codex_usage

    policy_path = tmp_path / "codex_route_policy.json"
    monkeypatch.setattr(codex_usage, "ROUTE_POLICY_PATH", policy_path)
    assert codex_usage.load_route_policy() == {}

    policy_path.write_text('{"mode":', encoding="utf-8")
    assert codex_usage.load_route_policy() == {
        "mode": "invalid",
        "error": "Codex route policy is invalid",
    }

    policy_path.write_text('{"mode":"surprise"}', encoding="utf-8")
    assert codex_usage.load_route_policy() == {
        "mode": "invalid",
        "error": "Codex route policy is invalid",
    }


def test_codex_route_command_registered_with_telegram_alias():
    from hermes_cli.commands import resolve_command

    command = resolve_command("codex_route")

    assert command is not None
    assert command.name == "codex-route"
    assert command.args_hint == "[status|auto|personal|company]"
    assert command.gateway_only is True


def test_risk_policy_supports_per_window_thresholds():
    payload = {
        "checked_at": "2026-07-06T15:00:00+09:00",
        "accounts": [
            {
                "label": "personal-backup",
                "ok": True,
                "primary_window": {"used_percent": 94, "reset_at": "2026-07-06T16:00:00+09:00"},
                "secondary_window": {"used_percent": 86, "reset_at": "2026-07-07T10:00:00+09:00"},
            },
            {
                "label": "company-plus-100",
                "ok": True,
                "primary_window": {"used_percent": 10, "reset_at": "2026-07-06T19:00:00+09:00"},
                "secondary_window": {"used_percent": 54, "reset_at": "2026-07-09T18:00:00+09:00"},
            },
        ],
    }

    events = apply_alert_policy(payload, primary_threshold=95, secondary_threshold=85)

    assert [event["label"] for event in events] == ["personal-backup"]
    assert events[0]["window"] == "7d"
    assert events[0]["used"] == 86


def test_collect_reports_fill_first_account_as_current_routing(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "hermes_cli.codex_usage.ROUTE_POLICY_PATH",
        tmp_path / "missing-route-policy.json",
    )

    class Entry:
        def __init__(self, label, priority):
            self.id = f"id-{label}"
            self.label = label
            self.priority = priority
            self.source = "manual"
            self.last_status = None
            self.last_error_reset_at = None
            self.extra = {}
            self.runtime_api_key = f"token-{label}"

    entries = [Entry("personal-backup", 0), Entry("company-plus-100", 10)]

    class Pool:
        _strategy = "fill_first"

        def _available_entries(self, *, clear_expired=False, refresh=False):
            return entries

        def _routable_entries(self, values):
            return values

        def entries(self):
            return entries

    monkeypatch.setattr("agent.credential_pool.load_pool", lambda _provider: Pool())
    monkeypatch.setattr(
        "hermes_cli.codex_usage.fetch_usage",
        lambda _token, account_id=None: {
            "plan_type": "pro",
            "rate_limit": {
                "primary_window": {"used_percent": 1},
                "secondary_window": {"used_percent": 1, "reset_at": "2099-07-17T06:40:00+09:00"},
            },
        },
    )

    payload = collect()

    assert payload["routing"] == {"strategy": "fill_first", "current_label": "personal-backup"}
    assert [row["credential_id"] for row in payload["accounts"]] == [
        "id-personal-backup",
        "id-company-plus-100",
    ]


def test_collect_merges_fixed_route_policy_into_live_routing(monkeypatch, tmp_path):
    import json

    import hermes_cli.codex_usage as codex_usage

    class Entry:
        id = "company-id"
        label = "company-plus-100"
        priority = 0
        source = "manual"
        last_status = None
        last_error_reset_at = None
        extra = {}
        runtime_api_key = "test-token"

    class Pool:
        _strategy = "fill_first"

        def _available_entries(self, **_kwargs):
            return [personal_entry, Entry()]

        def _routable_entries(self, entries):
            return [entry for entry in entries if entry.id == "company-id"]

        def entries(self):
            return [personal_entry, Entry()]

    personal_entry = Entry()
    personal_entry.id = "personal-id"
    personal_entry.label = "personal-backup"
    personal_entry.priority = -10

    policy_path = tmp_path / "codex_route_policy.json"
    policy_path.write_text(
        json.dumps({
            "mode": "fixed",
            "credential_id": "company-id",
            "label": "company-plus-100",
        }),
        encoding="utf-8",
    )
    monkeypatch.setattr(codex_usage, "ROUTE_POLICY_PATH", policy_path, raising=False)
    monkeypatch.setattr("agent.credential_pool.load_pool", lambda _provider: Pool())
    monkeypatch.setattr(
        codex_usage,
        "fetch_usage",
        lambda *_args, **_kwargs: {
            "rate_limit": {
                "primary_window": {"used_percent": 1},
                "secondary_window": {"used_percent": 1},
            },
        },
    )

    payload = collect()

    assert payload["routing"] == {
        "strategy": "fill_first",
        "current_label": "company-plus-100",
        "mode": "fixed",
        "fixed_credential_id": "company-id",
        "fixed_label": "company",
    }


def test_collect_and_compact_redact_raw_label_and_http_error_body(
    monkeypatch, tmp_path
):
    import hermes_cli.codex_usage as codex_usage

    private_label = "private-seat-owner@example.invalid"
    private_body = '{"error":"private-owner@example.invalid token=secret"}'

    class Entry:
        id = "opaque-credential-id"
        label = private_label
        priority = 0
        source = "manual"
        last_status = None
        last_error_reset_at = None
        extra = {}
        runtime_api_key = "opaque-runtime-token"

    class Pool:
        _strategy = "fill_first"

        def _available_entries(self, **_kwargs):
            return [Entry()]

        def _routable_entries(self, entries):
            return entries

        def entries(self):
            return [Entry()]

    error = urllib.error.HTTPError(
        codex_usage.USAGE_URL,
        403,
        "Forbidden",
        {},
        io.BytesIO(private_body.encode("utf-8")),
    )
    monkeypatch.setattr(
        codex_usage,
        "ROUTE_POLICY_PATH",
        tmp_path / "missing-route-policy.json",
    )
    monkeypatch.setattr("agent.credential_pool.load_pool", lambda _provider: Pool())
    monkeypatch.setattr(
        codex_usage,
        "fetch_usage",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(error),
    )

    payload = codex_usage.collect()
    account = payload["accounts"][0]
    compact = codex_usage.render_compact(payload)
    serialized = json.dumps(payload, ensure_ascii=False)

    assert account["label"] == private_label
    assert account["error"] == {"kind": "http_error", "status": 403}
    assert private_label not in compact
    assert private_body not in serialized + compact
    assert "private-owner@example.invalid" not in serialized + compact


def test_json_and_compact_output_boundaries_redact_legacy_raw_error_payload(
    monkeypatch, capsys
):
    import hermes_cli.codex_usage as codex_usage

    private_label = "private-seat-owner@example.invalid"
    private_body = '{"error":"private-owner@example.invalid token=secret"}'
    payload = {
        "checked_at": "2026-07-13T09:00:00+09:00",
        "provider": "openai-codex",
        "routing": {"current_label": private_label},
        "accounts": [
            {
                "credential_id": "opaque-credential-id",
                "label": private_label,
                "ok": False,
                "http": 403,
                "error": private_body,
            }
        ],
        "recommendation": {"label": private_label, "reason": "safe reason"},
    }

    compact = codex_usage.render_compact(payload)
    monkeypatch.setattr(codex_usage, "collect", lambda: payload)
    monkeypatch.setattr(codex_usage, "load_history", lambda _path: [])
    monkeypatch.setattr(codex_usage, "annotate_usage_trends", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(codex_usage, "save_history_snapshot", lambda *_args, **_kwargs: None)

    assert codex_usage.main(["--json"]) == 0
    output = capsys.readouterr().out
    public_payload = json.loads(output)

    assert public_payload["accounts"][0]["label"] == "unknown"
    assert public_payload["accounts"][0]["error"] == {
        "kind": "http_error",
        "status": 403,
    }
    assert private_label not in output + compact
    assert private_body not in output + compact
    assert "private-owner@example.invalid" not in output + compact


def test_invalid_route_is_visible_and_cannot_show_an_opposite_current_account():
    payload = {
        "checked_at": "2026-07-10T08:30:00+09:00",
        "routing": {
            "strategy": "fill_first",
            "current_label": "personal-backup",
            "mode": "invalid",
            "error": "Codex route policy is invalid",
        },
        "accounts": [],
        "recommendation": {},
    }

    full = render_text(payload)
    compact = render_compact(payload)

    assert "Routing blocked: Codex route policy is invalid" in full
    assert "Codex routing blocked" in compact
    assert "personal" not in full + compact


def test_recommendation_prefers_soonest_weekly_reset_then_usage_fallback():
    accounts = [
        {
            "label": "a",
            "ok": True,
            "primary_window": {"used_percent": 1},
            "secondary_window": {"used_percent": 70, "reset_at": "2099-07-09T18:00:00+09:00", "remaining": "2d"},
        },
        {
            "label": "b",
            "ok": True,
            "primary_window": {"used_percent": 60},
            "secondary_window": {"used_percent": 40, "reset_at": "2099-07-07T10:00:00+09:00", "remaining": "1h"},
        },
    ]

    rec = compute_recommendation(accounts)

    assert rec is not None
    assert rec["label"] == "b"
    assert rec["policy"] == "7d-reset-aware"
    assert "7d reset" in rec["reason"]


def test_recommendation_prefers_unstarted_weekly_window_before_known_resets():
    accounts = [
        {
            "label": "started",
            "ok": True,
            "available": True,
            "primary_window": {"used_percent": 1},
            "secondary_window": {"used_percent": 10, "reset_at": "2099-07-07T10:00:00+09:00", "remaining": "1h"},
        },
        {
            "label": "unstarted",
            "ok": True,
            "available": True,
            "primary_window": {"used_percent": 1},
            "secondary_window": {"used_percent": 0},
        },
    ]

    rec = compute_recommendation(accounts)

    assert rec is not None
    assert rec["label"] == "unstarted"


def test_annotate_usage_trends_uses_recent_history_for_burn_and_eta():
    payload = {
        "checked_at": "2026-07-06T20:00:00+09:00",
        "accounts": [
            {
                "label": "company-plus-100",
                "ok": True,
                "plan_type": "prolite",
                "primary_window": summarize_window(
                    {
                        "used_percent": 22,
                        "limit_window_seconds": 18000,
                        "reset_at": "2026-07-07T00:00:00+09:00",
                    }
                ),
                "secondary_window": summarize_window(
                    {
                        "used_percent": 57,
                        "limit_window_seconds": 604800,
                        "reset_at": "2026-07-09T18:00:00+09:00",
                    }
                ),
            }
        ],
    }
    history = [
        {
            "checked_at": "2026-07-06T19:30:00+09:00",
            "accounts": [
                {
                    "label": "company-plus-100",
                    "ok": True,
                    "primary_window": {
                        "used_percent": 20,
                        "reset_at": "2026-07-07T00:00:00+09:00",
                    },
                    "secondary_window": {
                        "used_percent": 56,
                        "reset_at": "2026-07-09T18:00:00+09:00",
                    },
                }
            ],
        }
    ]

    annotate_usage_trends(payload, history=history)

    primary_trend = payload["accounts"][0]["primary_window"]["trend"]
    assert primary_trend["source"] == "recent"
    assert primary_trend["burn_percent_per_hour"] == 4.0
    assert primary_trend["eta"]["95"]["at"] == "2026-07-07T14:15:00+09:00"


def test_render_compact_includes_burn_and_eta_when_trend_is_available():
    payload = {
        "checked_at": "2026-07-06T20:00:00+09:00",
        "accounts": [
            {
                "label": "company-plus-100",
                "ok": True,
                "plan_type": "prolite",
                "primary_window": summarize_window(
                    {
                        "used_percent": 22,
                        "limit_window_seconds": 18000,
                        "reset_at": "2026-07-07T00:00:00+09:00",
                    }
                ),
                "secondary_window": summarize_window(
                    {
                        "used_percent": 57,
                        "limit_window_seconds": 604800,
                        "reset_at": "2026-07-09T18:00:00+09:00",
                    }
                ),
            }
        ],
        "recommendation": {"label": "company-plus-100", "reason": "7d 57%, 5h 22%"},
    }
    annotate_usage_trends(
        payload,
        history=[
            {
                "checked_at": "2026-07-06T19:30:00+09:00",
                "accounts": [
                    {
                        "label": "company-plus-100",
                        "ok": True,
                        "primary_window": {"used_percent": 20, "reset_at": "2026-07-07T00:00:00+09:00"},
                        "secondary_window": {"used_percent": 56, "reset_at": "2026-07-09T18:00:00+09:00"},
                    }
                ],
            }
        ],
    )

    text = render_compact(payload)

    assert "🔥 Burn:" in text
    assert "5h +4.0%/h" in text
    assert "burn +4.0%/h · ETA95 07/07 14:15" in text


def test_annotate_usage_trends_falls_back_to_window_average_on_first_run():
    payload = {
        "checked_at": "2026-07-06T20:00:00+09:00",
        "accounts": [
            {
                "label": "company-plus-100",
                "ok": True,
                "primary_window": summarize_window(
                    {
                        "used_percent": 25,
                        "limit_window_seconds": 18000,
                        "reset_at": "2026-07-06T21:00:00+09:00",
                    }
                ),
                "secondary_window": {},
            }
        ],
    }

    annotate_usage_trends(payload, history=[])

    trend = payload["accounts"][0]["primary_window"]["trend"]
    assert trend["source"] == "window_avg"
    assert trend["burn_percent_per_hour"] == 6.25


def test_annotate_usage_trends_ignores_usage_rollbacks_and_uses_average():
    payload = {
        "checked_at": "2026-07-06T20:00:00+09:00",
        "accounts": [
            {
                "label": "company-plus-100",
                "ok": True,
                "primary_window": summarize_window(
                    {
                        "used_percent": 20,
                        "limit_window_seconds": 18000,
                        "reset_at": "2026-07-06T21:00:00+09:00",
                    }
                ),
                "secondary_window": {},
            }
        ],
    }

    annotate_usage_trends(
        payload,
        history=[
            {
                "checked_at": "2026-07-06T19:30:00+09:00",
                "accounts": [
                    {
                        "label": "company-plus-100",
                        "ok": True,
                        "primary_window": {"used_percent": 25, "reset_at": "2026-07-06T21:00:00+09:00"},
                    }
                ],
            }
        ],
    )

    trend = payload["accounts"][0]["primary_window"]["trend"]
    assert trend["source"] == "window_avg"
    assert trend["burn_percent_per_hour"] == 5.0


def test_history_load_skips_corrupt_lines_and_save_is_nonfatal(tmp_path):
    history_path = tmp_path / "history.jsonl"
    history_path.write_text('{"checked_at":"old"}\nnot-json\n{"checked_at":"new"}\n', encoding="utf-8")

    rows = load_history(history_path)

    assert [row["checked_at"] for row in rows] == ["old", "new"]
    save_history_snapshot(tmp_path / "missing" / "nested" / "history.jsonl", {"checked_at": "now", "accounts": []})
    save_history_snapshot(tmp_path, {"checked_at": "now", "accounts": []})


def test_render_compact_marks_eta_after_reset():
    payload = {
        "checked_at": "2026-07-06T20:00:00+09:00",
        "accounts": [
            {
                "label": "company-plus-100",
                "ok": True,
                "plan_type": "prolite",
                "primary_window": summarize_window(
                    {
                        "used_percent": 22,
                        "limit_window_seconds": 18000,
                        "reset_at": "2026-07-06T21:00:00+09:00",
                    }
                ),
                "secondary_window": {},
            }
        ],
        "recommendation": {"label": "company-plus-100", "reason": "7d ?, 5h 22%"},
    }
    annotate_usage_trends(
        payload,
        history=[
            {
                "checked_at": "2026-07-06T19:30:00+09:00",
                "accounts": [
                    {
                        "label": "company-plus-100",
                        "ok": True,
                        "primary_window": {"used_percent": 20, "reset_at": "2026-07-06T21:00:00+09:00"},
                    }
                ],
            }
        ],
    )

    text = render_compact(payload)

    assert "ETA95 07/07 14:15*" in text


def test_render_compact_includes_risk_and_recommendation():
    payload = {
        "checked_at": "2026-07-06T15:00:00+09:00",
        "accounts": [
            {
                "label": "personal-backup",
                "ok": True,
                "plan_type": "pro",
                "primary_window": summarize_window({"used_percent": 92, "reset_at": "2099-07-06T15:20:00+09:00"}),
                "secondary_window": summarize_window({"used_percent": 98, "reset_at": "2099-07-07T10:42:00+09:00"}),
            },
            {
                "label": "company-plus-100",
                "ok": True,
                "plan_type": "prolite",
                "primary_window": summarize_window({"used_percent": 1, "reset_at": "2099-07-06T19:17:00+09:00"}),
                "secondary_window": summarize_window({"used_percent": 54, "reset_at": "2099-07-09T18:46:00+09:00"}),
            },
        ],
        "recommendation": {"label": "company-plus-100", "reason": "7d 54%, 5h 1%"},
    }

    text = render_compact(payload)

    assert "🧭 Codex 사용량" in text
    assert "✅ 추천 company" in text
    assert "personal 7d 98% · 회복 전 보류" in text
    assert "⏱ 다음 회복 · personal 5h" in text
    assert "🚦 위험 회복 · personal 7d" in text
    assert "company" in text
    assert "5h  1% 🟢" in text
    assert "7d 54% 🟢" in text
    assert "[" in text and "]" in text


def test_render_compact_highlights_current_account_and_reduces_duplicate_detail():
    payload = {
        "checked_at": "2026-07-10T07:53:00+09:00",
        "routing": {"strategy": "fill_first", "current_label": "personal-backup"},
        "accounts": [
            {
                "label": "personal-backup",
                "ok": True,
                "plan_type": "pro",
                "primary_window": {
                    "used_percent": 27,
                    "remaining": "3h 47m",
                    "reset_at": "2026-07-10T11:40:00+09:00",
                },
                "secondary_window": {
                    "used_percent": 4,
                    "remaining": "6d 22h 47m",
                    "reset_at": "2026-07-17T06:40:00+09:00",
                },
            },
            {
                "label": "company-plus-100",
                "ok": True,
                "plan_type": "prolite",
                "primary_window": {
                    "used_percent": 7,
                    "remaining": "3h 53m",
                    "reset_at": "2026-07-10T11:47:00+09:00",
                },
                "secondary_window": {
                    "used_percent": 1,
                    "remaining": "6d 22h 53m",
                    "reset_at": "2026-07-17T06:47:00+09:00",
                },
            },
        ],
        "recommendation": {
            "label": "personal-backup",
            "reason": "7d reset 07/17 06:40 · 6d 22h 47m, 7d 4%, 5h 27%",
        },
    }

    text = render_compact(payload)

    assert "▶ 현재 personal · ✅ 추천과 일치" in text
    assert "└ 7d 리셋이 가장 빠름 · 07/17 06:40 (6d 22h 47m)" in text
    assert "⏱ 다음 회복 · personal 5h" in text
    assert "└ 07/10 11:40 (3h 47m)" in text
    assert "▶ personal · pro · 현재·추천" in text
    assert "○ company · prolite" in text
    assert "reset " not in text
    assert text.count("7d 4%") == 0


def test_render_compact_distinguishes_fixed_route_from_automatic_recommendation():
    payload = {
        "checked_at": "2026-07-10T08:30:00+09:00",
        "routing": {
            "strategy": "fill_first",
            "current_label": "company-plus-100",
            "mode": "fixed",
            "fixed_credential_id": "company-id",
            "fixed_label": "company-plus-100",
        },
        "accounts": [
            {
                "credential_id": "company-id",
                "label": "company-plus-100",
                "ok": True,
                "plan_type": "prolite",
                "primary_window": {"used_percent": 7, "remaining": "3h", "reset_at": "2026-07-10T11:47:00+09:00"},
                "secondary_window": {"used_percent": 1, "remaining": "6d", "reset_at": "2026-07-17T06:47:00+09:00"},
            },
            {
                "credential_id": "personal-id",
                "label": "personal-backup",
                "ok": True,
                "plan_type": "pro",
                "primary_window": {"used_percent": 50, "remaining": "3h", "reset_at": "2026-07-10T11:40:00+09:00"},
                "secondary_window": {"used_percent": 8, "remaining": "6d", "reset_at": "2026-07-17T06:40:00+09:00"},
            },
        ],
        "recommendation": {"label": "personal-backup", "policy": "7d-reset-aware"},
    }

    text = render_compact(payload)

    assert "▶ 현재 company · 🔒 고정" in text
    assert "💡 자동 추천 personal · 고정 모드라 미적용" in text
    assert "▶ company · prolite · 현재·고정" in text
    assert "★ personal · pro · 자동추천" in text


def test_render_alert_uses_card_layout_with_bar_and_recommendation():
    payload = {
        "checked_at": "2026-07-06T15:00:00+09:00",
        "accounts": [
            {
                "label": "personal-backup",
                "ok": True,
                "primary_window": summarize_window({"used_percent": 17, "reset_at": "2099-07-06T16:05:00+09:00"}),
                "secondary_window": summarize_window({"used_percent": 98, "reset_at": "2099-07-07T10:42:00+09:00"}),
            },
            {
                "label": "company-plus-100",
                "ok": True,
                "primary_window": summarize_window({"used_percent": 1, "reset_at": "2099-07-06T19:17:00+09:00"}),
                "secondary_window": summarize_window({"used_percent": 54, "reset_at": "2099-07-09T18:46:00+09:00"}),
            },
        ],
        "recommendation": {"label": "company-plus-100", "reason": "7d 54%, 5h 1%"},
    }

    text = render_alert(payload, primary_threshold=95, secondary_threshold=85)

    assert "🚨 Codex 한도 주의" in text
    assert "personal · 7d 98%" in text
    assert "[██████████]" in text
    assert "✅ 추천 company" in text
    assert "personal-backup" not in text
    assert "company-plus-100" not in text


def test_render_text_uses_display_aliases_without_mutating_payload_labels():
    payload = {
        "checked_at": "2026-07-10T09:34:00+09:00",
        "accounts": [
            {
                "label": "personal-backup",
                "ok": True,
                "plan_type": "pro",
                "primary_window": {"used_percent": 100},
                "secondary_window": {"used_percent": 16},
            }
        ],
        "recommendation": {"label": "company-plus-100", "reason": "7d reset 우선"},
    }

    text = render_text(payload)

    assert "추천: company" in text
    assert "[personal]" in text
    assert "personal-backup" not in text
    assert "company-plus-100" not in text
    assert payload["accounts"][0]["label"] == "personal-backup"


def test_usage_bar_visualizes_percent_buckets():
    assert usage_bar(0, width=10) == "░░░░░░░░░░"
    assert usage_bar(54, width=10) == "█████░░░░░"
    assert usage_bar(98, width=10) == "██████████"
    assert usage_bar(None, width=10) == "??????????"


def test_render_credential_insights_groups_rows_readably():
    rows = [
        {
            "credential_label": "personal-backup",
            "model": "gpt-5.5",
            "total_tokens": 1477482,
            "input_tokens": 77847,
            "output_tokens": 2996,
            "api_call_count": 10,
        },
        {
            "credential_label": "company-plus-100",
            "model": "gpt-5.5",
            "total_tokens": 200000,
            "input_tokens": 1000,
            "output_tokens": 2000,
            "api_call_count": 2,
        },
    ]

    text = render_credential_insights(rows, provider="openai-codex", days=30)

    assert "📊 Codex credential 사용량 · 30d" in text
    assert "personal" in text
    assert "1.48M tokens · 10 calls" in text
    assert "avg 147.7k/call" in text
    assert "company" in text
    assert "200.0k tokens · 2 calls" in text
    assert "personal-backup" not in text
    assert "company-plus-100" not in text


def test_render_credential_insights_shows_share_and_cache_breakdown():
    rows = [
        {
            "credential_label": "personal-backup",
            "model": "gpt-5.5",
            "total_tokens": 900,
            "input_tokens": 100,
            "output_tokens": 20,
            "cache_read_tokens": 700,
            "reasoning_tokens": 80,
            "api_calls": 3,
        },
        {
            "credential_label": "company-plus-100",
            "model": "gpt-5.5",
            "total_tokens": 100,
            "input_tokens": 50,
            "output_tokens": 50,
            "api_calls": 1,
        },
    ]

    text = render_credential_insights(rows, provider="openai-codex", days=7)

    assert "900 tokens · 3 calls · avg 300/call · 90%" in text
    assert "cache 700" in text
    assert "reason 80" in text


def test_render_credential_insights_empty_explains_future_rows():
    text = render_credential_insights([], provider="openai-codex", days=30)

    assert "아직 credential별 사용 기록 없음" in text
    assert "새 모델 턴부터 쌓임" in text
