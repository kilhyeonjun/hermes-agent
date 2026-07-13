"""Reset-aware Codex credential priority sync.

Policy:
- Pick the available Codex account whose 7d/secondary window resets soonest.
- Do not promote credentials that are currently exhausted/dead or whose 5h/7d
  windows are unsafe for immediate routing.
- Hermes uses lower numeric priority first; CLIProxyAPI uses higher priority first.

Prints only when a change/error occurs unless --report is passed.
Never prints tokens or credential material.
"""

from __future__ import annotations

import argparse
from enum import Enum
from http.client import HTTPConnection, HTTPException
from ipaddress import ip_address
import json
import os
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, NamedTuple
from urllib.parse import urlsplit

from agent.redact import redact_sensitive_text

HOME = Path.home()
GLOBAL_HERMES_HOME = HOME / ".hermes"
HERMES_HOME = Path(os.environ.get("HERMES_HOME") or GLOBAL_HERMES_HOME).expanduser()
HERMES_AUTH = HERMES_HOME / "auth.json"
CLIPROXY_MANAGEMENT_URL = "http://127.0.0.1:8317"
CLIPROXY_MANAGEMENT_TIMEOUT = 5.0
CLIPROXY_MANAGEMENT_MAX_RESPONSE_BYTES = 1_000_000
NATIVE_CODEX_ACCOUNT = HOME / ".local" / "bin" / "codex-account"
NATIVE_CODEX_ACCOUNT_ROOT = HOME / ".codex" / "accounts"
NATIVE_CODEX_AUTH = HOME / ".codex" / "auth.json"
STATE_PATH = HERMES_HOME / "state" / "codex_reset_aware_warmup.json"
ROUTE_POLICY_PATH = GLOBAL_HERMES_HOME / "state" / "codex_route_policy.json"
CLIPROXY_FIXED_ROUTE_STATE_PATH = (
    GLOBAL_HERMES_HOME / "state" / "cliproxy_fixed_route_state.json"
)
WARMUP_COOLDOWN_SECONDS = 6 * 60 * 60

from hermes_cli.codex_usage import (
    annotate_usage_trends,
    collect,
    load_history,
    DEFAULT_HISTORY,
)  # noqa: E402
from hermes_cli.codex_route_lock import atomic_write_bytes, route_lock  # noqa: E402

ROUTE_LOCK_TIMEOUT = 30
ROUTE_LOCK_PATH = GLOBAL_HERMES_HOME / "state" / "codex_route.lock"
ROUTE_LABEL_KINDS = {
    "company": "company",
    "company-plus-100": "company",
    "personal": "personal",
    "personal-backup": "personal",
}
ROUTE_KIND_SLOTS = {"company": 1, "personal": 2}
PROFILE_ACCOUNT_LABELS = {
    "company": "company-plus-100",
    "personal": "personal-backup",
}


class StageStatus(Enum):
    READY = "ready"
    NOOP = "noop"
    DRY_RUN = "dry_run"
    APPLIED = "applied"
    ERROR = "error"
    ROLLED_BACK = "rolled_back"


class StageErrorCode(Enum):
    PREFLIGHT = "preflight"
    INVENTORY_MISMATCH = "inventory_mismatch"
    REVISION_CONFLICT = "revision_conflict"
    SIDECAR_INVALID = "sidecar_invalid"
    STALE_SIDECAR = "stale_sidecar"
    APPLY_FAILED = "apply_failed"
    READBACK_MISMATCH = "readback_mismatch"
    ROLLBACK_FAILED = "rollback_failed"


class CLIProxyMutationOutcome(Enum):
    NOT_ATTEMPTED = "not_attempted"
    NOT_APPLIED = "not_applied"
    APPLIED = "applied"
    UNKNOWN = "unknown"


class StageError(NamedTuple):
    code: StageErrorCode
    message: str


class StageResult:
    """Typed stage result; list-like iteration is compatibility-only."""

    def __init__(
        self,
        stage: str,
        status: StageStatus,
        *,
        changes: tuple[str, ...] = (),
        error: StageError | None = None,
        token: Any = None,
    ) -> None:
        self.stage = stage
        self.status = status
        self.changes = tuple(changes)
        self.error = error
        self.token = token

    @property
    def ok(self) -> bool:
        return self.error is None and self.status is not StageStatus.ERROR

    @property
    def messages(self) -> tuple[str, ...]:
        if self.error is not None:
            return (self.error.message,)
        return self.changes

    def __iter__(self):
        return iter(self.messages)

    def __len__(self) -> int:
        return len(self.messages)

    def __getitem__(self, index):
        return self.messages[index]

    def __bool__(self) -> bool:
        return bool(self.messages)

    def __eq__(self, other: object) -> bool:
        if isinstance(other, StageResult):
            return (
                self.stage,
                self.status,
                self.changes,
                self.error,
            ) == (other.stage, other.status, other.changes, other.error)
        if isinstance(other, list):
            return list(self.messages) == other
        return NotImplemented


class PreparedStage(NamedTuple):
    stage: str
    plan: Any | None
    changes: tuple[str, ...] = ()
    error: StageError | None = None

    @property
    def ok(self) -> bool:
        return self.error is None and self.plan is not None


class TransactionResult(NamedTuple):
    stage_results: tuple[StageResult, ...]
    rollback_results: tuple[StageResult, ...]

    @property
    def ok(self) -> bool:
        return all(result.ok for result in self.stage_results) and all(
            result.ok for result in self.rollback_results
        )


class CredentialState(NamedTuple):
    disabled: bool
    priority: int


class CLIProxyRow(NamedTuple):
    credential_id: str
    kind: str
    slot: int
    state: CredentialState


class CLIProxySnapshot(NamedTuple):
    rows: tuple[CLIProxyRow, ...]
    routing_revision: int


class PendingRouteOperation(NamedTuple):
    operation: str
    expected_revision: int
    desired_states: dict[str, CredentialState]


class FixedRouteState(NamedTuple):
    source_revision: int
    applied_revision: int
    original_states: dict[str, CredentialState]
    desired_fixed_states: dict[str, CredentialState]
    pending: PendingRouteOperation | None


class CLIProxyPlan(NamedTuple):
    key: str
    auth_ids: dict[str, str]
    snapshot: CLIProxySnapshot
    sidecar: FixedRouteState | None
    sidecar_content: bytes | None
    wanted_kind: str
    fixed: bool


class CLIProxyApplyToken(NamedTuple):
    plan: CLIProxyPlan
    final_snapshot: CLIProxySnapshot
    rollback_safe: bool
    mutation_outcome: CLIProxyMutationOutcome


class PendingCompletion(NamedTuple):
    snapshot: CLIProxySnapshot
    state: FixedRouteState | None
    metadata_changed: bool
    operation: str | None


class PriorityValue(NamedTuple):
    present: bool
    value: Any


class HermesPlan(NamedTuple):
    identities: tuple[tuple[str, str], ...]
    original_priorities: tuple[PriorityValue, ...]
    desired_priorities: tuple[int, ...]
    changes: tuple[str, ...]


class NativePlan(NamedTuple):
    original_kind: str
    wanted_kind: str
    changes: tuple[str, ...]


def label_kind(label: str) -> str | None:
    return ROUTE_LABEL_KINDS.get(label.strip().lower())


class CLIProxyManagementError(RuntimeError):
    """Redacted management API failure safe for operator-facing output."""

    def __init__(
        self,
        message: str,
        *,
        code: StageErrorCode = StageErrorCode.APPLY_FAILED,
        mutation_outcome: CLIProxyMutationOutcome = CLIProxyMutationOutcome.UNKNOWN,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.mutation_outcome = mutation_outcome


def _stage_failure(
    stage: str,
    code: StageErrorCode,
    message: str,
    *,
    token: Any = None,
) -> StageResult:
    return StageResult(
        stage,
        StageStatus.ERROR,
        error=StageError(code, message),
        token=token,
    )


def _prepared_failure(
    stage: str,
    code: StageErrorCode,
    message: str,
) -> PreparedStage:
    return PreparedStage(stage, None, error=StageError(code, message))


def _slot_for_kind(kind: str) -> int:
    return ROUTE_KIND_SLOTS[kind]


def _safe_native_failure(returncode: int, stdout: str, stderr: str) -> str:
    detail = (stderr or stdout or "").lower()
    category = (
        "permission denied" if "permission denied" in detail else "command failed"
    )
    return f"Native Codex stage failed: {category} (exit {returncode})"


def _dotenv_value(path: Path, name: str) -> str | None:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError):
        return None
    for raw_line in lines:
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line.removeprefix("export ").lstrip()
        key, separator, raw_value = line.partition("=")
        if not separator or key.strip() != name:
            continue
        value = raw_value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        if value:
            return value
    return None


def _load_cliproxy_setting(name: str) -> str | None:
    checked: set[Path] = set()
    for root in (HERMES_HOME, GLOBAL_HERMES_HOME):
        env_path = root / ".env"
        if env_path in checked:
            continue
        checked.add(env_path)
        if value := _dotenv_value(env_path, name):
            return value
    value = os.environ.get(name)
    return value.strip() if value and value.strip() else None


def load_cliproxy_management_key() -> str | None:
    return _load_cliproxy_setting("CLIPROXY_MANAGEMENT_KEY")


def load_cliproxy_auth_ids() -> dict[str, str]:
    values = {
        "company": _load_cliproxy_setting("CLIPROXY_COMPANY_AUTH_ID"),
        "personal": _load_cliproxy_setting("CLIPROXY_PERSONAL_AUTH_ID"),
    }
    if not all(values.values()):
        raise CLIProxyManagementError("auth ID missing")
    normalized = {kind: str(value) for kind, value in values.items()}
    if len(set(normalized.values())) != len(normalized) or any(
        len(value) > 1024
        or value != value.strip()
        or any(ord(character) < 0x20 for character in value)
        for value in normalized.values()
    ):
        raise CLIProxyManagementError("auth IDs invalid")
    return normalized


def _cliproxy_management_origin() -> tuple[str, int]:
    try:
        parsed = urlsplit(CLIPROXY_MANAGEMENT_URL)
        port = parsed.port
        host = parsed.hostname or ""
        address = ip_address(host)
    except (TypeError, ValueError):
        raise CLIProxyManagementError("management URL must be loopback HTTP") from None
    if (
        parsed.scheme != "http"
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in ("", "/")
        or parsed.query
        or parsed.fragment
        or port is None
        or not address.is_loopback
    ):
        raise CLIProxyManagementError("management URL must be loopback HTTP")
    return host, port


def _cliproxy_management_request(
    method: str,
    endpoint: str,
    *,
    key: str,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    body = None
    headers = {
        "Accept": "application/json",
        "Authorization": f"Bearer {key}",
    }
    if payload is not None:
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        headers["Content-Type"] = "application/json"
    clean_endpoint = endpoint.strip("/")
    if not clean_endpoint or any(
        marker in clean_endpoint for marker in ("?", "#", "://")
    ):
        raise CLIProxyManagementError("invalid management endpoint")
    host, port = _cliproxy_management_origin()
    path = "/v0/management/" + clean_endpoint
    connection = None
    try:
        connection = HTTPConnection(
            host,
            port,
            timeout=CLIPROXY_MANAGEMENT_TIMEOUT,
        )
        connection.request(method, path, body=body, headers=headers)
        response = connection.getresponse()
        status = int(response.status)
        raw = response.read(CLIPROXY_MANAGEMENT_MAX_RESPONSE_BYTES + 1)
    except (HTTPException, TimeoutError, OSError, ValueError):
        raise CLIProxyManagementError("transport unavailable") from None
    finally:
        if connection is not None:
            try:
                connection.close()
            except Exception:
                pass
    if status == 409:
        raise CLIProxyManagementError(
            "routing revision conflict (HTTP 409)",
            code=StageErrorCode.REVISION_CONFLICT,
            mutation_outcome=(
                CLIProxyMutationOutcome.NOT_APPLIED
                if method == "PATCH"
                else CLIProxyMutationOutcome.UNKNOWN
            ),
        )
    if status < 200 or status >= 300:
        raise CLIProxyManagementError(f"HTTP {status}")
    if len(raw) > CLIPROXY_MANAGEMENT_MAX_RESPONSE_BYTES:
        raise CLIProxyManagementError("response too large")
    if not raw:
        return {}
    try:
        decoded = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise CLIProxyManagementError("invalid JSON response") from None
    if not isinstance(decoded, dict):
        raise CLIProxyManagementError("invalid response shape")
    return decoded


def _cliproxy_snapshot(
    key: str,
    auth_ids: dict[str, str],
) -> CLIProxySnapshot:
    payload = _cliproxy_management_request("GET", "auth-files", key=key)
    revision = payload.get("routing_revision")
    if isinstance(revision, bool) or not isinstance(revision, int) or revision < 0:
        raise CLIProxyManagementError("auth-files routing revision invalid")
    files = payload.get("files")
    if not isinstance(files, list):
        raise CLIProxyManagementError("auth-files response missing files")
    codex_rows: list[dict[str, Any]] = []
    for raw in files:
        if not isinstance(raw, dict):
            raise CLIProxyManagementError("invalid auth-files entry")
        provider = str(raw.get("provider") or raw.get("type") or "").lower()
        if provider == "codex":
            codex_rows.append(raw)

    expected_ids = set(auth_ids.values())
    present_ids = [str(row.get("id") or "").strip() for row in codex_rows]
    if (
        any(not credential_id for credential_id in present_ids)
        or len(present_ids) != len(set(present_ids))
        or set(present_ids) != expected_ids
    ):
        raise CLIProxyManagementError(
            "credential inventory mismatch",
            code=StageErrorCode.INVENTORY_MISMATCH,
        )

    by_id = {str(row.get("id") or "").strip(): row for row in codex_rows}
    rows: list[CLIProxyRow] = []
    for slot, kind in enumerate(("company", "personal"), start=1):
        credential_id = auth_ids[kind]
        raw = by_id[credential_id]
        disabled = raw.get("disabled")
        if not isinstance(disabled, bool):
            raise CLIProxyManagementError("credential disabled state missing")
        raw_priority = raw.get("priority")
        if (
            isinstance(raw_priority, bool)
            or not isinstance(raw_priority, int)
            or raw_priority < 0
        ):
            raise CLIProxyManagementError("invalid credential priority")
        rows.append(
            CLIProxyRow(
                credential_id,
                kind,
                slot,
                CredentialState(disabled, raw_priority),
            )
        )
    return CLIProxySnapshot(tuple(rows), revision)


def _snapshot_states(snapshot: CLIProxySnapshot) -> dict[str, CredentialState]:
    return {str(row.slot): row.state for row in snapshot.rows}


def _parse_revision(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise CLIProxyManagementError(
            "fixed-route state invalid",
            code=StageErrorCode.SIDECAR_INVALID,
        )
    return value


def _parse_slot_states(value: Any) -> dict[str, CredentialState]:
    if not isinstance(value, dict) or set(value) != {"1", "2"}:
        raise CLIProxyManagementError(
            "fixed-route state invalid",
            code=StageErrorCode.SIDECAR_INVALID,
        )
    states: dict[str, CredentialState] = {}
    for slot in ("1", "2"):
        raw = value.get(slot)
        if not isinstance(raw, dict) or set(raw) != {"disabled", "priority"}:
            raise CLIProxyManagementError(
                "fixed-route state invalid",
                code=StageErrorCode.SIDECAR_INVALID,
            )
        disabled = raw.get("disabled")
        priority = raw.get("priority")
        if (
            not isinstance(disabled, bool)
            or isinstance(priority, bool)
            or not isinstance(priority, int)
            or priority < 0
        ):
            raise CLIProxyManagementError(
                "fixed-route state invalid",
                code=StageErrorCode.SIDECAR_INVALID,
            )
        states[slot] = CredentialState(disabled, priority)
    return states


def _parse_pending(value: Any) -> PendingRouteOperation | None:
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) != {
        "operation",
        "expected_revision",
        "desired_states",
    }:
        raise CLIProxyManagementError(
            "fixed-route state invalid",
            code=StageErrorCode.SIDECAR_INVALID,
        )
    operation = value.get("operation")
    if operation not in {"fixed", "auto"}:
        raise CLIProxyManagementError(
            "fixed-route state invalid",
            code=StageErrorCode.SIDECAR_INVALID,
        )
    return PendingRouteOperation(
        operation,
        _parse_revision(value.get("expected_revision")),
        _parse_slot_states(value.get("desired_states")),
    )


def _is_canonical_fixed_states(states: dict[str, CredentialState]) -> bool:
    return states in (
        {
            "1": CredentialState(False, 100),
            "2": CredentialState(True, 0),
        },
        {
            "1": CredentialState(True, 0),
            "2": CredentialState(False, 100),
        },
    )


def _load_cliproxy_fixed_state_record() -> tuple[FixedRouteState | None, bytes | None]:
    if not CLIPROXY_FIXED_ROUTE_STATE_PATH.exists():
        return None, None
    try:
        if CLIPROXY_FIXED_ROUTE_STATE_PATH.stat().st_mode & 0o777 != 0o600:
            raise CLIProxyManagementError(
                "fixed-route state permissions invalid",
                code=StageErrorCode.SIDECAR_INVALID,
            )
        content = CLIPROXY_FIXED_ROUTE_STATE_PATH.read_bytes()
        payload = json.loads(content.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        raise CLIProxyManagementError(
            "fixed-route state read failed",
            code=StageErrorCode.SIDECAR_INVALID,
        ) from None
    if (
        not isinstance(payload, dict)
        or set(payload)
        != {
            "version",
            "source_revision",
            "applied_revision",
            "original_states",
            "desired_fixed_states",
            "pending",
        }
        or payload.get("version") != 2
    ):
        raise CLIProxyManagementError(
            "fixed-route state invalid",
            code=StageErrorCode.SIDECAR_INVALID,
        )
    state = FixedRouteState(
        _parse_revision(payload.get("source_revision")),
        _parse_revision(payload.get("applied_revision")),
        _parse_slot_states(payload.get("original_states")),
        _parse_slot_states(payload.get("desired_fixed_states")),
        _parse_pending(payload.get("pending")),
    )
    if state.source_revision > state.applied_revision or (
        state.pending is not None
        and state.pending.expected_revision != state.applied_revision
    ):
        raise CLIProxyManagementError(
            "fixed-route state invalid",
            code=StageErrorCode.SIDECAR_INVALID,
        )
    if not _is_canonical_fixed_states(state.desired_fixed_states):
        raise CLIProxyManagementError(
            "fixed-route state invalid",
            code=StageErrorCode.SIDECAR_INVALID,
        )
    if state.pending is not None:
        pending_semantics_valid = (
            state.pending.operation == "fixed"
            and _is_canonical_fixed_states(state.pending.desired_states)
        ) or (
            state.pending.operation == "auto"
            and state.pending.desired_states == state.original_states
        )
        if not pending_semantics_valid:
            raise CLIProxyManagementError(
                "fixed-route state invalid",
                code=StageErrorCode.SIDECAR_INVALID,
            )
    return state, content


def _load_cliproxy_fixed_state() -> FixedRouteState | None:
    state, _content = _load_cliproxy_fixed_state_record()
    return state


def _serialize_slot_states(
    states: dict[str, CredentialState],
) -> dict[str, dict[str, Any]]:
    return {
        slot: {
            "disabled": states[slot].disabled,
            "priority": states[slot].priority,
        }
        for slot in ("1", "2")
    }


def _save_cliproxy_fixed_state(state: FixedRouteState) -> None:
    pending = None
    if state.pending is not None:
        pending = {
            "operation": state.pending.operation,
            "expected_revision": state.pending.expected_revision,
            "desired_states": _serialize_slot_states(state.pending.desired_states),
        }
    payload = {
        "version": 2,
        "source_revision": state.source_revision,
        "applied_revision": state.applied_revision,
        "original_states": _serialize_slot_states(state.original_states),
        "desired_fixed_states": _serialize_slot_states(state.desired_fixed_states),
        "pending": pending,
    }
    content = (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    atomic_write_bytes(CLIPROXY_FIXED_ROUTE_STATE_PATH, content, mode=0o600)


def _sidecar_alignment_error(
    state: FixedRouteState,
    snapshot: CLIProxySnapshot,
) -> StageError | None:
    current = _snapshot_states(snapshot)
    pending = state.pending
    if pending is None:
        aligned = (
            snapshot.routing_revision == state.applied_revision
            and current == state.desired_fixed_states
        )
    elif snapshot.routing_revision == pending.expected_revision:
        aligned = current == state.desired_fixed_states or (
            pending.operation == "fixed"
            and state.source_revision == state.applied_revision
            and current == state.original_states
        )
    elif snapshot.routing_revision == pending.expected_revision + 1:
        aligned = current == pending.desired_states
    else:
        aligned = False
    if aligned:
        return None
    return StageError(
        StageErrorCode.STALE_SIDECAR,
        "CLIProxyAPI fixed-route state is stale; manual review required",
    )


def collect_payload() -> dict[str, Any]:
    payload = collect()
    annotate_usage_trends(payload, history=load_history(DEFAULT_HISTORY))
    return payload


def load_route_policy() -> dict[str, Any]:
    try:
        policy = json.loads(ROUTE_POLICY_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"mode": "auto"}
    except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError):
        return {
            "mode": "invalid",
            "error": "Codex route policy is invalid",
        }
    if not isinstance(policy, dict) or policy.get("mode") not in {"auto", "fixed"}:
        return {
            "mode": "invalid",
            "error": "Codex route policy is invalid",
        }
    if (
        policy.get("mode") == "fixed"
        and not str(policy.get("credential_id") or "").strip()
    ):
        return {
            "mode": "invalid",
            "error": "Codex route policy is invalid",
        }
    return policy


def choose_route_recommendation(payload: dict[str, Any]) -> dict[str, Any]:
    policy = load_route_policy()
    mode = policy.get("mode")
    if mode == "invalid":
        return {
            "policy": "invalid",
            "error": "Codex route policy is invalid; routing blocked",
        }
    if mode == "auto":
        return payload.get("recommendation") or {}
    if mode != "fixed":
        return {
            "policy": "invalid",
            "error": "Codex route policy is invalid; routing blocked",
        }
    credential_id = str(policy.get("credential_id") or "")
    for row in payload.get("accounts") or []:
        if str(row.get("credential_id") or "") != credential_id:
            continue
        if not row.get("ok") or not row.get("available", True):
            break
        return {
            "label": str(row.get("label") or policy.get("label") or credential_id),
            "credential_id": credential_id,
            "reason": "manual fixed route",
            "policy": "fixed",
        }
    return {
        "policy": "fixed",
        "error": "Fixed Codex route unavailable",
    }


def choose_profile_account(
    payload: dict[str, Any], wanted_kind: str
) -> dict[str, Any]:
    """Resolve one canonical account for a profile-local auto-mode affinity."""
    wanted_label = PROFILE_ACCOUNT_LABELS[wanted_kind]
    rows = [
        row
        for row in payload.get("accounts") or []
        if isinstance(row, dict)
        and row.get("ok")
        and row.get("available", True)
        and str(row.get("last_status") or "ok") == "ok"
    ]
    kind_rows = [
        row
        for row in rows
        if label_kind(str(row.get("label") or "")) == wanted_kind
    ]
    canonical_rows = [
        row
        for row in kind_rows
        if str(row.get("label") or "") == wanted_label
    ]
    if len(kind_rows) != 1 or len(canonical_rows) != 1:
        return {
            "policy": "profile-fixed",
            "error": "Profile Codex route unavailable",
        }
    row = canonical_rows[0]
    credential_id = str(row.get("credential_id") or row.get("id") or "").strip()
    if not credential_id:
        return {
            "policy": "profile-fixed",
            "error": "Profile Codex route unavailable",
        }
    return {
        "label": wanted_label,
        "credential_id": credential_id,
        "reason": f"profile account policy: {wanted_kind}",
        "policy": "profile-fixed",
    }


def choose_effective_recommendation(
    payload: dict[str, Any], profile_account: str | None
) -> dict[str, Any]:
    global_recommendation = choose_route_recommendation(payload)
    if (
        global_recommendation.get("policy") in {"fixed", "invalid"}
        or global_recommendation.get("error")
        or not profile_account
    ):
        return global_recommendation
    return choose_profile_account(payload, profile_account)


def is_unstarted_weekly_candidate(row: dict[str, Any]) -> bool:
    if not row.get("ok") or not row.get("available", True):
        return False
    if str(row.get("last_status") or "ok") != "ok":
        return False
    secondary = row.get("secondary_window") or {}
    # The 7d clock is considered unstarted when the usage endpoint cannot give
    # us a secondary reset timestamp yet. Some fresh accounts still show 0% but
    # no reset until their first model call.
    return not secondary.get("reset_at")


def load_state() -> dict[str, Any]:
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_state(state: dict[str, Any], *, dry_run: bool) -> None:
    if dry_run:
        return
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    content = (json.dumps(state, ensure_ascii=False, indent=2) + "\n").encode()
    atomic_write_bytes(STATE_PATH, content)


def warmup_recently_attempted(label: str, state: dict[str, Any]) -> bool:
    raw = ((state.get("warmups") or {}).get(label) or {}).get("attempted_at_epoch")
    if raw is None:
        return False
    try:
        attempted = float(raw)
    except Exception:
        return False
    return time.time() - attempted < WARMUP_COOLDOWN_SECONDS


def choose_unstarted_weekly(
    payload: dict[str, Any], state: dict[str, Any]
) -> dict[str, Any] | None:
    candidates = [
        row for row in payload.get("accounts", []) if is_unstarted_weekly_candidate(row)
    ]
    if not candidates:
        return None
    fresh = [
        row
        for row in candidates
        if not warmup_recently_attempted(str(row.get("label")), state)
    ]
    if not fresh:
        return None
    return min(
        fresh, key=lambda row: (row.get("priority") or 999, str(row.get("label")))
    )


def record_warmup(label: str, *, ok: bool, dry_run: bool) -> None:
    state = load_state()
    warmups = state.setdefault("warmups", {})
    warmups[label] = {
        "attempted_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "attempted_at_epoch": time.time(),
        "ok": ok,
    }
    save_state(state, dry_run=dry_run)


def run_warmup_call(label: str, *, dry_run: bool) -> tuple[bool, str]:
    if dry_run:
        return True, "dry-run"
    # This is intentionally tiny: one real model call is enough to start a fresh
    # 7d quota window, while spending negligible quota.
    cmd = [
        "hermes",
        "--provider",
        "openai-codex",
        "-z",
        "Reply exactly OK.",
        "--ignore-rules",
        "--safe-mode",
    ]
    proc = subprocess.run(cmd, text=True, capture_output=True, timeout=180, shell=False)
    if proc.returncode == 0:
        return True, "warmup call ok"
    detail = (proc.stderr or proc.stdout or "").strip().splitlines()[:2]
    safe_detail = redact_sensitive_text(" | ".join(detail), force=True)
    return False, "warmup call failed: " + safe_detail


def _hermes_pool_state(
    data: dict[str, Any],
) -> tuple[
    list[dict[str, Any]],
    tuple[tuple[str, str], ...],
    tuple[PriorityValue, ...],
]:
    credential_pool = data.get("credential_pool")
    if not isinstance(credential_pool, dict):
        raise ValueError("Hermes openai-codex credential pool missing")
    pool = credential_pool.get("openai-codex")
    if not isinstance(pool, list):
        raise ValueError("Hermes openai-codex credential pool missing")
    if not all(isinstance(entry, dict) for entry in pool):
        raise ValueError("Hermes credential inventory invalid")
    typed_pool = list(pool)
    identities: list[tuple[str, str]] = []
    priorities: list[PriorityValue] = []
    for entry in typed_pool:
        identity = (str(entry.get("id") or ""), str(entry.get("label") or ""))
        identities.append(identity)
        if "priority" not in entry:
            priorities.append(PriorityValue(False, None))
            continue
        priority = entry.get("priority")
        if isinstance(priority, bool) or not isinstance(priority, int):
            raise ValueError("Hermes credential priority invalid")
        priorities.append(PriorityValue(True, priority))
    return typed_pool, tuple(identities), tuple(priorities)


def _prepare_hermes(
    recommended_label: str,
    *,
    recommended_credential_id: str | None = None,
) -> PreparedStage:
    stage = "Hermes"
    wanted_kind = label_kind(recommended_label)
    if wanted_kind is None:
        return _prepared_failure(
            stage,
            StageErrorCode.PREFLIGHT,
            "Hermes recommendation mapping unknown",
        )
    from hermes_cli import auth as auth_store

    try:
        with auth_store._auth_store_lock():
            if not HERMES_AUTH.exists():
                return _prepared_failure(
                    stage,
                    StageErrorCode.PREFLIGHT,
                    "Hermes auth.json missing",
                )
            data = auth_store._load_auth_store(auth_file=HERMES_AUTH)
            pool, identities, priorities = _hermes_pool_state(data)
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return _prepared_failure(
            stage,
            StageErrorCode.PREFLIGHT,
            "Hermes credential inventory invalid",
        )

    if recommended_credential_id:
        id_matches = [
            index
            for index, entry in enumerate(pool)
            if str(entry.get("id") or "") == recommended_credential_id
        ]
        if len(id_matches) > 1:
            return _prepared_failure(
                stage,
                StageErrorCode.PREFLIGHT,
                "Hermes recommendation target duplicate",
            )
        candidates = [
            index
            for index in id_matches
            if label_kind(str(pool[index].get("label") or "")) == wanted_kind
        ]
    else:
        candidates = [
            index
            for index, entry in enumerate(pool)
            if str(entry.get("label") or "") == recommended_label
            and label_kind(str(entry.get("label") or "")) == wanted_kind
        ]
    if not candidates:
        return _prepared_failure(
            stage,
            StageErrorCode.PREFLIGHT,
            "Hermes recommendation target missing",
        )
    if len(candidates) > 1:
        return _prepared_failure(
            stage,
            StageErrorCode.PREFLIGHT,
            "Hermes recommendation target duplicate",
        )
    target = candidates[0]
    desired = tuple(0 if index == target else 10 for index in range(len(pool)))
    changes = tuple(
        f"Hermes credential #{index + 1}: priority "
        f"{priority.value if priority.present else '<unset>'} -> {desired[index]}"
        for index, priority in enumerate(priorities)
        if not priority.present or priority.value != desired[index]
    )
    plan = HermesPlan(identities, priorities, desired, changes)
    return PreparedStage(stage, plan, changes)


def _write_hermes_priorities(
    plan: HermesPlan,
    *,
    expected: tuple[PriorityValue, ...],
    desired: tuple[PriorityValue, ...],
    rollback: bool,
) -> StageResult:
    stage = "Hermes"
    from hermes_cli import auth as auth_store

    try:
        with auth_store._auth_store_lock():
            if not HERMES_AUTH.exists():
                raise ValueError("auth missing")
            data = auth_store._load_auth_store(auth_file=HERMES_AUTH)
            pool, identities, priorities = _hermes_pool_state(data)
            if identities != plan.identities:
                raise RuntimeError("inventory changed")
            if priorities == desired:
                status = StageStatus.ROLLED_BACK if rollback else StageStatus.NOOP
                return StageResult(stage, status, token=plan)
            if priorities != expected:
                raise RuntimeError("priority state changed")
            for index, entry in enumerate(pool):
                next_priority = desired[index]
                if next_priority.present:
                    entry["priority"] = next_priority.value
                else:
                    entry.pop("priority", None)
            auth_store._save_auth_store(data, target_path=HERMES_AUTH)
    except (OSError, TypeError, ValueError, RuntimeError, json.JSONDecodeError):
        code = (
            StageErrorCode.ROLLBACK_FAILED if rollback else StageErrorCode.APPLY_FAILED
        )
        action = "rollback" if rollback else "apply"
        return _stage_failure(
            stage,
            code,
            f"Hermes {action} failed: credential state changed",
            token=plan,
        )
    status = StageStatus.ROLLED_BACK if rollback else StageStatus.APPLIED
    return StageResult(
        stage,
        status,
        changes=() if rollback else plan.changes,
        token=plan,
    )


def _apply_hermes(plan: HermesPlan) -> StageResult:
    desired = tuple(PriorityValue(True, value) for value in plan.desired_priorities)
    return _write_hermes_priorities(
        plan,
        expected=plan.original_priorities,
        desired=desired,
        rollback=False,
    )


def _rollback_hermes(plan: HermesPlan) -> StageResult:
    desired = tuple(PriorityValue(True, value) for value in plan.desired_priorities)
    return _write_hermes_priorities(
        plan,
        expected=desired,
        desired=plan.original_priorities,
        rollback=True,
    )


def sync_hermes(
    recommended_label: str,
    *,
    dry_run: bool,
    recommended_credential_id: str | None = None,
) -> StageResult:
    prepared = _prepare_hermes(
        recommended_label,
        recommended_credential_id=recommended_credential_id,
    )
    if prepared.error is not None:
        return _stage_failure(
            prepared.stage,
            prepared.error.code,
            prepared.error.message,
        )
    if dry_run:
        return StageResult(
            prepared.stage,
            StageStatus.DRY_RUN,
            changes=prepared.changes,
            token=prepared.plan,
        )
    return _apply_hermes(prepared.plan)


def _fixed_desired_states(wanted_kind: str) -> dict[str, CredentialState]:
    wanted_slot = _slot_for_kind(wanted_kind)
    return {
        str(slot): CredentialState(
            slot != wanted_slot, 100 if slot == wanted_slot else 0
        )
        for slot in (1, 2)
    }


def _auto_desired_states(
    snapshot: CLIProxySnapshot,
    wanted_kind: str,
) -> dict[str, CredentialState]:
    wanted_slot = _slot_for_kind(wanted_kind)
    current = _snapshot_states(snapshot)
    return {
        slot: CredentialState(
            state.disabled,
            100 if int(slot) == wanted_slot else 0,
        )
        for slot, state in current.items()
    }


def _cliproxy_changes(
    before: dict[str, CredentialState],
    after: dict[str, CredentialState],
) -> tuple[str, ...]:
    changes: list[str] = []
    for slot in ("1", "2"):
        old = before[slot]
        new = after[slot]
        if old.disabled != new.disabled:
            changes.append(
                f"CLIProxyAPI credential #{slot}: disabled "
                f"{old.disabled} -> {new.disabled}"
            )
        if old.priority != new.priority:
            changes.append(
                f"CLIProxyAPI credential #{slot}: priority "
                f"{old.priority} -> {new.priority}"
            )
    return tuple(changes)


def _logical_sidecar_after_pending(
    state: FixedRouteState,
    applied_revision: int,
) -> FixedRouteState | None:
    pending = state.pending
    if pending is None:
        return state
    if pending.operation == "auto":
        return None
    return FixedRouteState(
        state.source_revision,
        applied_revision,
        state.original_states,
        pending.desired_states,
        None,
    )


def _predicted_cliproxy_states(plan: CLIProxyPlan) -> dict[str, CredentialState]:
    current = _snapshot_states(plan.snapshot)
    state = plan.sidecar
    if state is not None and state.pending is not None:
        current = state.pending.desired_states
        state = _logical_sidecar_after_pending(
            state,
            state.pending.expected_revision + 1,
        )
    if plan.fixed:
        return _fixed_desired_states(plan.wanted_kind)
    if state is not None:
        return state.original_states
    simulated = CLIProxySnapshot(
        tuple(row._replace(state=current[str(row.slot)]) for row in plan.snapshot.rows),
        plan.snapshot.routing_revision,
    )
    return _auto_desired_states(simulated, plan.wanted_kind)


def _prepare_cliproxy(
    recommended_label: str,
    *,
    fixed: bool,
) -> PreparedStage:
    stage = "CLIProxyAPI"
    wanted_kind = label_kind(recommended_label)
    if wanted_kind is None:
        return _prepared_failure(
            stage,
            StageErrorCode.PREFLIGHT,
            "CLIProxyAPI recommendation mapping unknown",
        )
    key = load_cliproxy_management_key()
    if not key:
        return _prepared_failure(
            stage,
            StageErrorCode.PREFLIGHT,
            "CLIProxyAPI management key missing",
        )
    try:
        auth_ids = load_cliproxy_auth_ids()
        snapshot = _cliproxy_snapshot(key, auth_ids)
        sidecar, sidecar_content = _load_cliproxy_fixed_state_record()
    except CLIProxyManagementError as exc:
        if exc.code is StageErrorCode.INVENTORY_MISMATCH:
            message = "CLIProxyAPI credential inventory mismatch"
        elif exc.code is StageErrorCode.SIDECAR_INVALID:
            message = "CLIProxyAPI fixed-route state invalid; manual review required"
        else:
            message = f"CLIProxyAPI management GET failed: {exc}"
        return _prepared_failure(stage, exc.code, message)
    if sidecar is not None:
        alignment_error = _sidecar_alignment_error(sidecar, snapshot)
        if alignment_error is not None:
            return PreparedStage(stage, None, error=alignment_error)
    plan = CLIProxyPlan(
        key,
        auth_ids,
        snapshot,
        sidecar,
        sidecar_content,
        wanted_kind,
        fixed,
    )
    changes = _cliproxy_changes(
        _snapshot_states(snapshot),
        _predicted_cliproxy_states(plan),
    )
    return PreparedStage(stage, plan, changes)


def _cliproxy_patch(
    plan: CLIProxyPlan,
    snapshot: CLIProxySnapshot,
    desired: dict[str, CredentialState],
) -> CLIProxySnapshot:
    states = []
    for row in snapshot.rows:
        state = desired[str(row.slot)]
        states.append({
            "id": row.credential_id,
            "disabled": state.disabled,
            "priority": state.priority,
        })
    response = _cliproxy_management_request(
        "PATCH",
        "auth-files/route",
        key=plan.key,
        payload={
            "states": states,
            "expected_revision": snapshot.routing_revision,
        },
    )
    revision = response.get("routing_revision", response.get("revision"))
    if (
        response.get("status") != "ok"
        or isinstance(revision, bool)
        or not isinstance(revision, int)
        or revision != snapshot.routing_revision + 1
    ):
        raise CLIProxyManagementError("invalid routing response")
    readback = _cliproxy_snapshot(plan.key, plan.auth_ids)
    if readback.routing_revision != revision or _snapshot_states(readback) != desired:
        raise CLIProxyManagementError(
            "routing readback mismatch",
            code=StageErrorCode.READBACK_MISMATCH,
        )
    return readback


def _unlink_cliproxy_sidecar() -> None:
    try:
        CLIPROXY_FIXED_ROUTE_STATE_PATH.unlink()
    except FileNotFoundError:
        return


def _restore_preapply_sidecar(
    plan: CLIProxyPlan,
    observed: CLIProxySnapshot,
    *,
    allow_aligned_rebase: bool,
) -> None:
    state = plan.sidecar
    if state is None:
        _unlink_cliproxy_sidecar()
        return
    if (
        allow_aligned_rebase
        and state.pending is None
        and observed.routing_revision >= state.applied_revision
        and _snapshot_states(observed) == state.desired_fixed_states
    ):
        _save_cliproxy_fixed_state(
            state._replace(applied_revision=observed.routing_revision)
        )
        return
    if plan.sidecar_content is None:
        raise CLIProxyManagementError("preapply fixed-route state unavailable")
    atomic_write_bytes(
        CLIPROXY_FIXED_ROUTE_STATE_PATH,
        plan.sidecar_content,
        mode=0o600,
    )


def _complete_pending_operation(
    plan: CLIProxyPlan,
    snapshot: CLIProxySnapshot,
    state: FixedRouteState,
) -> PendingCompletion:
    pending = state.pending
    if pending is None:
        return PendingCompletion(snapshot, state, False, None)
    if snapshot.routing_revision == pending.expected_revision:
        _save_cliproxy_fixed_state(state)
        snapshot = _cliproxy_patch(plan, snapshot, pending.desired_states)
    finalized = _logical_sidecar_after_pending(
        state,
        snapshot.routing_revision,
    )
    if finalized is None:
        _unlink_cliproxy_sidecar()
    else:
        _save_cliproxy_fixed_state(finalized)
    return PendingCompletion(snapshot, finalized, True, pending.operation)


def _fresh_cliproxy_plan_snapshot(plan: CLIProxyPlan) -> CLIProxySnapshot:
    fresh = _cliproxy_snapshot(plan.key, plan.auth_ids)
    if fresh != plan.snapshot:
        raise CLIProxyManagementError(
            "routing revision conflict",
            code=StageErrorCode.REVISION_CONFLICT,
            mutation_outcome=CLIProxyMutationOutcome.NOT_ATTEMPTED,
        )
    current_sidecar, current_sidecar_content = _load_cliproxy_fixed_state_record()
    if (
        current_sidecar != plan.sidecar
        or current_sidecar_content != plan.sidecar_content
    ):
        raise CLIProxyManagementError(
            "fixed-route state changed",
            code=StageErrorCode.STALE_SIDECAR,
            mutation_outcome=CLIProxyMutationOutcome.NOT_ATTEMPTED,
        )
    return fresh


def _apply_cliproxy(plan: CLIProxyPlan) -> StageResult:
    stage = "CLIProxyAPI"
    last_snapshot = plan.snapshot
    metadata_changed = False
    attempted_revision: int | None = None
    attempted_states: dict[str, CredentialState] | None = None
    mutation_outcome = CLIProxyMutationOutcome.NOT_ATTEMPTED
    try:
        snapshot = _fresh_cliproxy_plan_snapshot(plan)
        state = plan.sidecar
        completed_pending_operation: str | None = None
        if state is not None and state.pending is not None:
            if snapshot.routing_revision == state.pending.expected_revision:
                attempted_revision = snapshot.routing_revision
                attempted_states = state.pending.desired_states
                mutation_outcome = CLIProxyMutationOutcome.UNKNOWN
            completion = _complete_pending_operation(
                plan,
                snapshot,
                state,
            )
            snapshot = completion.snapshot
            state = completion.state
            metadata_changed = completion.metadata_changed
            completed_pending_operation = completion.operation
            if (
                attempted_revision is not None
                and snapshot.routing_revision == attempted_revision + 1
            ):
                mutation_outcome = CLIProxyMutationOutcome.APPLIED
            last_snapshot = snapshot

        current = _snapshot_states(snapshot)
        if completed_pending_operation == "auto" and not plan.fixed:
            pass
        elif plan.fixed:
            desired = _fixed_desired_states(plan.wanted_kind)
            if state is None:
                state = FixedRouteState(
                    snapshot.routing_revision,
                    snapshot.routing_revision,
                    current,
                    desired,
                    None,
                )
            if current == desired:
                finalized = FixedRouteState(
                    state.source_revision,
                    snapshot.routing_revision,
                    state.original_states,
                    desired,
                    None,
                )
                if finalized != state or not CLIPROXY_FIXED_ROUTE_STATE_PATH.exists():
                    _save_cliproxy_fixed_state(finalized)
                    metadata_changed = True
                state = finalized
            else:
                pending = PendingRouteOperation(
                    "fixed",
                    snapshot.routing_revision,
                    desired,
                )
                state = state._replace(pending=pending)
                _save_cliproxy_fixed_state(state)
                metadata_changed = True
                attempted_revision = snapshot.routing_revision
                attempted_states = desired
                mutation_outcome = CLIProxyMutationOutcome.UNKNOWN
                snapshot = _cliproxy_patch(plan, snapshot, desired)
                mutation_outcome = CLIProxyMutationOutcome.APPLIED
                last_snapshot = snapshot
                state = FixedRouteState(
                    state.source_revision,
                    snapshot.routing_revision,
                    state.original_states,
                    desired,
                    None,
                )
                _save_cliproxy_fixed_state(state)
        elif state is not None:
            desired = state.original_states
            if current != desired:
                pending = PendingRouteOperation(
                    "auto",
                    snapshot.routing_revision,
                    desired,
                )
                state = state._replace(pending=pending)
                _save_cliproxy_fixed_state(state)
                metadata_changed = True
                attempted_revision = snapshot.routing_revision
                attempted_states = desired
                mutation_outcome = CLIProxyMutationOutcome.UNKNOWN
                snapshot = _cliproxy_patch(plan, snapshot, desired)
                mutation_outcome = CLIProxyMutationOutcome.APPLIED
                last_snapshot = snapshot
            _unlink_cliproxy_sidecar()
            metadata_changed = True
        else:
            desired = _auto_desired_states(snapshot, plan.wanted_kind)
            if current != desired:
                attempted_revision = snapshot.routing_revision
                attempted_states = desired
                mutation_outcome = CLIProxyMutationOutcome.UNKNOWN
                snapshot = _cliproxy_patch(plan, snapshot, desired)
                mutation_outcome = CLIProxyMutationOutcome.APPLIED
                last_snapshot = snapshot
    except (CLIProxyManagementError, OSError) as exc:
        observed_verified = True
        try:
            observed = _cliproxy_snapshot(plan.key, plan.auth_ids)
        except CLIProxyManagementError:
            observed = last_snapshot
            observed_verified = False
        if (
            isinstance(exc, CLIProxyManagementError)
            and exc.mutation_outcome is not CLIProxyMutationOutcome.UNKNOWN
        ):
            mutation_outcome = exc.mutation_outcome
        if mutation_outcome is CLIProxyMutationOutcome.NOT_APPLIED:
            try:
                _restore_preapply_sidecar(
                    plan,
                    observed,
                    allow_aligned_rebase=observed_verified,
                )
            except (CLIProxyManagementError, OSError):
                mutation_outcome = CLIProxyMutationOutcome.UNKNOWN
        rollback_safe = observed in {plan.snapshot, last_snapshot} or (
            attempted_revision is not None
            and attempted_states is not None
            and observed.routing_revision == attempted_revision + 1
            and _snapshot_states(observed) == attempted_states
        )
        if mutation_outcome in {
            CLIProxyMutationOutcome.NOT_ATTEMPTED,
            CLIProxyMutationOutcome.NOT_APPLIED,
        }:
            rollback_safe = True
        token = CLIProxyApplyToken(
            plan,
            observed,
            rollback_safe,
            mutation_outcome,
        )
        if isinstance(exc, CLIProxyManagementError):
            code = exc.code
            detail = str(exc)
        else:
            code = StageErrorCode.APPLY_FAILED
            detail = type(exc).__name__
        return _stage_failure(
            stage,
            code,
            f"CLIProxyAPI sync failed: {detail}",
            token=token,
        )
    changes = _cliproxy_changes(
        _snapshot_states(plan.snapshot),
        _snapshot_states(last_snapshot),
    )
    status = StageStatus.APPLIED if changes or metadata_changed else StageStatus.NOOP
    return StageResult(
        stage,
        status,
        changes=changes,
        token=CLIProxyApplyToken(
            plan,
            last_snapshot,
            True,
            mutation_outcome,
        ),
    )


def _rollback_sidecar_state(
    plan: CLIProxyPlan,
    revision: int,
) -> FixedRouteState | None:
    state = plan.sidecar
    if state is None:
        return None
    pending = state.pending
    if pending is not None and (
        plan.snapshot.routing_revision == pending.expected_revision + 1
    ):
        if pending.operation == "auto":
            return None
        return FixedRouteState(
            state.source_revision,
            revision,
            state.original_states,
            _snapshot_states(plan.snapshot),
            None,
        )
    return FixedRouteState(
        state.source_revision,
        revision,
        state.original_states,
        _snapshot_states(plan.snapshot),
        None,
    )


def _rollback_cliproxy(
    plan: CLIProxyPlan,
    result: StageResult,
) -> StageResult:
    stage = "CLIProxyAPI"
    try:
        token = result.token
        if not isinstance(token, CLIProxyApplyToken):
            raise CLIProxyManagementError("rollback state unavailable")
        current = _cliproxy_snapshot(plan.key, plan.auth_ids)
        if token.mutation_outcome in {
            CLIProxyMutationOutcome.NOT_ATTEMPTED,
            CLIProxyMutationOutcome.NOT_APPLIED,
        }:
            _restore_preapply_sidecar(
                plan,
                current,
                allow_aligned_rebase=(
                    token.mutation_outcome is CLIProxyMutationOutcome.NOT_APPLIED
                ),
            )
            return StageResult(stage, StageStatus.ROLLED_BACK, token=result.token)
        original_states = _snapshot_states(plan.snapshot)
        if current == plan.snapshot:
            restored_revision = current.routing_revision
        else:
            if not token.rollback_safe:
                raise CLIProxyManagementError(
                    "rollback revision conflict",
                    code=StageErrorCode.REVISION_CONFLICT,
                )
            if current != token.final_snapshot:
                raise CLIProxyManagementError(
                    "rollback revision conflict",
                    code=StageErrorCode.REVISION_CONFLICT,
                )
            current = _cliproxy_patch(plan, current, original_states)
            restored_revision = current.routing_revision
        sidecar = _rollback_sidecar_state(plan, restored_revision)
        if sidecar is None:
            _unlink_cliproxy_sidecar()
        else:
            _save_cliproxy_fixed_state(sidecar)
    except (CLIProxyManagementError, OSError) as exc:
        detail = (
            str(exc) if isinstance(exc, CLIProxyManagementError) else type(exc).__name__
        )
        return _stage_failure(
            stage,
            StageErrorCode.ROLLBACK_FAILED,
            f"CLIProxyAPI rollback failed: {detail}",
            token=result.token,
        )
    return StageResult(stage, StageStatus.ROLLED_BACK, token=result.token)


def sync_cliproxy(
    recommended_label: str,
    *,
    dry_run: bool,
    fixed: bool = False,
) -> StageResult:
    prepared = _prepare_cliproxy(recommended_label, fixed=fixed)
    if prepared.error is not None:
        return _stage_failure(
            prepared.stage,
            prepared.error.code,
            prepared.error.message,
        )
    if dry_run:
        return StageResult(
            prepared.stage,
            StageStatus.DRY_RUN,
            changes=prepared.changes,
            token=prepared.plan,
        )
    return _apply_cliproxy(prepared.plan)


def _native_account_path(kind: str) -> Path:
    return NATIVE_CODEX_ACCOUNT_ROOT / kind / "auth.json"


def _native_current_kind() -> str | None:
    try:
        if not NATIVE_CODEX_AUTH.is_symlink():
            return None
        resolved = NATIVE_CODEX_AUTH.resolve(strict=False)
    except OSError:
        return None
    for kind in ("company", "personal"):
        if resolved == _native_account_path(kind).resolve(strict=False):
            return kind
    return None


def _prepare_native(recommended_label: str) -> PreparedStage:
    stage = "Native Codex"
    wanted_kind = label_kind(recommended_label)
    if wanted_kind is None:
        return _prepared_failure(
            stage,
            StageErrorCode.PREFLIGHT,
            "Native Codex recommendation mapping unknown",
        )
    if not NATIVE_CODEX_ACCOUNT.exists():
        return _prepared_failure(
            stage,
            StageErrorCode.PREFLIGHT,
            "Native Codex account command missing",
        )
    current = _native_current_kind()
    if current is None:
        return _prepared_failure(
            stage,
            StageErrorCode.PREFLIGHT,
            "Native Codex active account invalid",
        )
    if not _native_account_path(wanted_kind).exists():
        return _prepared_failure(
            stage,
            StageErrorCode.PREFLIGHT,
            "Native Codex target account missing",
        )
    changes = (
        ()
        if current == wanted_kind
        else (f"Native Codex credential #{_slot_for_kind(wanted_kind)} activated",)
    )
    return PreparedStage(
        stage,
        NativePlan(current, wanted_kind, changes),
        changes,
    )


def _activate_native(
    plan: NativePlan,
    *,
    target: str,
    expected: str,
    rollback: bool,
) -> StageResult:
    stage = "Native Codex"
    current = _native_current_kind()
    if current == target:
        status = StageStatus.ROLLED_BACK if rollback else StageStatus.NOOP
        return StageResult(stage, status, token=plan)
    if current != expected:
        action = "rollback" if rollback else "apply"
        code = (
            StageErrorCode.ROLLBACK_FAILED if rollback else StageErrorCode.APPLY_FAILED
        )
        return _stage_failure(
            stage,
            code,
            f"Native Codex {action} failed: active account changed",
            token=plan,
        )
    try:
        proc = subprocess.run(
            [str(NATIVE_CODEX_ACCOUNT), "activate", target],
            text=True,
            capture_output=True,
            timeout=30,
            shell=False,
        )
    except (OSError, subprocess.SubprocessError):
        proc = None
    if proc is None or proc.returncode != 0:
        code = (
            StageErrorCode.ROLLBACK_FAILED if rollback else StageErrorCode.APPLY_FAILED
        )
        if proc is None:
            message = f"Native Codex {'rollback' if rollback else 'stage'} failed: command failed"
        else:
            message = _safe_native_failure(
                int(proc.returncode),
                str(proc.stdout or ""),
                str(proc.stderr or ""),
            )
            if rollback:
                message = message.replace("stage failed", "rollback failed")
        return _stage_failure(stage, code, message, token=plan)
    if _native_current_kind() != target:
        code = (
            StageErrorCode.ROLLBACK_FAILED
            if rollback
            else StageErrorCode.READBACK_MISMATCH
        )
        action = "rollback" if rollback else "apply"
        return _stage_failure(
            stage,
            code,
            f"Native Codex {action} failed: account readback mismatch",
            token=plan,
        )
    status = StageStatus.ROLLED_BACK if rollback else StageStatus.APPLIED
    return StageResult(
        stage,
        status,
        changes=() if rollback else plan.changes,
        token=plan,
    )


def _apply_native(plan: NativePlan) -> StageResult:
    return _activate_native(
        plan,
        target=plan.wanted_kind,
        expected=plan.original_kind,
        rollback=False,
    )


def _rollback_native(plan: NativePlan) -> StageResult:
    return _activate_native(
        plan,
        target=plan.original_kind,
        expected=plan.wanted_kind,
        rollback=True,
    )


def sync_native_codex(recommended_label: str, *, dry_run: bool) -> StageResult:
    prepared = _prepare_native(recommended_label)
    if prepared.error is not None:
        return _stage_failure(
            prepared.stage,
            prepared.error.code,
            prepared.error.message,
        )
    if dry_run:
        return StageResult(
            prepared.stage,
            StageStatus.DRY_RUN,
            changes=prepared.changes,
            token=prepared.plan,
        )
    return _apply_native(prepared.plan)


def _apply_prepared_stage(prepared: PreparedStage) -> StageResult:
    plan = prepared.plan
    if isinstance(plan, HermesPlan):
        return _apply_hermes(plan)
    if isinstance(plan, CLIProxyPlan):
        return _apply_cliproxy(plan)
    if isinstance(plan, NativePlan):
        return _apply_native(plan)
    return _stage_failure(
        prepared.stage,
        StageErrorCode.APPLY_FAILED,
        f"{prepared.stage} apply failed: invalid plan",
    )


def _rollback_prepared_stage(
    prepared: PreparedStage,
    result: StageResult,
) -> StageResult:
    plan = prepared.plan
    if isinstance(plan, HermesPlan):
        return _rollback_hermes(plan)
    if isinstance(plan, CLIProxyPlan):
        return _rollback_cliproxy(plan, result)
    if isinstance(plan, NativePlan):
        return _rollback_native(plan)
    return _stage_failure(
        prepared.stage,
        StageErrorCode.ROLLBACK_FAILED,
        f"{prepared.stage} rollback failed: invalid plan",
    )


def _execute_transaction(
    prepared_stages: list[PreparedStage],
    *,
    dry_run: bool,
) -> TransactionResult:
    if dry_run:
        results = tuple(
            StageResult(
                prepared.stage,
                StageStatus.DRY_RUN,
                changes=prepared.changes,
                token=prepared.plan,
            )
            for prepared in prepared_stages
        )
        return TransactionResult(results, ())

    successful: list[tuple[PreparedStage, StageResult]] = []
    results: list[StageResult] = []
    for prepared in prepared_stages:
        result = _apply_prepared_stage(prepared)
        results.append(result)
        if result.ok:
            successful.append((prepared, result))
            continue
        rollback_results: list[StageResult] = []
        candidates = [(prepared, result), *reversed(successful)]
        for rollback_prepared, rollback_source in candidates:
            rollback_results.append(
                _rollback_prepared_stage(rollback_prepared, rollback_source)
            )
        return TransactionResult(tuple(results), tuple(rollback_results))
    return TransactionResult(tuple(results), ())


def _rollback_completed_transaction(
    prepared_stages: list[PreparedStage],
    stage_results: tuple[StageResult, ...],
) -> tuple[StageResult, ...]:
    completed = list(zip(prepared_stages, stage_results, strict=True))
    return tuple(
        _rollback_prepared_stage(prepared, result)
        for prepared, result in reversed(completed)
    )


def _main_unlocked(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Sync Codex priorities from 7d-reset-aware Hermes recommendation"
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--report",
        action="store_true",
        help="print current decision even if nothing changed",
    )
    parser.add_argument("--skip-hermes", action="store_true")
    parser.add_argument("--skip-cliproxy", action="store_true")
    parser.add_argument(
        "--profile-account",
        choices=("personal", "company"),
        help="pin this profile while global codex-route mode is auto; an explicit global fixed route still wins",
    )
    parser.add_argument(
        "--warmup-unstarted",
        action="store_true",
        help="if an available Codex account has no 7d reset timestamp yet, promote it and make one tiny model call to start the 7d window",
    )
    args = parser.parse_args(argv)

    if args.profile_account and (args.skip_hermes or not args.skip_cliproxy):
        print("profile-account requires Hermes-only profile sync")
        return 1

    payload = collect_payload()
    state = load_state()
    policy_recommendation = choose_effective_recommendation(
        payload, args.profile_account
    )
    route_error = str(policy_recommendation.get("error") or "")
    if route_error:
        print(route_error)
        return 1
    fixed_route = policy_recommendation.get("policy") in {
        "fixed",
        "profile-fixed",
    }
    warmup_row = (
        choose_unstarted_weekly(payload, state)
        if args.warmup_unstarted and not fixed_route and not args.skip_hermes
        else None
    )
    warmup_note = ""
    if fixed_route:
        recommendation = policy_recommendation
    elif warmup_row is not None:
        recommendation = {
            "label": str(warmup_row.get("label") or ""),
            "credential_id": str(warmup_row.get("credential_id") or ""),
            "reason": "7d reset timestamp missing/unstarted; warm-up call should start the weekly window",
            "policy": "7d-reset-aware-warmup",
        }
    else:
        recommendation = policy_recommendation
    label = str(recommendation.get("label") or "")
    if not label:
        # Silent when no recommendation — watchdog pattern: empty stdout = healthy.
        return 0
    if label_kind(label) is None:
        print("Codex recommendation mapping unknown; routing blocked")
        return 1
    recommended_credential_id = (
        str(recommendation.get("credential_id") or "").strip() or None
    )

    prepared_stages: list[PreparedStage] = []
    if not args.skip_hermes:
        prepared_stages.append(
            _prepare_hermes(
                label,
                recommended_credential_id=recommended_credential_id,
            )
        )
    if not args.skip_cliproxy:
        prepared_stages.append(_prepare_cliproxy(label, fixed=fixed_route))
        prepared_stages.append(_prepare_native(label))

    preflight_errors = [
        prepared.error for prepared in prepared_stages if prepared.error is not None
    ]
    transaction = TransactionResult((), ())
    if not preflight_errors:
        transaction = _execute_transaction(
            prepared_stages,
            dry_run=args.dry_run,
        )

    warmup_result: StageResult | None = None
    if warmup_row is not None:
        if preflight_errors or not transaction.ok:
            warmup_result = None
        else:
            try:
                ok, _warmup_note = run_warmup_call(
                    label,
                    dry_run=args.dry_run,
                )
                record_warmup(label, ok=ok, dry_run=args.dry_run)
            except (OSError, RuntimeError, ValueError, subprocess.SubprocessError):
                ok = False
            if ok:
                warmup_result = StageResult(
                    "Warm-up",
                    StageStatus.DRY_RUN if args.dry_run else StageStatus.APPLIED,
                    changes=(
                        f"Warm-up credential #{_slot_for_kind(label_kind(label))} complete",
                    ),
                )
            else:
                warmup_result = _stage_failure(
                    "Warm-up",
                    StageErrorCode.APPLY_FAILED,
                    "Warm-up stage failed",
                )
                if not args.dry_run:
                    transaction = TransactionResult(
                        transaction.stage_results,
                        _rollback_completed_transaction(
                            prepared_stages,
                            transaction.stage_results,
                        ),
                    )

    stage_errors = [
        result.error for result in transaction.stage_results if result.error is not None
    ]
    rollback_errors = [
        result.error
        for result in transaction.rollback_results
        if result.error is not None
    ]
    if warmup_result is not None and warmup_result.error is not None:
        stage_errors.append(warmup_result.error)
    failed = bool(preflight_errors or stage_errors or rollback_errors)
    rollback_attempted_stages = {
        result.stage for result in transaction.rollback_results
    }
    restored_stages = [
        result.stage
        for result in transaction.rollback_results
        if result.status is StageStatus.ROLLED_BACK and result.error is None
    ]
    successful_changes = [
        change
        for result in transaction.stage_results
        if result.ok and result.stage not in rollback_attempted_stages
        for change in result.changes
    ]
    if warmup_result is not None and warmup_result.ok:
        successful_changes.extend(warmup_result.changes)

    # This is an automatic routing loop. Normal 5h/7d quota movement can change
    # the chosen account several times per day, so routine priority changes are
    # intentionally silent. ``--report`` remains the explicit human-facing
    # status view; only operational errors page the cron destination.
    if args.report or failed:
        if failed:
            mode = "FAILED"
        else:
            mode = "DRY-RUN" if args.dry_run else "APPLIED"
        print(f"## Codex 7d reset-aware priority sync · {mode}")
        print(f"- 추천 슬롯: #{_slot_for_kind(label_kind(label))}")
        print(f"- 정책: {'fixed' if fixed_route else 'auto'}")
        if recommendation.get("blocked"):
            print("- 제외: 일부 계정은 안전 기준으로 제외됨")
        visible_changes = list(successful_changes) if args.report else []
        visible_changes.extend(f"{stage} restored" for stage in restored_stages)
        visible_errors = [
            error.message
            for error in (*preflight_errors, *stage_errors, *rollback_errors)
        ]
        if visible_changes or visible_errors:
            for change in visible_changes:
                print(f"- {change}")
            for message in visible_errors:
                print(f"- {message}")
        else:
            print("- 변경 없음")
    return 1 if failed else 0


def main(argv: list[str] | None = None) -> int:
    if os.environ.get("HERMES_CODEX_ROUTE_LOCK_HELD") == "1":
        return _main_unlocked(argv)
    try:
        with route_lock(path=ROUTE_LOCK_PATH, timeout=ROUTE_LOCK_TIMEOUT):
            return _main_unlocked(argv)
    except TimeoutError as exc:
        print(f"Codex route sync skipped: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
