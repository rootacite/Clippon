"""The `--quick` preset: resolution, input-method environment, and encoder.

`clipponctl set-quick` stores one preset. `clipponctl start --quick` reads it
back. With no file, the built-in preset is what a start uses.
"""

from __future__ import annotations

import json
import os
import shlex
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from clippon.config import ClipponError, env_pair, resolution_ok
from clippon.instance import sunshine_encoder

DEFAULT_RESOLUTION = "1920x1080@120Hz"
DEFAULT_ENCODER = "vaapi"
DEFAULT_ENV: tuple[tuple[str, str], ...] = (
    ("GTK_IM_MODULE", "fcitx"),
    ("QT_IM_MODULE", "fcitx"),
    ("XMODIFIERS", "@im=fcitx"),
    ("SDL_IM_MODULE", "fcitx"),
)


@dataclass(frozen=True)
class Quick:
    resolution: str
    env: tuple[tuple[str, str], ...]
    encoder: str


DEFAULT_QUICK = Quick(DEFAULT_RESOLUTION, DEFAULT_ENV, DEFAULT_ENCODER)


@dataclass(frozen=True)
class QuickUpdate:
    """Fields a `set-quick` command line named. None means "leave it"."""

    resolution: str | None
    env: tuple[tuple[str, str], ...] | None
    encoder: str | None


def config_home(env: Mapping[str, str] | None = None) -> Path:
    values = os.environ if env is None else env
    chosen = values.get("XDG_CONFIG_HOME")
    if chosen:
        return Path(chosen)
    home = values.get("HOME") or str(Path.home())
    return Path(home) / ".config"


def quick_path(env: Mapping[str, str] | None = None) -> Path:
    return config_home(env) / "clippon" / "quick.json"


def load_quick(env: Mapping[str, str] | None = None) -> Quick:
    path = quick_path(env)
    if not path.is_file():
        return DEFAULT_QUICK
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ClipponError(f"cannot read {path}: {exc}") from exc
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ClipponError(f"cannot read {path}: {exc}") from exc
    return _quick_from_json(data, path)


def save_quick(preset: Quick, env: Mapping[str, str] | None = None) -> Path:
    path = quick_path(env)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "resolution": preset.resolution,
        "env": [[key, value] for key, value in preset.env],
        "encoder": preset.encoder,
    }
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)
    return path


def format_quick(preset: Quick) -> str:
    argv = ["--res", preset.resolution]
    for key, value in preset.env:
        argv.extend(["--env", f"{key}={value}"])
    argv.extend(["--encoder", preset.encoder])
    return shlex.join(argv)


def command_basename(command: str) -> str:
    """Session name taken from the program that is about to run.

    `/usr/bin/dolphin` and `./dolphin` are both `dolphin`. The name is the
    file name only, so `wlc /path/to/game.exe` is named `wlc`.
    """
    name = Path(command).name
    if not name or name in {".", ".."}:
        raise ClipponError(
            f"cannot derive a session name from {command!r}",
            code=2,
        )
    return name


def apply_update(current: Quick, update: QuickUpdate) -> Quick:
    return Quick(
        resolution=current.resolution if update.resolution is None else update.resolution,
        env=current.env if update.env is None else update.env,
        encoder=current.encoder if update.encoder is None else update.encoder,
    )


def parse_quick_update(argv: list[str]) -> QuickUpdate | None:
    """Parse `set-quick` options. No options means "print the preset"."""
    if not argv:
        return None
    resolution: str | None = None
    encoder: str | None = None
    env: list[tuple[str, str]] | None = None
    index = 0
    while index < len(argv):
        item = argv[index]
        if item == "--":
            if argv[index + 1 :]:
                raise ClipponError("set-quick takes options, not a command", code=2)
            break
        if item == "--res" or item.startswith("--res="):
            if item == "--res":
                if index + 1 >= len(argv):
                    raise ClipponError("--res needs an argument, e.g. --res 1920x1080", code=2)
                resolution = argv[index + 1]
                index += 2
            else:
                resolution = item[len("--res=") :]
                index += 1
            if not resolution_ok(resolution):
                raise ClipponError(
                    "--res: expected AxB or AxB@C[Hz], e.g. 1920x1080 or "
                    f"2560x1440@120Hz (got {resolution!r})",
                    code=2,
                )
            continue
        if item == "--env" or item.startswith("--env="):
            if item == "--env":
                if index + 1 >= len(argv):
                    raise ClipponError("--env needs KEY=VALUE", code=2)
                pair = env_pair(argv[index + 1])
                index += 2
            else:
                pair = env_pair(item[len("--env=") :])
                index += 1
            if env is None:
                env = []
            env.append(pair)
            continue
        if item == "--encoder" or item.startswith("--encoder="):
            if item == "--encoder":
                if index + 1 >= len(argv):
                    raise ClipponError("--encoder needs one of vaapi, vulkan, soft", code=2)
                raw = argv[index + 1]
                index += 2
            else:
                raw = item[len("--encoder=") :]
                index += 1
            try:
                sunshine_encoder(raw)
            except ValueError as exc:
                raise ClipponError(str(exc), code=2) from exc
            encoder = raw
            continue
        if item.startswith("-"):
            raise ClipponError(f"unknown option: {item}", code=2)
        raise ClipponError("set-quick takes options, not a command", code=2)
    return QuickUpdate(
        resolution=resolution,
        env=None if env is None else _collapse_env(env),
        encoder=encoder,
    )


def merged_env(
    preset: tuple[tuple[str, str], ...],
    override: tuple[tuple[str, str], ...],
) -> dict[str, str]:
    """Preset variables, then the ones named on this start. A repeated name uses the later value."""
    found = dict(preset)
    found.update(dict(override))
    return found


def _collapse_env(pairs: list[tuple[str, str]]) -> tuple[tuple[str, str], ...]:
    order: list[str] = []
    values: dict[str, str] = {}
    for key, value in pairs:
        if key not in values:
            order.append(key)
        values[key] = value
    return tuple((key, values[key]) for key in order)


def _quick_from_json(data: object, path: Path) -> Quick:
    if not isinstance(data, dict):
        raise ClipponError(f"{path} must be a JSON object")
    extra = set(data) - {"resolution", "env", "encoder"}
    if extra:
        names = ", ".join(sorted(extra))
        raise ClipponError(f"{path} has unknown fields: {names}")
    resolution = data.get("resolution")
    encoder = data.get("encoder")
    raw_env = data.get("env")
    if not isinstance(resolution, str) or not resolution_ok(resolution):
        raise ClipponError(f"{path}: resolution must be AxB or AxB@C[Hz]")
    if not isinstance(encoder, str):
        raise ClipponError(f"{path}: encoder must be vaapi, vulkan, or soft")
    try:
        sunshine_encoder(encoder)
    except ValueError as exc:
        raise ClipponError(f"{path}: {exc}") from exc
    if not isinstance(raw_env, list):
        raise ClipponError(f"{path}: env must be a list of [KEY, VALUE] pairs")
    pairs: list[tuple[str, str]] = []
    for item in raw_env:
        if (
            not isinstance(item, list)
            or len(item) != 2
            or not isinstance(item[0], str)
            or not isinstance(item[1], str)
        ):
            raise ClipponError(f"{path}: env must be a list of [KEY, VALUE] pairs")
        key, value = env_pair(f"{item[0]}={item[1]}")
        pairs.append((key, value))
    return Quick(resolution, _collapse_env(pairs), encoder)
