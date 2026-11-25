#!/usr/bin/env python3
"""Run CustomerServiceAI with bounded, redacted stdout/stderr logs."""

from __future__ import annotations

import fcntl
import os
import signal
import subprocess
import sys
from collections import deque
from pathlib import Path

from logging_utils import redact_text


ROOT = Path(__file__).resolve().parent
LOG_PATH = ROOT / "customerserviceai.log"
MAIN_PATH = ROOT / "main.py"
LOCK_PATH = Path("/tmp/customerserviceai-log-runner.lock")
MAX_BYTES = 10 * 1024 * 1024
BACKUP_COUNT = 5


def _backup_path(path: Path, index: int) -> Path:
    return path.with_name(f"{path.name}.{index}")


def _write_private(path: Path, payload: bytes) -> None:
    temp = path.with_name(f".{path.name}.tmp.{os.getpid()}")
    with temp.open("wb") as handle:
        os.chmod(temp, 0o600)
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temp, path)


def normalize_existing_log(
    path: Path, *, max_bytes: int = MAX_BYTES, backups: int = BACKUP_COUNT
) -> bool:
    """Redact and bound an oversized closed log to the newest backup window."""
    if backups < 1 or max_bytes < 1 or not path.exists():
        return False
    size = path.stat().st_size
    if size <= max_bytes:
        os.chmod(path, 0o600)
        return False

    capacity = max_bytes * backups
    retained: deque[bytes] = deque()
    retained_size = 0
    with path.open("r", encoding="utf-8", errors="replace") as source:
        for line in source:
            payload = redact_text(line).encode("utf-8", "replace")
            if len(payload) > capacity:
                payload = payload[-capacity:]
            retained.append(payload)
            retained_size += len(payload)
            while retained_size > capacity:
                overflow = retained_size - capacity
                oldest = retained[0]
                if len(oldest) <= overflow:
                    retained.popleft()
                    retained_size -= len(oldest)
                else:
                    retained[0] = oldest[overflow:]
                    retained_size -= overflow

    payload = b"".join(retained)
    chunks = min(backups, (len(payload) + max_bytes - 1) // max_bytes)
    for index in range(1, chunks + 1):
        end = len(payload) - ((index - 1) * max_bytes)
        start = max(0, end - max_bytes)
        _write_private(_backup_path(path, index), payload[start:end])

    for index in range(chunks + 1, backups + 1):
        _backup_path(path, index).unlink(missing_ok=True)
    with path.open("r+b") as current:
        current.truncate(0)
        current.flush()
        os.fsync(current.fileno())
    os.chmod(path, 0o600)
    return True


class RotatingLogWriter:
    def __init__(self, path: Path, *, max_bytes: int = MAX_BYTES, backups: int = BACKUP_COUNT):
        self.path = path
        self.max_bytes = max_bytes
        self.backups = backups
        self.path.parent.mkdir(parents=True, exist_ok=True)
        normalize_existing_log(path, max_bytes=max_bytes, backups=backups)
        self._handle = path.open("ab", buffering=0)
        os.chmod(path, 0o600)
        self._size = path.stat().st_size

    def _rotate(self) -> None:
        self._handle.close()
        _backup_path(self.path, self.backups).unlink(missing_ok=True)
        for index in range(self.backups - 1, 0, -1):
            source = _backup_path(self.path, index)
            if source.exists():
                source.replace(_backup_path(self.path, index + 1))
        if self.path.exists():
            self.path.replace(_backup_path(self.path, 1))
        self._handle = self.path.open("ab", buffering=0)
        os.chmod(self.path, 0o600)
        self._size = 0

    def write(self, line: str) -> None:
        payload = redact_text(line).encode("utf-8", "replace")
        if self._size and self._size + len(payload) > self.max_bytes:
            self._rotate()
        self._handle.write(payload)
        self._size += len(payload)

    def close(self) -> None:
        if not self._handle.closed:
            self._handle.close()


def run() -> int:
    lock_handle = LOCK_PATH.open("a+")
    try:
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print("CustomerServiceAI log runner already active", file=sys.stderr)
        return 75

    writer = RotatingLogWriter(LOG_PATH)
    child: subprocess.Popen[str] | None = None

    def forward(signum: int, _frame: object) -> None:
        if child is not None and child.poll() is None:
            child.send_signal(signum)

    signal.signal(signal.SIGTERM, forward)
    signal.signal(signal.SIGINT, forward)

    try:
        child = subprocess.Popen(
            ["/usr/bin/python3", str(MAIN_PATH)],
            cwd=str(ROOT),
            env=os.environ.copy(),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
        assert child.stdout is not None
        for line in child.stdout:
            writer.write(line)
        return child.wait()
    finally:
        if child is not None and child.poll() is None:
            child.terminate()
            try:
                child.wait(timeout=10)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait(timeout=5)
        writer.close()
        lock_handle.close()


if __name__ == "__main__":
    raise SystemExit(run())
