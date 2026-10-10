"""Host listeners for 47989, 47984 and 47990.

47989 and 47984 are the GameStream control plane. serverinfo is answered
here, with LocalIP set to the address the client dialed. applist, launch,
resume, cancel, appasset and pair are sent on to a backend over its veth
address. launch also points the stream-port DNAT at that backend and rewrites
sessionUrl0. 47990 is a raw TCP proxy onto the exposed instance's web UI.
"""

from __future__ import annotations

import http.client
import socket
import ssl
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import uuid as uuidlib

from clippon.clients import PairedClients
from clippon.config import ClipponError
from clippon.gateway import (
    classify,
    external_app_id,
    merge_applists,
    replace_query_appid,
    request_path,
    rewrite_session_host,
    serverinfo_xml,
    xml_text,
)
from clippon.identity import Identity
from clippon.pair import Pairing
from clippon.instance import instance_dir, state_home
from clippon.log import error, note
from clippon.ports import GAMESTREAM_HTTP, GAMESTREAM_HTTPS, WEB_UI
from clippon.protocol import HELPER_SOCK, recv_obj, send_obj

_HOSTNAME = "clippon"


class Front:
    def __init__(self, identity: Identity) -> None:
        self.identity = identity
        self.clients = PairedClients(identity.root / "clients.json")
        self.pairing = Pairing(
            identity.server_cert.read_text(encoding="utf-8"),
            identity.server_key.read_text(encoding="utf-8"),
            on_paired=self.clients.add,
        )
        self._routes: dict[int, tuple[str, str]] = {}
        self._lock = threading.Lock()

    def submit_pin(self, pin: str) -> str:
        return self.pairing.submit_pin(pin)

    def start(self) -> None:
        self._serve_http(GAMESTREAM_HTTP, https=False)
        self._serve_http(GAMESTREAM_HTTPS, https=True)
        threading.Thread(target=self._serve_web, name="clippon-web", daemon=True).start()

    def handle(self, handler: BaseHTTPRequestHandler, https: bool) -> None:
        target = handler.path
        kind = classify(target)
        local_ip = handler.connection.getsockname()[0]
        peer = _peer_fingerprint(handler.connection) if https else None
        # Moonlight decides "paired" purely from ServerInfo.PairStatus, and it
        # only accepts HTTPS answers once it holds a pinned certificate. The
        # certificate itself is not visible to us under CERT_NONE, so report
        # from our own record of completed pairings.
        paired = https and bool(self.clients.all())
        if request_path(target) == "/pair":
            try:
                body = self.pairing.handle(target).encode()
            except (ValueError, OSError) as exc:
                error("gate", "pair failed: %s", exc)
                body = _status_page(400, "pairing failed").encode()
            _write(handler, 200, body)
            return
        if kind == "local":
            body = self._serverinfo(local_ip, paired).encode()
            _write(handler, 200, body)
            return
        if kind != "forward":
            _write(handler, 404, _status_page(404, "Not found").encode())
            return
        try:
            status, body = self._forward(target, local_ip)
        except (OSError, ClipponError, ValueError) as exc:
            error("gate", "%s %s failed: %s", "https" if https else "http", target, exc)
            _write(handler, 502, _status_page(502, "backend unavailable").encode())
            return
        _write(handler, status, body)

    def _serverinfo(self, local_ip: str, paired: bool) -> str:
        codec = "0"
        luma = "0"
        current = 0
        for name, ip in self._live():
            try:
                status, raw = _http_get(ip, GAMESTREAM_HTTP, "/serverinfo", 2.0)
            except OSError:
                continue
            if status != 200:
                continue
            text = raw.decode("utf-8", "replace")
            if codec == "0":
                codec = xml_text(text, "ServerCodecModeSupport") or "0"
                luma = xml_text(text, "MaxLumaPixelsHEVC") or "0"
            game = xml_text(text, "currentgame") or "0"
            if game.isdigit() and int(game) > 0:
                current = external_app_id(name, game)
                break
        return serverinfo_xml(
            local_ip=local_ip,
            unique_id=self.identity.unique_id,
            hostname=_HOSTNAME,
            paired=paired,
            current_game=current,
            codec_flags=codec,
            max_luma=luma,
        )

    def _forward(self, target: str, local_ip: str) -> tuple[int, bytes]:
        path = target.split("?", 1)[0]
        timeout = 180.0 if path == "/pair" else 20.0
        if path == "/applist":
            return 200, self._applist().encode()
        backend = self._backend_for(target)
        if backend is None:
            return 404, _status_page(404, "no backend for this request").encode()
        name, ip, internal = backend
        forwarded = replace_query_appid(target, internal) if internal else target
        if path in {"/launch", "/resume", "/cancel"} or (path == "/pair" and internal is None):
            self._expose(name)
        status, body = _https_get(ip, GAMESTREAM_HTTPS, forwarded, self.identity, timeout)
        if path in {"/launch", "/resume"} and b"sessionUrl0" in body:
            try:
                body = rewrite_session_host(body.decode("utf-8", "replace"), local_ip).encode()
            except ValueError as exc:
                note("gate", "sessionUrl0 left unchanged: %s", exc)
        return status, body

    def _applist(self) -> str:
        sources: list[tuple[str, str]] = []
        for name, ip in self._live():
            try:
                status, raw = _https_get(ip, GAMESTREAM_HTTPS, "/applist", self.identity, 8.0)
            except OSError as exc:
                note("gate", "applist %s skipped: %s", name, exc)
                continue
            if status != 200:
                note("gate", "applist %s returned HTTP %s", name, status)
                continue
            sources.append((name, raw.decode("utf-8", "replace")))
        merged, table = merge_applists(sources)
        with self._lock:
            self._routes = table
        return merged

    def _backend_for(self, target: str) -> tuple[str, str, str | None] | None:
        path = target.split("?", 1)[0]
        live = dict(self._live())
        exposed, exposed_ip = self._exposed()
        if path in {"/launch", "/appasset"}:
            external = _query_value(target, "appid")
            if external is None or not external.isdigit():
                return None
            with self._lock:
                found = self._routes.get(int(external))
            if found is None:
                self._applist()
                with self._lock:
                    found = self._routes.get(int(external))
            if found is None:
                return None
            name, internal = found
            ip = live.get(name)
            if ip is None:
                return None
            return name, ip, internal
        if path == "/pair":
            if exposed is not None and exposed_ip is not None:
                return exposed, exposed_ip, None
            live = self._live()
            if not live:
                return None
            return live[0][0], live[0][1], None
        if exposed is None or exposed_ip is None:
            return None
        return exposed, exposed_ip, None

    def _expose(self, name: str) -> None:
        _call_helper({"op": "expose", "name": name})
        note("gate", "stream ports follow %s", name)

    def _live(self) -> list[tuple[str, str]]:
        try:
            status = _call_helper({"op": "status"})
        except ClipponError as exc:
            note("gate", "clippon-net unavailable: %s", exc)
            return []
        raw = status.get("instances")
        found: list[tuple[str, str]] = []
        if not isinstance(raw, dict):
            return found
        for name, rec in raw.items():
            if not isinstance(name, str) or not isinstance(rec, dict):
                continue
            if rec.get("exited") or not isinstance(rec.get("pid"), int):
                continue
            ip = rec.get("ip")
            if isinstance(ip, str):
                found.append((name, ip))
        return found

    def _exposed(self) -> tuple[str | None, str | None]:
        try:
            status = _call_helper({"op": "status"})
        except ClipponError:
            return None, None
        name = status.get("exposed")
        raw = status.get("instances")
        if not isinstance(name, str) or not isinstance(raw, dict):
            return None, None
        rec = raw.get(name)
        if not isinstance(rec, dict) or not isinstance(rec.get("ip"), str):
            return None, None
        return name, rec["ip"]

    def _serve_http(self, port: int, https: bool) -> None:
        try:
            server = ThreadingHTTPServer(("0.0.0.0", port), _Handler)
        except OSError as exc:
            error("gate", "cannot bind %s: %s", port, exc)
            return
        server.front = self  # type: ignore[attr-defined]
        server.https = https  # type: ignore[attr-defined]
        if https:
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.load_cert_chain(self.identity.server_cert, self.identity.server_key)
            # Do not ask for a client certificate. Moonlight's certificate is
            # self-signed, and CPython's ssl module has no verify callback, so
            # CERT_OPTIONAL fails the handshake with "unknown ca" before we can
            # reach the pairing check. Pairing state is therefore reported from
            # our own record; see docs/OPEN-ISSUES.md.
            context.verify_mode = ssl.CERT_NONE
            server.socket = context.wrap_socket(server.socket, server_side=True)
        threading.Thread(
            target=server.serve_forever,
            name=f"clippon-{port}",
            daemon=True,
        ).start()
        note("gate", "listening on %s", port)

    def _serve_web(self) -> None:
        try:
            server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            server.bind(("0.0.0.0", WEB_UI))
            server.listen(16)
        except OSError as exc:
            error("gate", "cannot bind %s: %s", WEB_UI, exc)
            return
        note("gate", "listening on %s", WEB_UI)
        while True:
            client, _addr = server.accept()
            threading.Thread(target=self._proxy_web, args=(client,), daemon=True).start()

    def _proxy_web(self, client: socket.socket) -> None:
        _name, ip = self._exposed()
        if ip is None:
            client.close()
            return
        try:
            backend = socket.create_connection((ip, WEB_UI), timeout=10)
        except OSError as exc:
            note("gate", "web proxy to %s failed: %s", ip, exc)
            client.close()
            return
        _splice(client, backend)


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:  # noqa: N802
        front: Front = self.server.front  # type: ignore[attr-defined]
        https: bool = self.server.https  # type: ignore[attr-defined]
        front.handle(self, https)

    def log_message(self, fmt: str, *args: object) -> None:
        return


def _write(handler: BaseHTTPRequestHandler, status: int, body: bytes) -> None:
    handler.close_connection = True
    handler.send_response(status)
    handler.send_header("Content-Type", "application/xml")
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Connection", "close")
    handler.end_headers()
    handler.wfile.write(body)


def _peer_fingerprint(connection: object) -> str | None:
    """SHA-256 of the client certificate, in the form PairedClients stores."""
    getpeercert = getattr(connection, "getpeercert", None)
    if getpeercert is None:
        return None
    try:
        der = getpeercert(binary_form=True)
    except (ValueError, OSError):
        return None
    if not der:
        return None
    import hashlib

    return hashlib.sha256(der).hexdigest()


def _status_page(code: int, message: str) -> str:
    safe = (
        message.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )
    return (
        '<?xml version="1.0" encoding="utf-8"?>'
        f'<root status_code="{code}" status_message="{safe}"></root>'
    )


def _query_value(target: str, key: str) -> str | None:
    _path, _, query = target.partition("?")
    for part in query.split("&"):
        name, sep, value = part.partition("=")
        if sep and name == key:
            return value
    return None


def _http_get(ip: str, port: int, target: str, timeout: float) -> tuple[int, bytes]:
    conn = http.client.HTTPConnection(ip, port, timeout=timeout)
    try:
        conn.request("GET", target, headers={"Host": ip, "Connection": "close"})
        response = conn.getresponse()
        return response.status, response.read()
    finally:
        conn.close()


def _https_get(
    ip: str,
    port: int,
    target: str,
    identity: Identity,
    timeout: float,
) -> tuple[int, bytes]:
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    context.load_cert_chain(str(identity.client_cert), str(identity.client_key))
    conn = http.client.HTTPSConnection(ip, port, timeout=timeout, context=context)
    try:
        conn.request("GET", target, headers={"Host": ip, "Connection": "close"})
        response = conn.getresponse()
        return response.status, response.read()
    finally:
        conn.close()


def _splice(left: socket.socket, right: socket.socket) -> None:
    def pump(src: socket.socket, dst: socket.socket) -> None:
        try:
            while True:
                chunk = src.recv(65536)
                if not chunk:
                    break
                dst.sendall(chunk)
        except OSError:
            pass
        finally:
            try:
                dst.shutdown(socket.SHUT_WR)
            except OSError:
                pass

    first = threading.Thread(target=pump, args=(left, right), daemon=True)
    second = threading.Thread(target=pump, args=(right, left), daemon=True)
    first.start()
    second.start()
    first.join()
    second.join()
    left.close()
    right.close()


def _call_helper(request: dict[str, object]) -> dict[str, object]:
    try:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(30)
        sock.connect(str(HELPER_SOCK))
    except OSError as exc:
        raise ClipponError(f"cannot reach clippon-net: {exc}") from exc
    with sock:
        send_obj(sock, request)
        response = recv_obj(sock)
    if not response.get("ok"):
        raise ClipponError(str(response.get("error") or "clippon-net failed"))
    return response


def open_front(state: Path | None = None) -> Front:
    front = Front(ensure_identity(state))
    front.start()
    return front


def ensure_identity(state: Path | None = None) -> Identity:
    from clippon.identity import ensure

    return ensure(state or state_home())


def trust_instance(directory: Path, identity: Identity, name: str) -> None:
    from clippon.instance import ensure_backend_state

    unique = str(uuidlib.uuid5(uuidlib.NAMESPACE_DNS, f"clippon-{name}")).upper()
    ensure_backend_state(
        directory / "sunshine_state.json",
        unique,
        identity.client_pem(),
        identity.client_uuid,
    )


def instance_certificate() -> tuple[Path, Path] | None:
    """Shared server certificate, once the gateway identity exists."""
    root = gateway_paths()
    cert = root / "server.pem"
    key = root / "server.key"
    if cert.is_file() and key.is_file():
        return cert, key
    return None


def gateway_paths() -> Path:
    return state_home() / "clippon" / "gateway"


def configured_directory(name: str) -> Path:
    return instance_dir(state_home(), name)
