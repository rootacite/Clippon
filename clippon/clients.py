"""Paired Moonlight clients, remembered across gateway restarts."""

from __future__ import annotations

import json
import threading
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes


class PairedClients:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()
        self._pem: list[str] = []
        self._fingerprints: set[bytes] = set()
        self._load()

    def _load(self) -> None:
        if not self.path.is_file():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        if not isinstance(data, list):
            return
        for item in data:
            if isinstance(item, str):
                self.add(item, save=False)

    def add(self, pem: str, save: bool = True) -> None:
        digest = parse_fingerprint(pem)
        if digest is None:
            return
        with self._lock:
            already = pem in self._pem
            if not already:
                self._pem.append(pem)
            self._fingerprints.add(digest)
        if save and not already:
            self._write()

    def knows(self, fingerprint_hex: str) -> bool:
        return fingerprint_hex.lower() in {value.hex() for value in self._fingerprints}

    def all(self) -> list[str]:
        with self._lock:
            return list(self._pem)

    def _write(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(self.all(), indent=2) + "\n", encoding="utf-8")
        temporary.replace(self.path)


def fingerprint(pem: str) -> bytes:
    """SHA-256 of the DER body, the same digest TLS exposes for a peer chain.

    Some Moonlight builds emit certificates whose serial number cryptography
    warns about. The warning is noise here, so it is suppressed.
    """
    return parse_fingerprint(pem) or _strict_fingerprint(pem)


def parse_fingerprint(pem: str) -> bytes | None:
    import warnings

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        try:
            certificate = x509.load_pem_x509_certificate(pem.encode())
        except ValueError:
            return None
        return certificate.fingerprint(hashes.SHA256())


def _strict_fingerprint(pem: str) -> bytes:
    certificate = x509.load_pem_x509_certificate(pem.encode())
    return certificate.fingerprint(hashes.SHA256())
