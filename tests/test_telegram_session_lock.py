"""Cross-process exclusion for the persistent Telethon session file."""

import os
import subprocess
import sys
import time
from pathlib import Path

from hirevia.sources.telegram_session_lock import TelegramSessionLock


def test_session_lock_is_exclusive_between_instances_and_released(tmp_path):
    session_path = tmp_path / "sessions" / "user"
    first = TelegramSessionLock(session_path)
    second = TelegramSessionLock(session_path)

    assert first.try_acquire() is True
    assert second.try_acquire() is False
    first.release()
    assert second.try_acquire() is True
    second.release()
    assert second.lock_path.exists()


def test_session_lock_excludes_another_process(tmp_path):
    session_path = tmp_path / "sessions" / "user"
    lock = TelegramSessionLock(session_path)
    assert lock.try_acquire() is True

    script = (
        "from pathlib import Path; "
        "from hirevia.sources.telegram_session_lock import TelegramSessionLock; "
        f"lock=TelegramSessionLock(Path({str(session_path)!r})); "
        "raise SystemExit(0 if lock.try_acquire() else 3)"
    )
    blocked = subprocess.run(
        [sys.executable, "-c", script],
        cwd=Path.cwd(),
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert blocked.returncode == 3, blocked.stderr

    lock.release()
    acquired = subprocess.run(
        [sys.executable, "-c", script],
        cwd=Path.cwd(),
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert acquired.returncode == 0, acquired.stderr


def test_stale_lock_file_does_not_block_acquisition(tmp_path):
    lock = TelegramSessionLock(tmp_path / "sessions" / "user")
    lock.lock_path.parent.mkdir(parents=True)
    lock.lock_path.write_text("stale metadata", encoding="utf-8")

    assert lock.try_acquire() is True
    lock.release()
    assert lock.lock_path.read_text(encoding="utf-8") == "stale metadata"


def test_process_crash_releases_lock_without_changing_session(tmp_path):
    session_path = tmp_path / "sessions" / "user"
    session_path.parent.mkdir(parents=True)
    session_file = session_path.with_suffix(".session")
    session_file.write_bytes(b"authorized-session")
    ready_file = tmp_path / "owner-ready"
    script = (
        "import time; from pathlib import Path; "
        "from hirevia.sources.telegram_session_lock import TelegramSessionLock; "
        f"lock=TelegramSessionLock(Path({str(session_path)!r})); "
        "assert lock.try_acquire(); "
        f"Path({str(ready_file)!r}).write_text('ready'); "
        "time.sleep(60)"
    )
    python_executable = (
        getattr(sys, "_base_executable", sys.executable)
        if os.name == "nt"
        else sys.executable
    )
    owner = subprocess.Popen(
        [python_executable, "-c", script],
        cwd=Path.cwd(),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    contender = TelegramSessionLock(session_path)
    try:
        deadline = time.monotonic() + 10
        while not ready_file.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert ready_file.exists()
        assert contender.try_acquire() is False

        owner.kill()
        owner.wait(timeout=10)

        assert contender.try_acquire() is True
        assert session_file.read_bytes() == b"authorized-session"
    finally:
        contender.release()
        if owner.poll() is None:
            owner.kill()
            owner.wait(timeout=10)
