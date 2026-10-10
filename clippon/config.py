"""Command line and the STREAM_* environment the shell script already used."""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from clippon.instance import instance_dir, validate_name

SHIM_PATH = Path("/usr/local/lib/libseat_seat_cage.so")
ADDR_SHIM_PATH = Path("/usr/local/lib/libsunshine_addr.so")
CURSOR_SHIM_PATH = Path("/usr/local/lib/libwlr_cursor_map.so")
VHID_SHIM_PATH = Path("/usr/local/lib/libclippon_vhid_name.so")
UDEV_RULE_PATH = Path("/etc/udev/rules.d/99-sunshine-cage.rules")
DEFAULT_RES = "2560x1440@120Hz"
DEFAULT_SINK = "cage-sink"

# WxH, optionally @R or @RHz. Empty means "keep wlroots' 1280x720 default".
_RES_RE = re.compile(
    r"^(?:[0-9]+[xX][0-9]+(?:@[0-9]+(?:\.[0-9]+)?(?:[Hh][Zz])?)?)?$"
)

HELP = """\
Usage: clippon [--verbose] [--res AxB[@C[Hz]]] [--] <command> [args...]

Run <command> inside a headless cage compositor and start Sunshine against
that compositor's Wayland socket. Sunshine stops when cage exits.

Options:
  --res AxB[@C[Hz]]   output mode to set inside cage before the app starts,
                      e.g. --res 1920x1080 or --res 2560x1440@120Hz. Overrides
                      STREAM_RES; --res '' keeps wlroots' default of 1280x720.
  --instance NAME     per-instance config and logs, used by clippond. The
                      process must already be inside that instance's network
                      namespace. NAME matches [a-z][a-z0-9-]{0,11}.
  --verbose           log socket discovery and optional audio helpers
  -h, --help          show this help and exit

Options must come before the command; use -- to end them.

Environment:
  STREAM_RES=WxH[@RHz]  same as --res, but from the environment (default:
                        2560x1440@120Hz). The output name is looked up, not
                        hardcoded, and so is cage's wayland-N socket.
  STREAM_SINK=NAME      name of the private audio sink the app plays into
                        (default: cage-sink)
  STREAM_TRAY=auto|enabled|disabled
                        Sunshine's tray. On Linux Sunshine's main loop is the
                        tray loop, so with no desktop panel (getty/tmux) it
                        exits immediately; auto (default) disables the tray
                        unless a StatusNotifier host is present.
  STREAM_AUDIO=disabled skip the private audio instance; the app's audio then
                        uses the host's default sink
  STREAM_LOG_DIR=DIR    log directory
                        (default: ${XDG_STATE_HOME:-$HOME/.local/state}/cage-sunshine)

Audio: the app runs against a private PipeWire/WirePlumber/PipeWire-Pulse
instance, so its sound goes to a sink inside that instance (STREAM_SINK, before
a session starts). Sunshine captures the virtual sink it swaps the default to,
and none of this is visible to the host's audio stack.

Examples:
  clippon -- kitty
  clippon --res 1920x1080 -- steam -tenfoot
  STREAM_RES=1920x1080 clippon -- kitty
"""


class ClipponError(Exception):
    def __init__(self, message: str, code: int = 1) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class Config:
    command: tuple[str, ...]
    resolution: str
    sink: str
    audio: str
    tray: str
    verbose: bool
    runtime_dir: Path
    log_dir: Path
    instance: str = ""
    shim: Path = SHIM_PATH
    addr_shim: Path = ADDR_SHIM_PATH
    cursor_shim: Path = CURSOR_SHIM_PATH
    vhid_shim: Path = VHID_SHIM_PATH
    udev_rule: Path = UDEV_RULE_PATH

    @property
    def audio_enabled(self) -> bool:
        return self.audio != "disabled"


def resolution_ok(value: str) -> bool:
    return _RES_RE.fullmatch(value) is not None


def parse_args(argv: list[str], environ: Mapping[str, str] | None = None) -> Config:
    """Parse options the way the shell script did: they stop at the command."""
    env = os.environ if environ is None else environ
    args = list(argv)
    res_opt = ""
    res_set = False
    verbose = False
    instance = ""

    while args:
        item = args[0]
        if item in ("-h", "--help"):
            print(HELP, end="")
            raise SystemExit(0)
        if item == "--verbose":
            verbose = True
            del args[0]
            continue
        if item == "--instance":
            if len(args) < 2:
                raise ClipponError("--instance needs a name")
            instance = args[1]
            del args[:2]
            continue
        if item == "--res":
            if len(args) < 2:
                raise ClipponError("--res needs an argument, e.g. --res 1920x1080")
            res_opt = args[1]
            res_set = True
            del args[:2]
            continue
        if item.startswith("--res="):
            res_opt = item[len("--res=") :]
            res_set = True
            del args[0]
            continue
        if item == "--":
            del args[0]
            break
        if item.startswith("-"):
            raise ClipponError(
                f"unknown option: {item} (options must come before the command)"
            )
        break

    if res_set and not resolution_ok(res_opt):
        raise ClipponError(
            "--res: expected AxB or AxB@C[Hz], e.g. 1920x1080 or "
            f"2560x1440@120Hz (got {res_opt!r})"
        )
    if not args:
        raise ClipponError("a command is required", code=2)
    if instance:
        try:
            validate_name(instance)
        except ValueError as exc:
            raise ClipponError(str(exc)) from exc

    if res_set:
        resolution = res_opt
    elif "STREAM_RES" in env:
        resolution = env["STREAM_RES"]
    else:
        resolution = DEFAULT_RES

    uid = os.getuid()
    runtime = env.get("XDG_RUNTIME_DIR") or f"/run/user/{uid}"
    home = Path(env.get("HOME") or str(Path.home()))
    state = Path(env.get("XDG_STATE_HOME") or str(home / ".local" / "state"))
    if env.get("STREAM_LOG_DIR"):
        log_dir = Path(env["STREAM_LOG_DIR"])
    elif instance:
        log_dir = instance_dir(state, instance)
    else:
        log_dir = state / "cage-sunshine"

    return Config(
        command=tuple(args),
        resolution=resolution,
        sink=env["STREAM_SINK"] if "STREAM_SINK" in env else DEFAULT_SINK,
        audio=env["STREAM_AUDIO"] if "STREAM_AUDIO" in env else "enabled",
        tray=env["STREAM_TRAY"] if "STREAM_TRAY" in env else "auto",
        verbose=verbose,
        runtime_dir=Path(runtime),
        log_dir=log_dir,
        instance=instance,
    )
