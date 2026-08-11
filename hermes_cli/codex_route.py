"""Control the global Codex routing policy and apply it to every Hermes profile."""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import json
import os
import stat
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from agent.redact import redact_sensitive_text
from hermes_cli.codex_route_lock import atomic_write_bytes, route_lock

HOME = Path.home()
HERMES_HOME = HOME / ".hermes"
DEFAULT_AUTH = HERMES_HOME / "auth.json"
POLICY_PATH = HERMES_HOME / "state" / "codex_route_policy.json"
NATIVE_CODEX_AUTH = HOME / ".codex" / "auth.json"
PRIORITY_SYNC_MODULE = "hermes_cli.codex_priority_sync"
PROFILE_SYNC_TIMEOUT = 180
ROUTE_COMMAND_TIMEOUT = 2 * PROFILE_SYNC_TIMEOUT + 30
ROUTE_LOCK_TIMEOUT = 30
COLLECTED_PAYLOAD_MAX_AGE_SECONDS = 15.0
INTERNAL_PAYLOAD_VERSION = 1
INTERNAL_PAYLOAD_MAX_BYTES = 1024 * 1024

AUTO_ALIASES = {"auto", "recommended", "recommend", "추천", "자동"}
PERSONAL_ALIASES = {"personal", "personal-backup", "개인", "개인계정"}
COMPANY_ALIASES = {"company", "company-plus-100", "회사", "회사계정"}
CANONICAL_LABELS = {
    "personal": "personal",
    "company": "company",
}
ACCOUNT_LABEL_KINDS = {
    "personal-backup": "personal",
    "company-plus-100": "company",
}


def canonical_label(kind: str) -> str:
    return CANONICAL_LABELS.get(kind, kind)


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def credential_rows(auth_path: Path) -> list[dict[str, Any]]:
    try:
        data = load_json(auth_path)
    except Exception:
        return []
    rows = data.get("credential_pool", {}).get("openai-codex", [])
    return [row for row in rows if isinstance(row, dict)]


def _kind(row: dict[str, Any]) -> str | None:
    label = str(row.get("label") or "").strip().lower()
    return ACCOUNT_LABEL_KINDS.get(label)


def resolve_policy(mode: str, auth_path: Path = DEFAULT_AUTH) -> dict[str, Any]:
    normalized = (mode or "status").strip().lower()
    if normalized in AUTO_ALIASES:
        return {"mode": "auto"}
    if normalized in PERSONAL_ALIASES:
        wanted = "personal"
    elif normalized in COMPANY_ALIASES:
        wanted = "company"
    else:
        raise ValueError("지원 모드: auto | personal | company")

    rows = credential_rows(auth_path)
    seen_ids: set[str] = set()
    for item in rows:
        candidate_id = str(item.get("id") or "").strip()
        if not candidate_id:
            continue
        if candidate_id in seen_ids:
            raise ValueError("Codex credential ID가 중복되었습니다")
        seen_ids.add(candidate_id)
    matches = [item for item in rows if _kind(item) == wanted]
    if not matches:
        raise ValueError(f"{wanted} Codex 계정을 찾지 못했습니다")
    if len(matches) != 1:
        raise ValueError(f"{wanted} Codex 계정 매핑이 둘 이상입니다")
    row = matches[0]
    credential_id = str(row.get("id") or "").strip()
    if not credential_id:
        raise ValueError(f"{wanted} Codex credential ID가 없습니다")
    return {
        "mode": "fixed",
        "credential_id": credential_id,
        "label": str(row.get("label") or wanted),
        "kind": wanted,
    }


def load_policy() -> dict[str, Any]:
    try:
        policy = load_json(POLICY_PATH)
    except FileNotFoundError:
        return {"mode": "auto"}
    except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError):
        raise ValueError("Codex route policy is invalid") from None
    if not isinstance(policy, dict) or policy.get("mode") not in {"auto", "fixed"}:
        raise ValueError("Codex route policy is invalid")
    if policy.get("mode") == "fixed" and not str(
        policy.get("credential_id") or ""
    ).strip():
        raise ValueError("Codex route policy is invalid")
    return policy


def save_policy(policy: dict[str, Any]) -> None:
    payload = dict(policy)
    payload["updated_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
    content = (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode()
    atomic_write_bytes(POLICY_PATH, content)


def snapshot_policy() -> tuple[bool, bytes]:
    """Capture exact prior policy bytes so rollback does not rewrite metadata."""
    try:
        return True, POLICY_PATH.read_bytes()
    except FileNotFoundError:
        return False, b""


def restore_policy(snapshot: tuple[bool, bytes]) -> None:
    existed, content = snapshot
    if not existed:
        POLICY_PATH.unlink(missing_ok=True)
        return
    atomic_write_bytes(POLICY_PATH, content)


class RouteApplyError(RuntimeError):
    """Raised after a failed apply and the required full rollback attempt."""



def profile_homes() -> list[tuple[str, Path]]:
    homes = [("default", HERMES_HOME)]
    profiles_root = HERMES_HOME / "profiles"
    try:
        root_info = profiles_root.lstat()
        if not stat.S_ISDIR(root_info.st_mode):
            return homes
        resolved_root = profiles_root.resolve(strict=True)
        candidates = sorted(profiles_root.iterdir())
    except OSError:
        return homes

    for path in candidates:
        try:
            path_info = path.lstat()
            if not stat.S_ISDIR(path_info.st_mode):
                continue
            resolved_path = path.resolve(strict=True)
            if not resolved_path.is_relative_to(resolved_root):
                continue
            auth_info = (path / "auth.json").lstat()
            if not stat.S_ISREG(auth_info.st_mode):
                continue
        except OSError:
            continue
        homes.append((path.name, path))
    return homes


def _profile_home_key(home: Path) -> str:
    return str(home.expanduser().resolve(strict=False))


def _profile_inventory(homes: list[tuple[str, Path]]) -> tuple[tuple[str, str], ...]:
    inventory = tuple((name, _profile_home_key(home)) for name, home in homes)
    if len({home for _name, home in inventory}) != len(inventory):
        raise ValueError("Codex profile inventory contains duplicate homes")
    return inventory


def _normalize_precollected_payload(raw: str) -> str:
    if len(raw.encode("utf-8")) > INTERNAL_PAYLOAD_MAX_BYTES:
        raise ValueError("Codex usage payload exceeds internal transport limit")
    envelope = json.loads(raw)
    if (
        not isinstance(envelope, dict)
        or set(envelope) != {"version", "payload"}
        or envelope.get("version") != INTERNAL_PAYLOAD_VERSION
    ):
        raise ValueError("Codex usage payload envelope is invalid")
    payload = envelope.get("payload")
    if not isinstance(payload, dict):
        raise ValueError("Codex usage payload is invalid")
    accounts = payload.get("accounts")
    routing = payload.get("routing")
    recommendation = payload.get("recommendation")
    if (
        not isinstance(accounts, list)
        or any(not isinstance(row, dict) for row in accounts)
        or not isinstance(routing, dict)
        or not isinstance(recommendation, dict)
    ):
        raise ValueError("Codex usage payload shape is invalid")
    normalized = json.dumps(
        envelope,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    if len(normalized.encode("utf-8")) > INTERNAL_PAYLOAD_MAX_BYTES:
        raise ValueError("Codex usage payload exceeds internal transport limit")
    return normalized


def _child_failure_detail(proc: subprocess.CompletedProcess[str]) -> str:
    detail = (proc.stderr or proc.stdout or "실행 실패").strip().splitlines()[:2]
    return redact_sensitive_text(" | ".join(detail), force=True)


def _collect_profile_payload(
    name: str, home: Path
) -> tuple[str | None, str | None]:
    argv = [
        sys.executable,
        "-m",
        PRIORITY_SYNC_MODULE,
        "--internal-collect-payload",
    ]
    env = os.environ.copy()
    env["HERMES_HOME"] = str(home)
    env.pop("HERMES_CODEX_ROUTE_LOCK_HELD", None)
    try:
        proc = subprocess.run(
            argv,
            text=True,
            capture_output=True,
            timeout=PROFILE_SYNC_TIMEOUT,
            shell=False,
            env=env,
        )
    except subprocess.TimeoutExpired:
        return None, (
            f"{name}: payload collection timed out after "
            f"{PROFILE_SYNC_TIMEOUT}s"
        )
    except OSError as exc:
        safe_exc = redact_sensitive_text(str(exc), force=True)
        return None, f"{name}: payload collection launch failed: {safe_exc}"
    if proc.returncode != 0:
        return None, f"{name}: payload collection failed: {_child_failure_detail(proc)}"
    try:
        return _normalize_precollected_payload(proc.stdout), None
    except (UnicodeError, json.JSONDecodeError, TypeError, ValueError):
        return None, f"{name}: payload collection returned invalid data"


def collect_profile_payloads(
    homes: list[tuple[str, Path]],
) -> tuple[dict[str, str], list[str]]:
    """Collect once per unique profile home before acquiring the route lock."""
    unique: dict[str, tuple[str, Path]] = {}
    for name, home in homes:
        unique.setdefault(_profile_home_key(home), (name, home))

    payloads: dict[str, str] = {}
    errors: list[str] = []
    if not unique:
        return payloads, errors
    with ThreadPoolExecutor(max_workers=min(len(unique), 4)) as executor:
        futures = {
            executor.submit(_collect_profile_payload, name, home): key
            for key, (name, home) in unique.items()
        }
        for future in as_completed(futures):
            key = futures[future]
            payload, error = future.result()
            if error:
                errors.append(error)
            elif payload is not None:
                payloads[key] = payload
    return payloads, sorted(errors)


def _sync_profile(
    name: str,
    home: Path,
    payload_json: str,
    extra_args: tuple[str, ...] = (),
) -> str | None:
    argv = [
        sys.executable,
        "-m",
        PRIORITY_SYNC_MODULE,
        "--internal-payload-stdin",
        *extra_args,
    ]
    env = os.environ.copy()
    env["HERMES_HOME"] = str(home)
    env["HERMES_CODEX_ROUTE_LOCK_HELD"] = "1"
    try:
        proc = subprocess.run(
            argv,
            input=payload_json,
            text=True,
            capture_output=True,
            timeout=PROFILE_SYNC_TIMEOUT,
            shell=False,
            env=env,
        )
    except subprocess.TimeoutExpired:
        return f"{name}: sync timed out after {PROFILE_SYNC_TIMEOUT}s"
    except OSError as exc:
        safe_exc = redact_sensitive_text(str(exc), force=True)
        return f"{name}: sync launch failed: {safe_exc}"
    if proc.returncode == 0:
        return None
    return f"{name}: {_child_failure_detail(proc)}"


def sync_all_profiles(
    payloads: dict[str, str],
    *,
    homes: list[tuple[str, Path]] | None = None,
) -> list[str]:
    """Apply precollected payloads for every profile within one bounded round."""
    homes = list(homes if homes is not None else profile_homes())
    if not homes:
        return []
    default = next(
        ((name, home) for name, home in homes if name == "default"), None
    )
    if default is None:
        return ["default: profile home missing"]
    policy = load_policy()
    jobs: list[tuple[str, Path, str, tuple[str, ...]]] = []
    errors: list[str] = []

    def add_job(name: str, home: Path, extra_args: tuple[str, ...]) -> None:
        payload = payloads.get(_profile_home_key(home))
        if payload is None:
            errors.append(f"{name}: precollected payload missing")
            return
        jobs.append((name, home, payload, extra_args))

    if policy.get("mode") == "auto":
        default_home = default[1]
        add_job(
            "default",
            default_home,
            ("--profile-account", "personal", "--skip-cliproxy"),
        )
        add_job("global-clients", default_home, ("--skip-hermes",))
    else:
        add_job("default", default[1], ())
    for name, home in homes:
        if name != "default":
            add_job(name, home, ("--skip-cliproxy",))
    if not jobs:
        return sorted(errors)
    with ThreadPoolExecutor(max_workers=min(len(jobs), 4)) as executor:
        futures = {
            executor.submit(
                _sync_profile,
                name,
                home,
                payload_json,
                extra_args,
            ): name
            for name, home, payload_json, extra_args in jobs
        }
        for future in as_completed(futures):
            error = future.result()
            if error:
                errors.append(error)
    return sorted(errors)


def canonical_row_label(row: dict[str, Any]) -> str:
    kind = _kind(row)
    if kind:
        return canonical_label(kind)
    credential_id = str(row.get("id") or "")
    if credential_id:
        for default_row in credential_rows(DEFAULT_AUTH):
            if str(default_row.get("id") or "") != credential_id:
                continue
            default_kind = _kind(default_row)
            if default_kind:
                return canonical_label(default_kind)
            return "unknown"
    return "unknown"


def _credential_priority(row: dict[str, Any]) -> int | None:
    priority = row.get("priority", 0)
    if isinstance(priority, bool) or not isinstance(priority, int):
        return None
    return priority


def _credential_row_is_valid(row: dict[str, Any]) -> bool:
    if "disabled" in row:
        return False
    if _credential_priority(row) is None:
        return False
    status = row.get("last_status")
    return status is None or (
        isinstance(status, str) and status in {"ok", "dead", "exhausted"}
    )


def _safe_credential_status(row: dict[str, Any]) -> str:
    raw_status = row.get("last_status")
    if not isinstance(raw_status, str):
        return "unavailable"
    normalized = raw_status.strip().lower()
    if normalized in {"exhausted", "dead", "cooldown", "error", "unavailable"}:
        return normalized
    return "unavailable"


def _parse_route_timestamp(value: Any) -> float | None:
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        numeric = float(value)
        if numeric <= 0:
            return None
        return numeric / 1000.0 if numeric > 1_000_000_000_000 else numeric
    elif isinstance(value, str):
        raw = value.strip()
        if not raw:
            return None
        try:
            numeric = float(raw)
        except ValueError:
            try:
                return datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp()
            except ValueError:
                return None
    else:
        return None
    return numeric / 1000.0 if numeric > 1_000_000_000_000 else numeric


def _credential_exhausted_until(row: dict[str, Any]) -> float | None:
    reset_at = _parse_route_timestamp(row.get("last_error_reset_at"))
    if reset_at is not None:
        return reset_at

    last_status_at = row.get("last_status_at")
    if isinstance(last_status_at, str):
        last_status_at = _parse_route_timestamp(last_status_at)
    elif isinstance(last_status_at, bool) or not isinstance(
        last_status_at, (int, float)
    ):
        last_status_at = None
    if not last_status_at:
        return None

    # Mirror agent.credential_pool: a 401 cools down for five minutes; 429
    # and all other exhaustion reasons use one hour. Keeping this tiny local
    # calculation avoids cold-importing the full agent runtime for status.
    ttl_seconds = 5 * 60 if row.get("last_error_code") == 401 else 60 * 60
    return float(last_status_at) + ttl_seconds


def _credential_is_routable(row: dict[str, Any]) -> bool:
    raw_status = row.get("last_status")
    if raw_status is None or raw_status == "ok":
        return True
    if raw_status == "dead":
        return False
    if raw_status != "exhausted":
        return False
    exhausted_until = _credential_exhausted_until(row)
    return exhausted_until is None or time.time() >= exhausted_until


def current_label(
    auth_path: Path, policy: dict[str, Any] | None = None
) -> str:
    rows = credential_rows(auth_path)
    if not rows:
        return "계정 없음"

    if not all(_credential_row_is_valid(row) for row in rows):
        return "unknown · invalid credential state"
    priorities = [_credential_priority(row) for row in rows]
    priority_by_identity = {
        id(row): priority for row, priority in zip(rows, priorities, strict=True)
    }

    route_policy = policy or {"mode": "auto"}
    mode = route_policy.get("mode")
    if mode == "fixed":
        credential_id = str(route_policy.get("credential_id") or "").strip()
        if not credential_id:
            return "unknown · invalid route policy"
        matches = [
            row for row in rows if str(row.get("id") or "").strip() == credential_id
        ]
        if len(matches) != 1:
            policy_label = canonical_row_label(
                {
                    "id": credential_id,
                    "label": route_policy.get("label"),
                }
            )
            return (
                f"{policy_label}-fixed · no eligible route "
                f"({policy_label} unavailable)"
            )
        affinity_row = matches[0]
        affinity = canonical_row_label(affinity_row)
        if _credential_is_routable(affinity_row):
            return affinity
        return (
            f"{affinity}-fixed · no eligible route "
            f"({affinity} {_safe_credential_status(affinity_row)})"
        )
    if mode != "auto":
        return "unknown · invalid route policy"

    affinity_row = min(rows, key=lambda row: priority_by_identity[id(row)])
    affinity = canonical_row_label(affinity_row)
    if _credential_is_routable(affinity_row):
        return affinity

    fallback_rows = [row for row in rows if _credential_is_routable(row)]
    if fallback_rows:
        fallback_row = min(
            fallback_rows, key=lambda row: priority_by_identity[id(row)]
        )
        fallback = canonical_row_label(fallback_row)
        availability = f"fallback {fallback} eligible"
    else:
        availability = "no eligible route"
    return (
        f"{affinity}-first · {availability} "
        f"({affinity} {_safe_credential_status(affinity_row)})"
    )


def cliproxy_current_account() -> str:
    from hermes_cli.codex_priority_sync import (
        CLIProxyManagementError,
        cliproxy_active_kind,
    )

    try:
        return canonical_label(cliproxy_active_kind())
    except CLIProxyManagementError:
        return "unknown · management unavailable"


def native_codex_current_account() -> str:
    if not NATIVE_CODEX_AUTH.exists():
        return "none"
    if not NATIVE_CODEX_AUTH.is_symlink():
        return "legacy"
    resolved = NATIVE_CODEX_AUTH.resolve(strict=False)
    if resolved.parent.name in CANONICAL_LABELS and resolved.parent.parent.name == "accounts":
        return canonical_label(resolved.parent.name)
    return "unknown"


def render_status(policy: dict[str, Any], sync_errors: list[str] | None = None) -> str:
    if policy.get("mode") == "fixed":
        label = canonical_row_label(
            {
                "id": policy.get("credential_id"),
                "label": policy.get("label"),
            }
        )
        mode_text = f"고정 · {label}"
    else:
        mode_text = "자동 추천"
    lines = ["🎛 Codex 라우팅", f"모드: {mode_text}"]
    for name, home in profile_homes():
        lines.append(f"• {name}: {current_label(home / 'auth.json', policy)}")
    lines.append(f"• CLIProxy: {cliproxy_current_account()}")
    lines.append(f"• Native Codex CLI: {native_codex_current_account()}")
    if sync_errors:
        lines.append("⚠️ " + "; ".join(sync_errors))
    lines.extend(
        [
            "",
            "CLI: hermes codex-route [auto|personal|company]",
            "Telegram: /codex_route [auto|personal|company]",
        ]
    )
    return "\n".join(lines)


def _apply_mode_locked(
    mode: str,
    *,
    homes: list[tuple[str, Path]],
    payloads: dict[str, str],
) -> str:
    policy = resolve_policy(mode, DEFAULT_AUTH)
    prior = snapshot_policy()
    save_policy(policy)
    try:
        errors = sync_all_profiles(payloads, homes=homes)
    except Exception as exc:
        errors = [f"profile sync raised {type(exc).__name__}"]
    if not errors:
        return render_status(policy)

    restore_policy(prior)
    try:
        rollback_errors = sync_all_profiles(payloads, homes=homes)
    except Exception as exc:
        rollback_errors = [f"rollback sync raised {type(exc).__name__}"]
    lines = [
        "❌ Codex 라우팅 적용 실패: " + "; ".join(errors),
        "↩️ 이전 정책을 복원하고 전체 프로필 rollback을 시도했습니다.",
    ]
    if rollback_errors:
        lines.append("❌ rollback 실패: " + "; ".join(rollback_errors))
    else:
        lines.append("✅ rollback 완료")
    raise RouteApplyError("\n".join(lines))


def apply_mode(mode: str) -> str:
    for attempt in range(2):
        homes = profile_homes()
        inventory = _profile_inventory(homes)
        collection_started_at = time.monotonic()
        payloads, collection_errors = collect_profile_payloads(homes)
        if collection_errors:
            raise RouteApplyError(
                "❌ Codex usage collection failed: "
                + "; ".join(collection_errors)
            )
        retry = False
        with route_lock(
            path=POLICY_PATH.parent / "codex_route.lock",
            timeout=ROUTE_LOCK_TIMEOUT,
        ):
            current_homes = profile_homes()
            if (
                _profile_inventory(current_homes) != inventory
                or time.monotonic() - collection_started_at
                > COLLECTED_PAYLOAD_MAX_AGE_SECONDS
            ):
                retry = True
            else:
                return _apply_mode_locked(
                    mode,
                    homes=current_homes,
                    payloads=payloads,
                )
        if retry and attempt == 0:
            continue
        break
    raise RouteApplyError(
        "❌ Codex routing skipped: profile inventory or usage payload changed"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Set Codex routing mode across all Hermes profiles")
    parser.add_argument("mode", nargs="?", default="status")
    args = parser.parse_args(argv)
    try:
        if args.mode.lower() in {"status", "show", "상태"}:
            print(render_status(load_policy()))
        else:
            print(apply_mode(args.mode))
    except RouteApplyError as exc:
        print(str(exc))
        return 1
    except (ValueError, OSError, TimeoutError, subprocess.SubprocessError) as exc:
        print(f"❌ Codex 라우팅 변경 실패: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
