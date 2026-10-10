"""Sunshine process: tray decision, launch, and the fatal-line check."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from clippon.config import ClipponError
from clippon.helper import call as helper_call
from clippon.log import warn
from clippon.proc import RemoteProcess

_FATAL = re.compile(r"fatal|failed to bind|address already in use", re.IGNORECASE)

# Avahi publishes _nvstream._tcp with no config switch. A dead system bus makes
# that publish fail, which Sunshine treats as non-fatal, and leaves the session
# bus (tray, the streamed app) alone.
DEAD_SYSTEM_BUS = "unix:path=/run/clippon/no-system-bus"


@dataclass(frozen=True)
class TrayPlan:
    args: tuple[str, ...]
    note: str
    disabled: bool


def tray_host_present() -> bool:
    if shutil.which("busctl") is None:
        return False
    try:
        proc = subprocess.run(
            [
                "busctl",
                "--user",
                "call",
                "org.freedesktop.DBus",
                "/org/freedesktop/DBus",
                "org.freedesktop.DBus",
                "NameHasOwner",
                "s",
                "org.kde.StatusNotifierWatcher",
            ],
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError:
        return False
    return proc.returncode == 0 and "true" in proc.stdout.lower()


def managed_tray(requested: str) -> str:
    """Daemon instances share the desktop StatusNotifier host.

    auto would keep one Sunshine icon per instance. The foreground clippon
    command still uses auto; only the manager forces the tray off.
    """
    if requested == "enabled":
        return "enabled"
    return "disabled"


def tray_plan(mode: str) -> TrayPlan:
    """auto disables the tray unless a StatusNotifier host is actually there.

    Sunshine's Linux main loop *is* the tray loop. With system_tray=enabled and
    no desktop panel it exits immediately, which is what happens under getty.
    """
    if mode == "disabled":
        return TrayPlan(("system_tray=disabled",), "disabled", True)
    if mode == "enabled":
        return TrayPlan((), "kept", False)
    if tray_host_present():
        return TrayPlan((), "kept", False)
    return TrayPlan(
        ("system_tray=disabled",),
        "disabled (no StatusNotifier host)",
        True,
    )


def managed_env(addr_shim: Path) -> dict[str, str]:
    """Environment that keeps a managed Sunshine off the host's mDNS and ports.

    The address shim is loaded only into this process. Cage keeps the seat shim.
    """
    env = {"DBUS_SYSTEM_BUS_ADDRESS": DEAD_SYSTEM_BUS}
    if addr_shim.is_file():
        env["LD_PRELOAD"] = str(addr_shim)
    else:
        warn(
            "sun",
            "%s is missing; Moonlight will be told the namespace address",
            addr_shim,
        )
    return env


def sunshine_command(
    plan: TrayPlan,
    socket_name: str,
    audio_env: dict[str, str],
    config_file: Path | None = None,
    extra_env: dict[str, str] | None = None,
) -> tuple[list[str], dict[str, str]]:
    env = os_env()
    env.update(audio_env)
    if extra_env:
        env.update(extra_env)
    env["WAYLAND_DISPLAY"] = socket_name
    argv = ["sunshine"]
    # A path with no '=' is the config file. system_tray=disabled stays a setting.
    if config_file is not None:
        argv.append(str(config_file))
    argv.extend(plan.args)
    return argv, env


def start_sunshine(
    plan: TrayPlan,
    socket_name: str,
    audio_env: dict[str, str],
    log_file: object,
    config_file: Path | None = None,
    extra_env: dict[str, str] | None = None,
) -> subprocess.Popen[bytes]:
    """Foreground mode: Sunshine runs as our child, on the host network."""
    argv, env = sunshine_command(plan, socket_name, audio_env, config_file, extra_env)
    return subprocess.Popen(
        argv,
        env=env,
        stdout=log_file,  # type: ignore[arg-type]
        stderr=subprocess.STDOUT,
    )


def start_sunshine_in_netns(
    plan: TrayPlan,
    socket_name: str,
    audio_env: dict[str, str],
    log_path: Path,
    instance: str,
    config_file: Path | None = None,
    extra_env: dict[str, str] | None = None,
) -> RemoteProcess:
    """Managed mode: the helper runs Sunshine inside the instance's netns.

    Cage stays on the host, and that is the point: udev hotplug arrives over a
    netlink group that only init_net has, so a compositor inside the netns
    never sees the virtual input Sunshine creates after it started.
    """
    argv, env = sunshine_command(plan, socket_name, audio_env, config_file, extra_env)
    response = helper_call(
        {
            "op": "spawn-netns",
            "name": instance,
            "argv": argv,
            "env": env,
            "stdout": str(log_path),
        }
    )
    pid = response.get("pid")
    if not isinstance(pid, int):
        raise ClipponError("clippon-net did not return a pid for sunshine")
    return RemoteProcess(pid)


def os_env() -> dict[str, str]:
    return os.environ.copy()


def fatal_lines(path: Path) -> list[str]:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    return [line for line in text.splitlines() if _FATAL.search(line)]
