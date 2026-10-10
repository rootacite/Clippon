"""GameStream pairing, answered by the gateway.

Moonlight shows a 4-digit PIN and then blocks on the first /pair request.
`submit_pin` unblocks that request. The certificate it pins is the gateway
server certificate, which is also the certificate the backends present.
"""

from __future__ import annotations

import hashlib
import os
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from urllib.parse import parse_qsl

from clippon.log import note

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes


@dataclass
class _Session:
    unique_id: str
    client_cert_pem: str
    aes_key: bytes
    server_secret: bytes = b""
    server_challenge: bytes = b""
    client_hash: bytes = b""
    phase: str = "cert"


@dataclass
class Pairing:
    server_cert_pem: str
    server_key_pem: str
    on_paired: Callable[[str], None] | None = None
    _sessions: dict[str, _Session] = field(default_factory=dict)
    _pending_pin: str | None = None
    _waiting: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock)
    _pin_arrived: threading.Condition = field(init=False)

    def __post_init__(self) -> None:
        self._pin_arrived = threading.Condition(self._lock)
        self._server_cert = x509.load_pem_x509_certificate(self.server_cert_pem.encode())
        self._server_key = serialization.load_pem_private_key(self.server_key_pem.encode(), password=None)
        self._server_signature = self._server_cert.signature

    def submit_pin(self, pin: str) -> str:
        if len(pin) != 4 or not pin.isdigit():
            raise ValueError("PIN must be 4 digits")
        with self._pin_arrived:
            self._pending_pin = pin
            waiting = self._waiting
            self._pin_arrived.notify_all()
        if waiting:
            return f"pin {pin} sent"
        return f"pin {pin} saved"

    def handle(self, target: str, pin_timeout: float = 300.0) -> str:
        query = dict(parse_qsl(target.split("?", 1)[1] if "?" in target else "", keep_blank_values=True))
        unique_id = query.get("uniqueid", "")
        if not unique_id:
            return _xml(400, "Missing uniqueid")
        phrase = query.get("phrase", "")
        phase = _phase(query)
        note("pair", "%s uniqueid=%s", phase or "unknown", unique_id[:12])
        if phrase == "getservercert":
            return self._getservercert(unique_id, query, pin_timeout)
        if phrase == "pairchallenge":
            return _ok(paired="1")
        session = self._sessions.get(unique_id)
        if session is None:
            note("pair", "no session for uniqueid=%s", unique_id[:12])
            return _xml(400, "Invalid uniqueid")
        if "clientchallenge" in query:
            return self._clientchallenge(session, query["clientchallenge"])
        if "serverchallengeresp" in query:
            return self._serverchallenge(session, query["serverchallengeresp"])
        if "clientpairingsecret" in query:
            result = self._clientsecret(session, query["clientpairingsecret"])
            self._sessions.pop(unique_id, None)
            return result
        return _xml(400, "Invalid pairing request")

    def _getservercert(self, unique_id: str, query: dict[str, str], pin_timeout: float) -> str:
        salt_hex = query.get("salt", "")
        cert_hex = query.get("clientcert", "")
        if len(salt_hex) < 32 or not cert_hex:
            return _xml(400, "Missing salt or clientcert")
        try:
            pin = self._wait_pin(pin_timeout)
        except TimeoutError:
            return _xml(408, "PIN was not entered")
        salt = bytes.fromhex(salt_hex[:32])
        aes_key = hashlib.sha256(salt + pin.encode()).digest()[:16]
        try:
            client_pem = bytes.fromhex(cert_hex).decode()
        except ValueError:
            return _xml(400, "Bad client certificate")
        self._sessions[unique_id] = _Session(unique_id, client_pem, aes_key)
        return _ok(paired="1", plaincert=self.server_cert_pem.encode().hex())

    def _clientchallenge(self, session: _Session, challenge_hex: str) -> str:
        if session.phase != "cert":
            return _xml(400, "Out of order")
        challenge = _aes(session.aes_key, bytes.fromhex(challenge_hex), encrypt=False)
        server_secret = os.urandom(16)
        server_challenge = os.urandom(16)
        digest = hashlib.sha256(challenge + self._server_signature + server_secret).digest()
        encrypted = _aes(session.aes_key, digest + server_challenge, encrypt=True)
        session.server_secret = server_secret
        session.server_challenge = server_challenge
        session.phase = "challenge"
        return _ok(paired="1", challengeresponse=encrypted.hex())

    def _serverchallenge(self, session: _Session, response_hex: str) -> str:
        if session.phase != "challenge":
            return _xml(400, "Out of order")
        session.client_hash = _aes(session.aes_key, bytes.fromhex(response_hex), encrypt=False)
        signature = self._server_key.sign(session.server_secret, padding.PKCS1v15(), hashes.SHA256())
        session.phase = "secret"
        return _ok(paired="1", pairingsecret=(session.server_secret + signature).hex())

    def _clientsecret(self, session: _Session, secret_hex: str) -> str:
        if session.phase != "secret":
            note("pair", "clientpairingsecret out of order (phase=%s)", session.phase)
            return _xml(400, "Out of order")
        blob = bytes.fromhex(secret_hex)
        if len(blob) <= 16:
            note("pair", "clientpairingsecret too short (%d bytes)", len(blob))
            return _ok(paired="0")
        secret, signature = blob[:16], blob[16:]
        client = x509.load_pem_x509_certificate(session.client_cert_pem.encode())
        digest = hashlib.sha256(session.server_challenge + client.signature + secret).digest()
        try:
            client.public_key().verify(signature, secret, padding.PKCS1v15(), hashes.SHA256())
            signed_ok = True
        except Exception as exc:
            note("pair", "client signature rejected: %s", exc)
            signed_ok = False
        same_hash = digest == session.client_hash
        if not same_hash:
            note(
                "pair",
                "challenge hash mismatch (expected %s, got %s)",
                session.client_hash.hex()[:16],
                digest.hex()[:16],
            )
        paired = signed_ok and same_hash
        if paired and self.on_paired is not None:
            self.on_paired(session.client_cert_pem)
        note("pair", "pairing result paired=%s", "1" if paired else "0")
        return _ok(paired="1" if paired else "0")

    def _wait_pin(self, timeout: float) -> str:
        deadline = time.monotonic() + timeout
        with self._pin_arrived:
            self._waiting += 1
            try:
                while self._pending_pin is None:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError
                    self._pin_arrived.wait(remaining)
                pin = self._pending_pin
                self._pending_pin = None
                return pin
            finally:
                self._waiting -= 1


def _phase(query: dict[str, str]) -> str:
    for name in ("getservercert", "clientchallenge", "serverchallengeresp", "clientpairingsecret"):
        if query.get("phrase") == name or name in query:
            return name
    return ""


def _aes(key: bytes, data: bytes, encrypt: bool) -> bytes:
    cipher = Cipher(algorithms.AES(key), modes.ECB())
    transform = cipher.encryptor() if encrypt else cipher.decryptor()
    return transform.update(data) + transform.finalize()


def _ok(**tags: str) -> str:
    body = "".join(f"<{name}>{value}</{name}>" for name, value in tags.items())
    return f'<?xml version="1.0" encoding="utf-8"?><root status_code="200">{body}</root>'


def _xml(code: int, message: str) -> str:
    safe = message.replace("&", "&amp;").replace("<", "&lt;").replace('"', "&quot;")
    return (
        '<?xml version="1.0" encoding="utf-8"?>'
        f'<root status_code="{code}" status_message="{safe}"></root>'
    )
