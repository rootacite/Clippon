"""clipponctl: tell clippond which instance exists and which one is exposed."""

from __future__ import annotations

import shlex
import socket
import sys
from collections.abc import Mapping
from pathlib import Path

from clippon.config import ClipponError, parse_args
from clippon.instance import validate_name
from clippon.log import error, note, setup_logging
from clippon.protocol import daemon_sock, dumps, recv_obj

HELP = """\
Usage: clipponctl <command>

  list
  start NAME [--verbose] [--res AxB[@C[Hz]]] [--env K=V]... [--] <command> [args...]
  stop NAME
  expose NAME
  hide
  pin 1234

start runs that command inside its own network namespace. expose points the
stream ports at one instance. hide removes that mapping. pin submits the
4-digit PIN Moonlight is showing.
Options for start follow the same rules as clippon and must come before the
command. --env KEY=VALUE reaches cage and the program it runs, which is how a
session gets variables the daemon does not have, e.g.
  clipponctl start alpha --env GTK_IM_MODULE=fcitx --env XMODIFIERS=@im=fcitx -- kitty

clippond must already be running (systemd --user unit clippond.service).
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
    if op in ("stop", "expose") and len(argv) == 2:
        _name(argv[1])
        return {"op": op, "name": argv[1]}
    if op == "start":
        if len(argv) < 2:
            raise ClipponError("usage: clipponctl start NAME [--res ...] -- COMMAND", code=2)
        name = argv[1]
        _name(name)
        cfg = parse_args(argv[2:], environ=environ)
        return {
            "op": "start",
            "name": name,
            "command": list(cfg.command),
            "resolution": cfg.resolution,
            "sink": cfg.sink,
            "audio": cfg.audio,
            "tray": cfg.tray,
            "verbose": cfg.verbose,
            "env": dict(cfg.env),
        }
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
    for row in rows:
        if not isinstance(row, dict):
            continue
        name = str(row.get("name") or "")
        ip = str(row.get("ip") or "")
        pid = row.get("pid")
        pid_text = str(pid) if isinstance(pid, int) else "-"
        mark = "exposed" if name == exposed else ""
        if row.get("exited"):
            mark = "exited"
        command = row.get("command")
        cmd = shlex.join(command) if isinstance(command, list) else ""
        lines.append(f"{name:<12} {ip:<16} pid {pid_text:<8} {mark:<8} {cmd}".rstrip())
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


def main(argv: list[str] | None = None) -> int:
    setup_logging(False)
    args = list(sys.argv[1:] if argv is None else argv)
    try:
        request = parse_ctl(args)
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


def _name(name: str) -> None:
    try:
        validate_name(name)
    except ValueError as exc:
        raise ClipponError(str(exc), code=2) from exc
