import json
from pathlib import Path
import subprocess
import sys
import time
import types

import pytest


def test_resolve_policy_records_exact_canonical_kind(tmp_path):
    from hermes_cli import codex_route

    auth = tmp_path / "auth.json"
    auth.write_text(
        '{"credential_pool":{"openai-codex":['
        '{"id":"company-id","label":"company-plus-100"}]}}',
        encoding="utf-8",
    )

    policy = codex_route.resolve_policy("company", auth)

    assert policy == {
        "mode": "fixed",
        "credential_id": "company-id",
        "label": "company-plus-100",
        "kind": "company",
    }


@pytest.mark.parametrize(
    ("mode", "label"),
    [
        ("company", "personal-company-plus"),
        ("personal", "unclassified-account"),
    ],
)
def test_resolve_policy_rejects_ambiguous_or_unknown_labels(tmp_path, mode, label):
    from hermes_cli import codex_route

    auth = tmp_path / "auth.json"
    auth.write_text(
        json.dumps(
            {
                "credential_pool": {
                    "openai-codex": [
                        {"id": "candidate-id", "label": label},
                    ]
                }
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="계정을 찾지 못했습니다"):
        codex_route.resolve_policy(mode, auth)


def test_resolve_policy_rejects_duplicate_id_anywhere_in_pool(tmp_path):
    from hermes_cli import codex_route

    private_label = "private-seat-owner@example.invalid"
    auth = tmp_path / "auth.json"
    auth.write_text(
        json.dumps(
            {
                "credential_pool": {
                    "openai-codex": [
                        {
                            "id": "shared-id",
                            "label": "company-plus-100",
                        },
                        {
                            "id": "shared-id",
                            "label": private_label,
                        },
                    ]
                }
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError) as exc:
        codex_route.resolve_policy("company", auth)

    assert "credential ID" in str(exc.value)
    assert private_label not in str(exc.value)


def test_load_policy_missing_is_auto_but_corrupt_or_unknown_is_rejected(
    monkeypatch, tmp_path
):
    from hermes_cli import codex_route

    policy_path = tmp_path / "codex_route_policy.json"
    monkeypatch.setattr(codex_route, "POLICY_PATH", policy_path)
    assert codex_route.load_policy() == {"mode": "auto"}

    policy_path.write_text('{"mode":', encoding="utf-8")
    with pytest.raises(ValueError, match="policy is invalid"):
        codex_route.load_policy()

    policy_path.write_text('{"mode":"surprise"}', encoding="utf-8")
    with pytest.raises(ValueError, match="policy is invalid"):
        codex_route.load_policy()


def test_canonical_row_label_never_echoes_unknown_account_metadata(
    monkeypatch, tmp_path
):
    from hermes_cli import codex_route

    secret_label = "private-seat-owner@example.invalid"
    auth = tmp_path / "auth.json"
    auth.write_text(
        json.dumps(
            {
                "credential_pool": {
                    "openai-codex": [
                        {"id": "unknown-id", "label": secret_label},
                    ]
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(codex_route, "DEFAULT_AUTH", auth)

    label = codex_route.canonical_row_label(
        {"id": "unknown-id", "label": secret_label}
    )

    assert label == "unknown"
    assert secret_label not in label


def test_current_label_distinguishes_affinity_from_active_fallback(tmp_path):
    from hermes_cli import codex_route

    auth = tmp_path / "auth.json"
    auth.write_text(
        json.dumps(
            {
                "credential_pool": {
                    "openai-codex": [
                        {
                            "id": "personal-id",
                            "label": "personal-backup",
                            "priority": 0,
                            "last_status": "exhausted",
                            "last_status_at": time.time(),
                            "last_error_code": 429,
                        },
                        {
                            "id": "company-id",
                            "label": "company-plus-100",
                            "priority": 10,
                            "last_status": "ok",
                        },
                    ]
                }
            }
        ),
        encoding="utf-8",
    )

    assert codex_route.current_label(auth) == (
        "personal-first · fallback company eligible (personal exhausted)"
    )


def test_current_label_stays_compact_when_affinity_is_active(tmp_path):
    from hermes_cli import codex_route

    auth = tmp_path / "auth.json"
    auth.write_text(
        json.dumps(
            {
                "credential_pool": {
                    "openai-codex": [
                        {
                            "id": "company-id",
                            "label": "company-plus-100",
                            "priority": 0,
                            "last_status": "ok",
                        },
                        {
                            "id": "personal-id",
                            "label": "personal-backup",
                            "priority": 10,
                            "last_status": "ok",
                        },
                    ]
                }
            }
        ),
        encoding="utf-8",
    )

    assert codex_route.current_label(auth) == "company"


def test_current_label_fixed_route_never_reports_an_auto_fallback(
    monkeypatch, tmp_path
):
    from hermes_cli import codex_route

    auth = tmp_path / "auth.json"
    auth.write_text(
        json.dumps(
            {
                "credential_pool": {
                    "openai-codex": [
                        {
                            "id": "personal-id",
                            "label": "personal-backup",
                            "priority": 0,
                            "last_status": "exhausted",
                            "last_status_at": time.time(),
                            "last_error_code": 429,
                        },
                        {
                            "id": "company-id",
                            "label": "company-plus-100",
                            "priority": 10,
                            "last_status": "ok",
                        },
                    ]
                }
            }
        ),
        encoding="utf-8",
    )
    policy = {
        "mode": "fixed",
        "credential_id": "personal-id",
        "label": "personal-backup",
        "kind": "personal",
    }
    monkeypatch.setattr(codex_route, "profile_homes", lambda: [("default", tmp_path)])

    assert codex_route.current_label(auth, policy) == (
        "personal-fixed · no eligible route (personal exhausted)"
    )
    assert "fallback company" not in codex_route.render_status(policy)


def test_current_label_treats_expired_exhaustion_as_eligible(tmp_path):
    from hermes_cli import codex_route

    auth = tmp_path / "auth.json"
    auth.write_text(
        json.dumps(
            {
                "credential_pool": {
                    "openai-codex": [
                        {
                            "id": "personal-id",
                            "label": "personal-backup",
                            "priority": 0,
                            "last_status": "exhausted",
                            "last_status_at": time.time() - 7200,
                            "last_error_code": 429,
                        }
                    ]
                }
            }
        ),
        encoding="utf-8",
    )

    assert codex_route.current_label(auth) == "personal"


@pytest.mark.parametrize(
    "health_fields",
    [
        {"last_error_reset_at": "2030-01-01T00:00:00+00:00"},
        {"last_error_reset_at": 1_893_456_000_000},
        {
            "last_error_reset_at": "0",
            "last_status_at": 1_800_000_000,
            "last_error_code": 429,
        },
        {
            "last_error_reset_at": "-1",
            "last_status_at": 1_800_000_000,
            "last_error_code": 429,
        },
        {
            "last_error_reset_at": True,
            "last_status_at": 1_800_000_000,
            "last_error_code": 429,
        },
        {"last_status_at": 1_800_000_000, "last_error_code": 401},
        {"last_status_at": "2027-01-15T08:00:00Z", "last_error_code": 429},
        {"last_status_at": 1_800_000_000, "last_error_code": 402},
        {},
    ],
)
def test_route_status_exhaustion_window_matches_runtime_health_contract(
    health_fields,
):
    from agent.credential_pool import PooledCredential, _exhausted_until
    from hermes_cli import codex_route

    row = {
        "id": "personal-id",
        "label": "personal-backup",
        "priority": 0,
        "last_status": "exhausted",
        **health_fields,
    }
    runtime_entry = PooledCredential.from_dict("openai-codex", row)

    assert codex_route._credential_exhausted_until(row) == _exhausted_until(
        runtime_entry
    )


def test_current_label_fails_closed_without_leaking_malformed_state(tmp_path):
    from hermes_cli import codex_route

    secret_priority = "secret-priority-value"
    secret_status = "secret-status-value"
    auth = tmp_path / "auth.json"
    auth.write_text(
        json.dumps(
            {
                "credential_pool": {
                    "openai-codex": [
                        {
                            "id": "personal-id",
                            "label": "personal-backup",
                            "priority": secret_priority,
                            "last_status": secret_status,
                        }
                    ]
                }
            }
        ),
        encoding="utf-8",
    )

    label = codex_route.current_label(auth)

    assert label == "unknown · invalid credential state"
    assert secret_priority not in label
    assert secret_status not in label


def test_current_label_never_echoes_an_unknown_health_status(tmp_path):
    from hermes_cli import codex_route

    secret_status = "secret-status-value"
    auth = tmp_path / "auth.json"
    auth.write_text(
        json.dumps(
            {
                "credential_pool": {
                    "openai-codex": [
                        {
                            "id": "personal-id",
                            "label": "personal-backup",
                            "priority": 0,
                            "last_status": secret_status,
                        }
                    ]
                }
            }
        ),
        encoding="utf-8",
    )

    label = codex_route.current_label(auth)

    assert label == "unknown · invalid credential state"
    assert secret_status not in label


def test_current_label_reports_no_route_when_all_rows_are_disabled(tmp_path):
    from hermes_cli import codex_route

    auth = tmp_path / "auth.json"
    auth.write_text(
        json.dumps(
            {
                "credential_pool": {
                    "openai-codex": [
                        {
                            "id": "personal-id",
                            "label": "personal-backup",
                            "priority": 0,
                            "last_status": "ok",
                            "disabled": True,
                        },
                        {
                            "id": "company-id",
                            "label": "company-plus-100",
                            "priority": 10,
                            "last_status": "ok",
                            "disabled": True,
                        },
                    ]
                }
            }
        ),
        encoding="utf-8",
    )

    assert codex_route.current_label(auth) == "unknown · invalid credential state"


@pytest.mark.parametrize("fixed_match_count", [0, 2])
def test_current_label_fixed_route_fails_closed_when_target_is_missing_or_duplicate(
    tmp_path, fixed_match_count
):
    from hermes_cli import codex_route

    rows = [
        {
            "id": "personal-id",
            "label": "personal-backup",
            "priority": index,
            "last_status": "ok",
        }
        for index in range(fixed_match_count)
    ]
    rows.append(
        {
            "id": "company-id",
            "label": "company-plus-100",
            "priority": 10,
            "last_status": "ok",
        }
    )
    auth = tmp_path / "auth.json"
    auth.write_text(
        json.dumps({"credential_pool": {"openai-codex": rows}}),
        encoding="utf-8",
    )
    policy = {
        "mode": "fixed",
        "credential_id": "personal-id",
        "label": "personal-backup",
        "kind": "personal",
    }

    assert codex_route.current_label(auth, policy) == (
        "personal-fixed · no eligible route (personal unavailable)"
    )


def test_cliproxy_current_account_delegates_to_authoritative_management_helper(
    monkeypatch,
):
    from hermes_cli import codex_priority_sync, codex_route

    monkeypatch.setattr(
        codex_priority_sync,
        "cliproxy_active_kind",
        lambda: "company",
        raising=False,
    )

    assert codex_route.cliproxy_current_account() == "company"


def test_cliproxy_current_account_fails_closed_when_management_unavailable(
    monkeypatch,
):
    from hermes_cli import codex_priority_sync, codex_route

    def fail():
        raise codex_priority_sync.CLIProxyManagementError("transport unavailable")

    monkeypatch.setattr(
        codex_priority_sync,
        "cliproxy_active_kind",
        fail,
        raising=False,
    )

    assert (
        codex_route.cliproxy_current_account()
        == "unknown · management unavailable"
    )


def test_codex_route_status_works_without_external_control_script(monkeypatch, tmp_path, capsys):
    from hermes_cli import codex_route

    hermes_home = tmp_path / "clean-home" / ".hermes"
    monkeypatch.setattr(codex_route, "HERMES_HOME", hermes_home)
    monkeypatch.setattr(codex_route, "DEFAULT_AUTH", hermes_home / "auth.json")
    monkeypatch.setattr(codex_route, "POLICY_PATH", hermes_home / "state" / "codex_route_policy.json")
    monkeypatch.setattr(codex_route, "NATIVE_CODEX_AUTH", tmp_path / "missing-codex" / "auth.json")

    result = codex_route.main(["status"])

    assert result == 0
    output = capsys.readouterr().out
    assert "Codex 라우팅" in output
    assert "제어 스크립트" not in output


def test_auto_sync_splits_default_personal_from_global_clients(monkeypatch, tmp_path):
    from hermes_cli import codex_route

    homes = [("default", tmp_path / "default"), ("gameduo", tmp_path / "gameduo")]
    monkeypatch.setattr(codex_route, "profile_homes", lambda: homes)
    monkeypatch.setattr(codex_route, "load_policy", lambda: {"mode": "auto"})
    calls = []

    def fake_run(argv, **kwargs):
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(codex_route.subprocess, "run", fake_run)

    payloads = {
        str(home.resolve(strict=False)): json.dumps(
            {
                "version": codex_route.INTERNAL_PAYLOAD_VERSION,
                "payload": {
                    "accounts": [],
                    "routing": {},
                    "recommendation": {"label": "personal"},
                },
            }
        )
        for _name, home in homes
    }
    assert codex_route.sync_all_profiles(payloads, homes=homes) == []
    assert len(calls) == 3
    commands = sorted(
        (
            tuple(
                arg
                for arg in argv[3:]
                if arg != "--internal-payload-stdin"
            ),
            kwargs["env"]["HERMES_HOME"],
        )
        for argv, kwargs in calls
    )
    assert commands == sorted(
        [
            (("--profile-account", "personal", "--skip-cliproxy"), str(homes[0][1])),
            (("--skip-hermes",), str(homes[0][1])),
            (("--skip-cliproxy",), str(homes[1][1])),
        ]
    )
    for argv, kwargs in calls:
        assert argv[:3] == [sys.executable, "-m", "hermes_cli.codex_priority_sync"]
        assert kwargs["shell"] is False
        assert kwargs["timeout"] == codex_route.PROFILE_SYNC_TIMEOUT
        assert kwargs["env"]["HERMES_CODEX_ROUTE_LOCK_HELD"] == "1"
        expected_payload = payloads[
            str(Path(kwargs["env"]["HERMES_HOME"]).resolve(strict=False))
        ]
        assert kwargs["input"] == expected_payload

    default_inputs = [
        kwargs["input"]
        for _argv, kwargs in calls
        if kwargs["env"]["HERMES_HOME"] == str(homes[0][1])
    ]
    assert default_inputs == [payloads[str(homes[0][1].resolve())]] * 2


def test_fixed_sync_keeps_default_and_global_clients_in_one_transaction(
    monkeypatch, tmp_path
):
    from hermes_cli import codex_route

    homes = [("default", tmp_path / "default"), ("gameduo", tmp_path / "gameduo")]
    monkeypatch.setattr(codex_route, "profile_homes", lambda: homes)
    monkeypatch.setattr(
        codex_route,
        "load_policy",
        lambda: {"mode": "fixed", "credential_id": "company-id"},
    )
    calls = []

    def fake_run(argv, **kwargs):
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(codex_route.subprocess, "run", fake_run)

    payloads = {
        str(home.resolve(strict=False)): json.dumps(
            {
                "version": codex_route.INTERNAL_PAYLOAD_VERSION,
                "payload": {
                    "accounts": [],
                    "routing": {},
                    "recommendation": {"label": "company"},
                },
            }
        )
        for _name, home in homes
    }
    assert codex_route.sync_all_profiles(payloads, homes=homes) == []
    commands = sorted(
        (
            tuple(
                arg
                for arg in argv[3:]
                if arg != "--internal-payload-stdin"
            ),
            kwargs["env"]["HERMES_HOME"],
        )
        for argv, kwargs in calls
    )
    assert commands == sorted(
        [
            ((), str(homes[0][1])),
            (("--skip-cliproxy",), str(homes[1][1])),
        ]
    )


def test_profile_sync_failure_redacts_child_output(monkeypatch, tmp_path):
    from hermes_cli import codex_route

    secret = "sk" + "-" + "abcdefghijklmnopqrstuv"
    auth_header = "Author" + "ization: Bearer "
    token_field = "access_" + "token"
    result = subprocess.CompletedProcess(
        ["python", "-m", "hermes_cli.codex_priority_sync"],
        1,
        "",
        f'{auth_header}{secret}\n{{"{token_field}":"{secret}"}}\n',
    )
    monkeypatch.setattr(codex_route.subprocess, "run", lambda *_args, **_kwargs: result)

    detail = codex_route._sync_profile(
        "gameduo", tmp_path / "gameduo", '{"accounts":[]}'
    )

    assert detail is not None
    assert secret not in detail
    assert detail.startswith("gameduo: " + auth_header)


def test_profile_sync_launch_error_is_force_redacted(monkeypatch, tmp_path):
    from agent import redact
    from hermes_cli import codex_route

    secret = "sk" + "-proj-" + ("Z" * 40)
    failure = OSError(("Author" + "ization: Bearer ") + secret)

    def fail_run(*_args, **_kwargs):
        raise failure

    monkeypatch.setattr(codex_route.subprocess, "run", fail_run)
    monkeypatch.setattr(redact, "_REDACT_ENABLED", False)

    detail = codex_route._sync_profile(
        "gameduo", tmp_path / "gameduo", '{"accounts":[]}'
    )

    assert secret not in detail
    assert detail.startswith("gameduo: sync launch failed: ")


def test_partial_profile_failure_restores_policy_and_attempts_full_rollback(monkeypatch, tmp_path, capsys):
    from hermes_cli import codex_route

    auth = tmp_path / "auth.json"
    auth.write_text(
        '{"credential_pool": {"openai-codex": ['
        '{"id": "personal-id", "label": "personal-backup"},'
        '{"id": "company-id", "label": "company-plus-100"}]}}',
        encoding="utf-8",
    )
    policy = tmp_path / "state" / "codex_route_policy.json"
    policy.parent.mkdir()
    original = '{"mode": "auto", "owner": "before"}\n'
    policy.write_text(original, encoding="utf-8")
    monkeypatch.setattr(codex_route, "DEFAULT_AUTH", auth)
    monkeypatch.setattr(codex_route, "POLICY_PATH", policy)
    rounds = iter([["gameduo: sync failed"], ["penguincouple: rollback failed"]])
    calls = []

    def fake_sync(_payloads, *, homes):
        assert homes
        calls.append(policy.read_text(encoding="utf-8"))
        return next(rounds)

    monkeypatch.setattr(codex_route, "sync_all_profiles", fake_sync)
    monkeypatch.setattr(
        codex_route,
        "collect_profile_payloads",
        lambda homes: (
            {str(home.resolve(strict=False)): '{"version":1,"payload":{"accounts":[],"routing":{},"recommendation":{}}}' for _name, home in homes},
            [],
        ),
    )

    result = codex_route.main(["company"])

    assert result != 0
    assert len(calls) == 2
    assert '"mode": "fixed"' in calls[0]
    assert calls[1] == original
    assert policy.read_text(encoding="utf-8") == original
    output = capsys.readouterr().out
    assert "gameduo: sync failed" in output
    assert "penguincouple: rollback failed" in output
    assert "rollback" in output.lower()


def test_unexpected_sync_exception_still_restores_and_runs_rollback(
    monkeypatch, tmp_path, capsys
):
    from hermes_cli import codex_route

    auth = tmp_path / "auth.json"
    auth.write_text(
        '{"credential_pool": {"openai-codex": ['
        '{"id": "company-id", "label": "company-plus-100"}]}}',
        encoding="utf-8",
    )
    policy = tmp_path / "state" / "codex_route_policy.json"
    policy.parent.mkdir()
    original = b'{"mode": "auto"}\n'
    policy.write_bytes(original)
    monkeypatch.setattr(codex_route, "DEFAULT_AUTH", auth)
    monkeypatch.setattr(codex_route, "POLICY_PATH", policy)
    calls = 0

    def fake_sync(_payloads, *, homes):
        assert homes
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("unexpected profile discovery failure")
        return []

    monkeypatch.setattr(codex_route, "sync_all_profiles", fake_sync)
    monkeypatch.setattr(
        codex_route,
        "collect_profile_payloads",
        lambda homes: (
            {str(home.resolve(strict=False)): '{"version":1,"payload":{"accounts":[],"routing":{},"recommendation":{}}}' for _name, home in homes},
            [],
        ),
    )

    assert codex_route.main(["company"]) == 1
    assert calls == 2
    assert policy.read_bytes() == original
    output = capsys.readouterr().out
    assert "profile sync raised RuntimeError" in output
    assert "rollback 완료" in output


def test_apply_mode_collects_once_per_unique_home_before_route_lock(
    monkeypatch, tmp_path
):
    from contextlib import contextmanager
    from hermes_cli import codex_route

    default = tmp_path / "default"
    named = tmp_path / "named"
    homes = [("default", default), ("named", named)]
    events: list[str] = []
    payloads = {
        str(default.resolve(strict=False)): '{"version":1,"payload":{"accounts":[],"routing":{},"recommendation":{}}}',
        str(named.resolve(strict=False)): '{"version":1,"payload":{"accounts":[],"routing":{},"recommendation":{}}}',
    }

    monkeypatch.setattr(codex_route, "profile_homes", lambda: homes)

    def fake_collect(received_homes):
        assert received_homes == homes
        events.append("collect")
        return payloads, []

    @contextmanager
    def fake_lock(*, path, timeout):
        events.append("lock")
        yield
        events.append("unlock")

    def fake_apply(mode, *, homes, payloads):
        assert mode == "auto"
        assert homes == [("default", default), ("named", named)]
        assert payloads == payloads_expected
        events.append("apply")
        return "ok"

    payloads_expected = payloads
    monkeypatch.setattr(codex_route, "collect_profile_payloads", fake_collect)
    monkeypatch.setattr(codex_route, "route_lock", fake_lock)
    monkeypatch.setattr(codex_route, "_apply_mode_locked", fake_apply)

    assert codex_route.apply_mode("auto") == "ok"
    assert events == ["collect", "lock", "apply", "unlock"]


def test_apply_mode_rejects_payloads_aged_during_parallel_collection(
    monkeypatch, tmp_path
):
    from contextlib import contextmanager
    from hermes_cli import codex_route

    default = tmp_path / "default"
    named = tmp_path / "named"
    homes = [("default", default), ("named", named)]
    now = {"value": 0.0}
    collections = {"count": 0}
    payloads = {
        str(default.resolve(strict=False)): '{"version":1,"payload":{"accounts":[],"routing":{},"recommendation":{}}}',
        str(named.resolve(strict=False)): '{"version":1,"payload":{"accounts":[],"routing":{},"recommendation":{}}}',
    }

    monkeypatch.setattr(codex_route, "profile_homes", lambda: homes)
    monkeypatch.setattr(codex_route.time, "monotonic", lambda: now["value"])
    monkeypatch.setattr(codex_route, "COLLECTED_PAYLOAD_MAX_AGE_SECONDS", 15.0)

    def fake_collect(received_homes):
        assert received_homes == homes
        collections["count"] += 1
        now["value"] += 16.0
        return payloads, []

    @contextmanager
    def fake_lock(*, path, timeout):
        yield

    monkeypatch.setattr(codex_route, "collect_profile_payloads", fake_collect)
    monkeypatch.setattr(codex_route, "route_lock", fake_lock)
    monkeypatch.setattr(
        codex_route,
        "_apply_mode_locked",
        lambda *_args, **_kwargs: pytest.fail("stale payloads must not be applied"),
    )

    with pytest.raises(codex_route.RouteApplyError, match="payload changed"):
        codex_route.apply_mode("auto")

    assert collections["count"] == 2


def test_collection_failure_never_acquires_route_lock_or_writes_policy(
    monkeypatch, tmp_path
):
    from hermes_cli import codex_route

    policy = tmp_path / "codex_route_policy.json"
    original = b'{"mode":"auto"}\n'
    policy.write_bytes(original)
    monkeypatch.setattr(codex_route, "POLICY_PATH", policy)
    monkeypatch.setattr(
        codex_route,
        "collect_profile_payloads",
        lambda _homes: ({}, ["default: payload collection failed"]),
    )
    monkeypatch.setattr(
        codex_route,
        "route_lock",
        lambda **_kwargs: pytest.fail("collection failure must precede lock"),
    )

    with pytest.raises(codex_route.RouteApplyError, match="collection failed"):
        codex_route.apply_mode("auto")

    assert policy.read_bytes() == original


def test_top_level_codex_route_dispatches_mode_to_handler(monkeypatch):
    import hermes_cli.main as main_mod

    calls = []
    fake_module = types.ModuleType("hermes_cli.codex_route")
    setattr(fake_module, "main", lambda argv=None: calls.append(argv) or 0)
    monkeypatch.setitem(sys.modules, "hermes_cli.codex_route", fake_module)
    monkeypatch.setattr(sys, "argv", ["hermes", "codex-route", "company"])
    monkeypatch.setattr("hermes_cli.config.get_container_exec_info", lambda: None)
    monkeypatch.setattr(main_mod, "_prepare_agent_startup", lambda _args: None)

    main_mod.main()

    assert calls == [["company"]]
