"""Cross-process exclusive ownership of a Telethon session file."""

import asyncio
import os
import time
from pathlib import Path

AUTH_HANDOFF_TIMEOUT_SECONDS = 300


class TelegramSessionLock:
    """Hold an OS lock next to a Telethon session; the OS releases it on exit."""

    def __init__(self, session_path: Path):
        self.session_path = Path(session_path).expanduser().resolve()
        self.lock_path = Path(f"{self.session_path}.hirevia.lock")
        self._handle = None

    @property
    def acquired(self) -> bool:
        return self._handle is not None

    def try_acquire(self) -> bool:
        if self._handle is not None:
            return True

        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.lock_path.open("a+b")
        try:
            if os.name == "nt":
                import msvcrt

                handle.seek(0, os.SEEK_END)
                if handle.tell() == 0:
                    handle.write(b"\0")
                    handle.flush()
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            handle.close()
            return False

        self._handle = handle
        return True

    async def acquire(self, *, timeout: float = 0.0, poll_seconds: float = 0.1) -> bool:
        deadline = time.monotonic() + max(0.0, timeout)
        while True:
            if self.try_acquire():
                return True
            if time.monotonic() >= deadline:
                return False
            await asyncio.sleep(min(poll_seconds, max(0.0, deadline - time.monotonic())))

    def release(self) -> None:
        handle = self._handle
        if handle is None:
            return
        self._handle = None
        try:
            if os.name == "nt":
                import msvcrt

                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()

    def __enter__(self):
        if not self.try_acquire():
            raise RuntimeError("Telegram session is owned by another process")
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.release()
        return False
