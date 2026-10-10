"""Client for the clippon-net helper socket."""

from __future__ import annotations

import socket

from clippon.config import ClipponError
from clippon.protocol import HELPER_SOCK, recv_obj, send_obj


def call(request: dict[str, object], timeout: float = 30.0) -> dict[str, object]:
    try:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        sock.connect(str(HELPER_SOCK))
    except FileNotFoundError as exc:
        raise ClipponError(f"{HELPER_SOCK} is missing; is clippon-net running?") from exc
    except OSError as exc:
        raise ClipponError(f"cannot reach clippon-net: {exc}") from exc
    with sock:
        send_obj(sock, request)
        try:
            response = recv_obj(sock)
        except (ValueError, OSError) as exc:
            raise ClipponError(f"bad reply from clippon-net: {exc}") from exc
    if not response.get("ok"):
        raise ClipponError(str(response.get("error") or "clippon-net failed"))
    return response
