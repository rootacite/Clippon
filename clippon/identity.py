"""The certificate Moonlight pins, and the client certificate backends trust."""

from __future__ import annotations

import json
import os
import subprocess
import uuid
from dataclasses import dataclass
from pathlib import Path

_CLIENT_NAME = "clippon-gateway"


@dataclass(frozen=True)
class Identity:
    root: Path
    unique_id: str
    server_cert: Path
    server_key: Path
    client_cert: Path
    client_key: Path
    client_uuid: str

    def client_pem(self) -> str:
        return self.client_cert.read_text(encoding="utf-8")


def gateway_dir(state: Path) -> Path:
    return state / "clippon" / "gateway"


def ensure(state: Path) -> Identity:
    root = gateway_dir(state)
    root.mkdir(parents=True, exist_ok=True)
    record_path = root / "identity.json"
    record = _read_record(record_path)
    unique_id = record.get("uniqueid")
    client_uuid = record.get("client_uuid")
    if not isinstance(unique_id, str) or not unique_id:
        unique_id = str(uuid.uuid4()).upper()
    if not isinstance(client_uuid, str) or not client_uuid:
        client_uuid = str(uuid.uuid5(uuid.NAMESPACE_DNS, _CLIENT_NAME))
    server_key = root / "server.key"
    server_cert = root / "server.pem"
    client_key = root / "client.key"
    client_cert = root / "client.pem"
    _certificate(server_key, server_cert, "clippon")
    _certificate(client_key, client_cert, _CLIENT_NAME)
    record_path.write_text(
        json.dumps({"uniqueid": unique_id, "client_uuid": client_uuid}, indent=2) + "\n",
        encoding="utf-8",
    )
    return Identity(
        root=root,
        unique_id=unique_id,
        server_cert=server_cert,
        server_key=server_key,
        client_cert=client_cert,
        client_key=client_key,
        client_uuid=client_uuid,
    )


def _read_record(path: Path) -> dict[str, object]:
    if not path.is_file():
        return {}
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _certificate(key: Path, cert: Path, common_name: str) -> None:
    if key.is_file() and cert.is_file():
        return
    subprocess.run(
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-keyout",
            str(key),
            "-out",
            str(cert),
            "-days",
            "3650",
            "-nodes",
            "-subj",
            f"/CN={common_name}",
        ],
        check=True,
        capture_output=True,
    )
    os.chmod(key, 0o600)
