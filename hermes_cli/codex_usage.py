"""OpenAI Codex quota usage helpers for Hermes.

This module powers both the `hermes codex-usage` CLI command and local watchdog
scripts. It reads Hermes' `openai-codex` credential pool and calls the Codex
quota endpoint once per credential. Runtime OAuth tokens are never printed.
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from collections import deque
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from hermes_cli.config import get_hermes_home

USAGE_URL = "https://chatgpt.com/backend-api/wham/usage"
DEFAULT_WATERMARK = get_hermes_home() / "state" / "codex_usage_alerts.json"
DEFAULT_HISTORY = get_hermes_home() / "state" / "codex_usage_history.jsonl"
ROUTE_POLICY_PATH = get_hermes_home() / "state" / "codex_route_policy.json"
TREND_TARGETS = (80, 95, 100)
MIN_TREND_SAMPLE_SECONDS = 10 * 60
DISPLAY_LABELS = {
    "personal-backup": "personal",
    "company-plus-100": "company",
    "personal": "personal",
    "company": "company",
}


def display_label(label: Any) -> str:
    """Return the stable user-facing alias without changing internal IDs."""
    raw = str(label or "unknown")
    return DISPLAY_LABELS.get(raw, "unknown")


def _typed_usage_error(*, status: Any = None) -> Dict[str, Any]:
    try:
        http_status = int(status)
    except (TypeError, ValueError):
        http_status = 0
    if 100 <= http_status <= 599:
        return {"kind": "http_error", "status": http_status}
    return {"kind": "client_error"}


def _public_usage_error(row: Dict[str, Any]) -> Dict[str, Any]:
    error = row.get("error")
    status = row.get("http")
    if isinstance(error, dict) and status is None:
        status = error.get("status")
    return _typed_usage_error(status=status)


def _usage_error_text(row: Dict[str, Any]) -> str:
    error = _public_usage_error(row)
    if error.get("kind") == "http_error":
        return f"HTTP {error.get('status')} usage request failed"
    return "usage request failed"


def _public_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Return a JSON-safe view without raw account labels or error bodies."""
    public = dict(payload)
    routing = dict(public.get("routing") or {})
    for key in ("current_label", "fixed_label"):
        if routing.get(key) is not None:
            routing[key] = display_label(routing.get(key))
    if routing.get("mode") == "invalid":
        routing["error"] = "Codex route policy is invalid"
    public["routing"] = routing

    accounts = []
    for raw_row in public.get("accounts") or []:
        row = dict(raw_row)
        row["label"] = display_label(row.get("label"))
        if not row.get("ok"):
            typed_error = _public_usage_error(row)
            row["error"] = typed_error
            if typed_error.get("kind") == "http_error":
                row["http"] = typed_error.get("status")
            else:
                row.pop("http", None)
        accounts.append(row)
    public["accounts"] = accounts

    recommendation = dict(public.get("recommendation") or {})
    if recommendation.get("label") is not None:
        recommendation["label"] = display_label(recommendation.get("label"))
    if recommendation.get("blocked"):
        recommendation["blocked"] = "one or more accounts unavailable"
    public["recommendation"] = recommendation
    return public


def local_dt(epoch: Any) -> Optional[datetime]:
    if isinstance(epoch, str):
        try:
            dt = datetime.fromisoformat(epoch)
            return dt if dt.tzinfo is not None else dt.astimezone()
        except ValueError:
            return None
    if not isinstance(epoch, (int, float)):
        return None
    return datetime.fromtimestamp(epoch).astimezone()


def human_delta(target: Optional[datetime], now: Optional[datetime] = None) -> str:
    if target is None:
        return "unknown"
    now = now or datetime.now().astimezone()
    seconds = int((target - now).total_seconds())
    if seconds <= 0:
        return "already reset"
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, _ = divmod(rem, 60)
    parts: list[str] = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    if minutes or not parts:
        parts.append(f"{minutes}m")
    return " ".join(parts)


def risk(used_percent: Any) -> Dict[str, str]:
    try:
        value = float(used_percent)
    except (TypeError, ValueError):
        return {"level": "unknown", "icon": "⚪", "label": "unknown"}
    if value >= 95:
        return {"level": "critical", "icon": "🔴", "label": "거의 소진"}
    if value >= 80:
        return {"level": "warning", "icon": "🟠", "label": "주의"}
    return {"level": "ok", "icon": "🟢", "label": "정상"}


def summarize_window(window: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not isinstance(window, dict):
        return None
    reset_dt = local_dt(window.get("reset_at"))
    used = window.get("used_percent")
    return {
        "used_percent": used,
        "limit_window_seconds": window.get("limit_window_seconds"),
        "reset_at": reset_dt.isoformat(timespec="seconds") if reset_dt else window.get("reset_at"),
        "remaining": human_delta(reset_dt),
        "risk": risk(used),
    }


# Backward-compatible alias used by the first local helper script.
window_summary = summarize_window


def fetch_usage(access_token: str, account_id: Optional[str] = None, timeout: int = 30) -> Dict[str, Any]:
    headers = {
        "Authorization": f"Bearer {access_token}",
        "User-Agent": "Hermes Codex usage check",
        "Accept": "application/json",
    }
    if account_id:
        headers["ChatGPT-Account-Id"] = account_id
    req = urllib.request.Request(USAGE_URL, headers=headers, method="GET")
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def load_route_policy() -> Dict[str, Any]:
    """Read fixed Codex routing metadata without exposing credentials."""
    try:
        value = json.loads(ROUTE_POLICY_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError):
        return {
            "mode": "invalid",
            "error": "Codex route policy is invalid",
        }
    if not isinstance(value, dict) or value.get("mode") not in {"auto", "fixed"}:
        return {
            "mode": "invalid",
            "error": "Codex route policy is invalid",
        }
    if value.get("mode") == "auto":
        return {}
    credential_id = str(value.get("credential_id") or "")
    if not credential_id:
        return {
            "mode": "invalid",
            "error": "Codex route policy is invalid",
        }
    return {
        "mode": "fixed",
        "fixed_credential_id": credential_id,
        "fixed_label": display_label(value.get("label")),
    }


def collect(*, mutate: bool = True) -> Dict[str, Any]:
    from agent.credential_pool import load_pool

    pool = (
        load_pool("openai-codex")
        if mutate
        else load_pool("openai-codex", read_only=True)
    )
    # First let Hermes clear expired cooldowns / refresh available tokens, then
    # inspect the full pool. Quota reporting must include exhausted credentials
    # too; otherwise a 7d-reset-aware recommendation cannot see the account that
    # is about to recover next.
    available_entries = pool._available_entries(
        clear_expired=mutate,
        refresh=mutate,
    )  # intentional internal API
    routable_entries = pool._routable_entries(available_entries)  # intentional internal API
    entries = pool.entries()
    strategy = str(getattr(pool, "_strategy", "fill_first"))
    if strategy == "fill_first":
        current_entry = routable_entries[0] if routable_entries else None
    else:
        current_entry = pool.current()
    now = datetime.now().astimezone()
    rows: list[dict[str, Any]] = []
    for entry in entries:
        exhausted_until = local_dt(entry.last_error_reset_at)
        row: Dict[str, Any] = {
            "credential_id": entry.id,
            "label": entry.label,
            "priority": entry.priority,
            "source": entry.source,
            "last_status": entry.last_status or "ok",
            "available": (entry.last_status or "ok") == "ok",
            "exhausted_until": exhausted_until.isoformat(timespec="seconds") if exhausted_until else None,
        }
        account_id = (
            entry.extra.get("account_id")
            or entry.extra.get("chatgpt_account_id")
            or entry.extra.get("accountId")
        )
        try:
            data = fetch_usage(entry.runtime_api_key, account_id=account_id)
            rate_limit = data.get("rate_limit") or {}
            credits = data.get("credits") or {}
            row.update(
                {
                    "ok": True,
                    "plan_type": data.get("plan_type"),
                    "primary_window": summarize_window(rate_limit.get("primary_window")),
                    "secondary_window": summarize_window(rate_limit.get("secondary_window")),
                    "credits": {
                        key: credits.get(key)
                        for key in ("has_credits", "unlimited", "balance")
                        if key in credits
                    },
                }
            )
        except urllib.error.HTTPError as exc:
            row.update(
                {
                    "ok": False,
                    "http": exc.code,
                    "error": _typed_usage_error(status=exc.code),
                }
            )
        except Exception:  # noqa: BLE001 - CLI report should survive per-account failures
            row.update({"ok": False, "error": _typed_usage_error()})
        rows.append(row)
    payload = {
        "checked_at": now.isoformat(timespec="seconds"),
        "provider": "openai-codex",
        "routing": {
            "strategy": strategy,
            "current_label": current_entry.label if current_entry else None,
        },
        "accounts": rows,
    }
    payload["routing"].update(load_route_policy())
    payload["recommendation"] = compute_recommendation(rows)
    return payload


def _percent(row: Dict[str, Any], window_name: str) -> float:
    try:
        value = (row.get(window_name) or {}).get("used_percent")
        if value is None:
            return 999.0
        return float(value)
    except (TypeError, ValueError):
        return 999.0


def _window_reset_dt(row: Dict[str, Any], window_name: str) -> Optional[datetime]:
    window = row.get(window_name) or {}
    return local_dt(window.get("reset_at"))


def _window_remaining_seconds(row: Dict[str, Any], window_name: str) -> float:
    reset_dt = _window_reset_dt(row, window_name)
    if reset_dt is None:
        return float("inf")
    return (reset_dt - datetime.now().astimezone()).total_seconds()


def _reset_aware_blocker(row: Dict[str, Any]) -> Optional[str]:
    """Return why a row should not be actively routed right now."""
    if not row.get("ok"):
        return "usage 조회 실패"
    status = str(row.get("last_status") or "ok")
    if status == "dead":
        return "재로그인 필요"
    if not row.get("available", True):
        exhausted_until = row.get("exhausted_until") or "?"
        return f"cooldown/exhausted until {short_reset(exhausted_until)}"
    primary_used = _percent(row, "primary_window")
    secondary_window = row.get("secondary_window")
    secondary_used = _percent(row, "secondary_window")
    # Do not route into a near-full 5h window unless it is effectively about to
    # reset; the proxy cannot wait, so promoting it would just create 429s.
    if primary_used >= 95 and _window_remaining_seconds(row, "primary_window") > 15 * 60:
        return "5h 95%+ and reset >15m"
    # An absent secondary window means the weekly clock has not started yet.
    if secondary_window and secondary_used >= 100 and _window_remaining_seconds(row, "secondary_window") > 15 * 60:
        return "7d 100% and reset >15m"
    return None


def _recommendation_sort_key(row: Dict[str, Any]) -> tuple[Any, ...]:
    secondary_reset = _window_reset_dt(row, "secondary_window")
    secondary_missing = secondary_reset is None
    secondary_used = _percent(row, "secondary_window")
    # Prefer an available credential whose 7d window has not started / does not
    # expose a reset timestamp yet. A tiny warm-up call can start that 7d reset
    # clock, maximizing usable weekly rotation. After every candidate has a
    # reset timestamp, reset time becomes the primary policy. When reset times
    # tie, burn the fuller soon-resetting window first.
    priority = row.get("priority")
    return (
        0 if secondary_missing else 1,
        secondary_reset or datetime.min.replace(tzinfo=datetime.now().astimezone().tzinfo),
        secondary_used if secondary_missing else -secondary_used,
        _percent(row, "primary_window"),
        int(priority) if priority is not None else 999,
    )


def compute_recommendation(accounts: List[Dict[str, Any]]) -> Optional[Dict[str, str]]:
    candidates: list[Dict[str, Any]] = []
    blocked: list[tuple[str, str]] = []
    for row in accounts:
        reason = _reset_aware_blocker(row)
        if reason:
            blocked.append((str(row.get("label")), reason))
            continue
        candidates.append(row)
    if not candidates:
        return None

    best = min(candidates, key=_recommendation_sort_key)
    sec = best.get("secondary_window") or {}
    pri = best.get("primary_window") or {}
    reason = (
        f"7d reset {short_reset(sec.get('reset_at'))} · {sec.get('remaining', '?')}, "
        f"7d {sec.get('used_percent', '?')}%, 5h {pri.get('used_percent', '?')}%"
    )
    result = {
        "label": str(best.get("label")),
        "reason": reason,
        "policy": "7d-reset-aware",
    }
    credential_id = str(best.get("credential_id") or "").strip()
    if credential_id:
        result["credential_id"] = credential_id
    if blocked:
        result["blocked"] = "; ".join(
            f"{display_label(label)}: {why}" for label, why in blocked[:3]
        )
    return result


# Backward-compatible alias.
recommend = compute_recommendation


def iter_windows(row: Dict[str, Any]) -> Iterable[Tuple[str, str, Dict[str, Any]]]:
    yield "5h", "primary_window", row.get("primary_window") or {}
    yield "7d", "secondary_window", row.get("secondary_window") or {}


def _checked_dt(payload: Dict[str, Any]) -> Optional[datetime]:
    return local_dt(payload.get("checked_at"))


def _history_percent(row: Dict[str, Any], window_key: str) -> Optional[float]:
    window = row.get(window_key) or {}
    return _window_used(window)


def _matching_previous_window(
    history: Iterable[Dict[str, Any]],
    *,
    label: str,
    window_key: str,
    reset_at: Any,
    checked_at: datetime,
) -> Optional[tuple[datetime, Dict[str, Any]]]:
    best: Optional[tuple[datetime, Dict[str, Any]]] = None
    for snapshot in history:
        previous_checked = _checked_dt(snapshot)
        if previous_checked is None or previous_checked >= checked_at:
            continue
        if (checked_at - previous_checked).total_seconds() < MIN_TREND_SAMPLE_SECONDS:
            continue
        for row in snapshot.get("accounts") or []:
            if str(row.get("label")) != label or not row.get("ok", True):
                continue
            window = row.get(window_key) or {}
            previous_reset = local_dt(window.get("reset_at"))
            current_reset = local_dt(reset_at)
            if previous_reset is None or current_reset is None or previous_reset != current_reset:
                continue
            if _history_percent(row, window_key) is None:
                continue
            if best is None or previous_checked > best[0]:
                best = (previous_checked, window)
    return best


def _eta_map(*, checked_at: datetime, reset_at: Optional[datetime], used: float, burn_per_hour: float) -> dict[str, dict[str, Any]]:
    eta: dict[str, dict[str, Any]] = {}
    for target in TREND_TARGETS:
        if used >= target:
            eta[str(target)] = {"reached": True}
            continue
        if burn_per_hour <= 0:
            eta[str(target)] = {"reached": False, "at": None, "remaining": None, "before_reset": False}
            continue
        eta_at = checked_at + timedelta(hours=(target - used) / burn_per_hour)
        before_reset = bool(reset_at is None or eta_at <= reset_at)
        eta[str(target)] = {
            "reached": False,
            "at": eta_at.isoformat(timespec="seconds"),
            "remaining": human_delta(eta_at, checked_at),
            "before_reset": before_reset,
        }
    return eta


def annotate_usage_trends(
    payload: Dict[str, Any],
    *,
    history: Optional[Iterable[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Attach burn-rate and ETA trend data to each quota window in-place.

    Prefer the most recent prior sample with the same credential label, window,
    and reset timestamp. If no prior sample exists, fall back to the current
    window average from the inferred window start so the first run is still
    informative while clearly marking the source as `window_avg`.
    """
    checked_at = _checked_dt(payload)
    if checked_at is None:
        return payload
    history_rows = list(history or [])
    for row in payload.get("accounts") or []:
        if not row.get("ok"):
            continue
        label = str(row.get("label"))
        for _display, window_key, window in iter_windows(row):
            used = _window_used(window)
            reset_at = local_dt(window.get("reset_at"))
            if used is None or reset_at is None:
                continue
            source = "window_avg"
            burn_per_hour: Optional[float] = None
            previous = _matching_previous_window(
                history_rows,
                label=label,
                window_key=window_key,
                reset_at=window.get("reset_at"),
                checked_at=checked_at,
            )
            if previous:
                prev_checked, prev_window = previous
                prev_used = _window_used(prev_window)
                elapsed_hours = (checked_at - prev_checked).total_seconds() / 3600
                if prev_used is not None and elapsed_hours > 0:
                    delta = used - prev_used
                    if delta >= 0:
                        burn_per_hour = delta / elapsed_hours
                        source = "recent"
            if burn_per_hour is None:
                window_seconds = window.get("limit_window_seconds")
                if isinstance(window_seconds, (int, float)) and window_seconds > 0:
                    start_at = reset_at - timedelta(seconds=float(window_seconds))
                    elapsed_hours = (checked_at - start_at).total_seconds() / 3600
                    if elapsed_hours > 0:
                        burn_per_hour = max(0.0, used / elapsed_hours)
            if burn_per_hour is None:
                continue
            burn_per_hour = round(burn_per_hour, 2)
            window["trend"] = {
                "source": source,
                "burn_percent_per_hour": burn_per_hour,
                "eta": _eta_map(checked_at=checked_at, reset_at=reset_at, used=used, burn_per_hour=burn_per_hour),
            }
    return payload


def load_history(path: Path, *, limit: int = 200) -> list[dict[str, Any]]:
    try:
        lines = deque(path.open(encoding="utf-8"), maxlen=limit)
    except OSError:
        return []
    rows: list[dict[str, Any]] = []
    for line in lines:
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(item, dict):
            rows.append(item)
    return rows


def save_history_snapshot(path: Path, payload: Dict[str, Any]) -> None:
    snapshot = {
        "checked_at": payload.get("checked_at"),
        "accounts": [
            {
                "label": row.get("label"),
                "ok": row.get("ok"),
                "primary_window": {
                    "used_percent": (row.get("primary_window") or {}).get("used_percent"),
                    "reset_at": (row.get("primary_window") or {}).get("reset_at"),
                },
                "secondary_window": {
                    "used_percent": (row.get("secondary_window") or {}).get("used_percent"),
                    "reset_at": (row.get("secondary_window") or {}).get("reset_at"),
                },
            }
            for row in payload.get("accounts") or []
        ],
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(snapshot, ensure_ascii=False, separators=(",", ":")) + "\n")
    except OSError:
        return


def render_text(payload: Dict[str, Any]) -> str:
    lines = [f"Codex usage ({payload['checked_at']})"]
    routing = payload.get("routing") or {}
    if routing.get("mode") == "invalid":
        lines.append("Routing blocked: Codex route policy is invalid")
    accounts = payload.get("accounts") or []
    if not accounts:
        lines.append("No available openai-codex credentials found.")
        return "\n".join(lines)
    recommendation = payload.get("recommendation") or {}
    if recommendation:
        lines.append(f"추천: {display_label(recommendation.get('label'))} ({recommendation.get('reason')})")
    for row in accounts:
        lines.append("")
        lines.append(f"[{display_label(row.get('label'))}] plan={row.get('plan_type', 'unknown')} status={row.get('last_status', 'unknown')}")
        if not row.get("ok"):
            lines.append(f"  ERROR: {_usage_error_text(row)}")
            continue
        for display, _key, window in iter_windows(row):
            r = window.get("risk") or risk(window.get("used_percent"))
            lines.append(
                f"  {display}: {r.get('icon')} "
                f"{window.get('used_percent', '?')}% used ({r.get('label')}), "
                f"reset {window.get('reset_at', '?')} ({window.get('remaining', '?')})"
            )
        credits = row.get("credits") or {}
        if credits:
            lines.append(
                "  credits: "
                f"has={credits.get('has_credits')}, "
                f"unlimited={credits.get('unlimited')}, "
                f"balance={credits.get('balance')}"
            )
    return "\n".join(lines)


def short_reset(reset_at: Any) -> str:
    if not isinstance(reset_at, str):
        return "?"
    try:
        dt = datetime.fromisoformat(reset_at)
        return dt.strftime("%m/%d %H:%M")
    except ValueError:
        return reset_at


def short_checked_at(checked_at: Any) -> str:
    if not isinstance(checked_at, str):
        return "?"
    try:
        return datetime.fromisoformat(checked_at).strftime("%m/%d %H:%M")
    except ValueError:
        return checked_at


def usage_bar(used_percent: Any, width: int = 10) -> str:
    """Return a compact text progress bar for Telegram/CLI scanning."""
    try:
        value = max(0.0, min(100.0, float(used_percent)))
    except (TypeError, ValueError):
        return "?" * max(1, width)
    filled = int(value / 100 * width)
    if value > 0 and filled == 0:
        filled = 1
    if value >= 95:
        filled = width
    return "█" * filled + "░" * (width - filled)


def _fmt_percent(value: Any) -> str:
    try:
        f = float(value)
    except (TypeError, ValueError):
        return " ?%"
    if f.is_integer():
        return f"{int(f):>2}%"
    return f"{f:>4.1f}%"


def _worst_risk(accounts: list[dict[str, Any]]) -> dict[str, str]:
    order = {"critical": 3, "warning": 2, "ok": 1, "unknown": 0}
    worst = {"level": "unknown", "icon": "⚪", "label": "unknown"}
    for row in accounts:
        if not row.get("ok"):
            return {"level": "error", "icon": "❌", "label": "조회 실패"}
        for _display, _key, window in iter_windows(row):
            r = window.get("risk") or risk(window.get("used_percent"))
            if order.get(r.get("level", "unknown"), 0) > order.get(worst.get("level", "unknown"), 0):
                worst = r
    return worst


def _window_used(window: dict[str, Any]) -> Optional[float]:
    try:
        value = window.get("used_percent")
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _recommendation_explanation(payload: Dict[str, Any]) -> str:
    recommendation = payload.get("recommendation") or {}
    rec_label = recommendation.get("label")
    blockers: list[tuple[float, str]] = []
    for row in payload.get("accounts") or []:
        label = row.get("label")
        if not row.get("ok") or label == rec_label:
            continue
        for display, _key, window in iter_windows(row):
            used = _window_used(window)
            if used is not None and used >= 80:
                blockers.append((used, f"{display_label(label)} {display} {_fmt_percent(used).strip()}"))
    blocked = recommendation.get("blocked")
    if blocked:
        return "사유: 7d reset 우선 정책 · 제외: 일부 계정 사용 불가"
    if blockers:
        blockers.sort(reverse=True)
        return f"사유: {blockers[0][1]}로 회복 전까지 보류"
    reason = recommendation.get("reason")
    return f"사유: 7d reset이 가장 가까운 사용 가능 계정 ({reason})" if reason else ""


def _next_reset(
    accounts: list[dict[str, Any]],
    min_used: Optional[float] = None,
    *,
    reference_time: Optional[datetime] = None,
) -> Optional[dict[str, str]]:
    candidates: list[tuple[datetime, dict[str, str]]] = []
    now = reference_time or datetime.now().astimezone()
    for row in accounts:
        if not row.get("ok"):
            continue
        label = str(row.get("label"))
        for display, _key, window in iter_windows(row):
            used = _window_used(window)
            if min_used is not None and (used is None or used < min_used):
                continue
            reset_at = window.get("reset_at")
            dt = local_dt(reset_at)
            if dt is None or dt <= now:
                continue
            candidates.append(
                (
                    dt,
                    {
                        "label": label,
                        "window": display,
                        "reset": short_reset(reset_at),
                        "remaining": str(window.get("remaining", "?")),
                    },
                )
            )
    return min(candidates, key=lambda item: item[0])[1] if candidates else None


def _trend_for(row: dict[str, Any], window_key: str) -> Optional[dict[str, Any]]:
    window = row.get(window_key) or {}
    trend = window.get("trend")
    return trend if isinstance(trend, dict) else None


def _fmt_burn(value: Any) -> str:
    try:
        f = float(value)
    except (TypeError, ValueError):
        return "?%/h"
    return f"+{f:.1f}%/h"


def _fmt_eta_at(value: Any) -> Optional[str]:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value).strftime("%m/%d %H:%M")
    except ValueError:
        return None


def _render_trend_detail(trend: Optional[dict[str, Any]]) -> str:
    if not trend:
        return ""
    parts = [f"burn {_fmt_burn(trend.get('burn_percent_per_hour'))}"]
    eta95 = (trend.get("eta") or {}).get("95") or {}
    if eta95.get("reached"):
        parts.append("ETA95 도달")
    else:
        eta_at = _fmt_eta_at(eta95.get("at"))
        if eta_at:
            suffix = "" if eta95.get("before_reset") else "*"
            parts.append(f"ETA95 {eta_at}{suffix}")
    return " · ".join(parts)


def _recommended_row(payload: Dict[str, Any]) -> Optional[dict[str, Any]]:
    rec_label = (payload.get("recommendation") or {}).get("label")
    accounts = payload.get("accounts") or []
    if rec_label:
        for row in accounts:
            if row.get("ok") and str(row.get("label")) == str(rec_label):
                return row
    for row in accounts:
        if row.get("ok"):
            return row
    return None


def _burn_summary(payload: Dict[str, Any]) -> str:
    row = _recommended_row(payload)
    if not row:
        return ""
    parts: list[str] = []
    for display, key, _window in iter_windows(row):
        trend = _trend_for(row, key)
        if trend:
            source = "avg " if trend.get("source") == "window_avg" else ""
            parts.append(f"{display} {source}{_fmt_burn(trend.get('burn_percent_per_hour'))}")
    return "🔥 Burn: " + " · ".join(parts) if parts else ""


def _routing_current_label(payload: Dict[str, Any]) -> Optional[str]:
    routing = payload.get("routing") or {}
    if routing.get("mode") == "invalid":
        return None
    label = routing.get("current_label")
    return str(label) if label else None


def _compact_recommendation_lines(payload: Dict[str, Any]) -> list[str]:
    recommendation = payload.get("recommendation") or {}
    rec_label = recommendation.get("label")
    routing = payload.get("routing") or {}
    current_label = _routing_current_label(payload)
    fixed_route = routing.get("mode") == "fixed"
    lines: list[str] = []

    if routing.get("mode") == "invalid":
        return ["⚠ Codex routing blocked · policy invalid"]

    if current_label:
        if fixed_route:
            suffix = " · 🔒 고정"
        else:
            suffix = " · ✅ 추천과 일치" if rec_label and str(rec_label) == current_label else ""
        lines.append(f"▶ 현재 {display_label(current_label)}{suffix}")
    if rec_label and fixed_route:
        if str(rec_label) == current_label:
            lines.append("💡 자동 추천과도 일치")
        else:
            lines.append(f"💡 자동 추천 {display_label(rec_label)} · 고정 모드라 미적용")
    elif rec_label and str(rec_label) != current_label:
        lines.append(f"✅ 추천 {display_label(rec_label)}")

    blocked = recommendation.get("blocked")
    if blocked:
        display_blocked = str(blocked)
        for raw, alias in DISPLAY_LABELS.items():
            display_blocked = display_blocked.replace(raw, alias)
        lines.append(f"└ 7d 리셋 우선 · 제외: {display_blocked}")
        return lines

    blockers: list[tuple[float, str]] = []
    for row in payload.get("accounts") or []:
        label = row.get("label")
        if not row.get("ok") or label == rec_label:
            continue
        for display, _key, window in iter_windows(row):
            used = _window_used(window)
            if used is not None and used >= 80:
                blockers.append((used, f"{display_label(label)} {display} {_fmt_percent(used).strip()}"))
    if blockers:
        blockers.sort(reverse=True)
        lines.append(f"└ {blockers[0][1]} · 회복 전 보류")
        return lines

    row = _recommended_row(payload)
    secondary = (row or {}).get("secondary_window") or {}
    if rec_label and secondary:
        lines.append(
            f"└ 7d 리셋이 가장 빠름 · {short_reset(secondary.get('reset_at'))} "
            f"({secondary.get('remaining', '?')})"
        )
    return lines


def render_compact(payload: Dict[str, Any]) -> str:
    accounts = payload.get("accounts") or []
    worst = _worst_risk(accounts)
    lines = [f"🧭 Codex 사용량 · {short_checked_at(payload.get('checked_at'))} · {worst.get('icon')} {worst.get('label')}"]
    lines.extend(_compact_recommendation_lines(payload))
    burn_summary = _burn_summary(payload)
    if burn_summary:
        lines.append(burn_summary)
    reference_time = local_dt(payload.get("checked_at"))
    next_reset = _next_reset(accounts, reference_time=reference_time)
    if next_reset:
        lines.append(f"\n⏱ 다음 회복 · {display_label(next_reset['label'])} {next_reset['window']}")
        lines.append(f"└ {next_reset['reset']} ({next_reset['remaining']})")
    risk_reset = _next_reset(accounts, min_used=95, reference_time=reference_time)
    if risk_reset and risk_reset != next_reset:
        lines.append(f"🚦 위험 회복 · {display_label(risk_reset['label'])} {risk_reset['window']}")
        lines.append(f"└ {risk_reset['reset']} ({risk_reset['remaining']})")
    if not accounts:
        lines.append("계정 없음")
        return "\n".join(lines)

    current_label = _routing_current_label(payload)
    routing = payload.get("routing") or {}
    fixed_route = routing.get("mode") == "fixed"
    rec_label = str((payload.get("recommendation") or {}).get("label") or "")
    for row in accounts:
        label = str(row.get("label"))
        plan = row.get("plan_type") or "unknown"
        is_current = bool(current_label and label == current_label)
        is_recommended = bool(rec_label and label == rec_label)
        marker = "▶" if is_current else "★" if is_recommended else "○"
        tags = ""
        if fixed_route and is_current:
            tags = " · 현재·고정"
        elif fixed_route and is_recommended:
            tags = " · 자동추천"
        elif is_current and is_recommended:
            tags = " · 현재·추천"
        elif is_current:
            tags = " · 현재"
        elif is_recommended:
            tags = " · 추천"
        if not row.get("ok"):
            lines.append(f"\n❌ {display_label(label)} · {plan}{tags}")
            lines.append(f"└ ERROR {_usage_error_text(row)}")
            continue
        lines.append(f"\n{marker} {display_label(label)} · {plan}{tags}")
        for display, key, window in iter_windows(row):
            r = window.get("risk") or risk(window.get("used_percent"))
            used = window.get("used_percent")
            line = (
                f"  {display:>2} {_fmt_percent(used)} {r.get('icon')} "
                f"[{usage_bar(used)}] · {short_reset(window.get('reset_at'))} "
                f"({window.get('remaining', '?')})"
            )
            trend_detail = _render_trend_detail(_trend_for(row, key))
            if trend_detail:
                line = f"{line} · {trend_detail}"
            lines.append(line)
    return "\n".join(lines)

def load_seen(path: Path) -> set[str]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, list):
            return {str(item) for item in data}
    except FileNotFoundError:
        return set()
    except Exception:
        return set()
    return set()


def save_seen(path: Path, seen: set[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(sorted(seen), ensure_ascii=False, indent=2), encoding="utf-8")


def apply_alert_policy(
    payload: Dict[str, Any],
    *,
    threshold: Optional[float] = None,
    primary_threshold: Optional[float] = None,
    secondary_threshold: Optional[float] = None,
) -> List[Dict[str, Any]]:
    """Return alert events from a collected usage payload.

    `threshold` is the legacy single threshold. `primary_threshold` controls the
    5h window and `secondary_threshold` controls the 7d window. Missing specific
    thresholds fall back to `threshold`, then 90%.
    """
    fallback = 90.0 if threshold is None else float(threshold)
    primary = float(primary_threshold) if primary_threshold is not None else fallback
    secondary = float(secondary_threshold) if secondary_threshold is not None else fallback
    thresholds = {"primary_window": primary, "secondary_window": secondary}

    events: list[dict[str, Any]] = []
    for row in payload.get("accounts") or []:
        if not row.get("ok"):
            error = _public_usage_error(row)
            events.append(
                {
                    "key": (
                        f"error:{display_label(row.get('label'))}:"
                        f"{error.get('kind')}:{error.get('status', '')}"
                    ),
                    "row": row,
                    "error": True,
                }
            )
            continue
        for display, key, window in iter_windows(row):
            try:
                raw_used = window.get("used_percent")
                if raw_used is None:
                    continue
                used = float(raw_used)
            except (TypeError, ValueError):
                continue
            if used >= thresholds[key]:
                reset_key = str(window.get("reset_at"))[:16]
                events.append(
                    {
                        "key": f"quota:{row.get('label')}:{key}:{reset_key}",
                        "label": row.get("label"),
                        "window": display,
                        "window_key": key,
                        "threshold": thresholds[key],
                        "used": used if used % 1 else int(used),
                        "reset_at": window.get("reset_at"),
                        "remaining": window.get("remaining"),
                        "risk": window.get("risk") or risk(used),
                    }
                )
    return events


def alert_events(payload: Dict[str, Any], threshold: float) -> List[Dict[str, Any]]:
    return apply_alert_policy(payload, threshold=threshold)


def render_alert(
    payload: Dict[str, Any],
    threshold: Optional[float] = None,
    watermark: Optional[Path] = None,
    *,
    primary_threshold: Optional[float] = None,
    secondary_threshold: Optional[float] = None,
) -> str:
    events = apply_alert_policy(
        payload,
        threshold=threshold,
        primary_threshold=primary_threshold,
        secondary_threshold=secondary_threshold,
    )
    if watermark:
        seen = load_seen(watermark)
        fresh = [event for event in events if event["key"] not in seen]
        if fresh:
            seen.update(event["key"] for event in fresh)
            save_seen(watermark, seen)
        events = fresh
    if not events:
        return ""
    lines = [f"🚨 Codex 한도 주의 · {short_checked_at(payload.get('checked_at'))}"]
    for event in events:
        if event.get("error"):
            row = event["row"]
            lines.append(f"\n❌ {display_label(row.get('label'))}")
            lines.append(f"└ ERROR {_usage_error_text(row)}")
            continue
        r = event.get("risk") or {}
        lines.append(f"\n{r.get('icon', '⚠️')} {display_label(event.get('label'))} · {event.get('window')} {event.get('used'):g}% [{usage_bar(event.get('used'))}]")
        lines.append(f"└ 기준 {event.get('threshold'):g}% · reset {short_reset(event.get('reset_at'))} · {event.get('remaining')}")
    recommendation = payload.get("recommendation") or {}
    if recommendation:
        lines.append(f"\n✅ 추천 {display_label(recommendation.get('label'))} — {recommendation.get('reason')}")
        explanation = _recommendation_explanation(payload)
        if explanation:
            lines.append(explanation)
    return "\n".join(lines)


def _fmt_tokens(value: Any) -> str:
    try:
        n = int(value or 0)
    except (TypeError, ValueError):
        n = 0
    if n >= 1_000_000:
        return f"{n / 1_000_000:.2f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}k"
    return str(n)


def render_credential_insights(rows: list[dict[str, Any]], *, provider: Optional[str], days: int) -> str:
    title = "Codex credential" if provider == "openai-codex" else "Credential"
    lines = [f"📊 {title} 사용량 · {days}d"]
    if not rows:
        suffix = f" ({provider})" if provider else ""
        lines.append(f"아직 credential별 사용 기록 없음{suffix}")
        lines.append("새 모델 턴부터 쌓임")
        return "\n".join(lines)
    total_all = sum(int(row.get("total_tokens") or 0) for row in rows) or 1
    for row in rows:
        label = display_label(row.get("credential_label"))
        model = row.get("model") or "unknown"
        total = int(row.get("total_tokens") or 0)
        calls = int(row.get("api_calls") or row.get("api_call_count") or 0)
        avg = total / calls if calls else 0
        share = total / total_all * 100
        lines.append(f"\n• {label} · {model}")
        lines.append(f"  {_fmt_tokens(total)} tokens · {calls} calls · avg {_fmt_tokens(avg)}/call · {share:.0f}%")
        breakdown = [f"in {_fmt_tokens(row.get('input_tokens'))}", f"out {_fmt_tokens(row.get('output_tokens'))}"]
        if int(row.get("cache_read_tokens") or 0) or int(row.get("cache_write_tokens") or 0):
            cache_total = int(row.get("cache_read_tokens") or 0) + int(row.get("cache_write_tokens") or 0)
            breakdown.append(f"cache {_fmt_tokens(cache_total)}")
        if int(row.get("reasoning_tokens") or 0):
            breakdown.append(f"reason {_fmt_tokens(row.get('reasoning_tokens'))}")
        lines.append("  " + " · ".join(breakdown))
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Show Hermes OpenAI Codex quota usage by credential")
    parser.add_argument("--json", action="store_true", help="print machine-readable JSON")
    parser.add_argument("--compact", action="store_true", help="print Telegram-friendly compact output (default)")
    parser.add_argument("--verbose", action="store_true", help="print detailed per-account text output")
    parser.add_argument("--alert-threshold", type=float, help="legacy: print only accounts/windows at or above this percent")
    parser.add_argument("--primary-threshold", type=float, help="5h window alert threshold percent")
    parser.add_argument("--secondary-threshold", type=float, help="7d window alert threshold percent")
    parser.add_argument("--quiet-ok", action="store_true", help="with alert thresholds, print nothing when below threshold")
    parser.add_argument(
        "--watermark",
        type=Path,
        nargs="?",
        const=DEFAULT_WATERMARK,
        help="with alert thresholds, suppress duplicate alerts for the same reset window",
    )
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    payload = _public_payload(collect())
    history = load_history(DEFAULT_HISTORY)
    annotate_usage_trends(payload, history=history)
    save_history_snapshot(DEFAULT_HISTORY, payload)
    alert_mode = any(
        value is not None
        for value in (args.alert_threshold, args.primary_threshold, args.secondary_threshold)
    )
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    elif alert_mode:
        output = render_alert(
            payload,
            args.alert_threshold,
            args.watermark,
            primary_threshold=args.primary_threshold,
            secondary_threshold=args.secondary_threshold,
        )
        if output:
            print(output)
        elif not args.quiet_ok:
            primary = args.primary_threshold if args.primary_threshold is not None else args.alert_threshold or 90
            secondary = args.secondary_threshold if args.secondary_threshold is not None else args.alert_threshold or 90
            print(f"Codex usage OK: 5h below {primary:g}%, 7d below {secondary:g}%")
    elif args.verbose:
        print(render_text(payload))
    else:
        print(render_compact(payload))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
