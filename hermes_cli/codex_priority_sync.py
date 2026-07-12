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
import json
import os
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from agent.redact import redact_sensitive_text

HOME = Path.home()
GLOBAL_HERMES_HOME = HOME / ".hermes"
HERMES_HOME = Path(os.environ.get("HERMES_HOME") or GLOBAL_HERMES_HOME).expanduser()
HERMES_AUTH = HERMES_HOME / "auth.json"
CLIPROXY_AUTH_DIR = HOME / ".cli-proxy-api"
NATIVE_CODEX_ACCOUNT = HOME / ".local" / "bin" / "codex-account"
STATE_PATH = HERMES_HOME / "state" / "codex_reset_aware_warmup.json"
ROUTE_POLICY_PATH = GLOBAL_HERMES_HOME / "state" / "codex_route_policy.json"
WARMUP_COOLDOWN_SECONDS = 6 * 60 * 60

from hermes_cli.codex_usage import annotate_usage_trends, collect, load_history, DEFAULT_HISTORY  # noqa: E402
from hermes_cli.codex_route_lock import atomic_write_bytes, route_lock  # noqa: E402

ROUTE_LOCK_TIMEOUT = 30
ROUTE_LOCK_PATH = GLOBAL_HERMES_HOME / "state" / "codex_route.lock"


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, data: dict[str, Any], *, dry_run: bool) -> bool:
    if dry_run:
        return False
    content = (json.dumps(data, ensure_ascii=False, indent=2) + "\n").encode()
    atomic_write_bytes(path, content)
    return True


def label_kind(label: str) -> str | None:
    lower = label.lower()
    if "company" in lower or "gameduo" in lower or "plus" in lower:
        return "company"
    if "personal" in lower or "backup" in lower:
        return "personal"
    return None


def cliproxy_kind(data: dict[str, Any]) -> str | None:
    blob = " ".join(str(data.get(k, "")) for k in ("label", "note"))
    lower = blob.lower()
    if "gameduo" in lower or "company" in lower or "plus" in lower:
        return "company"
    if "personal" in lower or "backup" in lower:
        return "personal"
    return None


def collect_payload() -> dict[str, Any]:
    payload = collect()
    annotate_usage_trends(payload, history=load_history(DEFAULT_HISTORY))
    return payload


def load_route_policy() -> dict[str, Any]:
    try:
        policy = json.loads(ROUTE_POLICY_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {"mode": "auto"}
    return policy if isinstance(policy, dict) else {"mode": "auto"}


def choose_route_recommendation(payload: dict[str, Any]) -> dict[str, Any]:
    policy = load_route_policy()
    if policy.get("mode") != "fixed":
        return payload.get("recommendation") or {}
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


def choose_unstarted_weekly(payload: dict[str, Any], state: dict[str, Any]) -> dict[str, Any] | None:
    candidates = [row for row in payload.get("accounts", []) if is_unstarted_weekly_candidate(row)]
    if not candidates:
        return None
    fresh = [row for row in candidates if not warmup_recently_attempted(str(row.get("label")), state)]
    if not fresh:
        return None
    return min(fresh, key=lambda row: (row.get("priority") or 999, str(row.get("label"))))


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


def sync_hermes(recommended_label: str, *, dry_run: bool) -> list[str]:
    if not HERMES_AUTH.exists():
        return ["Hermes auth.json missing"]
    data = load_json(HERMES_AUTH)
    pool = data.get("credential_pool", {}).get("openai-codex", [])
    changed: list[str] = []
    for entry in pool:
        label = str(entry.get("label") or "")
        desired = 0 if label == recommended_label else 10
        if entry.get("priority") != desired:
            changed.append(f"Hermes {label}: priority {entry.get('priority')} -> {desired}")
            entry["priority"] = desired
    if changed:
        write_json(HERMES_AUTH, data, dry_run=dry_run)
    return changed


def sync_cliproxy(recommended_label: str, *, dry_run: bool) -> list[str]:
    wanted_kind = label_kind(recommended_label)
    if wanted_kind is None:
        return [f"CLIProxyAPI mapping unknown for recommendation label={recommended_label}"]
    changed: list[str] = []
    for index, path in enumerate(sorted(CLIPROXY_AUTH_DIR.glob("codex-*.json")), 1):
        try:
            data = load_json(path)
        except Exception as exc:  # noqa: BLE001 - operational script should keep going
            changed.append(f"CLIProxyAPI credential #{index}: read failed {type(exc).__name__}")
            continue
        kind = cliproxy_kind(data)
        if kind is None:
            continue
        desired = 100 if kind == wanted_kind else 0
        attrs = data.get("attributes")
        if not isinstance(attrs, dict):
            attrs = {}
            data["attributes"] = attrs
        old_top = data.get("priority")
        old_attr = attrs.get("priority")
        if old_top != desired or old_attr != str(desired):
            changed.append(f"CLIProxyAPI credential #{index}: priority {old_top}/{old_attr} -> {desired}")
            data["priority"] = desired
            attrs["priority"] = str(desired)
            data["updated_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
            write_json(path, data, dry_run=dry_run)
    return changed


def sync_native_codex(recommended_label: str, *, dry_run: bool) -> list[str]:
    wanted_kind = label_kind(recommended_label)
    if wanted_kind is None:
        return [f"Native Codex mapping unknown for recommendation label={recommended_label}"]
    if dry_run:
        return [f"Native Codex CLI -> {wanted_kind}"]
    if not NATIVE_CODEX_ACCOUNT.exists():
        return [f"Native Codex account command missing: {NATIVE_CODEX_ACCOUNT}"]
    proc = subprocess.run(
        [str(NATIVE_CODEX_ACCOUNT), "activate", wanted_kind],
        text=True,
        capture_output=True,
        timeout=30,
        shell=False,
    )
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "activation failed").strip().splitlines()[:2]
        safe_detail = redact_sensitive_text(" | ".join(detail), force=True)
        return [f"Native Codex CLI {wanted_kind}: {safe_detail}"]
    if (proc.stdout or "").strip() == "UNCHANGED":
        return []
    return [f"Native Codex CLI -> {wanted_kind}"]


def _main_unlocked(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Sync Codex priorities from 7d-reset-aware Hermes recommendation")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--report", action="store_true", help="print current decision even if nothing changed")
    parser.add_argument("--skip-hermes", action="store_true")
    parser.add_argument("--skip-cliproxy", action="store_true")
    parser.add_argument(
        "--warmup-unstarted",
        action="store_true",
        help="if an available Codex account has no 7d reset timestamp yet, promote it and make one tiny model call to start the 7d window",
    )
    args = parser.parse_args(argv)

    payload = collect_payload()
    state = load_state()
    policy_recommendation = choose_route_recommendation(payload)
    route_error = str(policy_recommendation.get("error") or "")
    if route_error:
        print(route_error)
        return 1
    fixed_route = policy_recommendation.get("policy") == "fixed"
    warmup_row = choose_unstarted_weekly(payload, state) if args.warmup_unstarted and not fixed_route else None
    warmup_note = ""
    if fixed_route:
        recommendation = policy_recommendation
    elif warmup_row is not None:
        recommendation = {
            "label": str(warmup_row.get("label") or ""),
            "reason": "7d reset timestamp missing/unstarted; warm-up call should start the weekly window",
            "policy": "7d-reset-aware-warmup",
        }
    else:
        recommendation = policy_recommendation
    label = str(recommendation.get("label") or "")
    if not label:
        # Silent when no recommendation — watchdog pattern: empty stdout = healthy.
        return 0

    changes: list[str] = []
    if not args.skip_hermes:
        changes.extend(sync_hermes(label, dry_run=args.dry_run))
    if not args.skip_cliproxy:
        changes.extend(sync_cliproxy(label, dry_run=args.dry_run))
        changes.extend(sync_native_codex(label, dry_run=args.dry_run))

    if warmup_row is not None:
        ok, warmup_note = run_warmup_call(label, dry_run=args.dry_run)
        record_warmup(label, ok=ok, dry_run=args.dry_run)
        changes.append(f"Warm-up {label}: {warmup_note}")

    actionable_errors = [
        change
        for change in changes
        if any(marker in change.lower() for marker in (" failed", " missing", " unknown", " read failed"))
    ]
    # This is an automatic routing loop. Normal 5h/7d quota movement can change
    # the chosen account several times per day, so routine priority changes are
    # intentionally silent. ``--report`` remains the explicit human-facing
    # status view; only operational errors page the cron destination.
    if args.report or actionable_errors:
        mode = "DRY-RUN" if args.dry_run else "APPLIED"
        visible_changes = changes if args.report else actionable_errors
        print(f"## Codex 7d reset-aware priority sync · {mode}")
        print(f"- 추천: {label}")
        print(f"- 정책: {recommendation.get('policy', '7d-reset-aware')}")
        print(f"- 사유: {recommendation.get('reason', '?')}")
        if recommendation.get("blocked"):
            print(f"- 제외: {recommendation.get('blocked')}")
        if visible_changes:
            for change in visible_changes:
                print(f"- {change}")
        else:
            print("- 변경 없음")
    return 1 if actionable_errors else 0


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
