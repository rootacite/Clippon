"""clipponctl: tell clippond which instance exists and which one is exposed."""

from __future__ import annotations

import shlex
import socket
import sys
from collections.abc import Mapping
from pathlib import Path

from clippon.config import ClipponError, parse_args
from clippon.instance import sunshine_encoder, validate_name
from clippon.log import error, note, setup_logging
from clippon.protocol import daemon_sock, dumps, recv_obj
from clippon.quick import (
    apply_update,
    command_basename,
    format_quick,
    load_quick,
    merged_env,
    parse_quick_update,
    save_quick,
)

HELP = """\
Usage: clipponctl <command>

  list
  start NAME [--verbose] [--res AxB[@C[Hz]]] [--encoder vaapi|vulkan|soft]
        [--env K=V]... [--] <command> [args...]
  start --quick [--verbose] [--res ...] [--encoder ...] [--env K=V]...
        [--] <command> [args...]
  set-quick [--res ...] [--encoder ...] [--env K=V]...
  stop NAME
  release NAME
  expose NAME
  hide
  pin 1234

start runs that command inside its own network namespace on its own port
group, so several instances can stream at the same time. expose points the
47990 web UI proxy at one instance; every instance's stream ports stay open.
release stops the app an instance is running and forgets the client sessions
that claim it, which is the way out when a client vanished mid-stream. pin
submits the 4-digit PIN Moonlight is showing.
Options for start follow the same rules as clippon and must come before the
command. --env KEY=VALUE reaches cage and the program it runs, which is how a
session gets variables the daemon does not have, e.g.
  clipponctl start alpha --env GTK_IM_MODULE=fcitx --env XMODIFIERS=@im=fcitx -- kitty
--encoder picks the Sunshine video backend for that instance. vaapi is the
default. soft is Sunshine's software encoder.
--quick applies the saved preset and, when NAME is omitted, names the session
after the command's executable (dolphin from /usr/bin/dolphin). A NAME written
before the options still wins. --res, --env and --encoder next to --quick
override the preset for that start only. The built-in preset is:
  --res 1920x1080@120Hz --env GTK_IM_MODULE=fcitx --env QT_IM_MODULE=fcitx
  --env XMODIFIERS=@im=fcitx --env SDL_IM_MODULE=fcitx --encoder vaapi
set-quick with no options prints the preset. Options change that part and are
saved in ${XDG_CONFIG_HOME:-~/.config}/clippon/quick.json. Giving --env
replaces the whole environment list; leaving it out keeps the saved one.
Deleting that file restores the built-in preset. set-quick does not talk to
clippond.

clippond must already be running for every other command (systemd --user unit
clippond.service).
"""


def parse_ctl(argv: list[str], environ: Mapping[str, str] | None = None) -> dict[str, object]:
    if not argv or argv[0] in ("-h", "--help"):
        print(HELP, end="")
        raise SystemExit(0)
    op = argv[0]
    if op == "list" and len(argv) == 1:
        return {"op": "list"}
    if op == "hide" and len(argv) == 1:
        return {"op": "hide"}
    if op == "pin" and len(argv) == 2:
        pin = argv[1]
        if len(pin) != 4 or not pin.isdigit():
            raise ClipponError("PIN must be 4 digits", code=2)
        return {"op": "pin", "pin": pin}
    if op in ("stop", "expose", "release") and len(argv) == 2:
        _name(argv[1])
        return {"op": op, "name": argv[1]}
    if op == "set-quick":
        text = _apply_set_quick(argv[1:], environ)
        return {"op": "set-quick", "local": True, "text": text}
    if op == "start":
        return _parse_start(argv[1:], environ)
    raise ClipponError(
        f"unknown command {op!r}; try clipponctl --help",
        code=2,
    )


def format_list(response: dict[str, object]) -> str:
    lines: list[str] = []
    lan = response.get("lan")
    if isinstance(lan, list) and lan:
        lines.append("lan       " + ", ".join(str(item) for item in lan))
    else:
        lines.append("lan       (none)")
    exposed = response.get("exposed")
    rows = response.get("instances")
    if not isinstance(rows, list) or not rows:
        lines.append("(no instances)")
        return "\n".join(lines)
    lines.append(
        f"{'name':<12} {'address':<16} {'base':<6} {'pid':<11} {'state':<8} command"
    )
    for row in rows:
        if not isinstance(row, dict):
            continue
        name = str(row.get("name") or "")
        ip = str(row.get("ip") or "")
        port = row.get("port")
        ports = str(port) if isinstance(port, int) else "-"
        pid = row.get("pid")
        pid_text = str(pid) if isinstance(pid, int) else "-"
        mark = "web" if name == exposed else ""
        if row.get("exited"):
            mark = "exited"
        command = row.get("command")
        cmd = shlex.join(command) if isinstance(command, list) else ""
        lines.append(f"{name:<12} {ip:<16} {ports:<6} pid {pid_text:<8} {mark:<8} {cmd}".rstrip())
    return "\n".join(lines)


def transact(path: Path, request: dict[str, object]) -> dict[str, object]:
    try:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(30)
        sock.connect(str(path))
    except FileNotFoundError as exc:
        raise ClipponError(f"{path} is missing; is clippond running?") from exc
    except OSError as exc:
        raise ClipponError(f"cannot connect to {path}: {exc}") from exc
    with sock:
        sock.sendall(dumps(request))
        try:
            response = recv_obj(sock)
        except (ValueError, OSError) as exc:
            raise ClipponError(f"bad reply from clippond: {exc}") from exc
    return response


def main(argv: list[str] | None = None, environ: Mapping[str, str] | None = None) -> int:
    setup_logging(False)
    args = list(sys.argv[1:] if argv is None else argv)
    try:
        request = parse_ctl(args, environ=environ)
        if request.get("local"):
            print(request.get("text") or "")
            return 0
        response = transact(daemon_sock(), request)
    except ClipponError as exc:
        error("ctl", "%s", exc)
        if exc.code == 2:
            print(HELP, end="", file=sys.stderr)
        return exc.code
    if not response.get("ok"):
        error("ctl", "%s", response.get("error") or "failed")
        return 1
    if request["op"] == "list":
        print(format_list(response))
    else:
        message = response.get("message")
        note("ctl", "%s", message if isinstance(message, str) else "ok")
    return 0


_START_USAGE = (
    "usage: clipponctl start NAME [--res ...] -- COMMAND, "
    "or clipponctl start --quick -- COMMAND"
)


def _parse_start(argv: list[str], environ: Mapping[str, str] | None) -> dict[str, object]:
    """One start request. `--quick` is expanded here, so the daemon sees an ordinary start."""
    if not argv:
        raise ClipponError(_START_USAGE, code=2)
    name: str | None = None
    rest = argv
    if argv[0] != "--" and not argv[0].startswith("-"):
        name = argv[0]
        _name(name)
        rest = argv[1:]
    quick, encoder, encoder_set, rest = _take_quick_and_encoder(rest)
    cfg = parse_args(rest, environ=environ)
    if quick:
        preset = load_quick(environ)
        if name is None:
            name = command_basename(cfg.command[0])
            try:
                _name(name)
            except ClipponError as exc:
                raise ClipponError(
                    f"{exc}; pass a name before --quick to choose one",
                    code=2,
                ) from exc
        resolution = cfg.resolution if cfg.resolution_set else preset.resolution
        env = merged_env(preset.env, cfg.env)
        if not encoder_set:
            encoder = preset.encoder
    else:
        if name is None:
            raise ClipponError(_START_USAGE, code=2)
        resolution = cfg.resolution
        env = dict(cfg.env)
        if not encoder_set:
            encoder = "vaapi"
    return {
        "op": "start",
        "name": name,
        "command": list(cfg.command),
        "resolution": resolution,
        "sink": cfg.sink,
        "audio": cfg.audio,
        "tray": cfg.tray,
        "verbose": cfg.verbose,
        "env": env,
        "encoder": encoder,
    }


def _apply_set_quick(argv: list[str], environ: Mapping[str, str] | None) -> str:
    update = parse_quick_update(argv)
    current = load_quick(environ)
    if update is None:
        return format_quick(current)
    preset = apply_update(current, update)
    save_quick(preset, environ)
    return format_quick(preset)


def _take_quick_and_encoder(argv: list[str]) -> tuple[bool, str, bool, list[str]]:
    """Pull `--quick` and `--encoder` out before the shared option parser.

    The encoder word kept is `vaapi`, `vulkan` or `soft`. A later `--encoder`
    replaces an earlier one. The bool is whether the command line named one.
    """
    quick = False
    choice = "vaapi"
    chosen = False
    kept: list[str] = []
    index = 0
    while index < len(argv):
        item = argv[index]
        if item == "--":
            kept.extend(argv[index:])
            break
        if item == "--quick":
            quick = True
            index += 1
            continue
        if item.startswith("--quick="):
            raise ClipponError("--quick does not take a value", code=2)
        if item == "--encoder" or item.startswith("--encoder="):
            if item == "--encoder":
                if index + 1 >= len(argv):
                    raise ClipponError("--encoder needs one of vaapi, vulkan, soft", code=2)
                raw = argv[index + 1]
                index += 2
            else:
                raw = item[len("--encoder=") :]
                index += 1
            try:
                sunshine_encoder(raw)
            except ValueError as exc:
                raise ClipponError(str(exc), code=2) from exc
            choice = raw
            chosen = True
            continue
        kept.append(item)
        index += 1
    return quick, choice, chosen, kept


def _name(name: str) -> None:
    try:
        validate_name(name)
    except ValueError as exc:
        raise ClipponError(str(exc), code=2) from exc
