"""Box art for the gateway: a screenshot of the instance, not the placeholder.

Moonlight asks for AssetType=2 and draws whatever it gets at a fixed 200x267,
so a 16:9 screen returned unchanged would be stretched into the card. What is
served here is the whole frame, centred and undistorted, over a blurred copy of
itself that fills the card. A short cache keeps the repeated requests of one
applist away from grim.
"""

from __future__ import annotations

import io
import math
import os
import re
import subprocess
import tempfile
import threading
import time
from pathlib import Path

from clippon.log import debug, note
from clippon.proc import process_env_value

try:
    from PIL import Image, ImageFilter
except ImportError:  # Pillow is declared by both installers; keep the app alive without it.
    Image = None  # type: ignore[assignment]
    ImageFilter = None  # type: ignore[assignment]

CARD = (400, 534)  # twice the 200x267 Moonlight draws, so HiDPI stays sharp
CACHE_SECONDS = 20.0
GRIM_TIMEOUT = 8.0
GRIM_QUALITY = 92

_WAYLAND_SOCKET = re.compile(r"/wayland-[0-9]+$")
_PID = re.compile(r"pid=(\d+)")


def parse_sockets(text: str) -> list[tuple[str, list[int]]]:
    """Wayland sockets and their owning pids, from `ss -xlp` output."""
    found: list[tuple[str, list[int]]] = []
    for line in text.splitlines():
        if "wayland-" not in line:
            continue
        path = next((field for field in line.split() if _WAYLAND_SOCKET.search(field)), None)
        if path is None:
            continue
        pids = sorted({int(pid) for pid in _PID.findall(line)})
        if pids:
            found.append((path, pids))
    return found


def socket_table() -> str:
    try:
        proc = subprocess.run(
            ["ss", "-xlp"],
            check=False,
            capture_output=True,
            text=True,
            timeout=5.0,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return proc.stdout


def display_for(instance: str) -> tuple[str, str] | None:
    """(runtime dir, socket name) of the cage serving *instance*.

    cage keeps the real XDG_RUNTIME_DIR and the seat shim's tag names it, so the
    owning pid is matched on CLIPPON_SEAT rather than on the socket name, which
    is handed out per compositor and differs from one boot to the next.
    """
    seat = f"seat-cage-{instance}"
    for path, pids in parse_sockets(socket_table()):
        for pid in pids:
            if process_env_value(pid, "CLIPPON_SEAT") != seat:
                continue
            runtime = process_env_value(pid, "XDG_RUNTIME_DIR")
            if runtime:
                return runtime, Path(path).name
    return None


def compose(shot: bytes, card: tuple[int, int] = CARD) -> bytes | None:
    """Fit one screenshot into the card: blurred fill behind, whole frame in front."""
    if Image is None or ImageFilter is None:
        return None
    try:
        with Image.open(io.BytesIO(shot)) as source:
            picture = source.convert("RGB")
    except (OSError, ValueError):
        return None
    width, height = card
    cover = max(width / picture.width, height / picture.height)
    grown = picture.resize(
        (math.ceil(picture.width * cover), math.ceil(picture.height * cover)),
        Image.LANCZOS,
    )
    left = (grown.width - width) // 2
    top = (grown.height - height) // 2
    canvas = grown.crop((left, top, left + width, top + height))
    canvas = canvas.filter(ImageFilter.GaussianBlur(16))
    fit = min(width / picture.width, height / picture.height)
    inner = picture.resize(
        (max(1, round(picture.width * fit)), max(1, round(picture.height * fit))),
        Image.LANCZOS,
    )
    canvas.paste(inner, ((width - inner.width) // 2, (height - inner.height) // 2))
    out = io.BytesIO()
    canvas.save(out, "PNG", optimize=True)
    return out.getvalue()


class LiveArtwork:
    """Screenshots of running instances, cached for a few seconds."""

    def __init__(self, card: tuple[int, int] = CARD, ttl: float = CACHE_SECONDS) -> None:
        self.card = card
        self.ttl = ttl
        self._lock = threading.Lock()
        self._per_instance: dict[str, threading.Lock] = {}
        self._cache: dict[str, tuple[float, bytes]] = {}
        self._warned = False

    def box_art(self, instance: str) -> bytes | None:
        cached = self._from_cache(instance, time.monotonic())
        if cached is not None:
            return cached
        with self._lock:
            guard = self._per_instance.setdefault(instance, threading.Lock())
        # One screenshot per instance at a time; a second client asking during
        # the capture waits here and then reads the fresh cache entry.
        with guard:
            cached = self._from_cache(instance, time.monotonic())
            if cached is not None:
                return cached
            picture = self._capture(instance)
        if picture is None:
            return None
        with self._lock:
            self._cache[instance] = (time.monotonic(), picture)
        return picture

    def _from_cache(self, instance: str, now: float) -> bytes | None:
        with self._lock:
            hit = self._cache.get(instance)
        if hit is None or now - hit[0] >= self.ttl:
            return None
        return hit[1]

    def _capture(self, instance: str) -> bytes | None:
        if Image is None and not self._warned:
            self._warned = True
            note("gate", "Pillow is missing; box art stays on the backend placeholder")
            return None
        found = display_for(instance)
        if found is None:
            debug("gate", "no cage display for %s, using the backend box art", instance)
            return None
        runtime, socket_name = found
        with tempfile.TemporaryDirectory(prefix="clippon-art-") as temporary:
            shot = Path(temporary) / "shot.jpg"
            env = os.environ | {"XDG_RUNTIME_DIR": runtime, "WAYLAND_DISPLAY": socket_name}
            try:
                # JPEG because grim's PNG encoder spends 1.3s on a 1440p frame
                # against 0.3s here; the card is rescaled anyway.
                proc = subprocess.run(
                    ["grim", "-t", "jpeg", "-q", str(GRIM_QUALITY), str(shot)],
                    env=env,
                    check=False,
                    capture_output=True,
                    timeout=GRIM_TIMEOUT,
                )
            except (OSError, subprocess.TimeoutExpired) as exc:
                note("gate", "grim for %s failed: %s", instance, exc)
                return None
            if proc.returncode != 0 or not shot.is_file():
                detail = proc.stderr.decode("utf-8", "replace").strip().splitlines()
                note(
                    "gate",
                    "grim for %s exited %s%s",
                    instance,
                    proc.returncode,
                    f": {detail[-1]}" if detail else "",
                )
                return None
            raw = shot.read_bytes()
        return compose(raw, self.card)
