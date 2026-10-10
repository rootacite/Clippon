"""Process-state helpers shared by the session and the private audio instance."""

from __future__ import annotations

import os
import signal
import subprocess
import time
from pathlib import Path


def shell_status(code: int) -> int:
    """Match bash `wait`: a signal death is reported as 128+signal."""
    if code < 0:
        return 128 + (-code)
    return code


def gone(pid: int) -> bool:
    """True when *pid* does not exist or is a zombie waiting to be reaped."""
    stat = Path(f"/proc/{pid}/stat")
    try:
        text = stat.read_text()
    except OSError:
        return True
    try:
        after = text.rsplit(")", 1)[1].strip()
        state = after.split()[0]
    except (IndexError, ValueError):
        return False
    return state.startswith("Z")


def wait_gone(pid: int, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while True:
        if gone(pid):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.1)


def signal_pid(pid: int, sig: int) -> None:
    if pid <= 0:
        return
    try:
        os.kill(pid, sig)
    except OSError:
        pass


def signal_group(pid: int, sig: int) -> None:
    """Signal a setsid'd process group, falling back to the process itself."""
    if pid <= 0:
        return
    try:
        os.killpg(pid, sig)
    except OSError:
        signal_pid(pid, sig)


def reap(proc: subprocess.Popen[bytes] | None, timeout: float) -> None:
    if proc is None:
        return
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        pass


class RemoteProcess:
    """A process the helper started. We hold only its pid.

    Sunshine runs inside the instance's network namespace, so it cannot be a
    child of this process; the helper owns it and we talk to it by pid.
    """

    def __init__(self, pid: int) -> None:
        self.pid = pid

    def poll(self) -> int | None:
        return 0 if gone(self.pid) else None

    def wait(self, timeout: float | None = None) -> int:
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            if gone(self.pid):
                return 0
            if deadline is not None and time.monotonic() >= deadline:
                raise subprocess.TimeoutExpired(str(self.pid), timeout)
            time.sleep(0.1)


def env_has_pair(blob: bytes, key: str, value: str) -> bool:
    needle = f"{key}={value}".encode()
    return needle in blob.split(b"\0")


def process_has_env(pid: int, key: str, value: str) -> bool:
    try:
        blob = Path(f"/proc/{pid}/environ").read_bytes()
    except OSError:
        return False
    return env_has_pair(blob, key, value)


def pids_named(name: str) -> list[int]:
    try:
        proc = subprocess.run(
            ["pgrep", "-x", name],
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError:
        return []
    if proc.returncode != 0:
        return []
    found: list[int] = []
    for line in proc.stdout.split():
        try:
            found.append(int(line))
        except ValueError:
            continue
    return found


def terminate(pid: int) -> None:
    signal_pid(pid, signal.SIGTERM)


def kill(pid: int) -> None:
    signal_pid(pid, signal.SIGKILL)
