"""User-level manager for clippon instances.

clipponctl talks to this process. This process talks to the root helper
clippon-net, which owns the network namespaces. A foreground `clippon`
invocation does not come through here.
"""

from __future__ import annotations

import os
import signal
import socket
import sys
import time
from pathlib import Path

from clippon.config import DEFAULT_SINK, ClipponError
from clippon.front import ensure_identity, instance_certificate, open_front, trust_instance
from clippon.instance import instance_dir, read_meta, state_home, validate_name, write_instance
from clippon.log import error, note, setup_logging
from clippon.sunshine import managed_tray
from clippon.net_helper import alive
from clippon.protocol import HELPER_SOCK, daemon_sock, recv_obj, send_obj

ROOT = Path(__file__).resolve().parents[1]


def clippon_argv(name: str, command: list[str], resolution: str, verbose: bool) -> list[str]:
    argv = [sys.executable, str(ROOT / "bin" / "clippon"), "--instance", name]
    if verbose:
        argv.append("--verbose")
    argv.extend(["--res", resolution, "--", *command])
    return argv


class Daemon:
    def __init__(self, sock_path: Path | None = None) -> None:
        self.sock_path = sock_path or daemon_sock()
        self._stop = False
        self._helper_down = False
        self.front = None

    def serve(self) -> None:
        path = self.sock_path
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            path.unlink()
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(str(path))
        os.chmod(path, 0o600)
        server.listen(8)
        server.settimeout(2.0)
        signal.signal(signal.SIGTERM, self._on_signal)
        signal.signal(signal.SIGINT, self._on_signal)
        note("clip", "listening on %s", path)
        try:
            self.front = open_front()
        except (OSError, ClipponError) as exc:
            error("gate", "gateway did not start: %s", exc)
        try:
            while not self._stop:
                self._reap()
                try:
                    conn, _ = server.accept()
                except TimeoutError:
                    continue
                except OSError:
                    if self._stop:
                        break
                    raise
                self._client(conn)
        finally:
            server.close()
            path.unlink(missing_ok=True)

    def handle(self, request: dict[str, object]) -> dict[str, object]:
        op = request.get("op")
        if op == "list":
            return self.list_instances()
        if op == "hide":
            status = self._call({"op": "expose", "name": None})
            return {"ok": True, "message": "hidden", "lan": status.get("lan") or []}
        if op == "expose":
            name = _required_name(request)
            status = self._call({"op": "expose", "name": name})
            instances = status.get("instances")
            ip = ""
            if isinstance(instances, dict):
                rec = instances.get(name)
                if isinstance(rec, dict) and isinstance(rec.get("ip"), str):
                    ip = rec["ip"]
            lan = status.get("lan") if isinstance(status.get("lan"), list) else []
            return {
                "ok": True,
                "message": f"exposed {name} ({ip}); lan {_lan_text(lan)}",
                "name": name,
                "ip": ip,
                "lan": lan,
            }
        if op == "stop":
            name = _required_name(request)
            self.stop(name)
            return {"ok": True, "message": f"stopped {name}"}
        if op == "start":
            return self.start(request)
        if op == "pin":
            pin = request.get("pin")
            if not isinstance(pin, str) or len(pin) != 4 or not pin.isdigit():
                raise ClipponError("PIN must be 4 digits")
            if self.front is None:
                raise ClipponError("gateway is not running")
            return {"ok": True, "message": self.front.submit_pin(pin)}
        raise ClipponError(f"unknown op {op!r}")

    def list_instances(self) -> dict[str, object]:
        status = self._call({"op": "status"})
        raw = status.get("instances")
        rows: list[dict[str, object]] = []
        if isinstance(raw, dict):
            for name, rec in sorted(raw.items()):
                if not isinstance(rec, dict):
                    continue
                meta = read_meta(instance_dir(state_home(), name))
                command = meta.get("command")
                rows.append(
                    {
                        "name": name,
                        "ip": rec.get("ip") or "",
                        "pid": rec.get("pid"),
                        "exited": bool(rec.get("exited")),
                        "command": command if isinstance(command, list) else [],
                    }
                )
        return {
            "ok": True,
            "exposed": status.get("exposed"),
            "lan": status.get("lan") or [],
            "instances": rows,
        }

    def start(self, request: dict[str, object]) -> dict[str, object]:
        name = _required_name(request)
        command = request.get("command")
        if not isinstance(command, list) or not command or not all(isinstance(item, str) for item in command):
            raise ClipponError("command must be a non-empty list of strings")
        resolution = request.get("resolution")
        if not isinstance(resolution, str):
            raise ClipponError("resolution must be a string")
        sink = request.get("sink") if isinstance(request.get("sink"), str) else DEFAULT_SINK
        audio = request.get("audio") if isinstance(request.get("audio"), str) else "enabled"
        requested = request.get("tray") if isinstance(request.get("tray"), str) else "auto"
        tray = managed_tray(requested)
        verbose = bool(request.get("verbose"))
        extra = _env_map(request.get("env"))
        status = self._call({"op": "status"})
        raw = status.get("instances")
        if isinstance(raw, dict) and name in raw:
            rec = raw[name]
            if isinstance(rec, dict) and isinstance(rec.get("pid"), int) and alive(rec["pid"]):
                raise ClipponError(f"{name} is already running as pid {rec['pid']}")
        directory = instance_dir(state_home(), name)
        identity = ensure_identity()
        shared = instance_certificate()
        cert, key = shared if shared is not None else (None, None)
        write_instance(
            directory,
            name,
            list(command),
            resolution,
            sink,
            audio,
            tray,
            verbose,
            cert=cert,
            key=key,
        )
        trust_instance(directory, identity, name)
        log_path = directory / "spawn.log"
        log_path.touch(exist_ok=True)
        self._call({"op": "create", "name": name})
        try:
            spawned = self._call(
                {
                    "op": "spawn",
                    "name": name,
                    "argv": clippon_argv(name, list(command), resolution, verbose),
                    "env": _child_env(sink, audio, tray, extra),
                    "stdout": str(log_path),
                }
            )
        except ClipponError:
            self._destroy_quiet(name)
            raise
        pid = spawned.get("pid")
        if not isinstance(pid, int):
            self._destroy_quiet(name)
            raise ClipponError("clippon-net did not return a pid")
        time.sleep(0.5)
        if not alive(pid):
            detail = _tail(log_path)
            self._destroy_quiet(name)
            message = f"{name} exited immediately"
            if detail:
                message = f"{message}\n{detail}"
            raise ClipponError(message)
        listed = self.list_instances()
        ip = ""
        for row in listed["instances"]:  # type: ignore[union-attr]
            if isinstance(row, dict) and row.get("name") == name:
                ip = str(row.get("ip") or "")
        lan = listed.get("lan") if isinstance(listed.get("lan"), list) else []
        return {
            "ok": True,
            "message": f"started {name} pid {pid} at {ip}",
            "name": name,
            "pid": pid,
            "ip": ip,
            "lan": lan,
        }

    def stop(self, name: str) -> None:
        status = self._call({"op": "status"})
        raw = status.get("instances")
        pid = None
        if isinstance(raw, dict):
            rec = raw.get(name)
            if isinstance(rec, dict) and isinstance(rec.get("pid"), int):
                pid = rec["pid"]
        if pid is not None and alive(pid):
            os.kill(pid, signal.SIGTERM)
            if not _wait_dead(pid, 15.0):
                os.kill(pid, signal.SIGKILL)
                _wait_dead(pid, 3.0)
        self._call({"op": "destroy", "name": name})

    def _destroy_quiet(self, name: str) -> None:
        try:
            self._call({"op": "destroy", "name": name})
        except ClipponError as exc:
            error("clip", "cannot remove %s after a failed start: %s", name, exc)

    def _reap(self) -> None:
        try:
            status = self._call({"op": "status"})
        except ClipponError as exc:
            if not self._helper_down:
                note("clip", "clippon-net unavailable: %s", exc)
                self._helper_down = True
            return
        self._helper_down = False
        raw = status.get("instances")
        if not isinstance(raw, dict):
            return
        for name, rec in raw.items():
            if isinstance(rec, dict) and rec.get("exited"):
                note("clip", "instance %s exited; removing its network namespace", name)
                try:
                    self._call({"op": "destroy", "name": name})
                except ClipponError as exc:
                    error("clip", "cannot remove %s: %s", name, exc)

    def _call(self, request: dict[str, object]) -> dict[str, object]:
        try:
            response = _transact(HELPER_SOCK, request)
        except ClipponError:
            raise
        if not response.get("ok"):
            raise ClipponError(str(response.get("error") or "clippon-net failed"))
        return response

    def _client(self, conn: socket.socket) -> None:
        with conn:
            try:
                uid = _peer_uid(conn)
                if uid != os.geteuid():
                    send_obj(conn, {"ok": False, "error": "permission denied"})
                    return
                response = self.handle(recv_obj(conn))
            except Exception as exc:
                response = {"ok": False, "error": str(exc)}
            try:
                send_obj(conn, response)
            except OSError as exc:
                error("clip", "reply failed: %s", exc)

    def _on_signal(self, _signum: int, _frame: object) -> None:
        self._stop = True


def _child_env(sink: str, audio: str, tray: str, extra: dict[str, str]) -> dict[str, str]:
    """The environment the session runs with.

    *extra* is what `clipponctl start --env` asked for: it lands in the
    environment of the session's clippon process, which passes it on to cage
    and to the program cage starts. The STREAM_* names below are set after it,
    so the daemon keeps deciding the sink, the audio mode and the tray, and
    LD_PRELOAD is dropped so a caller cannot preload into the session.
    """
    env = dict(os.environ)
    env.update(extra)
    env["STREAM_SINK"] = sink
    env["STREAM_AUDIO"] = audio
    env["STREAM_TRAY"] = tray
    env.pop("LD_PRELOAD", None)
    return env


def _env_map(raw: object) -> dict[str, str]:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ClipponError("env must be an object")
    found: dict[str, str] = {}
    for key, value in raw.items():
        if not isinstance(key, str) or not isinstance(value, str):
            raise ClipponError("env keys and values must be strings")
        if not key or "\0" in key or "\0" in value:
            raise ClipponError("env contains an invalid name or value")
        found[key] = value
    return found


def _required_name(request: dict[str, object]) -> str:
    name = request.get("name")
    if not isinstance(name, str):
        raise ClipponError("name must be a string")
    try:
        validate_name(name)
    except ValueError as exc:
        raise ClipponError(str(exc)) from exc
    return name


def _lan_text(lan: object) -> str:
    if isinstance(lan, list) and all(isinstance(item, str) for item in lan) and lan:
        return ", ".join(lan)
    return "no global IPv4 address found"


def _wait_dead(pid: int, seconds: float) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if not alive(pid):
            return True
        time.sleep(0.1)
    return not alive(pid)


def _tail(path: Path) -> str:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return ""
    return "\n".join(lines[-20:])


def _peer_uid(conn: socket.socket) -> int:
    import struct

    creds = conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
    _pid, uid, _gid = struct.unpack("3i", creds)
    return uid


def _transact(path: Path, request: dict[str, object]) -> dict[str, object]:
    try:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(30)
        sock.connect(str(path))
    except FileNotFoundError as exc:
        raise ClipponError(f"{path} is missing; is clippon-net running?") from exc
    except OSError as exc:
        raise ClipponError(f"cannot connect to {path}: {exc}") from exc
    with sock:
        send_obj(sock, request)
        try:
            return recv_obj(sock)
        except (ValueError, OSError) as exc:
            raise ClipponError(f"bad reply from {path}: {exc}") from exc


def main() -> int:
    setup_logging(False)
    try:
        Daemon().serve()
    except ClipponError as exc:
        error("clip", "%s", exc)
        return 1
    return 0
