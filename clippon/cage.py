"""Headless cage: its environment, the client wrapper, and its Wayland socket."""

from __future__ import annotations

import os
import re
import subprocess
from collections.abc import Mapping
from pathlib import Path

from clippon.config import CURSOR_SHIM_PATH, SHIM_PATH

# cage starts its client as soon as the socket exists, so the mode is set from
# inside that client, before the app itself runs. An app that only reads the
# mode at startup would otherwise come up at wlroots' 1280x720 default.
# LD_PRELOAD is stripped here so the seat shim stays out of the streamed app.
_MODE_SCRIPT = """\
out=$(wlr-randr 2>/dev/null | head -n1 | cut -d" " -f1)
if [ -n "$out" ]; then
    wlr-randr --output "$out" --custom-mode "$STREAM_RES_IN" >/dev/null 2>&1
fi
exec "$@"
"""

_PID_IN_SS = re.compile(r"pid=(\d+)\b")
_WAYLAND_IN_SS = re.compile(r"wayland-[0-9]+")


def client_argv(command: tuple[str, ...], resolution: str) -> list[str]:
    if not resolution:
        return ["env", "-u", "LD_PRELOAD", "--", *command]
    return [
        "env",
        "-u",
        "LD_PRELOAD",
        f"STREAM_RES_IN={resolution}",
        "sh",
        "-c",
        _MODE_SCRIPT,
        "sh",
        *command,
    ]


def cage_environment(
    audio_env: dict[str, str],
    shim: Path = SHIM_PATH,
    cursor_shim: Path = CURSOR_SHIM_PATH,
    seat: str = "",
    extra: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Environment for cage itself. The client wrapper drops LD_PRELOAD again.

    Two shims ride along: the seat-name override, and the cursor mapping that
    stands in for the WL_OUTPUT property wlroots 0.20 does not read. *seat* is
    the name the override reports, so this instance reads only the devices
    udev tagged for it.

    *extra* is what `--env` asked for. It beats the value inherited from the
    environment, and the entries set below still beat it in turn: the display,
    the audio socket and the shims stay this function's to decide.
    """
    env = os.environ.copy()
    if extra:
        env.update(extra)
    env.pop("WAYLAND_DISPLAY", None)
    env.pop("DISPLAY", None)
    env.update(audio_env)
    preload = [str(shim)]
    if cursor_shim.is_file():
        preload.append(str(cursor_shim))
    env.update(
        {
            "WLR_BACKENDS": "headless,libinput",
            "WLR_LIBINPUT_NO_DEVICES": "1",
            "WLR_NO_HARDWARE_CURSORS": "1",
            "WLR_HEADLESS_OUTPUTS": "1",
            "LIBSEAT_BACKEND": "noop",
            "LD_PRELOAD": ":".join(preload),
        }
    )
    if seat:
        env["CLIPPON_SEAT"] = seat
    return env


def start_cage(
    command: tuple[str, ...],
    resolution: str,
    audio_env: dict[str, str],
    log_file: object,
    shim: Path = SHIM_PATH,
    cursor_shim: Path = CURSOR_SHIM_PATH,
    seat: str = "",
    extra: Mapping[str, str] | None = None,
) -> subprocess.Popen[bytes]:
    return subprocess.Popen(
        ["cage", "--", *client_argv(command, resolution)],
        env=cage_environment(audio_env, shim, cursor_shim, seat, extra),
        stdout=log_file,  # type: ignore[arg-type]
        stderr=subprocess.STDOUT,
    )


def wayland_names(runtime_dir: Path) -> list[str]:
    names: list[str] = []
    try:
        entries = list(runtime_dir.iterdir())
    except OSError:
        return names
    for entry in entries:
        name = entry.name
        if name.startswith("wayland-") and not name.endswith(".lock"):
            names.append(name)
    return names


def wayland_socket_on_line(line: str, pid: int) -> str | None:
    """Pull wayland-N out of one `ss -xlp` line owned by *pid*.

    The pid has to match as a whole token: `pid=12` must not hit `pid=123`.
    """
    if not any(int(found) == pid for found in _PID_IN_SS.findall(line)):
        return None
    match = _WAYLAND_IN_SS.search(line)
    if match is None:
        return None
    return match.group(0)


def socket_from_ss(pid: int) -> str | None:
    """Return the wayland-N socket *pid* is listening on, if ss can see it."""
    try:
        proc = subprocess.run(
            ["ss", "-xlp"],
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError:
        return None
    if proc.returncode != 0:
        return None
    for line in proc.stdout.splitlines():
        found = wayland_socket_on_line(line, pid)
        if found:
            return found
    return None


def new_wayland_socket(runtime_dir: Path, before: set[str]) -> str | None:
    """Fallback once ss never sees a socket: the new name in the runtime dir."""
    for name in wayland_names(runtime_dir):
        if name not in before:
            return name
    return None


def current_mode(socket_name: str) -> str:
    """The `(current)` mode line, or "" when wlr-randr cannot answer."""
    try:
        proc = subprocess.run(
            ["wlr-randr"],
            check=False,
            capture_output=True,
            text=True,
            env=os.environ | {"WAYLAND_DISPLAY": socket_name},
        )
    except OSError:
        return ""
    if proc.returncode != 0:
        return ""
    for line in proc.stdout.splitlines():
        if "(current)" in line:
            return line.strip()
    return ""
