"""Verify scripts/run_tests_parallel.py kills test-spawned grandchildren.

Setup
-----
A test in this file spawns a long-lived Python grandchild that writes
its PID + a nonce to a tempfile, then exits without cleaning up.
With the old ``subprocess.run`` runner, that grandchild would orphan
and outlive the test (and the whole runner). With the current Popen +
``start_new_session`` + ``_kill_tree`` runner, the grandchild gets
SIGKILL'd via process-group kill when its file's pytest exits.

The leaker test always passes — its only job is to spawn a grandchild
and walk away. The verifier runs the runner over the leaker file in a
subprocess, then waits for the grandchild PID to disappear from the
kernel's process table.

POSIX-only: Windows has its own grandchild lifecycle (no shared session,
``taskkill /F /T`` semantics). Marked accordingly.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest


# Both tests share the same handoff file: the leaker writes here, the
# verifier reads here. We park it in $TMPDIR with a unique-per-run name
# so concurrent invocations of the suite don't clobber each other.
_HANDOFF_DIR = Path(os.environ.get("TMPDIR", "/tmp")) / "hermes-isolation-probe"
_HANDOFF_DIR.mkdir(exist_ok=True)


def _handoff_path_for(nonce: str) -> Path:
    return _HANDOFF_DIR / f"grandchild-{nonce}.json"


def _pid_alive(pid: int) -> bool:
    """POSIX: send signal 0 to probe whether ``pid`` is still alive.

    ``os.kill(pid, 0)`` raises ``ProcessLookupError`` if the process is
    gone, ``PermissionError`` if it exists but we can't signal it
    (someone else's pid). We treat PermissionError as "alive" because
    the process exists and that's all we need to know.
    """
    if sys.platform == "win32":  # pragma: no cover — POSIX-only test
        # On Windows we'd use OpenProcess + GetExitCodeProcess; this
        # test is skipped on Windows so the path is unreachable.
        raise RuntimeError("_pid_alive POSIX-only")
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX-only probe")
@pytest.mark.live_system_guard_bypass
def test_grandchild_leak_is_killed_by_runner(tmp_path: Path) -> None:
    """Run the parallel runner over a probe file and verify cleanup.

    1. Materialize a probe file that spawns a long-lived grandchild and
       writes its PID to disk before exiting.
    2. Invoke ``scripts/run_tests_parallel.py`` against the probe file.
    3. Wait for the grandchild PID to vanish (poll for ~5s).
    4. Assert the runner exited cleanly AND the grandchild is dead.
    """
    repo_root = Path(__file__).resolve().parent.parent
    runner = repo_root / "scripts" / "run_tests_parallel.py"
    assert runner.exists(), f"runner missing at {runner}"

    # Probe lives in a temp dir, NOT under tests/, so the regular suite
    # never picks it up — only our explicit invocation does.
    probe_dir = tmp_path / "probe"
    probe_dir.mkdir()
    probe = probe_dir / "test_probe_leaker.py"
    nonce = f"{os.getpid()}-{int(time.time() * 1000)}"
    handoff = _handoff_path_for(nonce)
    if handoff.exists():
        handoff.unlink()

    probe_src = textwrap.dedent(f"""
        import json, os, subprocess, sys, time
        from pathlib import Path

        HANDOFF = Path({str(handoff)!r})

        def test_spawns_grandchild_and_walks_away():
            # Long-lived grandchild: detached, ignores SIGTERM (we want
            # SIGKILL or process-group kill to be the only thing that
            # works, simulating a misbehaving server).
            child = subprocess.Popen(
                [
                    sys.executable, "-c",
                    "import os, signal, sys, time; "
                    "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
                    "sys.stdout.write(f'gc-pgid={{os.getpgid(0)}} gc-pid={{os.getpid()}}\\\\n'); "
                    "sys.stdout.flush(); "
                    "time.sleep(600)",
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                # IMPORTANT: do NOT pass start_new_session here. We want
                # the grandchild to inherit the pytest subprocess's
                # process group, so when the runner kills the group the
                # grandchild dies too.
            )
            # Read the first line so we can record gc's pgid in the
            # handoff, then walk away — don't close the pipe (would
            # signal EOF and let the child see SIGPIPE on next write).
            first_line = child.stdout.readline().decode().strip()
            HANDOFF.write_text(json.dumps({{
                "pid": child.pid,
                "diag": first_line,
                "test_pid": os.getpid(),
                "test_pgid": os.getpgid(0),
            }}))
            assert child.pid > 0
    """).strip()
    probe.write_text(probe_src + "\n")

    # Run the parallel runner against just the probe file. The runner
    # discovers under ``tests/`` by default, so we override via --paths.
    proc = subprocess.run(
        [
            sys.executable,
            str(runner),
            "--paths",
            str(probe_dir),
            "-j",
            "1",
            # Tight per-file timeout: the probe finishes in <1s, no
            # need for 10min.
            "--file-timeout",
            "30",
        ],
        cwd=repo_root,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        timeout=60,
    )

    assert handoff.exists(), (
        f"probe never wrote handoff file; runner output:\n{proc.stdout}"
    )
    handoff_data = json.loads(handoff.read_text())
    grandchild_pid = handoff_data["pid"]
    diag = handoff_data.get("diag", "(no diag)")
    test_pid = handoff_data.get("test_pid")
    test_pgid = handoff_data.get("test_pgid")
    handoff.unlink()

    # The runner must have exited cleanly (probe test passes).
    assert proc.returncode == 0, (
        f"runner exited {proc.returncode}; output:\n{proc.stdout}"
    )

    # The grandchild must be gone. Poll for a bit because process-group
    # SIGKILL + reaping isn't synchronous; on a loaded box it can take
    # a beat.
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        if not _pid_alive(grandchild_pid):
            break
        time.sleep(0.05)
    else:
        # Test cleanup: kill the leaked grandchild ourselves so a
        # FAILED assertion doesn't leave a sleep(600) running.
        try:
            os.kill(grandchild_pid, 9)
        except ProcessLookupError:
            pass
        pytest.fail(
            f"grandchild PID {grandchild_pid} survived runner exit; "
            f"diag={diag!r} test_pid={test_pid} test_pgid={test_pgid}; "
            f"runner output:\n{proc.stdout}"
        )


# ── Bare pytest-flag passthrough ─────────────────────────────────────────────
#
# The runner routes any token starting with ``-`` that isn't one of its own
# options (``-j``/``--jobs``, ``--paths``, ``--slice``, ``--file-timeout``,
# ``--generate-slices``, ``--files``, ``--include-integration``) straight
# through to each per-file pytest invocation — no ``--`` separator required.
# Before this, a bare ``-q`` errored out with "unrecognized arguments",
# forcing a retry on every run. These tests are behavior contracts, not
# snapshots: they assert that bare flags reach pytest and that value-taking
# flags (``-k expr``) keep their value instead of having it stolen by the
# positional-path discovery.


def _make_probe_dir(tmp_path: Path) -> Path:
    """Two trivial passing tests, one named test_alpha, one test_beta."""
    probe_dir = tmp_path / "probe"
    probe_dir.mkdir()
    (probe_dir / "test_flagprobe.py").write_text(
        "def test_alpha():\n    assert True\n\n"
        "def test_beta():\n    assert True\n"
    )
    return probe_dir


def _run_runner(probe_dir: Path, *extra: str) -> subprocess.CompletedProcess:
    repo_root = Path(__file__).resolve().parent.parent
    runner = repo_root / "scripts" / "run_tests_parallel.py"
    return subprocess.run(
        [sys.executable, str(runner), "--paths", str(probe_dir),
         "-j", "1", "--file-timeout", "30", *extra],
        cwd=repo_root,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        timeout=60,
    )


def test_bare_q_flag_passes_through(tmp_path: Path) -> None:
    """A bare ``-q`` (no ``--``) runs clean instead of erroring out."""
    probe_dir = _make_probe_dir(tmp_path)
    proc = _run_runner(probe_dir, "-q")
    assert proc.returncode == 0, proc.stdout
    assert "unrecognized arguments" not in proc.stdout


def test_bare_value_flag_keeps_its_value(tmp_path: Path) -> None:
    """``-k test_alpha`` reaches pytest as a selector, not as a path.

    The value token (``test_alpha``) must NOT be swallowed by the runner's
    positional-path discovery — if it were, discovery would look for a path
    named ``test_alpha``, find nothing, and the run would degrade. We assert
    the run succeeds AND only one of the two tests was selected (proving the
    ``-k`` filter actually applied inside pytest).
    """
    probe_dir = _make_probe_dir(tmp_path)
    proc = _run_runner(probe_dir, "-k", "test_alpha")
    assert proc.returncode == 0, proc.stdout
    # Exactly one test selected: the per-file summary shows "1✓" (1 passed).
    # test_beta is deselected by the -k filter.
    assert "1✓" in proc.stdout or "1 passed" in proc.stdout, proc.stdout
    assert "2✓" not in proc.stdout, (
        f"both tests ran — -k filter did not apply:\n{proc.stdout}"
    )


def test_explicit_double_dash_still_works(tmp_path: Path) -> None:
    """The legacy ``--`` separator keeps working alongside bare flags."""
    probe_dir = _make_probe_dir(tmp_path)
    proc = _run_runner(probe_dir, "-q", "--", "--tb=short")
    assert proc.returncode == 0, proc.stdout
    assert "unrecognized arguments" not in proc.stdout


def test_positional_path_not_treated_as_flag(tmp_path: Path) -> None:
    """A positional path arg still overrides discovery (not routed to pytest)."""
    probe_dir = _make_probe_dir(tmp_path)
    repo_root = Path(__file__).resolve().parent.parent
    runner = repo_root / "scripts" / "run_tests_parallel.py"
    # Pass the probe dir positionally (no --paths), plus a bare -q.
    proc = subprocess.run(
        [sys.executable, str(runner), str(probe_dir), "-j", "1",
         "--file-timeout", "30", "-q"],
        cwd=repo_root, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, timeout=60,
    )
    assert proc.returncode == 0, proc.stdout
    # Discovery found the probe file (2 tests), proving the positional path
    # was consumed as a root, not forwarded to pytest as a bad flag.
    assert "test_flagprobe.py" in proc.stdout, proc.stdout


def test_runner_isolates_home_before_test_module_import(tmp_path: Path) -> None:
    """Collection-time imports must never see or mutate the caller's HOME."""
    repo_root = Path(__file__).resolve().parent.parent
    runner = repo_root / "scripts" / "run_tests_parallel.py"
    outer_home = tmp_path / "outer-home"
    outer_hermes = outer_home / ".hermes"
    outer_hermes.mkdir(parents=True)
    protected_auth = outer_hermes / "auth.json"
    protected_auth.write_text("protected\n")

    probe_dir = tmp_path / "home-probe"
    probe_dir.mkdir()
    handoff = tmp_path / "home-probe.json"
    probe = probe_dir / "test_home_import.py"
    probe.write_text(
        textwrap.dedent(
            f"""
            import json
            import os
            import subprocess
            import sys
            from pathlib import Path
            from hermes_constants import get_real_home

            HOME = Path.home()
            HERMES_HOME = Path(os.environ["HERMES_HOME"])
            REAL_HOME = Path(get_real_home())
            (HOME / ".hermes").mkdir(parents=True, exist_ok=True)
            (HOME / ".hermes" / "auth.json").write_text("test-write\\n")
            subprocess.run(
                [
                    sys.executable,
                    "-c",
                    "from pathlib import Path; from hermes_constants import get_real_home; "
                    "p = Path(get_real_home()) / '.hermes' / 'subprocess-auth.json'; "
                    "p.parent.mkdir(parents=True, exist_ok=True); "
                    "p.write_text('child-write\\\\n')",
                ],
                check=True,
            )
            Path({str(handoff)!r}).write_text(json.dumps({{
                "home": str(HOME),
                "hermes_home": str(HERMES_HOME),
                "real_home": str(REAL_HOME),
                "child_marker": str(HOME / ".hermes" / "subprocess-auth.json"),
                "child_exists": (HOME / ".hermes" / "subprocess-auth.json").exists(),
            }}))

            def test_import_completed():
                assert HERMES_HOME == HOME / ".hermes"
                assert REAL_HOME == HOME
            """
        ).strip()
        + "\n"
    )

    env = os.environ.copy()
    env["HOME"] = str(outer_home)
    env["HERMES_HOME"] = str(outer_hermes)
    env.pop("HERMES_TEST_HOME_ISOLATED", None)
    env.pop("HERMES_TEST_REAL_HOME", None)
    proc = subprocess.run(
        [
            sys.executable,
            str(runner),
            "--paths",
            str(probe_dir),
            "-j",
            "1",
            "--file-timeout",
            "30",
            "-q",
        ],
        cwd=repo_root,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        timeout=60,
    )

    assert proc.returncode == 0, proc.stdout
    assert handoff.exists(), proc.stdout
    payload = json.loads(handoff.read_text())
    assert Path(payload["home"]) != outer_home
    assert Path(payload["hermes_home"]) == Path(payload["home"]) / ".hermes"
    assert Path(payload["real_home"]) == Path(payload["home"])
    assert Path(payload["child_marker"]).is_relative_to(Path(payload["home"]))
    assert payload["child_exists"] is True
    assert protected_auth.read_text() == "protected\n"


def test_shell_runner_does_not_depend_on_optional_host_live_guard() -> None:
    """The canonical runner owns isolation even on a clean host or CI worker."""
    repo_root = Path(__file__).resolve().parent.parent
    script = (repo_root / "scripts" / "run_tests.sh").read_text()

    assert "pytest_live_guard" not in script
    assert '"$PYTHON" "$SCRIPT_DIR/run_tests_parallel.py"' in script


def test_shell_runner_supports_native_windows_virtualenv_layout(tmp_path: Path) -> None:
    """The canonical wrapper selects ``Scripts/python.exe`` when ``bin`` is absent."""
    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("bash is unavailable")

    repo_root = Path(__file__).resolve().parent.parent
    fake_repo = tmp_path / "repo"
    scripts_dir = fake_repo / "scripts"
    windows_venv = fake_repo / ".venv" / "Scripts"
    scripts_dir.mkdir(parents=True)
    windows_venv.mkdir(parents=True)
    shutil.copy2(repo_root / "scripts" / "run_tests.sh", scripts_dir)
    (scripts_dir / "run_tests_parallel.py").write_text("# probe\n", encoding="utf-8")
    fake_python = windows_venv / "python.exe"
    fake_python.write_text(
        "#!/usr/bin/env bash\n"
        "printf '%s\\n' \"$@\" > \"$(dirname \"$0\")/invocation.txt\"\n",
        encoding="utf-8",
    )
    fake_python.chmod(0o755)

    proc = subprocess.run(
        [bash, str(scripts_dir / "run_tests.sh"), "tests/example.py", "-q"],
        cwd=fake_repo,
        env={**os.environ, "HOME": str(tmp_path / "home")},
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=30,
    )

    assert proc.returncode == 0, proc.stdout
    invocation = (windows_venv / "invocation.txt").read_text(encoding="utf-8")
    assert str(scripts_dir / "run_tests_parallel.py") in invocation
    assert "tests/example.py" in invocation


def test_runner_fails_if_caller_live_state_changes(tmp_path: Path) -> None:
    """A direct-path escape is detected even when it bypasses guarded APIs."""
    repo_root = Path(__file__).resolve().parent.parent
    runner = repo_root / "scripts" / "run_tests_parallel.py"
    outer_home = tmp_path / "outer-home"
    outer_hermes = outer_home / ".hermes"
    outer_hermes.mkdir(parents=True)
    protected_auth = outer_hermes / "auth.json"
    protected_auth.write_text("before\n")

    probe_dir = tmp_path / "live-drift-probe"
    probe_dir.mkdir()
    probe = probe_dir / "test_live_drift.py"
    probe.write_text(
        "import os\n"
        "from pathlib import Path\n\n"
        "def test_direct_live_write():\n"
        "    live = Path(os.environ['HERMES_TEST_REAL_HOME']) / '.hermes' / 'auth.json'\n"
        "    live.write_text('after\\n')\n"
    )

    env = os.environ.copy()
    env["HOME"] = str(outer_home)
    env.pop("HERMES_TEST_REAL_HOME", None)
    proc = subprocess.run(
        [
            sys.executable,
            str(runner),
            "--paths",
            str(probe_dir),
            "-j",
            "1",
            "--file-timeout",
            "30",
            "-q",
        ],
        cwd=repo_root,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        timeout=60,
    )

    assert proc.returncode == 1, proc.stdout
    assert "caller live state changed during test run" in proc.stdout
    assert ".hermes/auth.json" in proc.stdout


def test_live_state_snapshot_failure_is_fail_closed(tmp_path: Path, monkeypatch) -> None:
    from scripts import run_tests_parallel as runner

    monkeypatch.setattr(
        runner,
        "_path_fingerprint",
        lambda _path: ("error:PermissionError", 0, 0, ""),
    )

    with pytest.raises(RuntimeError, match="cannot snapshot caller live state"):
        runner._live_state_snapshot(tmp_path, (Path(".hermes/auth.json"),))


def test_symlink_fingerprint_tracks_target_content(tmp_path: Path) -> None:
    from scripts import run_tests_parallel as runner

    target = tmp_path / "account-auth.json"
    target.write_text("before\n")
    link = tmp_path / "auth.json"
    link.symlink_to(target)

    before = runner._path_fingerprint(link)
    target.write_text("after\n")
    after = runner._path_fingerprint(link)

    assert before[0] == "symlink:file"
    assert after[0] == "symlink:file"
    assert before != after


def test_live_state_labels_redact_dynamic_identity_names() -> None:
    from scripts import run_tests_parallel as runner

    assert runner._safe_live_state_label(
        Path(".hermes/profiles/private-name/auth.json")
    ) == ".hermes/profiles/<redacted>/auth.json"
    assert runner._safe_live_state_label(
        Path(".codex/accounts/private-name/auth.json")
    ) == ".codex/accounts/<redacted>/auth.json"
    assert runner._safe_live_state_label(
        Path(".cli-proxy-api/codex-private-name.json")
    ) == ".cli-proxy-api/codex-<redacted>.json"


def test_runner_post_snapshot_runs_when_main_raises(tmp_path: Path, monkeypatch) -> None:
    from scripts import run_tests_parallel as runner

    snapshots = []
    monkeypatch.setenv("HERMES_TEST_REAL_HOME", str(tmp_path))
    monkeypatch.setattr(runner, "_live_state_guard_paths", lambda _home: ())
    monkeypatch.setattr(
        runner,
        "_live_state_snapshot",
        lambda _home, paths: snapshots.append(paths) or {},
    )
    monkeypatch.setattr(
        runner,
        "_main",
        lambda: (_ for _ in ()).throw(KeyboardInterrupt()),
    )

    with pytest.raises(KeyboardInterrupt):
        runner.main()

    assert snapshots == [(), ()]


def test_live_state_guard_covers_secret_and_priority_state(tmp_path: Path) -> None:
    from scripts import run_tests_parallel as runner

    profile = tmp_path / ".hermes" / "profiles" / "private-name"
    profile.mkdir(parents=True)
    paths = set(runner._live_state_guard_paths(tmp_path))

    assert Path(".hermes/.env") in paths
    assert Path(".hermes/.anthropic_oauth.json") in paths
    assert Path(".hermes/state/codex_reset_aware_warmup.json") in paths
    assert Path(".hermes/profiles/private-name/.env") in paths
    assert Path(".hermes/profiles/private-name/.anthropic_oauth.json") in paths
    assert (
        Path(
            ".hermes/profiles/private-name/state/"
            "codex_reset_aware_warmup.json"
        )
        in paths
    )


def test_direct_pytest_isolates_home_before_collection(tmp_path: Path) -> None:
    """The repository conftest protects direct python -m pytest too."""
    repo_root = Path(__file__).resolve().parent.parent
    outer_home = tmp_path / "outer-home"
    outer_hermes = outer_home / ".hermes"
    outer_hermes.mkdir(parents=True)
    protected_auth = outer_hermes / "auth.json"
    protected_auth.write_text("protected\n")
    handoff = tmp_path / "direct-home.json"
    probe = repo_root / "tests" / "fixtures" / "direct_pytest_home_guard_probe.py"
    env = os.environ.copy()
    env["HOME"] = str(outer_home)
    env["HERMES_DIRECT_PROBE_MODE"] = "home"
    env["HERMES_DIRECT_PROBE_HANDOFF"] = str(handoff)
    for key in (
        "HERMES_HOME",
        "HERMES_REAL_HOME",
        "HERMES_TESTING",
        "HERMES_TEST_REAL_HOME",
        "HERMES_TEST_HOME_ISOLATED",
    ):
        env.pop(key, None)
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", str(probe), "-q"],
        cwd=repo_root,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        timeout=60,
    )

    assert proc.returncode == 0, proc.stdout
    payload = json.loads(handoff.read_text())
    assert Path(payload["home"]) != outer_home
    assert Path(payload["real_home"]) == Path(payload["home"])
    assert Path(payload["hermes_home"]) == Path(payload["home"]) / ".hermes"
    assert protected_auth.read_text() == "protected\n"


def test_direct_pytest_fails_on_caller_live_state_drift(tmp_path: Path) -> None:
    """The direct-pytest sessionfinish hook enforces the same invariant."""
    repo_root = Path(__file__).resolve().parent.parent
    outer_home = tmp_path / "outer-home"
    outer_auth = outer_home / ".hermes" / "auth.json"
    outer_auth.parent.mkdir(parents=True)
    outer_auth.write_text("before\n")
    probe = repo_root / "tests" / "fixtures" / "direct_pytest_home_guard_probe.py"
    env = os.environ.copy()
    env["HOME"] = str(outer_home)
    env["HERMES_DIRECT_PROBE_MODE"] = "drift"
    for key in (
        "HERMES_HOME",
        "HERMES_REAL_HOME",
        "HERMES_TESTING",
        "HERMES_TEST_REAL_HOME",
        "HERMES_TEST_HOME_ISOLATED",
    ):
        env.pop(key, None)
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", str(probe), "-q"],
        cwd=repo_root,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        timeout=60,
    )

    assert proc.returncode != 0, proc.stdout
    assert "caller live state changed during direct pytest" in proc.stdout
    assert ".hermes/auth.json" in proc.stdout
