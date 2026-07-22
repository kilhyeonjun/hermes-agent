"""Dux Distributed Global Search — plugin form (via ``ddgs``).

Subclasses the plugin-facing :class:`agent.web_search_provider.WebSearchProvider`.
The legacy in-tree module ``tools.web_providers.ddgs`` was removed in the
same commit that moved this code under ``plugins/``; this file is now the
canonical implementation.

The ``ddgs`` package is an optional dependency. ``is_available()`` reflects
whether the package is importable; the plugin still registers either way so
``hermes tools`` can prompt the user to install it.

Optional profile-scoped configuration::

    web:
      ddgs:
        region: "kr-kr"      # default: us-en
        timelimit: null       # one of d, w, m, y
        backend: "auto"      # or comma-separated engine names
"""

from __future__ import annotations

import concurrent.futures as _cf
import logging
import threading
from typing import Any, Dict

from agent.web_search_provider import WebSearchProvider

logger = logging.getLogger(__name__)

# Overall wall-clock cap for a single ddgs search. The DDGS constructor's
# ``timeout`` only bounds individual HTTP requests; ddgs's multi-engine retry
# loop has no overall cap, so a slow/rate-limited DuckDuckGo response can hang
# the (single, shared) agent loop indefinitely and block every platform
# (#36776). Enforce a hard cap here via a worker thread.
_SEARCH_TIMEOUT_SECS = 30
_VALID_TIMELIMITS = {"d", "w", "m", "y"}
# Timed-out HTTP calls cannot be cancelled. Bound orphaned workers so repeated
# upstream hangs fail fast instead of growing threads without limit.
_SEARCH_SLOTS = threading.BoundedSemaphore(2)


def _load_ddgs_web_config() -> Dict[str, Any]:
    """Read ``web.ddgs`` from the active profile config."""
    try:
        from hermes_cli.config import load_config

        cfg = load_config()
        web_section = cfg.get("web") if isinstance(cfg, dict) else None
        ddgs_section = web_section.get("ddgs") if isinstance(web_section, dict) else None
        return ddgs_section if isinstance(ddgs_section, dict) else {}
    except Exception as exc:  # noqa: BLE001
        logger.debug("Could not load web.ddgs config: %s", exc)
        return {}


def _ddgs_search_options() -> Dict[str, Any]:
    cfg = _load_ddgs_web_config()
    region = str(cfg.get("region") or "us-en").strip() or "us-en"
    backend = str(cfg.get("backend") or "auto").strip() or "auto"
    raw_timelimit = cfg.get("timelimit")
    timelimit = str(raw_timelimit).strip().lower() if raw_timelimit else None
    if timelimit not in _VALID_TIMELIMITS:
        timelimit = None
    return {"region": region, "timelimit": timelimit, "backend": backend}


def _run_ddgs_search(
    query: str,
    safe_limit: int,
    *,
    region: str = "us-en",
    timelimit: str | None = None,
    backend: str = "auto",
) -> list[dict[str, Any]]:
    """Run the blocking ddgs query and return normalized hits.

    Module-level (not a closure) so tests can patch it directly without
    spawning a real multi-second worker thread. ``DDGS(timeout=...)`` bounds
    each individual HTTP request; the overall wall-clock cap is enforced by
    the caller via a future timeout.
    """
    from ddgs import DDGS  # type: ignore

    results: list[dict[str, Any]] = []
    with DDGS(timeout=10) as client:
        for i, hit in enumerate(
            client.text(
                query,
                max_results=safe_limit,
                region=region,
                timelimit=timelimit,
                backend=backend,
            )
        ):
            if i >= safe_limit:
                break
            url = str(hit.get("href") or hit.get("url") or "")
            results.append(
                {
                    "title": str(hit.get("title", "")),
                    "url": url,
                    "description": str(hit.get("body", "")),
                    "position": i + 1,
                }
            )
    return results


def _run_ddgs_search_guarded(query: str, safe_limit: int, **options: Any) -> list[dict[str, Any]]:
    try:
        return _run_ddgs_search(query, safe_limit, **options)
    finally:
        _SEARCH_SLOTS.release()


class DDGSWebSearchProvider(WebSearchProvider):
    """No-key metasearch provider backed by the ``ddgs`` package.

    No API key is needed, but upstream engines can rate-limit or block it and
    there is no official API SLA. The provider surfaces ddgs errors
    as ``{"success": False, "error": ...}`` rather than raising.
    """

    @property
    def name(self) -> str:
        return "ddgs"

    @property
    def display_name(self) -> str:
        # Stable public identifier used by persisted dashboard selections.
        return "DuckDuckGo (ddgs)"

    def is_available(self) -> bool:
        """Return True when the ``ddgs`` package is importable.

        Probes the import once; cheap because Python caches the import. Must
        NOT perform network I/O — runs at tool-registration time and on every
        ``hermes tools`` paint.
        """
        try:
            import ddgs  # noqa: F401

            return True
        except ImportError:
            return False

    def supports_search(self) -> bool:
        return True

    def supports_extract(self) -> bool:
        return False

    def search(self, query: str, limit: int = 5) -> Dict[str, Any]:
        """Execute a DDGS metasearch and return normalized results.

        The synchronous ``ddgs`` call is run in a worker thread with a hard
        wall-clock timeout (``_SEARCH_TIMEOUT_SECS``) so a hung search cannot
        block the shared agent loop indefinitely (#36776).
        """
        try:
            import ddgs  # type: ignore  # noqa: F401 — availability probe
        except ImportError:
            return {
                "success": False,
                "error": "ddgs package is not installed — run `pip install ddgs`",
            }

        # DDGS().text yields at most `max_results` items; we cap defensively
        # in case the package ignores the hint.
        safe_limit = max(1, int(limit))
        options = _ddgs_search_options()

        # A fresh single-worker pool per call (rather than a module-level one)
        # is intentional: on timeout the blocking ddgs call cannot be cancelled
        # and keeps running, so a shared pool would serialise every later search
        # behind that hung worker. A per-call pool isolates each search from a
        # previously-hung one.
        pool = _cf.ThreadPoolExecutor(max_workers=1)
        if not _SEARCH_SLOTS.acquire(blocking=False):
            pool.shutdown(wait=False, cancel_futures=True)
            return {
                "success": False,
                "error": "DDGS search already in progress — upstream searches are saturated. Try again later.",
            }
        try:
            try:
                future = pool.submit(_run_ddgs_search_guarded, query, safe_limit, **options)
            except Exception:
                _SEARCH_SLOTS.release()
                raise
            try:
                web_results = future.result(timeout=_SEARCH_TIMEOUT_SECS)
            except _cf.TimeoutError:
                logger.warning(
                    "DDGS search timed out after %ds for query: %r",
                    _SEARCH_TIMEOUT_SECS, query,
                )
                return {
                    "success": False,
                    "error": (
                        f"DDGS search timed out after {_SEARCH_TIMEOUT_SECS}s — "
                        "an upstream engine may be rate-limiting or slow. Try again later "
                        "or switch to a different search provider."
                    ),
                }
        except Exception as exc:  # noqa: BLE001 — ddgs raises its own exceptions
            logger.warning("DDGS search error: %s", exc)
            return {"success": False, "error": f"DDGS search failed: {exc}"}
        finally:
            # Return immediately without joining the worker. On timeout the
            # already-running ddgs call can't be cancelled (cancel_futures only
            # affects not-yet-started work), so the worker runs to completion
            # on its own; it writes nothing shared, so leaking it is safe.
            pool.shutdown(wait=False, cancel_futures=True)

        logger.info(
            "DDGS search '%s': %d results (limit %d, region=%s, backend=%s, timelimit=%s)",
            query, len(web_results), limit, options["region"], options["backend"], options["timelimit"],
        )
        return {"success": True, "data": {"web": web_results}}

    def get_setup_schema(self) -> Dict[str, Any]:
        return {
            "name": "DuckDuckGo (ddgs)",
            "badge": "free · no key · search only",
            "tag": "No-key metasearch via the ddgs package; upstream limits and no SLA (search only)",
            "env_vars": [],
            # Trigger `_run_post_setup("ddgs")` after the user picks this row
            # so the ddgs Python package gets pip-installed on first selection.
            "post_setup": "ddgs",
        }
