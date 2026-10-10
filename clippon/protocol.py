"""One JSON object per line, over a Unix socket."""

from __future__ import annotations

import json
import os
import socket
from collections.abc import Mapping
from pathlib import Path

HELPER_SOCK = Path("/run/clippon/net.sock")
_MAX_LINE = 1_000_000


def daemon_sock(env: Mapping[str, str] | None = None) -> Path:
    values = os.environ if env is None else env
    runtime = values.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
    return Path(runtime) / "clippon" / "daemon.sock"


def dumps(obj: object) -> bytes:
    return json.dumps(obj, separators=(",", ":")).encode() + b"\n"


def loads(line: bytes) -> dict[str, object]:
    data = json.loads(line.decode())
    if not isinstance(data, dict):
        raise ValueError("expected a JSON object")
    return data


def send_obj(conn: socket.socket, obj: object) -> None:
    conn.sendall(dumps(obj))


def recv_obj(conn: socket.socket) -> dict[str, object]:
    buf = b""
    while b"\n" not in buf:
        chunk = conn.recv(65536)
        if not chunk:
            break
        buf += chunk
        if len(buf) > _MAX_LINE:
            raise ValueError("message exceeds 1 MiB")
    if b"\n" not in buf:
        raise ValueError("incomplete message")
    return loads(buf.split(b"\n", 1)[0])
