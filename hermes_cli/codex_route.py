"""Control the global Codex routing policy and apply it to every Hermes profile."""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

from agent.redact import redact_sensitive_text
from hermes_cli.codex_route_lock import atomic_write_bytes, route_lock

HOME = Path.home()
HERMES_HOME = HOME / ".hermes"
DEFAULT_AUTH = HERMES_HOME / "auth.json"
POLICY_PATH = HERMES_HOME / "state" / "codex_route_policy.json"
CLIPROXY_AUTH_DIR = HOME / ".cli-proxy-api"
NATIVE_CODEX_AUTH = HOME / ".codex" / "auth.json"
PRIORITY_SYNC_MODULE = "hermes_cli.codex_priority_sync"
PROFILE_SYNC_TIMEOUT = 180
ROUTE_COMMAND_TIMEOUT = 2 * PROFILE_SYNC_TIMEOUT + 30
ROUTE_LOCK_TIMEOUT = 30

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
    if profiles_root.exists():
        for path in sorted(profiles_root.iterdir()):
            if path.is_dir() and (path / "auth.json").exists():
                homes.append((path.name, path))
    return homes


def _sync_profile(
    name: str, home: Path, extra_args: tuple[str, ...] = ()
) -> str | None:
    argv = [
        sys.executable,
        "-m",
        PRIORITY_SYNC_MODULE,
        *extra_args,
    ]
    env = os.environ.copy()
    env["HERMES_HOME"] = str(home)
    env["HERMES_CODEX_ROUTE_LOCK_HELD"] = "1"
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
        return f"{name}: sync timed out after {PROFILE_SYNC_TIMEOUT}s"
    except OSError as exc:
        safe_exc = redact_sensitive_text(str(exc), force=True)
        return f"{name}: sync launch failed: {safe_exc}"
    if proc.returncode == 0:
        return None
    detail = (proc.stderr or proc.stdout or "실행 실패").strip().splitlines()[:2]
    safe_detail = redact_sensitive_text(" | ".join(detail), force=True)
    return f"{name}: {safe_detail}"


def sync_all_profiles() -> list[str]:
    """Run tracked priority sync for every profile within one bounded round."""
    homes = profile_homes()
    if not homes:
        return []
    default = next(
        ((name, home) for name, home in homes if name == "default"), None
    )
    if default is None:
        return ["default: profile home missing"]
    policy = load_policy()
    jobs: list[tuple[str, Path, tuple[str, ...]]] = []
    if policy.get("mode") == "auto":
        default_home = default[1]
        jobs.extend(
            [
                (
                    "default",
                    default_home,
                    ("--profile-account", "personal", "--skip-cliproxy"),
                ),
                ("global-clients", default_home, ("--skip-hermes",)),
            ]
        )
    else:
        jobs.append(("default", default[1], ()))
    jobs.extend(
        (name, home, ("--skip-cliproxy",))
        for name, home in homes
        if name != "default"
    )
    errors: list[str] = []
    with ThreadPoolExecutor(max_workers=min(len(jobs), 4)) as executor:
        futures = {
            executor.submit(_sync_profile, name, home, extra_args): name
            for name, home, extra_args in jobs
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


def current_label(auth_path: Path) -> str:
    rows = [row for row in credential_rows(auth_path) if (row.get("last_status") or "ok") == "ok"]
    if not rows:
        return "계정 없음"
    row = min(rows, key=lambda item: int(item.get("priority") or 0))
    return canonical_row_label(row)


def _cliproxy_kind(data: dict[str, Any]) -> str | None:
    lower = " ".join(
        str(data.get(key, "")) for key in ("label", "note")
    ).lower()
    if any(part in lower for part in ("gameduo", "company", "plus")):
        return "company"
    if any(part in lower for part in ("personal", "backup")):
        return "personal"
    return None


def cliproxy_current_account() -> str:
    candidates: list[tuple[int, str]] = []
    for path in sorted(CLIPROXY_AUTH_DIR.glob("codex-*.json")):
        try:
            data = load_json(path)
        except Exception:
            continue
        kind = _cliproxy_kind(data)
        if kind is None:
            continue
        raw_attrs = data.get("attributes")
        attrs = raw_attrs if isinstance(raw_attrs, dict) else {}
        raw_priority = attrs.get("priority", data.get("priority", 0))
        try:
            priority = int(raw_priority)
        except (TypeError, ValueError):
            priority = 0
        candidates.append((priority, kind))
    return canonical_label(max(candidates)[1]) if candidates else "계정 없음"


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
        lines.append(f"• {name}: {current_label(home / 'auth.json')}")
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


def _apply_mode_locked(mode: str) -> str:
    policy = resolve_policy(mode, DEFAULT_AUTH)
    prior = snapshot_policy()
    save_policy(policy)
    try:
        errors = sync_all_profiles()
    except Exception as exc:
        errors = [f"profile sync raised {type(exc).__name__}"]
    if not errors:
        return render_status(policy)

    restore_policy(prior)
    try:
        rollback_errors = sync_all_profiles()
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
    with route_lock(
        path=POLICY_PATH.parent / "codex_route.lock",
        timeout=ROUTE_LOCK_TIMEOUT,
    ):
        return _apply_mode_locked(mode)


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
