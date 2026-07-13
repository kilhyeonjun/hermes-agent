import importlib.util
from contextlib import contextmanager
import io
import json
from pathlib import Path

import pytest


SCRIPT = Path(__file__).parents[2] / "hermes_cli" / "codex_priority_sync.py"


def load_module():
    spec = importlib.util.spec_from_file_location(
        "codex_reset_aware_priority_sync_test", SCRIPT
    )
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    ("label", "expected"),
    [
        ("company-plus-100", "company"),
        ("personal-backup", "personal"),
        ("company", "company"),
        ("personal", "personal"),
        ("personal-company-plus", None),
        ("private-plus@example.com", None),
    ],
)
def test_label_kind_uses_only_explicit_canonical_labels(label, expected):
    module = load_module()

    assert module.label_kind(label) == expected


def test_priority_policy_missing_is_auto_but_corrupt_blocks_recommendation(
    monkeypatch, tmp_path
):
    module = load_module()
    policy_path = tmp_path / "codex_route_policy.json"
    monkeypatch.setattr(module, "ROUTE_POLICY_PATH", policy_path)
    assert module.load_route_policy() == {"mode": "auto"}

    policy_path.write_text('{"mode":', encoding="utf-8")
    policy = module.load_route_policy()
    assert policy["mode"] == "invalid"
    decision = module.choose_route_recommendation({
        "recommendation": {"label": "personal-backup"}
    })
    assert decision["policy"] == "invalid"
    assert "invalid" in decision["error"].lower()


class FakeResponse:
    def __init__(self, status, payload):
        self.status = status
        self.reason = "fixture"
        self.payload = payload

    def read(self, _limit=-1):
        return json.dumps(self.payload).encode("utf-8")


class FakeConnection:
    def __init__(self, api, host, port, timeout):
        self.api = api
        self.host = host
        self.port = port
        self.timeout = timeout
        self.pending = None

    def request(self, method, path, body=None, headers=None):
        self.pending = {
            "method": method,
            "path": path,
            "headers": {key.lower(): value for key, value in (headers or {}).items()},
            "body": json.loads(body) if body else None,
            "host": self.host,
            "port": self.port,
            "timeout": self.timeout,
        }

    def getresponse(self):
        assert self.pending is not None
        return self.api.respond(self.pending)

    def close(self):
        return None


class FakeCLIProxyManagementAPI:
    def __init__(
        self,
        rows,
        *,
        fail_patch_number=None,
        get_error_body=None,
        revision=0,
        conflict_patch_number=None,
        conflict_rows=None,
    ):
        self.rows = json.loads(json.dumps(rows))
        self.calls = []
        self.fail_patch_number = fail_patch_number
        self.get_error_body = get_error_body
        self.patch_attempts = 0
        self.failed_once = False
        self.revision = revision
        self.conflict_patch_number = conflict_patch_number
        self.conflict_rows = conflict_rows

    def connection(self, host, port, timeout):
        return FakeConnection(self, host, port, timeout)

    def respond(self, call):
        method = call["method"]
        path = call["path"]
        body = call["body"]
        self.calls.append(call)
        if method == "GET":
            if self.get_error_body is not None:
                return FakeResponse(503, json.loads(self.get_error_body))
            return FakeResponse(
                200,
                {"files": self.rows, "routing_revision": self.revision},
            )

        assert method == "PATCH"
        assert path == "/v0/management/auth-files/route"
        self.patch_attempts += 1
        expected_revision = body.get("expected_revision")
        if self.conflict_patch_number == self.patch_attempts:
            if self.conflict_rows is not None:
                self.rows = json.loads(json.dumps(self.conflict_rows))
            self.revision += 1
            return FakeResponse(409, {"error": "private conflict body"})
        if isinstance(expected_revision, bool) or expected_revision != self.revision:
            return FakeResponse(409, {"error": "private conflict body"})
        if self.fail_patch_number == self.patch_attempts and not self.failed_once:
            self.failed_once = True
            return FakeResponse(500, {"error": "private response body"})

        by_id = {row["id"]: row for row in self.rows}
        for state in body["states"]:
            row = by_id[state["id"]]
            row["disabled"] = state["disabled"]
            row["priority"] = state["priority"]
        self.revision += 1
        return FakeResponse(200, {"status": "ok", "revision": self.revision})


def cliproxy_rows(*, company_disabled=False, personal_disabled=False):
    return [
        {
            "id": "company-id",
            "name": "codex-company.json",
            "provider": "codex",
            "label": "gameduo company plus",
            "disabled": company_disabled,
            "priority": 0,
        },
        {
            "id": "personal-id",
            "name": "codex-personal.json",
            "provider": "codex",
            "label": "personal backup",
            "disabled": personal_disabled,
            "priority": 100,
        },
    ]


def configure_cliproxy(
    monkeypatch, tmp_path, module, api, *, key="test-management-key"
):
    profile_home = tmp_path / "profile"
    global_home = tmp_path / "global"
    profile_home.mkdir(exist_ok=True)
    global_home.mkdir(exist_ok=True)
    if key is not None:
        (profile_home / ".env").write_text(
            f"CLIPROXY_MANAGEMENT_KEY={key}\n"
            "CLIPROXY_COMPANY_AUTH_ID=company-id\n"
            "CLIPROXY_PERSONAL_AUTH_ID=personal-id\n",
            encoding="utf-8",
        )
    else:
        (profile_home / ".env").write_text(
            "CLIPROXY_COMPANY_AUTH_ID=company-id\n"
            "CLIPROXY_PERSONAL_AUTH_ID=personal-id\n",
            encoding="utf-8",
        )
    state_path = global_home / "state" / "cliproxy_fixed_route_state.json"
    monkeypatch.delenv("CLIPROXY_MANAGEMENT_KEY", raising=False)
    monkeypatch.setattr(module, "HERMES_HOME", profile_home)
    monkeypatch.setattr(module, "GLOBAL_HERMES_HOME", global_home)
    monkeypatch.setattr(
        module, "CLIPROXY_FIXED_ROUTE_STATE_PATH", state_path, raising=False
    )
    monkeypatch.setattr(
        module,
        "CLIPROXY_MANAGEMENT_URL",
        "http://127.0.0.1:8317",
        raising=False,
    )
    monkeypatch.setattr(module, "HTTPConnection", api.connection, raising=False)
    return state_path


def test_cliproxy_active_kind_uses_authoritative_management_priority(
    monkeypatch, tmp_path
):
    module = load_module()
    rows = cliproxy_rows()
    rows[0]["priority"] = 100
    rows[1]["priority"] = 0
    api = FakeCLIProxyManagementAPI(rows, revision=7)
    configure_cliproxy(monkeypatch, tmp_path, module, api)

    assert module.cliproxy_active_kind() == "company"
    assert [call["method"] for call in api.calls] == ["GET"]


def test_cliproxy_active_kind_ignores_disabled_high_priority_row(
    monkeypatch, tmp_path
):
    module = load_module()
    rows = cliproxy_rows(company_disabled=True)
    rows[0]["priority"] = 100
    rows[1]["priority"] = 0
    api = FakeCLIProxyManagementAPI(rows)
    configure_cliproxy(monkeypatch, tmp_path, module, api)

    assert module.cliproxy_active_kind() == "personal"


def test_cliproxy_active_kind_fails_closed_on_ambiguous_priority(
    monkeypatch, tmp_path
):
    module = load_module()
    rows = cliproxy_rows()
    rows[0]["priority"] = 100
    rows[1]["priority"] = 100
    api = FakeCLIProxyManagementAPI(rows)
    configure_cliproxy(monkeypatch, tmp_path, module, api)

    with pytest.raises(module.CLIProxyManagementError, match="unique active route"):
        module.cliproxy_active_kind()


def test_cliproxy_active_kind_requires_management_key(monkeypatch, tmp_path):
    module = load_module()
    api = FakeCLIProxyManagementAPI(cliproxy_rows())
    configure_cliproxy(monkeypatch, tmp_path, module, api, key=None)

    with pytest.raises(module.CLIProxyManagementError, match="management key missing"):
        module.cliproxy_active_kind()


def test_profile_home_controls_auth_and_state_paths(monkeypatch, tmp_path):
    profile_home = tmp_path / "profile"
    monkeypatch.setenv("HERMES_HOME", str(profile_home))

    module = load_module()

    assert module.HERMES_HOME == profile_home
    assert module.HERMES_AUTH == profile_home / "auth.json"
    assert module.STATE_PATH == profile_home / "state" / "codex_reset_aware_warmup.json"


def test_fixed_route_policy_overrides_reset_aware_recommendation(monkeypatch, tmp_path):
    profile_home = tmp_path / "profile"
    profile_home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(profile_home))
    module = load_module()
    policy_path = tmp_path / "global-state" / "codex_route_policy.json"
    policy_path.parent.mkdir()
    policy_path.write_text(
        json.dumps({
            "mode": "fixed",
            "credential_id": "personal-id",
            "label": "personal-backup",
        }),
        encoding="utf-8",
    )
    monkeypatch.setattr(module, "ROUTE_POLICY_PATH", policy_path)
    payload = {
        "accounts": [
            {"credential_id": "company-id", "label": "company-plus-100", "ok": True},
            {
                "credential_id": "personal-id",
                "label": "openai-codex-oauth-1",
                "ok": True,
            },
        ],
        "recommendation": {"label": "company-plus-100", "policy": "7d-reset-aware"},
    }

    recommendation = module.choose_route_recommendation(payload)

    assert recommendation == {
        "label": "openai-codex-oauth-1",
        "credential_id": "personal-id",
        "reason": "manual fixed route",
        "policy": "fixed",
    }


def test_profile_account_selects_exact_canonical_personal_label(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "profile"))
    module = load_module()
    monkeypatch.setattr(module, "load_route_policy", lambda: {"mode": "auto"})
    payload = {
        "accounts": [
            {"credential_id": "company-id", "label": "company-plus-100", "ok": True},
            {"credential_id": "personal-id", "label": "personal-backup", "ok": True},
        ],
        "recommendation": {"label": "company-plus-100", "policy": "7d-reset-aware"},
    }

    recommendation = module.choose_effective_recommendation(payload, "personal")

    assert recommendation["credential_id"] == "personal-id"
    assert recommendation["label"] == "personal-backup"
    assert recommendation["policy"] == "profile-fixed"


def test_profile_affinity_keeps_exhausted_personal_as_priority_owner(
    monkeypatch, tmp_path
):
    profile_home = tmp_path / "profile"
    profile_home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(profile_home))
    auth_path = profile_home / "auth.json"
    auth_path.write_text(
        json.dumps(
            {
                "credential_pool": {
                    "openai-codex": [
                        {
                            "id": "personal-id",
                            "label": "personal-backup",
                            "priority": 10,
                            "last_status": "exhausted",
                        },
                        {
                            "id": "company-id",
                            "label": "company-plus-100",
                            "priority": 0,
                            "last_status": "ok",
                        },
                    ]
                }
            }
        ),
        encoding="utf-8",
    )
    module = load_module()
    monkeypatch.setattr(module, "load_route_policy", lambda: {"mode": "auto"})
    payload = {
        "accounts": [
            {
                "credential_id": "personal-id",
                "label": "personal-backup",
                "ok": True,
                "available": False,
                "last_status": "exhausted",
            },
            {
                "credential_id": "company-id",
                "label": "company-plus-100",
                "ok": True,
                "available": True,
                "last_status": "ok",
            },
        ],
        "recommendation": {
            "credential_id": "company-id",
            "label": "company-plus-100",
            "policy": "7d-reset-aware",
        },
    }

    recommendation = module.choose_effective_recommendation(payload, "personal")
    result = module.sync_hermes(
        recommendation["label"],
        recommended_credential_id=recommendation["credential_id"],
        dry_run=False,
    )
    saved = json.loads(auth_path.read_text(encoding="utf-8"))
    priorities = {
        row["label"]: row["priority"]
        for row in saved["credential_pool"]["openai-codex"]
    }

    assert recommendation["credential_id"] == "personal-id"
    assert recommendation["policy"] == "profile-fixed"
    assert result.ok
    assert priorities == {"personal-backup": 0, "company-plus-100": 10}


def test_sync_hermes_rejects_import_time_live_auth_path(
    monkeypatch, tmp_path
):
    real_home = tmp_path / "real-home"
    live_home = real_home / ".hermes"
    live_home.mkdir(parents=True)
    live_auth = live_home / "auth.json"
    live_auth.write_text(
        json.dumps(
            {
                "credential_pool": {
                    "openai-codex": [
                        {
                            "id": "personal-id",
                            "label": "personal-backup",
                            "priority": 0,
                        }
                    ]
                }
            }
        ),
        encoding="utf-8",
    )
    before = live_auth.read_bytes()
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.setenv("HERMES_TESTING", "1")
    monkeypatch.setenv("HERMES_TEST_REAL_HOME", str(real_home))
    monkeypatch.setenv("HERMES_HOME", str(live_home))
    module = load_module()

    result = module.sync_hermes("personal-backup", dry_run=False)

    assert not result.ok
    assert result.error is not None
    assert result.error.code == module.StageErrorCode.PREFLIGHT
    assert live_auth.read_bytes() == before


@pytest.mark.parametrize("label", ["personal-old", "openai-codex-oauth-1"])
def test_profile_account_rejects_noncanonical_labels(monkeypatch, tmp_path, label):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "profile"))
    module = load_module()
    monkeypatch.setattr(module, "load_route_policy", lambda: {"mode": "auto"})

    recommendation = module.choose_effective_recommendation(
        {"accounts": [{"credential_id": "legacy-id", "label": label, "ok": True}]},
        "personal",
    )

    assert recommendation["policy"] == "profile-fixed"
    assert recommendation["error"] == "Profile Codex route unavailable"


def test_global_fixed_route_overrides_profile_account(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "profile"))
    module = load_module()
    monkeypatch.setattr(
        module,
        "load_route_policy",
        lambda: {
            "mode": "fixed",
            "credential_id": "company-id",
            "label": "company-plus-100",
        },
    )
    payload = {
        "accounts": [
            {"credential_id": "company-id", "label": "company-plus-100", "ok": True},
            {"credential_id": "personal-id", "label": "personal-backup", "ok": True},
        ]
    }

    recommendation = module.choose_effective_recommendation(payload, "personal")

    assert recommendation["credential_id"] == "company-id"
    assert recommendation["policy"] == "fixed"


def test_skip_hermes_disables_unstarted_warmup_and_state_recording(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "profile"))
    monkeypatch.setenv("HERMES_CODEX_ROUTE_LOCK_HELD", "1")
    module = load_module()
    monkeypatch.setattr(module, "load_route_policy", lambda: {"mode": "auto"})
    monkeypatch.setattr(module, "load_state", lambda: {})
    monkeypatch.setattr(
        module,
        "collect_payload",
        lambda: {
            "accounts": [
                {
                    "credential_id": "company-id",
                    "label": "company-plus-100",
                    "ok": True,
                    "secondary_window": {},
                }
            ],
            "recommendation": {
                "credential_id": "company-id",
                "label": "company-plus-100",
                "policy": "7d-reset-aware",
            },
        },
    )
    monkeypatch.setattr(
        module,
        "run_warmup_call",
        lambda *_args, **_kwargs: pytest.fail("skip-hermes must not call warmup"),
    )
    monkeypatch.setattr(
        module,
        "record_warmup",
        lambda *_args, **_kwargs: pytest.fail("skip-hermes must not record warmup"),
    )

    assert module.main(
        ["--warmup-unstarted", "--skip-hermes", "--skip-cliproxy"]
    ) == 0


@pytest.mark.parametrize(
    "argv",
    [
        ["--profile-account", "personal"],
        [
            "--profile-account",
            "personal",
            "--skip-hermes",
            "--skip-cliproxy",
        ],
    ],
)
def test_profile_account_rejects_non_profile_only_invocations_before_collection(
    monkeypatch, tmp_path, capsys, argv
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "profile"))
    monkeypatch.setenv("HERMES_CODEX_ROUTE_LOCK_HELD", "1")
    module = load_module()
    monkeypatch.setattr(
        module,
        "collect_payload",
        lambda: pytest.fail("invalid profile pin must fail before collection"),
    )

    assert module.main(argv) == 1
    assert "profile-account requires Hermes-only profile sync" in capsys.readouterr().out


def test_main_applies_fixed_route_policy(monkeypatch, tmp_path):
    profile_home = tmp_path / "profile"
    profile_home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(profile_home))
    module = load_module()
    monkeypatch.setenv("HERMES_CODEX_ROUTE_LOCK_HELD", "1")
    policy_path = tmp_path / "codex_route_policy.json"
    policy_path.write_text(
        json.dumps({
            "mode": "fixed",
            "credential_id": "personal-id",
            "label": "personal-backup",
        }),
        encoding="utf-8",
    )
    monkeypatch.setattr(module, "ROUTE_POLICY_PATH", policy_path)
    monkeypatch.setattr(
        module,
        "collect_payload",
        lambda: {
            "accounts": [
                {
                    "credential_id": "company-id",
                    "label": "company-plus-100",
                    "ok": True,
                },
                {
                    "credential_id": "personal-id",
                    "label": "personal-backup",
                    "ok": True,
                },
            ],
            "recommendation": {"label": "company-plus-100", "policy": "7d-reset-aware"},
        },
    )
    monkeypatch.setattr(module, "load_state", lambda: {})
    selected = []
    monkeypatch.setattr(
        module,
        "_prepare_hermes",
        lambda label, **_kwargs: (
            selected.append(label) or module.PreparedStage("Hermes", object())
        ),
    )

    result = module.main(["--dry-run", "--skip-cliproxy"])

    assert result == 0
    assert selected == ["personal-backup"]


@pytest.mark.parametrize(
    ("policy", "accounts"),
    [
        ({"mode": "fixed"}, []),
        (
            {"mode": "fixed", "credential_id": "missing-secret-credential"},
            [{"credential_id": "other-id", "label": "other", "ok": True}],
        ),
        (
            {"mode": "fixed", "credential_id": "dead-secret-credential"},
            [{"credential_id": "dead-secret-credential", "label": "dead", "ok": False}],
        ),
        (
            {"mode": "fixed", "credential_id": "busy-secret-credential"},
            [
                {
                    "credential_id": "busy-secret-credential",
                    "label": "busy",
                    "ok": True,
                    "available": False,
                }
            ],
        ),
        (
            {"mode": "fixed", "credential_id": "exhausted-secret-credential"},
            [
                {
                    "credential_id": "exhausted-secret-credential",
                    "label": "exhausted",
                    "ok": True,
                    "available": True,
                    "last_status": "exhausted",
                }
            ],
        ),
    ],
)
def test_main_fails_closed_when_fixed_route_is_unavailable(
    monkeypatch, tmp_path, capsys, policy, accounts
):
    profile_home = tmp_path / "profile"
    profile_home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(profile_home))
    monkeypatch.setenv("HERMES_CODEX_ROUTE_LOCK_HELD", "1")
    module = load_module()
    monkeypatch.setattr(module, "load_route_policy", lambda: policy)
    monkeypatch.setattr(
        module,
        "collect_payload",
        lambda: {
            "accounts": accounts,
            "recommendation": {
                "label": "automatic-fallback",
                "policy": "7d-reset-aware",
            },
        },
    )
    monkeypatch.setattr(module, "load_state", lambda: {})
    sync_calls = []
    monkeypatch.setattr(
        module,
        "sync_hermes",
        lambda label, dry_run: sync_calls.append((label, dry_run)) or [],
    )

    result = module.main(["--dry-run", "--skip-cliproxy"])

    output = capsys.readouterr().out
    assert result == 1
    assert sync_calls == []
    assert "Fixed Codex route unavailable" in output
    for secret in (
        "missing-secret-credential",
        "dead-secret-credential",
        "busy-secret-credential",
        "exhausted-secret-credential",
    ):
        assert secret not in output


def test_routine_priority_changes_are_silent_without_report(
    monkeypatch, tmp_path, capsys
):
    """The 15-minute auto-sync must not page Telegram for expected route churn."""
    profile_home = tmp_path / "profile"
    profile_home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(profile_home))
    module = load_module()
    monkeypatch.setenv("HERMES_CODEX_ROUTE_LOCK_HELD", "1")
    monkeypatch.setattr(module, "load_route_policy", lambda: {"mode": "auto"})
    monkeypatch.setattr(
        module,
        "collect_payload",
        lambda: {
            "recommendation": {
                "label": "company-plus-100",
                "policy": "7d-reset-aware",
                "reason": "quota availability changed",
            },
            "accounts": [],
        },
    )
    monkeypatch.setattr(module, "load_state", lambda: {})
    prepared = [
        module.PreparedStage("Hermes", object()),
        module.PreparedStage("CLIProxyAPI", object()),
        module.PreparedStage("Native Codex", object()),
    ]
    monkeypatch.setattr(
        module, "_prepare_hermes", lambda *_args, **_kwargs: prepared[0]
    )
    monkeypatch.setattr(
        module, "_prepare_cliproxy", lambda *_args, **_kwargs: prepared[1]
    )
    monkeypatch.setattr(
        module, "_prepare_native", lambda *_args, **_kwargs: prepared[2]
    )
    monkeypatch.setattr(
        module,
        "_execute_transaction",
        lambda *_args, **_kwargs: module.TransactionResult(
            (
                module.StageResult(
                    "Hermes",
                    module.StageStatus.APPLIED,
                    changes=("Hermes credential #1: priority 10 -> 0",),
                ),
                module.StageResult(
                    "CLIProxyAPI",
                    module.StageStatus.APPLIED,
                    changes=("CLIProxyAPI credential #1: priority 0 -> 100",),
                ),
                module.StageResult(
                    "Native Codex",
                    module.StageStatus.APPLIED,
                    changes=("Native Codex credential #1 activated",),
                ),
            ),
            (),
        ),
    )

    assert module.main([]) == 0
    assert capsys.readouterr().out == ""


def test_warmup_stage_failure_rolls_back_completed_surfaces_in_reverse_order(
    monkeypatch, tmp_path, capsys
):
    profile_home = tmp_path / "profile"
    profile_home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(profile_home))
    monkeypatch.setenv("HERMES_CODEX_ROUTE_LOCK_HELD", "1")
    module = load_module()
    monkeypatch.setattr(module, "load_route_policy", lambda: {"mode": "auto"})
    monkeypatch.setattr(module, "load_state", lambda: {})
    monkeypatch.setattr(
        module,
        "collect_payload",
        lambda: {
            "recommendation": {"label": "personal-backup"},
            "accounts": [],
        },
    )
    monkeypatch.setattr(
        module,
        "choose_unstarted_weekly",
        lambda *_args, **_kwargs: {
            "label": "company-plus-100",
            "credential_id": "company-id",
        },
    )
    prepared = [
        module.PreparedStage("Hermes", object()),
        module.PreparedStage("CLIProxyAPI", object()),
        module.PreparedStage("Native Codex", object()),
    ]
    monkeypatch.setattr(
        module, "_prepare_hermes", lambda *_args, **_kwargs: prepared[0]
    )
    monkeypatch.setattr(
        module, "_prepare_cliproxy", lambda *_args, **_kwargs: prepared[1]
    )
    monkeypatch.setattr(
        module, "_prepare_native", lambda *_args, **_kwargs: prepared[2]
    )
    applied = tuple(
        module.StageResult(stage.stage, module.StageStatus.APPLIED)
        for stage in prepared
    )
    monkeypatch.setattr(
        module,
        "_execute_transaction",
        lambda *_args, **_kwargs: module.TransactionResult(applied, ()),
    )
    monkeypatch.setattr(
        module, "run_warmup_call", lambda *_args, **_kwargs: (False, "private")
    )
    monkeypatch.setattr(module, "record_warmup", lambda *_args, **_kwargs: None)
    rollback_order = []

    def rollback(stage, result):
        rollback_order.append(stage.stage)
        return module.StageResult(
            stage.stage,
            module.StageStatus.ROLLED_BACK,
            token=result.token,
        )

    monkeypatch.setattr(module, "_rollback_prepared_stage", rollback)

    assert module.main(["--warmup-unstarted"]) == 1
    assert rollback_order == ["Native Codex", "CLIProxyAPI", "Hermes"]
    assert "Warm-up stage failed" in capsys.readouterr().out


def test_sync_native_codex_activates_matching_native_account(monkeypatch, tmp_path):
    module = load_module()
    active_auth = configure_native_surface(monkeypatch, tmp_path, module)
    calls = []

    class Result:
        returncode = 0
        stdout = "CHANGED\n"
        stderr = ""

    def activate(argv, **kwargs):
        calls.append((argv, kwargs))
        active_auth.unlink()
        active_auth.symlink_to(
            module.NATIVE_CODEX_ACCOUNT_ROOT / "company" / "auth.json"
        )
        return Result()

    monkeypatch.setattr(module.subprocess, "run", activate)

    changes = module.sync_native_codex("company-plus-100", dry_run=False)

    assert changes == ["Native Codex credential #1 activated"]
    assert calls[0][0][-2:] == ["activate", "company"]
    assert calls[0][1]["shell"] is False


def test_sync_native_codex_unknown_mapping_never_echoes_raw_label():
    module = load_module()
    secret_label = "private-seat-owner@example.invalid"

    changes = module.sync_native_codex(secret_label, dry_run=True)

    assert changes == ["Native Codex recommendation mapping unknown"]
    assert secret_label not in "\n".join(changes)


def test_subprocess_failure_details_are_secret_redacted(monkeypatch, tmp_path):
    module = load_module()
    secret = "sk" + "-" + "abcdefghijklmnopqrstuv"
    auth_header = "Author" + "ization: Bearer "
    token_field = "access_" + "token"

    class Result:
        returncode = 1
        stdout = ""
        stderr = f'{auth_header}{secret}\n{{"{token_field}":"{secret}"}}\n'

    monkeypatch.setattr(module.subprocess, "run", lambda *_args, **_kwargs: Result())

    warmup_ok, warmup_detail = module.run_warmup_call("company", dry_run=False)
    assert warmup_ok is False
    assert secret not in warmup_detail
    assert "warmup call failed" in warmup_detail

    configure_native_surface(monkeypatch, tmp_path, module)
    native_changes = module.sync_native_codex("company", dry_run=False)
    assert secret not in "\n".join(native_changes)
    assert "Native Codex stage failed" in native_changes[0]


def test_sync_hermes_updates_only_active_profile_auth(monkeypatch, tmp_path):
    profile_home = tmp_path / "profile"
    profile_home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(profile_home))
    auth_path = profile_home / "auth.json"
    auth_path.write_text(
        json.dumps({
            "credential_pool": {
                "openai-codex": [
                    {"label": "company-plus-100", "priority": 0},
                    {"label": "personal-backup", "priority": 2},
                ]
            }
        }),
        encoding="utf-8",
    )
    module = load_module()

    changes = module.sync_hermes("personal-backup", dry_run=False)
    saved = json.loads(auth_path.read_text(encoding="utf-8"))
    rows = saved["credential_pool"]["openai-codex"]

    assert changes
    assert [(row["label"], row["priority"]) for row in rows] == [
        ("company-plus-100", 10),
        ("personal-backup", 0),
    ]
    assert auth_path.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize(
    ("rows", "recommended_label", "recommended_credential_id", "message"),
    [
        (
            [
                {"id": "personal-id", "label": "personal-backup", "priority": 0},
                {"id": "company-id", "label": "company-plus-100", "priority": 10},
            ],
            "company-plus-100",
            "missing-id",
            "Hermes recommendation target missing",
        ),
        (
            [
                {"id": "duplicate-id", "label": "company-plus-100", "priority": 0},
                {"id": "duplicate-id", "label": "company-plus-100", "priority": 10},
            ],
            "company-plus-100",
            "duplicate-id",
            "Hermes recommendation target duplicate",
        ),
    ],
)
def test_sync_hermes_missing_or_duplicate_target_is_a_noop(
    monkeypatch,
    tmp_path,
    rows,
    recommended_label,
    recommended_credential_id,
    message,
):
    profile_home = tmp_path / "profile"
    profile_home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(profile_home))
    auth_path = profile_home / "auth.json"
    auth_path.write_text(
        json.dumps({"credential_pool": {"openai-codex": rows}}, indent=2) + "\n",
        encoding="utf-8",
    )
    before = auth_path.read_bytes()
    module = load_module()

    changes = module.sync_hermes(
        recommended_label,
        recommended_credential_id=recommended_credential_id,
        dry_run=False,
    )

    assert changes == [message]
    assert auth_path.read_bytes() == before


def test_sync_hermes_reports_slots_without_echoing_raw_labels(monkeypatch, tmp_path):
    profile_home = tmp_path / "profile"
    profile_home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(profile_home))
    auth_path = profile_home / "auth.json"
    secret_label = "private-seat-owner@example.invalid"
    auth_path.write_text(
        json.dumps({
            "credential_pool": {
                "openai-codex": [
                    {
                        "id": "company-id",
                        "label": "company-plus-100",
                        "priority": 10,
                    },
                    {
                        "id": "unmapped-id",
                        "label": secret_label,
                        "priority": 0,
                    },
                ]
            }
        }),
        encoding="utf-8",
    )
    module = load_module()

    changes = module.sync_hermes(
        "company-plus-100",
        recommended_credential_id="company-id",
        dry_run=True,
    )

    output = "\n".join(changes)
    assert secret_label not in output
    assert "Hermes credential #1" in output
    assert "Hermes credential #2" in output


def test_main_rejects_noncanonical_recommendation_before_side_effects(
    monkeypatch, tmp_path, capsys
):
    profile_home = tmp_path / "profile"
    profile_home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(profile_home))
    monkeypatch.setenv("HERMES_CODEX_ROUTE_LOCK_HELD", "1")
    module = load_module()
    secret_label = "private-seat-owner@example.invalid"
    monkeypatch.setattr(module, "load_route_policy", lambda: {"mode": "auto"})
    monkeypatch.setattr(
        module,
        "collect_payload",
        lambda: {
            "recommendation": {
                "label": secret_label,
                "credential_id": "secret-id",
                "policy": "7d-reset-aware",
            },
            "accounts": [],
        },
    )
    monkeypatch.setattr(module, "load_state", lambda: {})
    calls = []
    monkeypatch.setattr(
        module,
        "sync_hermes",
        lambda *_args, **_kwargs: calls.append("hermes") or [],
    )
    monkeypatch.setattr(
        module,
        "sync_cliproxy",
        lambda *_args, **_kwargs: calls.append("cliproxy") or [],
    )
    monkeypatch.setattr(
        module,
        "sync_native_codex",
        lambda *_args, **_kwargs: calls.append("native") or [],
    )

    assert module.main(["--report"]) == 1
    output = capsys.readouterr().out
    assert calls == []
    assert "recommendation mapping unknown" in output
    assert secret_label not in output


def test_sync_hermes_reloads_under_auth_lock_and_preserves_rotated_fields(
    monkeypatch, tmp_path
):
    profile_home = tmp_path / "profile"
    profile_home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(profile_home))
    auth_path = profile_home / "auth.json"
    fresh = {
        "credential_pool": {
            "openai-codex": [
                {
                    "label": "company-plus-100",
                    "priority": 0,
                    "access_token": "fresh-access-token",
                    "refresh_token": "fresh-refresh-token",
                    "generation": 7,
                },
                {"label": "personal-backup", "priority": 2},
            ],
            "other-provider": [{"opaque": "preserve-me"}],
        },
        "providers": {"other-provider": {"opaque": "still-fresh"}},
        "unrelated_top_level": {"sequence": 42},
    }
    auth_path.write_text(json.dumps(fresh), encoding="utf-8")
    module = load_module()

    from hermes_cli import auth as auth_module

    stale = json.loads(json.dumps(fresh))
    stale_entry = stale["credential_pool"]["openai-codex"][0]
    stale_entry.update({
        "access_token": "stale-access-token",
        "refresh_token": "stale-refresh-token",
        "generation": 6,
    })
    monkeypatch.setattr(module, "load_json", lambda _path: stale, raising=False)

    lock_depth = 0
    lock_events = []
    real_load = auth_module._load_auth_store
    real_save = auth_module._save_auth_store

    @contextmanager
    def recording_lock(*_args, **_kwargs):
        nonlocal lock_depth
        lock_events.append("enter")
        lock_depth += 1
        try:
            yield
        finally:
            lock_depth -= 1
            lock_events.append("exit")

    def checked_load(*args, **kwargs):
        assert lock_depth in {1, 2}
        return real_load(*args, **kwargs)

    def checked_save(*args, **kwargs):
        assert lock_depth == 1
        return real_save(*args, **kwargs)

    monkeypatch.setattr(auth_module, "_auth_store_lock", recording_lock)
    monkeypatch.setattr(auth_module, "_load_auth_store", checked_load)
    monkeypatch.setattr(auth_module, "_save_auth_store", checked_save)

    changes = module.sync_hermes("personal-backup", dry_run=False)

    saved = json.loads(auth_path.read_text(encoding="utf-8"))
    saved_entry = saved["credential_pool"]["openai-codex"][0]
    assert changes
    assert lock_events == [
        "enter",
        "exit",
        "enter",
        "enter",
        "exit",
        "exit",
    ]
    assert saved_entry["priority"] == 10
    assert saved_entry["access_token"] == "fresh-access-token"
    assert saved_entry["refresh_token"] == "fresh-refresh-token"
    assert saved_entry["generation"] == 7
    other_rows = saved["credential_pool"]["other-provider"]
    assert len(other_rows) == 1
    assert other_rows[0]["opaque"] == "preserve-me"
    assert other_rows[0]["id"].startswith("legacy-")
    assert saved["providers"]["other-provider"] == {"opaque": "still-fresh"}
    assert saved["unrelated_top_level"] == {"sequence": 42}


def test_cliproxy_fixed_uses_management_api_and_disables_fallback(
    monkeypatch, tmp_path
):
    module = load_module()
    api = FakeCLIProxyManagementAPI(cliproxy_rows())
    state_path = configure_cliproxy(monkeypatch, tmp_path, module, api)
    credential_path = tmp_path / "codex-company.json"
    credential_bytes = b'{"access_token":"must-not-be-read-or-rewritten"}\n'
    credential_path.write_bytes(credential_bytes)
    monkeypatch.setattr(module, "CLIPROXY_AUTH_DIR", tmp_path, raising=False)
    monkeypatch.setattr(
        module,
        "load_json",
        lambda *_args: pytest.fail("credential JSON must not be read"),
        raising=False,
    )
    monkeypatch.setattr(
        module,
        "write_json",
        lambda *_args, **_kwargs: pytest.fail("credential JSON must not be written"),
        raising=False,
    )

    changes = module.sync_cliproxy("company-plus-100", dry_run=False, fixed=True)

    assert not [change for change in changes if "failed" in change.lower()]
    assert credential_path.read_bytes() == credential_bytes
    by_id = {row["id"]: row for row in api.rows}
    assert by_id["company-id"]["disabled"] is False
    assert by_id["personal-id"]["disabled"] is True
    assert by_id["company-id"]["priority"] == 100
    assert by_id["personal-id"]["priority"] == 0
    assert api.calls[0]["method"] == "GET"
    assert api.calls[0]["path"] == "/v0/management/auth-files"
    patch_calls = [call for call in api.calls if call["method"] == "PATCH"]
    assert len(patch_calls) == 1
    assert patch_calls[0]["path"] == "/v0/management/auth-files/route"
    assert all(
        call["headers"]["authorization"] == "Bearer test-management-key"
        for call in api.calls
    )
    assert all(
        call["headers"]["content-type"] == "application/json" for call in patch_calls
    )
    assert patch_calls[0]["body"] == {
        "states": [
            {"id": "company-id", "disabled": False, "priority": 100},
            {"id": "personal-id", "disabled": True, "priority": 0},
        ],
        "expected_revision": 0,
    }
    assert [call["method"] for call in api.calls] == [
        "GET",
        "GET",
        "PATCH",
        "GET",
    ]
    assert state_path.stat().st_mode & 0o777 == 0o600
    assert json.loads(state_path.read_text(encoding="utf-8"))["original_states"] == {
        "1": {"disabled": False, "priority": 0},
        "2": {"disabled": False, "priority": 100},
    }


def test_cliproxy_fixed_switch_then_auto_exactly_restores_operator_state(
    monkeypatch, tmp_path
):
    module = load_module()
    api = FakeCLIProxyManagementAPI(
        cliproxy_rows(company_disabled=True, personal_disabled=False)
    )
    state_path = configure_cliproxy(monkeypatch, tmp_path, module, api)

    module.sync_cliproxy("company-plus-100", dry_run=False, fixed=True)
    module.sync_cliproxy("personal-backup", dry_run=False, fixed=True)
    during_fixed = {row["id"]: row["disabled"] for row in api.rows}
    assert during_fixed == {"company-id": True, "personal-id": False}
    assert json.loads(state_path.read_text(encoding="utf-8"))["original_states"] == {
        "1": {"disabled": True, "priority": 0},
        "2": {"disabled": False, "priority": 100},
    }

    changes = module.sync_cliproxy("company-plus-100", dry_run=False, fixed=False)

    assert not [change for change in changes if "failed" in change.lower()]
    restored = {row["id"]: row["disabled"] for row in api.rows}
    assert restored == {"company-id": True, "personal-id": False}
    assert {row["id"]: row["priority"] for row in api.rows} == {
        "company-id": 0,
        "personal-id": 100,
    }
    assert not state_path.exists()


def test_cliproxy_legacy_raw_id_sidecar_fails_closed(monkeypatch, tmp_path):
    module = load_module()
    api = FakeCLIProxyManagementAPI(
        cliproxy_rows(company_disabled=False, personal_disabled=True)
    )
    state_path = configure_cliproxy(monkeypatch, tmp_path, module, api)
    state_path.parent.mkdir(parents=True)
    state_path.write_text(
        json.dumps({
            "version": 1,
            "original_disabled": {
                "company-id": True,
                "personal-id": False,
            },
        }),
        encoding="utf-8",
    )
    state_path.chmod(0o600)
    before = state_path.read_bytes()

    result = module.sync_cliproxy("personal-backup", dry_run=False, fixed=True)

    assert result.status is module.StageStatus.ERROR
    assert result.error.code is module.StageErrorCode.SIDECAR_INVALID
    assert api.rows == cliproxy_rows(company_disabled=False, personal_disabled=True)
    assert state_path.read_bytes() == before


def test_cliproxy_atomic_failure_retains_state_without_partial_change(
    monkeypatch, tmp_path
):
    module = load_module()
    initial = cliproxy_rows()
    api = FakeCLIProxyManagementAPI(initial, fail_patch_number=1)
    state_path = configure_cliproxy(monkeypatch, tmp_path, module, api)

    changes = module.sync_cliproxy("company-plus-100", dry_run=False, fixed=True)

    assert any("failed" in change.lower() for change in changes)
    assert any("HTTP 500" in change for change in changes)
    assert api.rows == initial
    assert len([call for call in api.calls if call["method"] == "PATCH"]) == 1
    assert state_path.exists()
    assert state_path.stat().st_mode & 0o777 == 0o600
    sidecar = json.loads(state_path.read_text(encoding="utf-8"))
    assert sidecar["original_states"] == {
        "1": {"disabled": False, "priority": 0},
        "2": {"disabled": False, "priority": 100},
    }
    assert sidecar["pending"]["expected_revision"] == 0


def test_cliproxy_readback_mismatch_fails_closed_and_retains_state(
    monkeypatch, tmp_path
):
    module = load_module()
    api = FakeCLIProxyManagementAPI(cliproxy_rows())
    original_respond = api.respond

    def mismatching_readback(call):
        response = original_respond(call)
        if call["method"] == "GET" and api.patch_attempts:
            payload = json.loads(json.dumps(response.payload))
            payload["files"][0]["priority"] = 999
            return FakeResponse(response.status, payload)
        return response

    api.respond = mismatching_readback
    state_path = configure_cliproxy(monkeypatch, tmp_path, module, api)

    changes = module.sync_cliproxy("company-plus-100", dry_run=False, fixed=True)

    assert changes.status is module.StageStatus.ERROR
    assert changes.error.code is module.StageErrorCode.READBACK_MISMATCH
    assert changes.token.mutation_outcome is module.CLIProxyMutationOutcome.UNKNOWN
    assert changes == ["CLIProxyAPI sync failed: routing readback mismatch"]
    assert state_path.exists()
    assert [call["method"] for call in api.calls] == [
        "GET",
        "GET",
        "PATCH",
        "GET",
        "GET",
    ]


def test_cliproxy_identical_state_skips_patch_and_revision_churn(monkeypatch, tmp_path):
    module = load_module()
    rows = cliproxy_rows(personal_disabled=True)
    rows[0]["priority"] = 100
    rows[1]["priority"] = 0
    api = FakeCLIProxyManagementAPI(rows)
    state_path = configure_cliproxy(monkeypatch, tmp_path, module, api)

    changes = module.sync_cliproxy("company-plus-100", dry_run=False, fixed=True)

    assert changes == []
    assert [call["method"] for call in api.calls] == ["GET", "GET"]
    assert api.revision == 0
    assert state_path.exists()


def test_cliproxy_auto_atomic_failure_retains_state_and_retry_completes(
    monkeypatch, tmp_path
):
    module = load_module()
    fixed_rows = cliproxy_rows(company_disabled=False, personal_disabled=True)
    fixed_rows[0]["priority"] = 100
    fixed_rows[1]["priority"] = 0
    api = FakeCLIProxyManagementAPI(fixed_rows, fail_patch_number=1)
    state_path = configure_cliproxy(monkeypatch, tmp_path, module, api)
    state_path.parent.mkdir(parents=True)
    state_path.write_text(
        json.dumps({
            "version": 2,
            "source_revision": 0,
            "applied_revision": 0,
            "original_states": {
                "1": {"disabled": True, "priority": 0},
                "2": {"disabled": False, "priority": 100},
            },
            "desired_fixed_states": {
                "1": {"disabled": False, "priority": 100},
                "2": {"disabled": True, "priority": 0},
            },
            "pending": None,
        }),
        encoding="utf-8",
    )
    state_path.chmod(0o600)
    before_retry = json.loads(json.dumps(api.rows))

    first_changes = module.sync_cliproxy("personal-backup", dry_run=False, fixed=False)

    assert any("failed" in change.lower() for change in first_changes)
    assert api.rows == before_retry
    assert state_path.exists()

    retry_changes = module.sync_cliproxy("personal-backup", dry_run=False, fixed=False)

    assert not [change for change in retry_changes if "failed" in change.lower()]
    assert {row["id"]: row["disabled"] for row in api.rows} == {
        "company-id": True,
        "personal-id": False,
    }
    assert {row["id"]: row["priority"] for row in api.rows} == {
        "company-id": 0,
        "personal-id": 100,
    }
    assert not state_path.exists()


def test_cliproxy_missing_key_and_target_fail_closed_before_patch(
    monkeypatch, tmp_path
):
    module = load_module()
    no_key_api = FakeCLIProxyManagementAPI(cliproxy_rows())
    configure_cliproxy(monkeypatch, tmp_path, module, no_key_api, key=None)

    no_key_changes = module.sync_cliproxy("company-plus-100", dry_run=False, fixed=True)

    assert any("key missing" in change.lower() for change in no_key_changes)
    assert no_key_api.calls == []

    second_root = tmp_path / "target-missing"
    second_root.mkdir()
    target_missing_api = FakeCLIProxyManagementAPI([cliproxy_rows()[1]])
    state_path = configure_cliproxy(
        monkeypatch,
        second_root,
        module,
        target_missing_api,
    )
    target_changes = module.sync_cliproxy("company-plus-100", dry_run=False, fixed=True)

    assert target_changes == ["CLIProxyAPI credential inventory mismatch"]
    assert [call["method"] for call in target_missing_api.calls] == ["GET"]
    assert not state_path.exists()


def test_cliproxy_api_failure_never_leaks_key_response_body_or_pii(
    monkeypatch, tmp_path, capsys
):
    module = load_module()
    secret_key = "management-secret-never-print"
    private_body = '{"email":"private-person@example.com","token":"body-secret"}'
    api = FakeCLIProxyManagementAPI(
        cliproxy_rows(),
        get_error_body=private_body,
    )
    configure_cliproxy(monkeypatch, tmp_path, module, api, key=secret_key)

    changes = module.sync_cliproxy("company-plus-100", dry_run=False, fixed=True)

    visible = "\n".join(changes) + capsys.readouterr().out
    assert any("failed" in change.lower() for change in changes)
    assert "HTTP 503" in visible
    assert secret_key not in visible
    assert "private-person@example.com" not in visible
    assert "body-secret" not in visible
    assert "service unavailable" not in visible


def test_cliproxy_unknown_recommendation_does_not_echo_pii():
    module = load_module()
    private_label = "private-person@example.com"

    changes = module.sync_cliproxy(private_label, dry_run=False, fixed=True)

    assert any("mapping unknown" in change.lower() for change in changes)
    assert private_label not in "\n".join(changes)


def test_cliproxy_management_key_prefers_active_then_global_env(monkeypatch, tmp_path):
    module = load_module()
    api = FakeCLIProxyManagementAPI(cliproxy_rows())
    state_path = configure_cliproxy(
        monkeypatch,
        tmp_path,
        module,
        api,
        key="active-key",
    )
    (tmp_path / "global" / ".env").write_text(
        "CLIPROXY_MANAGEMENT_KEY=global-key\n"
        "CLIPROXY_COMPANY_AUTH_ID=company-id\n"
        "CLIPROXY_PERSONAL_AUTH_ID=personal-id\n",
        encoding="utf-8",
    )

    module.sync_cliproxy("company-plus-100", dry_run=True, fixed=True)
    assert api.calls[0]["headers"]["authorization"] == "Bearer active-key"

    (tmp_path / "profile" / ".env").unlink()
    api.calls.clear()
    module.sync_cliproxy("company-plus-100", dry_run=True, fixed=True)
    assert api.calls[0]["headers"]["authorization"] == "Bearer global-key"
    assert not state_path.exists()


def test_cliproxy_exact_id_inventory_and_provider_are_required(monkeypatch, tmp_path):
    module = load_module()
    rows = cliproxy_rows()
    rows.append({
        "id": "unexpected-id",
        "name": "codex-unexpected.json",
        "provider": "codex",
        "label": "private-owner@example.invalid",
        "disabled": False,
        "priority": 50,
    })
    api = FakeCLIProxyManagementAPI(rows)
    state_path = configure_cliproxy(monkeypatch, tmp_path, module, api)

    changes = module.sync_cliproxy("company-plus-100", dry_run=False, fixed=True)

    assert changes == ["CLIProxyAPI credential inventory mismatch"]
    assert [call["method"] for call in api.calls] == ["GET"]
    assert not state_path.exists()
    assert "private-owner@example.invalid" not in "\n".join(changes)


def test_cliproxy_management_transport_rejects_nonloopback_and_redirects(
    monkeypatch,
):
    module = load_module()
    calls = []
    monkeypatch.setattr(
        module,
        "HTTPConnection",
        lambda *_args, **_kwargs: calls.append((_args, _kwargs)),
        raising=False,
    )
    monkeypatch.setattr(
        module,
        "CLIPROXY_MANAGEMENT_URL",
        "http://192.0.2.10:8317",
    )

    with pytest.raises(module.CLIProxyManagementError, match="loopback"):
        module._cliproxy_management_request(
            "GET", "auth-files", key="test-management-key"
        )
    assert calls == []

    api = FakeCLIProxyManagementAPI(cliproxy_rows())

    def redirect(_call):
        return FakeResponse(302, {"location": "http://example.invalid"})

    api.respond = redirect
    monkeypatch.setattr(module, "CLIPROXY_MANAGEMENT_URL", "http://127.0.0.1:8317")
    monkeypatch.setattr(module, "HTTPConnection", api.connection, raising=False)
    with pytest.raises(module.CLIProxyManagementError, match="HTTP 302"):
        module._cliproxy_management_request(
            "GET", "auth-files", key="test-management-key"
        )


def test_cliproxy_auth_ids_are_required_and_distinct(monkeypatch, tmp_path):
    module = load_module()
    profile = tmp_path / "profile"
    global_home = tmp_path / "global"
    profile.mkdir()
    global_home.mkdir()
    monkeypatch.setattr(module, "HERMES_HOME", profile)
    monkeypatch.setattr(module, "GLOBAL_HERMES_HOME", global_home)
    monkeypatch.delenv("CLIPROXY_COMPANY_AUTH_ID", raising=False)
    monkeypatch.delenv("CLIPROXY_PERSONAL_AUTH_ID", raising=False)

    with pytest.raises(module.CLIProxyManagementError, match="auth ID missing"):
        module.load_cliproxy_auth_ids()

    (profile / ".env").write_text(
        "CLIPROXY_COMPANY_AUTH_ID=same-id\nCLIPROXY_PERSONAL_AUTH_ID=same-id\n",
        encoding="utf-8",
    )
    with pytest.raises(module.CLIProxyManagementError, match="auth IDs invalid"):
        module.load_cliproxy_auth_ids()


def test_main_forwards_fixed_mode_to_cliproxy(monkeypatch, tmp_path):
    profile_home = tmp_path / "profile"
    profile_home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(profile_home))
    monkeypatch.setenv("HERMES_CODEX_ROUTE_LOCK_HELD", "1")
    module = load_module()
    monkeypatch.setattr(
        module,
        "collect_payload",
        lambda: {
            "accounts": [
                {
                    "credential_id": "company-id",
                    "label": "company-plus-100",
                    "ok": True,
                }
            ],
            "recommendation": {"label": "personal-backup"},
        },
    )
    monkeypatch.setattr(
        module,
        "load_route_policy",
        lambda: {
            "mode": "fixed",
            "credential_id": "company-id",
            "label": "company-plus-100",
        },
    )
    monkeypatch.setattr(module, "load_state", lambda: {})
    monkeypatch.setattr(
        module,
        "_prepare_hermes",
        lambda *_args, **_kwargs: module.PreparedStage("Hermes", object()),
    )
    calls = []
    monkeypatch.setattr(
        module,
        "_prepare_cliproxy",
        lambda label, *, fixed: (
            calls.append((label, fixed))
            or module.PreparedStage("CLIProxyAPI", object())
        ),
    )
    monkeypatch.setattr(
        module,
        "_prepare_native",
        lambda *_args, **_kwargs: module.PreparedStage("Native Codex", object()),
    )

    assert module.main(["--dry-run"]) == 0
    assert calls == [("company-plus-100", True)]


def configure_native_surface(monkeypatch, tmp_path, module, *, current="personal"):
    command = tmp_path / "codex-account"
    command.write_text("#!/bin/sh\n", encoding="utf-8")
    account_root = tmp_path / "codex" / "accounts"
    for kind in ("company", "personal"):
        auth = account_root / kind / "auth.json"
        auth.parent.mkdir(parents=True)
        auth.write_text("{}\n", encoding="utf-8")
    active_auth = tmp_path / "codex" / "auth.json"
    active_auth.parent.mkdir(exist_ok=True)
    active_auth.symlink_to(account_root / current / "auth.json")
    monkeypatch.setattr(module, "NATIVE_CODEX_ACCOUNT", command)
    monkeypatch.setattr(
        module,
        "NATIVE_CODEX_ACCOUNT_ROOT",
        account_root,
        raising=False,
    )
    monkeypatch.setattr(module, "NATIVE_CODEX_AUTH", active_auth, raising=False)
    return active_auth


def configure_main_transaction(monkeypatch, tmp_path, api, *, current="personal"):
    profile_home = tmp_path / "profile"
    profile_home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(profile_home))
    monkeypatch.setenv("HERMES_CODEX_ROUTE_LOCK_HELD", "1")
    module = load_module()
    configure_cliproxy(monkeypatch, tmp_path, module, api)
    configure_native_surface(monkeypatch, tmp_path, module, current=current)
    monkeypatch.setattr(module, "load_route_policy", lambda: {"mode": "auto"})
    monkeypatch.setattr(module, "load_state", lambda: {})
    monkeypatch.setattr(
        module,
        "collect_payload",
        lambda: {
            "accounts": [
                {
                    "credential_id": "company-id",
                    "label": "company-plus-100",
                    "ok": True,
                },
                {
                    "credential_id": "personal-id",
                    "label": "personal-backup",
                    "ok": True,
                },
            ],
            "recommendation": {
                "credential_id": "company-id",
                "label": "company-plus-100",
                "policy": "7d-reset-aware",
                "reason": "fixture",
            },
        },
    )
    return module


def test_cliproxy_uses_revision_cas_and_writes_slot_only_sidecar(monkeypatch, tmp_path):
    module = load_module()
    api = FakeCLIProxyManagementAPI(cliproxy_rows(), revision=41)
    state_path = configure_cliproxy(monkeypatch, tmp_path, module, api)

    result = module.sync_cliproxy(
        "company-plus-100",
        dry_run=False,
        fixed=True,
    )

    assert result.status is module.StageStatus.APPLIED
    patch = next(call for call in api.calls if call["method"] == "PATCH")
    assert patch["body"]["expected_revision"] == 41
    sidecar = json.loads(state_path.read_text(encoding="utf-8"))
    assert sidecar == {
        "version": 2,
        "source_revision": 41,
        "applied_revision": 42,
        "original_states": {
            "1": {"disabled": False, "priority": 0},
            "2": {"disabled": False, "priority": 100},
        },
        "desired_fixed_states": {
            "1": {"disabled": False, "priority": 100},
            "2": {"disabled": True, "priority": 0},
        },
        "pending": None,
    }
    serialized = state_path.read_text(encoding="utf-8")
    assert state_path.stat().st_mode & 0o777 == 0o600
    for forbidden in (
        "company-id",
        "personal-id",
        "company-plus-100",
        "personal-backup",
        "test-management-key",
    ):
        assert forbidden not in serialized


def test_cliproxy_get_patch_race_is_typed_conflict_without_success(
    monkeypatch, tmp_path
):
    module = load_module()
    initial = cliproxy_rows()
    api = FakeCLIProxyManagementAPI(
        initial,
        revision=7,
        conflict_patch_number=1,
    )
    configure_cliproxy(monkeypatch, tmp_path, module, api)

    result = module.sync_cliproxy(
        "company-plus-100",
        dry_run=False,
        fixed=True,
    )

    assert result.status is module.StageStatus.ERROR
    assert result.error.code is module.StageErrorCode.REVISION_CONFLICT
    assert result.token.mutation_outcome is module.CLIProxyMutationOutcome.NOT_APPLIED
    assert api.rows == initial
    assert api.revision == 8
    assert result.changes == ()


def test_cliproxy_revision_conflict_restores_preapply_sidecar(monkeypatch, tmp_path):
    module = load_module()
    fixed_rows = cliproxy_rows(personal_disabled=True)
    fixed_rows[0]["priority"] = 100
    fixed_rows[1]["priority"] = 0
    externally_changed_rows = cliproxy_rows(
        company_disabled=True,
        personal_disabled=False,
    )
    api = FakeCLIProxyManagementAPI(
        fixed_rows,
        revision=5,
        conflict_patch_number=1,
        conflict_rows=externally_changed_rows,
    )
    state_path = configure_cliproxy(monkeypatch, tmp_path, module, api)
    state_path.parent.mkdir(parents=True)
    state_path.write_text(
        json.dumps({
            "version": 2,
            "source_revision": 4,
            "applied_revision": 5,
            "original_states": {
                "1": {"disabled": False, "priority": 33},
                "2": {"disabled": False, "priority": 44},
            },
            "desired_fixed_states": {
                "1": {"disabled": False, "priority": 100},
                "2": {"disabled": True, "priority": 0},
            },
            "pending": None,
        }),
        encoding="utf-8",
    )
    state_path.chmod(0o600)
    before = state_path.read_bytes()

    result = module.sync_cliproxy(
        "personal-backup",
        dry_run=False,
        fixed=True,
    )

    assert result.status is module.StageStatus.ERROR
    assert result.error.code is module.StageErrorCode.REVISION_CONFLICT
    assert api.rows == externally_changed_rows
    assert state_path.read_bytes() == before


def test_cliproxy_revision_conflict_rebases_existing_fixed_sidecar_only_when_aligned(
    monkeypatch, tmp_path
):
    module = load_module()
    fixed_rows = cliproxy_rows(personal_disabled=True)
    fixed_rows[0]["priority"] = 100
    fixed_rows[1]["priority"] = 0
    api = FakeCLIProxyManagementAPI(
        fixed_rows,
        revision=5,
        conflict_patch_number=1,
    )
    state_path = configure_cliproxy(monkeypatch, tmp_path, module, api)
    state_path.parent.mkdir(parents=True)
    state_path.write_text(
        json.dumps({
            "version": 2,
            "source_revision": 4,
            "applied_revision": 5,
            "original_states": {
                "1": {"disabled": False, "priority": 33},
                "2": {"disabled": False, "priority": 44},
            },
            "desired_fixed_states": {
                "1": {"disabled": False, "priority": 100},
                "2": {"disabled": True, "priority": 0},
            },
            "pending": None,
        }),
        encoding="utf-8",
    )
    state_path.chmod(0o600)

    result = module.sync_cliproxy(
        "personal-backup",
        dry_run=False,
        fixed=True,
    )

    assert result.status is module.StageStatus.ERROR
    assert result.error.code is module.StageErrorCode.REVISION_CONFLICT
    restored = json.loads(state_path.read_text(encoding="utf-8"))
    assert restored["applied_revision"] == 6
    assert restored["pending"] is None
    assert restored["original_states"] == {
        "1": {"disabled": False, "priority": 33},
        "2": {"disabled": False, "priority": 44},
    }


def test_cliproxy_conflict_followup_get_failure_restores_exact_sidecar_without_rebase(
    monkeypatch, tmp_path
):
    module = load_module()
    fixed_rows = cliproxy_rows(personal_disabled=True)
    fixed_rows[0]["priority"] = 100
    fixed_rows[1]["priority"] = 0
    api = FakeCLIProxyManagementAPI(
        fixed_rows,
        revision=5,
        conflict_patch_number=1,
    )
    original_respond = api.respond

    def fail_conflict_followup_get(call):
        if call["method"] == "GET" and api.patch_attempts:
            api.calls.append(call)
            return FakeResponse(503, {"error": "private followup failure"})
        return original_respond(call)

    api.respond = fail_conflict_followup_get
    state_path = configure_cliproxy(monkeypatch, tmp_path, module, api)
    state_path.parent.mkdir(parents=True)
    state_path.write_text(
        json.dumps({
            "version": 2,
            "source_revision": 4,
            "applied_revision": 5,
            "original_states": {
                "1": {"disabled": False, "priority": 33},
                "2": {"disabled": False, "priority": 44},
            },
            "desired_fixed_states": {
                "1": {"disabled": False, "priority": 100},
                "2": {"disabled": True, "priority": 0},
            },
            "pending": None,
        }),
        encoding="utf-8",
    )
    state_path.chmod(0o600)
    before = state_path.read_bytes()

    result = module.sync_cliproxy(
        "personal-backup",
        dry_run=False,
        fixed=True,
    )

    assert result.status is module.StageStatus.ERROR
    assert result.error.code is module.StageErrorCode.REVISION_CONFLICT
    assert result.token.mutation_outcome is module.CLIProxyMutationOutcome.NOT_APPLIED
    assert api.revision == 6
    assert state_path.read_bytes() == before


def test_cliproxy_pending_auto_before_patch_restores_original_and_stops(
    monkeypatch, tmp_path
):
    module = load_module()
    fixed_rows = cliproxy_rows(personal_disabled=True)
    fixed_rows[0]["priority"] = 100
    fixed_rows[1]["priority"] = 0
    api = FakeCLIProxyManagementAPI(fixed_rows, revision=7)
    state_path = configure_cliproxy(monkeypatch, tmp_path, module, api)
    state_path.parent.mkdir(parents=True)
    state_path.write_text(
        json.dumps({
            "version": 2,
            "source_revision": 5,
            "applied_revision": 7,
            "original_states": {
                "1": {"disabled": False, "priority": 33},
                "2": {"disabled": False, "priority": 44},
            },
            "desired_fixed_states": {
                "1": {"disabled": False, "priority": 100},
                "2": {"disabled": True, "priority": 0},
            },
            "pending": {
                "operation": "auto",
                "expected_revision": 7,
                "desired_states": {
                    "1": {"disabled": False, "priority": 33},
                    "2": {"disabled": False, "priority": 44},
                },
            },
        }),
        encoding="utf-8",
    )
    state_path.chmod(0o600)

    result = module.sync_cliproxy(
        "personal-backup",
        dry_run=False,
        fixed=False,
    )

    assert result.ok
    patches = [call for call in api.calls if call["method"] == "PATCH"]
    assert len(patches) == 1
    assert patches[0]["body"]["expected_revision"] == 7
    assert [(row["disabled"], row["priority"]) for row in api.rows] == [
        (False, 33),
        (False, 44),
    ]
    assert not state_path.exists()


def test_cliproxy_pending_auto_after_patch_finalizes_and_stops(monkeypatch, tmp_path):
    module = load_module()
    restored_rows = cliproxy_rows()
    restored_rows[0]["priority"] = 33
    restored_rows[1]["priority"] = 44
    api = FakeCLIProxyManagementAPI(restored_rows, revision=8)
    state_path = configure_cliproxy(monkeypatch, tmp_path, module, api)
    state_path.parent.mkdir(parents=True)
    state_path.write_text(
        json.dumps({
            "version": 2,
            "source_revision": 5,
            "applied_revision": 7,
            "original_states": {
                "1": {"disabled": False, "priority": 33},
                "2": {"disabled": False, "priority": 44},
            },
            "desired_fixed_states": {
                "1": {"disabled": False, "priority": 100},
                "2": {"disabled": True, "priority": 0},
            },
            "pending": {
                "operation": "auto",
                "expected_revision": 7,
                "desired_states": {
                    "1": {"disabled": False, "priority": 33},
                    "2": {"disabled": False, "priority": 44},
                },
            },
        }),
        encoding="utf-8",
    )
    state_path.chmod(0o600)

    result = module.sync_cliproxy(
        "company-plus-100",
        dry_run=False,
        fixed=False,
    )

    assert result.ok
    assert not [call for call in api.calls if call["method"] == "PATCH"]
    assert [(row["disabled"], row["priority"]) for row in api.rows] == [
        (False, 33),
        (False, 44),
    ]
    assert not state_path.exists()


def test_cliproxy_pending_before_patch_retries_with_recorded_revision(
    monkeypatch, tmp_path
):
    module = load_module()
    rows = cliproxy_rows(personal_disabled=True)
    rows[0]["priority"] = 100
    rows[1]["priority"] = 0
    api = FakeCLIProxyManagementAPI(rows, revision=5)
    state_path = configure_cliproxy(monkeypatch, tmp_path, module, api)
    state_path.parent.mkdir(parents=True)
    state_path.write_text(
        json.dumps({
            "version": 2,
            "source_revision": 4,
            "applied_revision": 5,
            "original_states": {
                "1": {"disabled": False, "priority": 0},
                "2": {"disabled": False, "priority": 100},
            },
            "desired_fixed_states": {
                "1": {"disabled": False, "priority": 100},
                "2": {"disabled": True, "priority": 0},
            },
            "pending": {
                "operation": "fixed",
                "expected_revision": 5,
                "desired_states": {
                    "1": {"disabled": True, "priority": 0},
                    "2": {"disabled": False, "priority": 100},
                },
            },
        }),
        encoding="utf-8",
    )
    state_path.chmod(0o600)

    result = module.sync_cliproxy(
        "personal-backup",
        dry_run=False,
        fixed=True,
    )

    assert result.ok
    patches = [call["body"] for call in api.calls if call["method"] == "PATCH"]
    assert [patch["expected_revision"] for patch in patches] == [5]
    sidecar = json.loads(state_path.read_text(encoding="utf-8"))
    assert sidecar["applied_revision"] == 6
    assert sidecar["pending"] is None


def test_cliproxy_auto_restore_crash_finalizes_exact_applied_window(
    monkeypatch, tmp_path
):
    module = load_module()
    rows = cliproxy_rows()
    api = FakeCLIProxyManagementAPI(rows, revision=8)
    state_path = configure_cliproxy(monkeypatch, tmp_path, module, api)
    state_path.parent.mkdir(parents=True)
    state_path.write_text(
        json.dumps({
            "version": 2,
            "source_revision": 5,
            "applied_revision": 7,
            "original_states": {
                "1": {"disabled": False, "priority": 0},
                "2": {"disabled": False, "priority": 100},
            },
            "desired_fixed_states": {
                "1": {"disabled": False, "priority": 100},
                "2": {"disabled": True, "priority": 0},
            },
            "pending": {
                "operation": "auto",
                "expected_revision": 7,
                "desired_states": {
                    "1": {"disabled": False, "priority": 0},
                    "2": {"disabled": False, "priority": 100},
                },
            },
        }),
        encoding="utf-8",
    )
    state_path.chmod(0o600)

    result = module.sync_cliproxy(
        "personal-backup",
        dry_run=False,
        fixed=False,
    )

    assert result.ok
    assert not [call for call in api.calls if call["method"] == "PATCH"]
    assert not state_path.exists()


def test_cliproxy_fixed_to_fixed_to_auto_restores_exact_original_state(
    monkeypatch, tmp_path
):
    module = load_module()
    original = cliproxy_rows(company_disabled=True, personal_disabled=False)
    api = FakeCLIProxyManagementAPI(original, revision=9)
    state_path = configure_cliproxy(monkeypatch, tmp_path, module, api)

    assert module.sync_cliproxy("company-plus-100", dry_run=False, fixed=True).ok
    assert module.sync_cliproxy("personal-backup", dry_run=False, fixed=True).ok
    assert module.sync_cliproxy("company-plus-100", dry_run=False, fixed=False).ok

    assert api.rows == original
    patch_bodies = [call["body"] for call in api.calls if call["method"] == "PATCH"]
    assert [body["expected_revision"] for body in patch_bodies] == [9, 10]
    assert not state_path.exists()


def test_cliproxy_repeated_fixed_crash_recovers_only_exact_revision_and_state(
    monkeypatch, tmp_path
):
    module = load_module()
    rows = cliproxy_rows(company_disabled=True, personal_disabled=False)
    api = FakeCLIProxyManagementAPI(rows, revision=12)
    state_path = configure_cliproxy(monkeypatch, tmp_path, module, api)
    state_path.parent.mkdir(parents=True)
    state_path.write_text(
        json.dumps({
            "version": 2,
            "source_revision": 10,
            "applied_revision": 11,
            "original_states": {
                "1": {"disabled": False, "priority": 0},
                "2": {"disabled": False, "priority": 100},
            },
            "desired_fixed_states": {
                "1": {"disabled": False, "priority": 100},
                "2": {"disabled": True, "priority": 0},
            },
            "pending": {
                "operation": "fixed",
                "expected_revision": 11,
                "desired_states": {
                    "1": {"disabled": True, "priority": 0},
                    "2": {"disabled": False, "priority": 100},
                },
            },
        }),
        encoding="utf-8",
    )
    state_path.chmod(0o600)

    result = module.sync_cliproxy(
        "company-plus-100",
        dry_run=False,
        fixed=True,
    )

    assert result.ok
    patch_bodies = [call["body"] for call in api.calls if call["method"] == "PATCH"]
    assert [body["expected_revision"] for body in patch_bodies] == [12]
    sidecar = json.loads(state_path.read_text(encoding="utf-8"))
    assert sidecar["source_revision"] == 10
    assert sidecar["applied_revision"] == 13
    assert sidecar["pending"] is None
    assert sidecar["original_states"] == {
        "1": {"disabled": False, "priority": 0},
        "2": {"disabled": False, "priority": 100},
    }


@pytest.mark.parametrize("corrupt", [False, True])
def test_cliproxy_stale_or_corrupt_sidecar_fails_closed_without_overwrite(
    monkeypatch, tmp_path, corrupt
):
    module = load_module()
    api = FakeCLIProxyManagementAPI(cliproxy_rows(), revision=4)
    state_path = configure_cliproxy(monkeypatch, tmp_path, module, api)
    state_path.parent.mkdir(parents=True)
    if corrupt:
        before = b'{"version":2,"original_states":{"1":{"token":"secret"}}}'
    else:
        before = json.dumps({
            "version": 2,
            "source_revision": 1,
            "applied_revision": 3,
            "original_states": {
                "1": {"disabled": False, "priority": 0},
                "2": {"disabled": False, "priority": 100},
            },
            "desired_fixed_states": {
                "1": {"disabled": False, "priority": 100},
                "2": {"disabled": True, "priority": 0},
            },
            "pending": None,
        }).encode("utf-8")
    state_path.write_bytes(before)
    state_path.chmod(0o600)

    result = module.sync_cliproxy(
        "company-plus-100",
        dry_run=False,
        fixed=True,
    )

    assert result.status is module.StageStatus.ERROR
    expected = (
        module.StageErrorCode.SIDECAR_INVALID
        if corrupt
        else module.StageErrorCode.STALE_SIDECAR
    )
    assert result.error.code is expected
    assert state_path.read_bytes() == before
    assert [call["method"] for call in api.calls] == ["GET"]


@pytest.mark.parametrize(
    "invalid_case",
    ["desired_fixed", "pending_fixed", "pending_auto"],
)
def test_cliproxy_sidecar_semantic_invariants_reject_before_patch(
    monkeypatch, tmp_path, invalid_case
):
    module = load_module()
    fixed_rows = cliproxy_rows(personal_disabled=True)
    fixed_rows[0]["priority"] = 100
    fixed_rows[1]["priority"] = 0
    api = FakeCLIProxyManagementAPI(fixed_rows, revision=5)
    state_path = configure_cliproxy(monkeypatch, tmp_path, module, api)
    payload = {
        "version": 2,
        "source_revision": 4,
        "applied_revision": 5,
        "original_states": {
            "1": {"disabled": False, "priority": 33},
            "2": {"disabled": False, "priority": 44},
        },
        "desired_fixed_states": {
            "1": {"disabled": False, "priority": 100},
            "2": {"disabled": True, "priority": 0},
        },
        "pending": None,
    }
    if invalid_case == "desired_fixed":
        payload["desired_fixed_states"]["2"]["priority"] = 1
    elif invalid_case == "pending_fixed":
        payload["pending"] = {
            "operation": "fixed",
            "expected_revision": 5,
            "desired_states": {
                "1": {"disabled": False, "priority": 50},
                "2": {"disabled": True, "priority": 50},
            },
        }
    else:
        payload["pending"] = {
            "operation": "auto",
            "expected_revision": 5,
            "desired_states": {
                "1": {"disabled": False, "priority": 33},
                "2": {"disabled": False, "priority": 45},
            },
        }
    state_path.parent.mkdir(parents=True)
    state_path.write_text(json.dumps(payload), encoding="utf-8")
    state_path.chmod(0o600)
    before = state_path.read_bytes()

    result = module.sync_cliproxy(
        "personal-backup",
        dry_run=False,
        fixed=True,
    )

    assert result.status is module.StageStatus.ERROR
    assert result.error.code is module.StageErrorCode.SIDECAR_INVALID
    assert state_path.read_bytes() == before
    assert [call["method"] for call in api.calls] == ["GET"]


def test_main_hermes_preflight_failure_starts_no_surface_mutation(
    monkeypatch, tmp_path
):
    api = FakeCLIProxyManagementAPI(cliproxy_rows())
    module = configure_main_transaction(monkeypatch, tmp_path, api)
    module.HERMES_AUTH.write_text(
        json.dumps({
            "credential_pool": {
                "openai-codex": [
                    {
                        "id": "personal-id",
                        "label": "personal-backup",
                        "priority": 0,
                    }
                ]
            }
        }),
        encoding="utf-8",
    )
    subprocess_calls = []
    monkeypatch.setattr(
        module.subprocess,
        "run",
        lambda *args, **kwargs: subprocess_calls.append((args, kwargs)),
    )

    assert module.main(["--report"]) == 1
    assert not [call for call in api.calls if call["method"] == "PATCH"]
    assert subprocess_calls == []


def test_main_inventory_mismatch_is_nonzero_and_never_mutates_other_surfaces(
    monkeypatch, tmp_path, capsys
):
    rows = cliproxy_rows()
    rows.append({
        "id": "secret-extra-id",
        "provider": "codex",
        "label": "private@example.invalid",
        "disabled": False,
        "priority": 50,
    })
    api = FakeCLIProxyManagementAPI(rows)
    module = configure_main_transaction(monkeypatch, tmp_path, api)
    initial_auth = {
        "credential_pool": {
            "openai-codex": [
                {
                    "id": "company-id",
                    "label": "company-plus-100",
                    "priority": 10,
                },
                {
                    "id": "personal-id",
                    "label": "personal-backup",
                    "priority": 0,
                },
            ]
        }
    }
    module.HERMES_AUTH.write_text(json.dumps(initial_auth), encoding="utf-8")
    subprocess_calls = []
    monkeypatch.setattr(
        module.subprocess,
        "run",
        lambda *args, **kwargs: subprocess_calls.append((args, kwargs)),
    )

    assert module.main(["--report"]) == 1

    output = capsys.readouterr().out
    assert "CLIProxyAPI credential inventory mismatch" in output
    assert "secret-extra-id" not in output
    assert "private@example.invalid" not in output
    restored_auth = json.loads(module.HERMES_AUTH.read_text(encoding="utf-8"))
    assert restored_auth["credential_pool"] == initial_auth["credential_pool"]
    assert not [call for call in api.calls if call["method"] == "PATCH"]
    assert subprocess_calls == []


def test_main_get_patch_race_rolls_back_hermes_and_fails_closed(
    monkeypatch, tmp_path, capsys
):
    original_rows = cliproxy_rows()
    api = FakeCLIProxyManagementAPI(
        original_rows,
        conflict_patch_number=1,
    )
    module = configure_main_transaction(monkeypatch, tmp_path, api)
    initial_auth = {
        "credential_pool": {
            "openai-codex": [
                {
                    "id": "company-id",
                    "label": "company-plus-100",
                    "priority": 10,
                },
                {
                    "id": "personal-id",
                    "label": "personal-backup",
                    "priority": 0,
                },
            ]
        }
    }
    module.HERMES_AUTH.write_text(json.dumps(initial_auth), encoding="utf-8")
    subprocess_calls = []
    monkeypatch.setattr(
        module.subprocess,
        "run",
        lambda *args, **kwargs: subprocess_calls.append((args, kwargs)),
    )

    assert module.main(["--report"]) == 1

    output = capsys.readouterr().out
    restored_auth = json.loads(module.HERMES_AUTH.read_text(encoding="utf-8"))
    assert restored_auth["credential_pool"] == initial_auth["credential_pool"]
    assert api.rows == original_rows
    assert api.revision == 1
    assert "routing revision conflict" in output
    assert "rollback failed" not in output.lower()
    assert "CLIProxyAPI restored" in output
    assert "Hermes restored" in output
    assert "priority 10 -> 0" not in output
    assert subprocess_calls == []
    assert not module.CLIPROXY_FIXED_ROUTE_STATE_PATH.exists()


def test_main_fixed_revision_conflict_rolls_back_without_stale_sidecar(
    monkeypatch, tmp_path, capsys
):
    original_rows = cliproxy_rows()
    api = FakeCLIProxyManagementAPI(
        original_rows,
        conflict_patch_number=1,
    )
    module = configure_main_transaction(monkeypatch, tmp_path, api)
    monkeypatch.setattr(
        module,
        "load_route_policy",
        lambda: {
            "mode": "fixed",
            "credential_id": "company-id",
            "label": "company-plus-100",
        },
    )
    initial_auth = {
        "credential_pool": {
            "openai-codex": [
                {
                    "id": "company-id",
                    "label": "company-plus-100",
                    "priority": 10,
                },
                {
                    "id": "personal-id",
                    "label": "personal-backup",
                    "priority": 0,
                },
            ]
        }
    }
    module.HERMES_AUTH.write_text(json.dumps(initial_auth), encoding="utf-8")
    subprocess_calls = []
    monkeypatch.setattr(
        module.subprocess,
        "run",
        lambda *args, **kwargs: subprocess_calls.append((args, kwargs)),
    )

    assert module.main(["--report"]) == 1

    output = capsys.readouterr().out
    restored_auth = json.loads(module.HERMES_AUTH.read_text(encoding="utf-8"))
    assert restored_auth["credential_pool"] == initial_auth["credential_pool"]
    assert api.rows == original_rows
    assert api.revision == 1
    assert api.patch_attempts == 1
    assert "routing revision conflict" in output
    assert "rollback failed" not in output.lower()
    assert "Hermes restored" in output
    assert "priority 10 -> 0" not in output
    assert subprocess_calls == []
    assert not module.CLIPROXY_FIXED_ROUTE_STATE_PATH.exists()


@pytest.mark.parametrize("rollback_fails", [False, True])
def test_main_native_apply_failure_compensates_in_reverse_order(
    monkeypatch, tmp_path, capsys, rollback_fails
):
    original_rows = cliproxy_rows()
    api = FakeCLIProxyManagementAPI(
        original_rows,
        fail_patch_number=2 if rollback_fails else None,
    )
    module = configure_main_transaction(monkeypatch, tmp_path, api)
    initial_auth = {
        "credential_pool": {
            "openai-codex": [
                {
                    "id": "company-id",
                    "label": "company-plus-100",
                    "priority": 10,
                },
                {
                    "id": "personal-id",
                    "label": "personal-backup",
                    "priority": 0,
                },
            ]
        }
    }
    module.HERMES_AUTH.write_text(json.dumps(initial_auth), encoding="utf-8")

    class Result:
        returncode = 1
        stdout = ""
        stderr = "permission denied for private@example.invalid token=secret"

    monkeypatch.setattr(module.subprocess, "run", lambda *_args, **_kwargs: Result())

    assert module.main(["--report"]) == 1

    output = capsys.readouterr().out
    assert "permission denied" in output
    assert "private@example.invalid" not in output
    assert "secret" not in output
    restored_auth = json.loads(module.HERMES_AUTH.read_text(encoding="utf-8"))
    assert restored_auth["credential_pool"] == initial_auth["credential_pool"]
    patch_bodies = [call["body"] for call in api.calls if call["method"] == "PATCH"]
    assert [body["expected_revision"] for body in patch_bodies] == [0, 1]
    if rollback_fails:
        assert "rollback failed" in output.lower()
        assert api.rows != original_rows
    else:
        assert "rollback failed" not in output.lower()
        assert api.rows == original_rows


def test_standalone_main_collects_before_marking_and_acquiring_host_lock(
    monkeypatch,
):
    module = load_module()
    monkeypatch.delenv("HERMES_CODEX_ROUTE_LOCK_HELD", raising=False)
    events = []

    payload = {
        "accounts": [],
        "recommendation": {},
        "routing": {},
    }

    def fake_collect_payload(*, mutate=True):
        assert mutate is False
        assert "HERMES_CODEX_ROUTE_LOCK_HELD" not in module.os.environ
        events.append("collected")
        return payload

    @contextmanager
    def fake_route_lock(*, path, timeout):
        assert path == module.ROUTE_LOCK_PATH
        assert timeout == module.ROUTE_LOCK_TIMEOUT
        assert "HERMES_CODEX_ROUTE_LOCK_HELD" not in module.os.environ
        events.append("locked")
        yield
        events.append("unlocked")

    def fake_main_unlocked(argv, *, payload=None):
        assert argv == ["--dry-run"]
        assert payload is not None
        assert module.os.environ["HERMES_CODEX_ROUTE_LOCK_HELD"] == "1"
        events.append("ran")
        return 0

    monkeypatch.setattr(module, "collect_payload", fake_collect_payload)
    monkeypatch.setattr(module, "route_lock", fake_route_lock)
    monkeypatch.setattr(module, "_main_unlocked", fake_main_unlocked)

    assert module.main(["--dry-run"]) == 0
    assert events == ["collected", "locked", "ran", "unlocked"]
    assert "HERMES_CODEX_ROUTE_LOCK_HELD" not in module.os.environ


def test_standalone_main_rejects_payload_made_stale_during_collection(
    monkeypatch, capsys
):
    module = load_module()
    monkeypatch.delenv("HERMES_CODEX_ROUTE_LOCK_HELD", raising=False)
    now = [100.0]
    collections = []

    def fake_collect_payload(*, dry_run):
        assert dry_run is True
        collections.append(now[0])
        now[0] += module.COLLECTED_PAYLOAD_MAX_AGE_SECONDS + 1.0
        return {"accounts": [], "recommendation": {}, "routing": {}}

    @contextmanager
    def fake_route_lock(*, path, timeout):
        assert path == module.ROUTE_LOCK_PATH
        assert timeout == module.ROUTE_LOCK_TIMEOUT
        yield

    monkeypatch.setattr(module.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(module, "_collect_payload_for_run", fake_collect_payload)
    monkeypatch.setattr(module, "route_lock", fake_route_lock)
    monkeypatch.setattr(
        module,
        "_main_unlocked",
        lambda *_args, **_kwargs: pytest.fail("stale payload must not apply"),
    )

    assert module.main(["--dry-run"]) == 1
    assert len(collections) == 2
    assert "became stale" in capsys.readouterr().out


def test_collect_payload_skips_unused_history_annotation(monkeypatch):
    module = load_module()
    payload = {"accounts": [], "routing": {}, "recommendation": {}}
    monkeypatch.setattr(module, "collect", lambda **_kwargs: payload)
    assert module.collect_payload() is payload


def test_internal_collect_mode_never_acquires_route_lock(monkeypatch, capsys):
    module = load_module()
    payload = {"accounts": [], "routing": {}, "recommendation": {}}
    monkeypatch.delenv("HERMES_CODEX_ROUTE_LOCK_HELD", raising=False)
    monkeypatch.setattr(module, "collect_payload", lambda **_kwargs: payload)
    monkeypatch.setattr(
        module,
        "route_lock",
        lambda **_kwargs: pytest.fail("internal collection must stay outside lock"),
    )

    assert module.main(["--internal-collect-payload"]) == 0

    assert json.loads(capsys.readouterr().out) == {
        "version": module.INTERNAL_PAYLOAD_VERSION,
        "payload": payload,
    }


def test_internal_payload_stdin_never_recollects(monkeypatch):
    module = load_module()
    payload = {"accounts": [], "routing": {}, "recommendation": {}}
    monkeypatch.setenv("HERMES_CODEX_ROUTE_LOCK_HELD", "1")
    monkeypatch.setattr(
        module.sys,
        "stdin",
        io.StringIO(module._encode_precollected_payload(payload)),
    )
    monkeypatch.setattr(
        module,
        "collect_payload",
        lambda **_kwargs: pytest.fail("supplied payload must not recollect"),
    )

    def fake_main_unlocked(argv, *, payload=None):
        assert argv == ["--internal-payload-stdin"]
        assert payload == {
            "accounts": [],
            "routing": {},
            "recommendation": {},
        }
        return 0

    monkeypatch.setattr(module, "_main_unlocked", fake_main_unlocked)

    assert module.main(["--internal-payload-stdin"]) == 0


def test_internal_payload_stdin_rejects_malformed_before_apply(
    monkeypatch, capsys
):
    module = load_module()
    monkeypatch.setenv("HERMES_CODEX_ROUTE_LOCK_HELD", "1")
    monkeypatch.setattr(module.sys, "stdin", io.StringIO('{"accounts":'))
    monkeypatch.setattr(
        module,
        "_main_unlocked",
        lambda *_args, **_kwargs: pytest.fail(
            "malformed payload must be rejected before surface mutation"
        ),
    )

    assert module.main(["--internal-payload-stdin"]) == 1
    assert "invalid precollected payload" in capsys.readouterr().out.lower()


def test_inherited_host_lock_marker_bypasses_reacquire(monkeypatch):
    module = load_module()
    monkeypatch.setenv("HERMES_CODEX_ROUTE_LOCK_HELD", "1")
    monkeypatch.setattr(
        module,
        "route_lock",
        lambda **_kwargs: pytest.fail("inherited lock must not be reacquired"),
    )
    monkeypatch.setattr(
        module,
        "_main_unlocked",
        lambda argv: 0 if argv == ["--dry-run"] else 1,
    )

    assert module.main(["--dry-run"]) == 0
    assert module.os.environ["HERMES_CODEX_ROUTE_LOCK_HELD"] == "1"
