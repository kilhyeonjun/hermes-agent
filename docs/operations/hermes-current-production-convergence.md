# Current-Production Convergence — Phase A Manifest

Phase A of the Mac mini convergence (personal fork, 2026-10-06). It changes no feature behavior. It does three things:

- rebuilds the runtime from `uv.lock` so Tool Search assembles again
- moves SQLite to a fixed version
- adds one reviewed fix to the previously unreviewed deployed snapshot

Phase B covers skill write containment, Slack alias exclusion, MCP stale deregistration, and Codex usage accounting. It is out of scope here and needs a separate approval and cutover.

## Source

- **Base, and the production rollback anchor:** `a7be3e65b64745b87e213d944ce16a4739ea39f3` on `personal/deployed-snapshot-20261006`. This is byte-identical to the working tree the live gateways ran before Phase A.
- **Candidate branch:** `personal/release/current-convergence-20260906`.
- **Candidate commits on top of the base:**
  1. `fix(telegram): keep sending remaining images after one fails`. The deployed snapshot raised on the first failed animation or album-fallback send, which skipped later images. The fix attempts every image, then raises the first failure. It passed one independent review.
  2. This manifest.
- **Snapshot review of `835bdfaced`:**
  - The Telegram adapter needed the fix above.
  - The other nine runtime files were kept as is: chat-completion watchdog error field, run notifications logging, shutdown forensics `ps` portability, openai-codex image `api_model`, approval-pattern anchoring (match-equivalent, faster), delegate SessionDB path guard, process registry PTY marker, local transcription compute type, parallel test runner error count.

## Runtime

- **Python:** 3.11 private managed runtime.
- **SQLite:** 3.53.1, up from the vulnerable 3.50.4.
- **Venv:** built by `repair_vulnerable_runtime()` through `uv sync --extra all --locked`.
- **Lazy features pre-installed at their `tools/lazy_deps.py` pins:** These match every feature that was installed in production. The production install was satisfied for most of them, but `platform.telegram` and `platform.slack` ran on older unpinned versions.
  - provider.anthropic, provider.bedrock, provider.vertex
  - search.firecrawl, tts.edge, image.fal, memory.honcho
  - platform.telegram, platform.discord, platform.slack, platform.wecom_callback, platform.teams
  - skill.youtube, tool.acp, tool.doc_extract, tool.computer_use
- **Extra packages installed at production versions:** These have no source pin. `ddgs` (configured web search) and `lxml` (document skills) are used; `simple-term-menu` was installed for parity.
- **Test runner and packaging:** `pytest`, `pytest-asyncio`, and `setuptools` at the `dev` extra pins. The packaging guard tests import `setuptools`.
- **Production packages intentionally dropped:** `loguru` and `pip`. Source does not import them, and lazy installs use `uv`.

## Gates

- **Focused suite (24 files: preserved behavior, Astra, usage and cost provenance, GPT-6, snapshot-touched runtime, Tool Search):** 1009 passed, 18 skipped, 0 failed on the candidate venv. The same files under the drifted production venv gave 71 Tool Search failures, all from the missing `snowballstemmer`.
- **Full suite:** ran once with `scripts/run_tests_parallel.py` (per-file isolation, 8 workers, 1438 s). An earlier single-process `pytest -q` run was aborted at about 5% and is not evidence; the repository documents it as the cross-file state-leak flake source.
  - Result: 38 failed tests in 12 files, plus one file that hit the runner's 300 s per-file timeout.
  - **Same failures in the old production venv at the base SHA (31 tests).** Ran the same files with the drifted production venv at the base SHA.
    - `test_relaunch` (1)
    - `test_hindsight_provider` (8; the package is not installed)
    - `test_browser_open_timeout` (2)
    - `test_daytona_environment` (15; the package is not installed)
    - `test_read_special_file_guard` (1)
    - `test_process_registry_write_stdin_surrogates` (1)
    - `test_web_providers_searxng` (1)
    - `test_web_tools_config` (2)
  - **Load-only failures (pass in isolation).**
    - `test_delegate_timeout_cleanup` passes in isolation.
    - `tests/run_agent/test_run_agent.py` passes standalone: 282 passed in 47 s.
  - **Caused by the candidate venv.**
    - The 5 packaging failures came from the missing `setuptools`. They passed after the `setuptools` pin was installed.
    - `test_hermes_state.py::TestFTS5Search::test_search_projection_skips_context_enrichment_queries`. Fixed SQLite enables WAL, so `_get_read_conn()` opens a fresh read connection on every call. The test traces only the connection it grabbed itself, so it counts zero context queries, while its functional assertions (`context` present) pass. The vulnerable runtime skipped the WAL path. This is a test instrumentation defect, recorded for Phase B.
- **Final focused gates:** 1154 passed, 19 skipped, 0 failed. Coverage was the preserved-behavior and snapshot sets, Tool Search, managed-uv, SQLite runtime, WAL-reset, and packaging.
- **Cutover set:**
  - Covered: the owner-run profiles `default`, `gameduo`, and `seul`.
  - Must stay `gateway_state=stopped`: the owner-account `penguincouple` copy. Its live gateway runs from a separate install outside this cutover.
- **Rollback:** the recorded base SHA and branch, the saved venv, and the per-profile database bundle, restored as one unit.
