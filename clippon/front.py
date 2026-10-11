"""Host listeners for 47989, 47984 and 47990.

47989 and 47984 are the GameStream control plane. serverinfo is answered here,
per client: a client is told about its own session only, so two clients can
stream two instances at once. applist, launch, resume, cancel and pair are sent
on to a backend over its veth address, on that instance's own port group, and
box art is a screenshot of the instance the app belongs to. launch also
rewrites sessionUrl0 onto the address the client dialed. 47990 is a raw TCP
proxy onto the exposed instance's web UI.
"""

from __future__ import annotations

import http.client
import io
import select
import socket
import ssl
import subprocess
import threading
import time
from dataclasses import dataclass, replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import uuid as uuidlib

from OpenSSL import SSL as openssl

from clippon.artwork import LiveArtwork
from clippon.clients import PairedClients, openssl_fingerprint
from clippon.config import ClipponError
from clippon.gateway import (
    classify,
    external_app_id,
    may_use_host,
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
from clippon.ports import (
    BASE_PORT,
    GAMESTREAM_HTTP,
    GAMESTREAM_HTTPS,
    WEB_UI,
    PortGroup,
    base_port_ok,
    port_group,
)
from clippon.protocol import HELPER_SOCK, recv_obj, send_obj
from clippon.sessions import Sessions

_HOSTNAME = "clippon"
HANDSHAKE_TIMEOUT = 10.0


@dataclass(frozen=True)
class Backend:
    """One live instance, as the helper recorded it."""

    name: str
    ip: str
    port: int
    internal: str | None = None  # the app id this Sunshine knows the app by

    @property
    def ports(self) -> PortGroup:
        return port_group(self.port)


@dataclass(frozen=True)
class HostStatus:
    """What the backends report right now: who is running which app."""

    busy: dict[str, int]
    codec: str = "0"
    max_luma: str = "0"


class Front:
    def __init__(self, identity: Identity) -> None:
        self.identity = identity
        self.clients = PairedClients(identity.root / "clients.json")
        self.pairing = Pairing(
            identity.server_cert.read_text(encoding="utf-8"),
            identity.server_key.read_text(encoding="utf-8"),
            on_paired=self.clients.add,
        )
        self.artwork = LiveArtwork()
        self.sessions = Sessions()
        self._routes: dict[int, tuple[str, str]] = {}
        self._lock = threading.Lock()

    def submit_pin(self, pin: str) -> str:
        return self.pairing.submit_pin(pin)

    def release(self, instance: str) -> int:
        """Give an instance back: stop its app and drop the records claiming it.

        A client that vanishes mid-stream leaves the backend reporting the app
        running, and that instance then stays hidden from everyone. This is the
        way out without stopping the instance.
        """
        dropped = self.sessions.forget_instance(instance)
        backend = next((item for item in self._backends() if item.name == instance), None)
        if backend is not None:
            self._cancel_backend(backend)
        note("gate", "released %s; %s client session(s) dropped", instance, dropped)
        return dropped

    def start(self) -> None:
        self._serve_http(GAMESTREAM_HTTP, https=False)
        self._serve_http(GAMESTREAM_HTTPS, https=True)
        threading.Thread(target=self._serve_web, name="clippon-web", daemon=True).start()

    def handle(self, handler: BaseHTTPRequestHandler, https: bool) -> None:
        target = handler.path
        path = request_path(target)
        kind = classify(target)
        local_ip = handler.connection.getsockname()[0]
        peer = _peer_fingerprint(handler.connection)
        # Moonlight decides "paired" from ServerInfo.PairStatus, and it is the
        # client certificate that tells us whose answer to give. A request
        # without a certificate (the plain HTTP port, an unknown client) is not
        # paired, and only the pairing paths are served for it: the rest of the
        # GameStream surface belongs to clients this host has recorded.
        paired = peer is not None and self.clients.knows(peer)
        if path == "/pair":
            try:
                body = self.pairing.handle(target).encode()
            except (ValueError, OSError) as exc:
                error("gate", "pair failed: %s", exc)
                body = _status_page(400, "pairing failed").encode()
            _write(handler, 200, body)
            return
        if kind == "local":
            body = self._serverinfo(local_ip, paired, peer).encode()
            _write(handler, 200, body)
            return
        if not may_use_host(path, paired):
            note(
                "gate",
                "refused %s from %s: no paired client certificate",
                target,
                handler.client_address[0],
            )
            _write(handler, 403, _status_page(403, "pair with this host first").encode())
            return
        if kind != "forward":
            _write(handler, 404, _status_page(404, "Not found").encode())
            return
        if path == "/appasset":
            picture = self._live_box_art(target)
            if picture is not None:
                _write(handler, 200, picture, "image/png")
                return
        try:
            status, body = self._forward(target, local_ip, peer)
        except (OSError, ClipponError, ValueError) as exc:
            error("gate", "%s %s failed: %s", "https" if https else "http", target, exc)
            _write(handler, 502, _status_page(502, "backend unavailable").encode())
            return
        _write(handler, status, body)

    def _serverinfo(self, local_ip: str, paired: bool, fingerprint: str | None) -> str:
        """One client's view of the host.

        A paired client is told about its own session and nothing else, which is
        what lets a second client start an app while the first one is streaming.
        The plain HTTP port carries no certificate, so it keeps the old global
        answer; that is the pre-pairing path, where there is no session yet.
        """
        status = self._host_status()
        current = 0
        if paired:
            mine = self.sessions.live(status.busy).get(fingerprint or "")
            if mine is not None:
                current = mine[1]
        else:
            for name in sorted(status.busy):
                current = external_app_id(name, status.busy[name])
                break
        return serverinfo_xml(
            local_ip=local_ip,
            unique_id=self.identity.unique_id,
            hostname=_HOSTNAME,
            paired=paired,
            current_game=current,
            codec_flags=status.codec,
            max_luma=status.max_luma,
        )

    def _forward(self, target: str, local_ip: str, fingerprint: str | None) -> tuple[int, bytes]:
        path = target.split("?", 1)[0]
        timeout = 180.0 if path == "/pair" else 20.0
        if path == "/applist":
            return 200, self._applist(fingerprint).encode()
        backend = self._backend_for(target, fingerprint)
        if backend is None:
            return 404, _status_page(404, "no backend for this request").encode()
        if path == "/launch":
            refusal = self._launch_guard(backend, fingerprint)
            if refusal is not None:
                return refusal
        internal = backend.internal
        forwarded = replace_query_appid(target, internal) if internal else target
        status, body = _https_get(
            backend.ip, backend.ports.https, forwarded, self.identity, timeout
        )
        if path in {"/launch", "/resume"} and status == 200 and b"sessionUrl0" in body:
            self.sessions.remember(fingerprint, backend.name)
            note("gate", "%s is streaming %s", _short(fingerprint), backend.name)
            try:
                body = rewrite_session_host(body.decode("utf-8", "replace"), local_ip).encode()
            except ValueError as exc:
                note("gate", "sessionUrl0 left unchanged: %s", exc)
        elif path == "/cancel" and status == 200:
            self.sessions.forget(fingerprint)
        return status, body

    def _launch_guard(self, backend: Backend, fingerprint: str | None) -> tuple[int, bytes] | None:
        """Refuse a launch someone else is streaming, finish a stale one.

        The refusal is the gateway's own answer, so the client never reaches a
        backend that would report "an app is already running" for an instance
        the client cannot even see. When nobody else holds the instance, a
        leftover app is cancelled first, which makes /launch idempotent: an app
        with no command never stops by itself.
        """
        busy = self._host_status().busy
        if self.sessions.holder(backend.name, busy, fingerprint) is not None:
            note("gate", "%s is streamed by another client; refusing launch", backend.name)
            return 503, _status_page(
                503, "another client is already streaming this application"
            ).encode()
        if backend.name in busy:
            note("gate", "%s still reports an app; cancelling it first", backend.name)
            self._cancel_backend(backend)
        return None

    def _applist(self, fingerprint: str | None) -> str:
        """The merged list, with instances someone else is streaming left out.

        The instance names and ids stay stable, so a client's cached list only
        gains and loses entries; its stored ids keep resolving.
        """
        hidden: set[str] = set()
        if self.sessions.active():
            hidden = self.sessions.hidden(self._host_status().busy, fingerprint)
        sources = self._applist_sources()
        merged, table = merge_applists(sources, hidden)
        self._remember_routes(table)
        return merged

    def _applist_sources(self) -> list[tuple[str, str]]:
        sources: list[tuple[str, str]] = []
        for backend in self._backends():
            try:
                status, raw = _https_get(
                    backend.ip, backend.ports.https, "/applist", self.identity, 8.0
                )
            except OSError as exc:
                note("gate", "applist %s skipped: %s", backend.name, exc)
                continue
            if status != 200:
                note("gate", "applist %s returned HTTP %s", backend.name, status)
                continue
            sources.append((backend.name, raw.decode("utf-8", "replace")))
        return sources

    def _remember_routes(self, table: dict[int, tuple[str, str]]) -> None:
        with self._lock:
            self._routes = table

    def _live_box_art(self, target: str) -> bytes | None:
        """The instance's own screen, for the app the client asked about.

        None means "not ours to answer", and the request goes on to the backend.
        """
        external = box_art_request(target)
        if external is None:
            return None
        found = self._route_for(external)
        if found is None:
            return None
        name, _internal = found
        try:
            return self.artwork.box_art(name)
        except (OSError, subprocess.SubprocessError) as exc:
            note("gate", "box art for %s failed: %s", name, exc)
            return None

    def _route_for(self, external: int) -> tuple[str, str] | None:
        """(instance, internal app id) for an external app id Moonlight stored."""
        with self._lock:
            found = self._routes.get(external)
        if found is None:
            # A client can remember ids across a gateway restart, so rebuild the
            # table from the backends before giving up on the request.
            _merged, table = merge_applists(self._applist_sources())
            self._remember_routes(table)
            with self._lock:
                found = self._routes.get(external)
        return found

    def _backend_for(self, target: str, fingerprint: str | None) -> Backend | None:
        path = target.split("?", 1)[0]
        live = {backend.name: backend for backend in self._backends()}
        if path in {"/launch", "/resume", "/appasset"}:
            external = _query_value(target, "appid")
            if external is None or not external.isdigit():
                return None
            found = self._route_for(int(external))
            if found is None:
                return None
            name, internal = found
            backend = live.get(name)
            if backend is None:
                return None
            return replace(backend, internal=internal)
        if path == "/cancel":
            # A cancel belongs to the client that started the session, which is
            # not necessarily the instance the web UI points at.
            mine = self.sessions.claimed(fingerprint)
            if mine is not None and mine in live:
                return live[mine]
        if path == "/pair":
            exposed = self._exposed()
            if exposed is not None:
                return exposed
            if not live:
                return None
            return next(iter(live.values()))
        return self._exposed()

    def _cancel_backend(self, backend: Backend) -> None:
        try:
            _https_get(backend.ip, backend.ports.https, "/cancel", self.identity, 10.0)
        except OSError as exc:
            note("gate", "cancel on %s failed: %s", backend.name, exc)

    def _host_status(self) -> HostStatus:
        """Poll every live backend for the app it reports running."""
        busy: dict[str, int] = {}
        codec = "0"
        luma = "0"
        for backend in self._backends():
            try:
                status, raw = _http_get(backend.ip, backend.ports.http, "/serverinfo", 2.0)
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
                busy[backend.name] = int(game)
        return HostStatus(busy=busy, codec=codec, max_luma=luma)

    def _backends(self) -> list[Backend]:
        raw = self._status().get("instances")
        found: list[Backend] = []
        if not isinstance(raw, dict):
            return found
        for name, rec in raw.items():
            if not isinstance(name, str) or not isinstance(rec, dict):
                continue
            if rec.get("exited") or not isinstance(rec.get("pid"), int):
                continue
            ip = rec.get("ip")
            if not isinstance(ip, str):
                continue
            port = rec.get("port")
            found.append(
                Backend(name=name, ip=ip, port=port if base_port_ok(port) else BASE_PORT)
            )
        return found

    def _exposed(self) -> Backend | None:
        status = self._status()
        name = status.get("exposed")
        raw = status.get("instances")
        if not isinstance(name, str) or not isinstance(raw, dict):
            return None
        rec = raw.get(name)
        if not isinstance(rec, dict) or not isinstance(rec.get("ip"), str):
            return None
        port = rec.get("port")
        return Backend(
            name=name, ip=rec["ip"], port=port if base_port_ok(port) else BASE_PORT
        )

    def _status(self) -> dict[str, object]:
        try:
            return _call_helper({"op": "status"})
        except ClipponError as exc:
            note("gate", "clippon-net unavailable: %s", exc)
            return {}

    def _serve_http(self, port: int, https: bool) -> None:
        try:
            server = _server_for(port, https, self.identity)
        except OSError as exc:
            error("gate", "cannot bind %s: %s", port, exc)
            return
        server.front = self  # type: ignore[attr-defined]
        server.https = https  # type: ignore[attr-defined]
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
        backend = self._exposed()
        if backend is None:
            client.close()
            return
        try:
            upstream = socket.create_connection(
                (backend.ip, backend.ports.web), timeout=10
            )
        except OSError as exc:
            note("gate", "web proxy to %s failed: %s", backend.ip, exc)
            client.close()
            return
        _splice(client, upstream)


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:  # noqa: N802
        front: Front = self.server.front  # type: ignore[attr-defined]
        https: bool = self.server.https  # type: ignore[attr-defined]
        front.handle(self, https)

    def log_message(self, fmt: str, *args: object) -> None:
        return


def _write(
    handler: BaseHTTPRequestHandler,
    status: int,
    body: bytes,
    content_type: str = "application/xml",
) -> None:
    handler.close_connection = True
    handler.send_response(status)
    handler.send_header("Content-Type", content_type)
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Connection", "close")
    handler.end_headers()
    handler.wfile.write(body)


def _peer_fingerprint(connection: object) -> str | None:
    """SHA-256 of the client certificate, in the form PairedClients stores.

    The TLS layer attaches it to the connection it accepted; a request that
    presented no certificate (the plain HTTP port, an unknown client) has none.
    """
    return getattr(connection, "peer_fingerprint", None)


def _short(fingerprint: str | None) -> str:
    return (fingerprint or "no-certificate")[:8]


def _server_for(port: int, https: bool, identity: Identity) -> ThreadingHTTPServer:
    if https:
        return _TlsServer(("0.0.0.0", port), _Handler, identity)
    return ThreadingHTTPServer(("0.0.0.0", port), _Handler)


def _tls_context(identity: Identity) -> openssl.Context:
    """TLS for 47984 that keeps the client certificate readable.

    CPython's ssl module cannot verify a self-signed client certificate, and it
    exposes no verify callback, so the handshake here asks for a certificate and
    accepts whatever arrives. front.handle() then compares its fingerprint
    against the recorded clients, per request, which is what lets pairing accept
    an unknown certificate while the rest of the surface does not.
    """
    context = openssl.Context(openssl.TLS_SERVER_METHOD)
    context.use_certificate_file(str(identity.server_cert))
    context.use_privatekey_file(str(identity.server_key))
    context.set_verify(openssl.VERIFY_PEER, lambda *_args: True)
    return context


class _TlsConnection:
    """What http.server needs from a connection, over a pyOpenSSL one.

    OpenSSL.SSL.Connection has no makefile and no socket shortcuts such as
    getpeername, so this carries both it and the socket underneath.
    """

    def __init__(
        self,
        connection: openssl.Connection,
        raw: socket.socket,
        peer_fingerprint: str | None,
    ) -> None:
        self._connection = connection
        self._raw = raw
        self.peer_fingerprint = peer_fingerprint

    def makefile(self, mode: str = "rb", buffering: int = -1) -> object:
        if "b" not in mode:
            raise ValueError(f"{mode!r} is not a binary mode")
        stream = _TlsStream(self._connection)
        if buffering == 0:
            return stream
        if "r" in mode:
            return io.BufferedReader(stream)
        return io.BufferedWriter(stream)

    def recv(self, size: int) -> bytes:
        return self._connection.recv(size)

    def sendall(self, data: bytes) -> None:
        self._connection.sendall(data)

    def getsockname(self) -> tuple[str, int]:
        return self._raw.getsockname()

    def getpeername(self) -> tuple[str, int]:
        return self._raw.getpeername()

    def settimeout(self, value: float | None) -> None:
        self._raw.settimeout(value)

    def shutdown(self, _how: int = socket.SHUT_RDWR) -> None:
        try:
            self._connection.shutdown()
        except (openssl.Error, OSError):
            pass

    def close(self) -> None:
        for closer in (self._connection.close, self._raw.close):
            try:
                closer()
            except (openssl.Error, OSError):
                pass


class _TlsStream(io.RawIOBase):
    """A binary stream over an OpenSSL connection, for BufferedReader."""

    def __init__(self, connection: openssl.Connection) -> None:
        self._connection = connection

    def readable(self) -> bool:
        return True

    def writable(self) -> bool:
        return True

    def readinto(self, buffer: object) -> int:
        view = memoryview(buffer)  # type: ignore[arg-type]
        try:
            data = self._connection.recv(len(view))
        except openssl.ZeroReturnError:
            return 0
        except openssl.WantReadError:
            return 0
        if not data:
            return 0
        view[: len(data)] = data
        return len(data)

    def write(self, data: object) -> int:
        payload = bytes(data)  # type: ignore[arg-type]
        self._connection.sendall(payload)
        return len(payload)


class _TlsServer(ThreadingHTTPServer):
    """ThreadingHTTPServer whose connections are TLS from the start."""

    def __init__(self, address: tuple[str, int], handler: type, identity: Identity) -> None:
        self._context = _tls_context(identity)
        super().__init__(address, handler)

    def get_request(self) -> tuple[object, tuple[str, int]]:
        while True:
            raw, address = super().get_request()
            connection = self._handshake(raw, address)
            if connection is not None:
                return connection, address

    def _handshake(
        self, raw: socket.socket, address: tuple[str, int]
    ) -> _TlsConnection | None:
        """Finish the handshake within a deadline, or close and give up.

        A timeout on the socket puts it in non-blocking mode, so the handshake
        reports WantReadError instead of waiting; select() provides the wait.
        """
        deadline = time.monotonic() + HANDSHAKE_TIMEOUT
        connection = openssl.Connection(self._context, raw)
        connection.set_accept_state()
        try:
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("handshake deadline")
                raw.settimeout(remaining)
                try:
                    connection.do_handshake()
                    break
                except openssl.WantReadError:
                    if not select.select([raw], [], [], remaining)[0]:
                        raise TimeoutError("handshake deadline") from None
        except (openssl.Error, OSError, TimeoutError) as exc:
            note("gate", "tls handshake with %s failed: %r", address[0], exc)
            raw.close()
            return None
        raw.settimeout(None)
        return _TlsConnection(
            connection,
            raw,
            openssl_fingerprint(connection.get_peer_certificate()),
        )


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


def box_art_request(target: str) -> int | None:
    """The external app id when *target* asks for box art, else None.

    Moonlight asks for AssetType=2 only (nvhttp.cpp getBoxArt); other asset
    types stay with the backend, which is the only place that has them.
    """
    asset = _query_value(target, "AssetType")
    if asset not in (None, "2"):
        return None
    external = _query_value(target, "appid")
    if external is None or not external.isdigit():
        return None
    return int(external)


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
