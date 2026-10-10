"""Fail before starting anything when a hard dependency is missing."""

from __future__ import annotations

import shutil
import subprocess

from clippon.config import Config, ClipponError
from clippon.log import warn

_REQUIRED = ("cage", "sunshine", "env", "sh")


def _have(name: str) -> bool:
    return shutil.which(name) is not None


def check(cfg: Config) -> None:
    if not cfg.runtime_dir.is_dir():
        raise ClipponError(f"XDG_RUNTIME_DIR={cfg.runtime_dir} is not a directory")

    missing = [name for name in _REQUIRED if not _have(name)]
    if missing:
        raise ClipponError("not in PATH: " + ", ".join(missing))

    if not cfg.shim.is_file() or not os_readable(cfg.shim):
        raise ClipponError(
            f"{cfg.shim} is missing; cage could not see Sunshine's virtual input"
        )
    if not cfg.udev_rule.exists():
        warn(
            "seat",
            "%s is missing: Moonlight input will also reach the host desktop",
            cfg.udev_rule,
        )

    if cfg.udev_rule.exists():
        text = cfg.udev_rule.read_text(encoding="utf-8", errors="replace")
        if "clippon-seat-tag" not in text:
            warn(
                "seat",
                "%s does not call clippon-seat-tag: every instance's input will "
                "reach the other instances' apps",
                cfg.udev_rule,
            )

    # Managed instances each have a network namespace, so another host sunshine
    # is not competing for these ports.
    if not cfg.instance and _sunshine_running():
        warn("sun", "another sunshine is already running; this one may fail to bind its ports")
    if cfg.instance and not cfg.vhid_shim.is_file():
        warn(
            "sun",
            "%s is missing; every instance's devices stay on one seat",
            cfg.vhid_shim,
        )

    if cfg.resolution and not _have("wlr-randr"):
        warn("cage", "wlr-randr is not in PATH; the requested mode will not be applied")
    if not _have("ss"):
        warn("cage", "ss is not in PATH; the Wayland socket will be found by directory diff")


def os_readable(path: object) -> bool:
    try:
        with open(path, "rb"):
            return True
    except OSError:
        return False


def _sunshine_running() -> bool:
    if not _have("pgrep"):
        return False
    proc = subprocess.run(["pgrep", "-x", "sunshine"], check=False, capture_output=True)
    return proc.returncode == 0
