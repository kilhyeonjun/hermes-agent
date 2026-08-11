"""Single-writer and atomic-file helpers for host-wide Codex routing."""
from __future__ import annotations

from contextlib import contextmanager
import fcntl
import os
from pathlib import Path
import tempfile
import time
from typing import Iterator


DEFAULT_ROUTE_LOCK_PATH = Path.home() / ".hermes" / "state" / "codex_route.lock"


def assert_test_path_not_live(path: Path) -> None:
    """Block test-mode routing I/O anywhere below the caller's real home."""
    if not (
        os.environ.get("PYTEST_CURRENT_TEST")
        or os.environ.get("HERMES_TESTING") == "1"
    ):
        return
    real_home = Path(
        os.environ.get("HERMES_TEST_REAL_HOME") or Path.home()
    ).expanduser()
    lexical_root = Path(os.path.abspath(real_home))
    lexical_path = Path(os.path.abspath(path.expanduser()))
    resolved_root = lexical_root.resolve(strict=False)
    resolved_path = lexical_path.resolve(strict=False)
    if not (
        lexical_path.is_relative_to(lexical_root)
        or resolved_path.is_relative_to(resolved_root)
    ):
        return
    raise RuntimeError("Refusing live routing path during test run")


@contextmanager
def route_lock(
    *, path: Path | None = None, timeout: float = 30.0
) -> Iterator[None]:
    """Acquire the host-wide Codex route lock within a bounded interval."""
    lock_path = path or DEFAULT_ROUTE_LOCK_PATH
    assert_test_path_not_live(lock_path)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    os.fchmod(fd, 0o600)
    deadline = time.monotonic() + max(timeout, 0.0)
    try:
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise TimeoutError(
                        f"Codex route lock busy after {timeout:g}s"
                    )
                time.sleep(0.05)
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def atomic_write_bytes(path: Path, content: bytes, *, mode: int = 0o600) -> None:
    """Replace ``path`` from a unique same-directory, fsynced temp file."""
    assert_test_path_not_live(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, raw_tmp = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    tmp = Path(raw_tmp)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
    except Exception:
        try:
            os.close(fd)
        except OSError:
            pass
        tmp.unlink(missing_ok=True)
        raise
