"""POSIX PTY input round-trips surrogateescape bytes through write_stdin."""
import shlex
import sys
import time

import pytest

from tools.process_registry import ProcessRegistry


@pytest.mark.macos_only
def test_macos_pty_preserves_zshrc_and_stdin_without_nested_terminal(tmp_path, monkeypatch):
    pytest.importorskip("ptyprocess")
    import tools.process_registry as process_registry
    monkeypatch.setattr(process_registry, "_find_shell", lambda: "/bin/zsh")
    zdot = tmp_path / "z"
    zdot.mkdir()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "hermes-pty-tool").symlink_to(sys.executable)
    # A shell pre-hook checks this marker before wrapping its own PTY.
    (zdot / ".zshrc").write_text(
        f"export PATH={shlex.quote(str(bin_dir))}:$PATH\n"
        "[[ -n $PROCESS_LAUNCHED_BY_Q ]] || export HERMES_NESTED_PTY=1\n"
    )
    monkeypatch.delenv("PROCESS_LAUNCHED_BY_Q", raising=False)
    child = tmp_path / "child.py"
    received = tmp_path / "received.bin"
    child.write_text(
        "import os, tty\ntty.setraw(0)\n"
        "assert os.getenv('PROCESS_LAUNCHED_BY_Q') == '1'\n"
        "assert os.getenv('HERMES_NESTED_PTY') is None\n"
        "print('READY', flush=True)\n"
        "data = b''\nwhile len(data) < 2: data += os.read(0, 2 - len(data))\n"
        f"open({str(received)!r}, 'wb').write(data)\n"
    )
    registry = ProcessRegistry()
    session = registry.spawn_local(
        f"hermes-pty-tool {shlex.quote(str(child))}", cwd=str(tmp_path),
        env_vars={"ZDOTDIR": str(zdot)}, use_pty=True,
    )
    try:
        assert session._pty is not None
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and "READY" not in registry.poll(session.id)["output_preview"]:
            time.sleep(.02)
        assert "READY" in registry.poll(session.id)["output_preview"], registry.poll(session.id)
        assert registry.write_stdin(session.id, b"\xff\n".decode("utf-8", "surrogateescape"))["status"] == "ok"
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and not received.exists():
            time.sleep(.02)
        assert received.read_bytes() == b"\xff\n"
    finally:
        registry.kill_process(session.id)


@pytest.mark.macos_only
def test_write_stdin_pty_surrogateescape_roundtrip(tmp_path):
    pytest.importorskip("ptyprocess")
    registry = ProcessRegistry()
    out = tmp_path / "out.bin"
    script = tmp_path / "read_stdin.py"
    script.write_text(
        "import os, tty\ntty.setraw(0)\nprint('READY', flush=True)\n"
        "data = b''\nwhile len(data) < 2:\n"
        " data += os.read(0, 2 - len(data))\n"
        f"open({str(out)!r}, 'wb').write(data)\n"
    )
    session = registry.spawn_local(
        f"{shlex.quote(sys.executable)} {shlex.quote(str(script))}",
        cwd=str(tmp_path), use_pty=True,
    )
    try:
        assert session._pty is not None, "spawn_local fell back to pipe mode"
        assert session._reader_thread is not None
        # Wait until raw mode is installed before writing a byte that canonical mode transforms.
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if "READY" in registry.poll(session.id)["output_preview"]:
                break
            time.sleep(0.02)
        else:
            pytest.fail("PTY session never reached READY")
        result = registry.write_stdin(
            session.id, b"\xff".decode("utf-8", "surrogateescape") + "\n"
        )
        assert result["status"] == "ok", result
        assert result["bytes_written"] == 2
        deadline = time.monotonic() + 5
        got = b""
        while time.monotonic() < deadline:
            try:
                got = out.read_bytes()
            except FileNotFoundError:
                got = b""
            if got == b"\xff\n":
                break
            time.sleep(0.05)
        assert got == b"\xff\n", registry.poll(session.id)["output_preview"]
    finally:
        registry.kill_process(session.id)
