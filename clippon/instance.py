"""Per-instance Sunshine config. One instance, one network namespace, one app."""

from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping
from pathlib import Path

# vb-<name> must fit in IFNAMSIZ-1 (15). "vb-" is three characters.
_NAME = re.compile(r"[a-z][a-z0-9-]{0,11}")


def validate_name(name: str) -> None:
    if _NAME.fullmatch(name) is None:
        raise ValueError(
            "instance name must match [a-z][a-z0-9-]{0,11} "
            f"(got {name!r}); it is used as a network interface suffix"
        )


def seat_name(instance: str) -> str:
    """The libinput seat an instance's cage reads.

    Empty instance means the shared seat, which is what a plain single-instance
    run and any untagged device use.
    """
    return f"seat-cage-{instance}" if instance else "seat-cage"


def instance_seat(instance: str, vhid_shim: Path) -> str:
    """The seat to start this instance's cage on, or "" for the shared one.

    Without the name shim Sunshine cannot tag its devices, so a cage on a
    per-instance seat would find nothing at all. Falling back to the shared
    seat trades cross-talk for a working input path, which is what an instance
    started without the shim gets today.
    """
    if not instance or not vhid_shim.is_file():
        return ""
    return seat_name(instance)


def state_home(env: Mapping[str, str] | None = None) -> Path:
    values = os.environ if env is None else env
    chosen = values.get("XDG_STATE_HOME")
    if chosen:
        return Path(chosen)
    home = values.get("HOME") or str(Path.home())
    return Path(home) / ".local" / "state"


def instance_dir(state: Path, name: str) -> Path:
    return state / "clippon" / "instances" / name


def sunshine_conf(
    directory: Path,
    name: str,
    cert: Path | None = None,
    key: Path | None = None,
) -> str:
    """Absolute paths so two instances never share apps or state.

    The server certificate may be the gateway's, so Moonlight's pinned
    identity and the certificate inside pairing answers are the same one.
    """
    apps = directory / "apps.json"
    state = directory / "sunshine_state.json"
    key = key or (directory / "pkey.pem")
    cert = cert or (directory / "cert.pem")
    log = directory / "sunshine.log"
    return (
        f"port = 47989\n"
        f"address_family = ipv4\n"
        f"upnp = disabled\n"
        f"sunshine_name = {name}\n"
        f"file_apps = {apps}\n"
        f"file_state = {state}\n"
        f"credentials_file = {state}\n"
        f"pkey = {key}\n"
        f"cert = {cert}\n"
        f"log_path = {log}\n"
    )


def apps_document(name: str) -> str:
    """One Desktop-style app: no cmd, so launch only starts the stream."""
    return json.dumps(
        {
            "env": {},
            "apps": [
                {
                    "name": name,
                    "image-path": "desktop.png",
                }
            ],
        },
        indent=2,
    ) + "\n"


def meta_document(
    command: list[str],
    resolution: str,
    sink: str,
    audio: str,
    tray: str,
    verbose: bool,
) -> str:
    return json.dumps(
        {
            "command": command,
            "resolution": resolution,
            "sink": sink,
            "audio": audio,
            "tray": tray,
            "verbose": verbose,
        },
        indent=2,
    ) + "\n"


def write_instance(
    directory: Path,
    name: str,
    command: list[str],
    resolution: str,
    sink: str,
    audio: str,
    tray: str,
    verbose: bool,
    cert: Path | None = None,
    key: Path | None = None,
) -> None:
    validate_name(name)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "sunshine.conf").write_text(
        sunshine_conf(directory, name, cert, key),
        encoding="utf-8",
    )
    (directory / "apps.json").write_text(apps_document(name), encoding="utf-8")
    (directory / "meta.json").write_text(
        meta_document(command, resolution, sink, audio, tray, verbose),
        encoding="utf-8",
    )


def ensure_backend_state(path: Path, unique_id: str, client_pem: str, client_uuid: str) -> None:
    """Let the gateway's client certificate call this Sunshine.

    An existing file keeps every other paired client. The gateway entry is
    added when its name is absent.
    """
    root: dict[str, object] = {}
    if path.is_file():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            loaded = {}
        if isinstance(loaded, dict) and isinstance(loaded.get("root"), dict):
            root = loaded["root"]
    if not isinstance(root.get("uniqueid"), str):
        root["uniqueid"] = unique_id
    devices = root.get("named_devices")
    if not isinstance(devices, list):
        devices = []
    if not any(isinstance(item, dict) and item.get("name") == "clippon-gateway" for item in devices):
        devices.append(
            {
                "name": "clippon-gateway",
                "cert": client_pem,
                "uuid": client_uuid,
                "enabled": True,
            }
        )
    root["named_devices"] = devices
    path.write_text(json.dumps({"root": root}, indent=2) + "\n", encoding="utf-8")


def read_meta(directory: Path) -> dict[str, object]:
    path = directory / "meta.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    return data
