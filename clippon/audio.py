"""A private PipeWire instance so the streamed app never touches host audio.

Only processes whose environment contains this instance's PIPEWIRE_RUNTIME_DIR
are stopped. The host's pipewire, wireplumber and pulse daemons are not.
"""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import time
from pathlib import Path
from typing import BinaryIO

from clippon.log import debug, warn
from clippon.proc import (
    kill,
    pids_named,
    process_has_env,
    reap,
    signal_group,
    terminate,
    wait_gone,
)

PIPEWIRE_CONF = Path("/usr/share/pipewire/pipewire.conf")
WIREPLUMBER_CONF = Path("/usr/share/wireplumber/wireplumber.conf")
_DAEMON_NAMES = ("pipewire", "pipewire-pulse", "wireplumber", "dbus-daemon")


def pipewire_dropin(sink: str) -> str:
    return f"""\
# written by clippon: the sink the streamed app plays into
context.objects = [
    {{ factory = metadata
      args = {{
        metadata.name = default
        metadata.values = [
          {{ key = default.audio.sink value = {{ name = {sink} }} }}
        ]
      }}
    }}
    {{ factory = adapter
      args = {{
        factory.name     = support.null-audio-sink
        node.name        = {sink}
        node.description = "Cage stream sink"
        media.class      = Audio/Sink
        object.linger    = true
        audio.position   = [ FL FR ]
      }}
    }}
]
"""


def wireplumber_dropin() -> str:
    return """\
# written by clippon: session manager for the stream instance only,
# with all real hardware monitors off so no host device shows up in it.
wireplumber.profiles = {
  main = {
    monitor.alsa = disabled
    monitor.alsa-midi = disabled
    monitor.bluez = disabled
    monitor.v4l2 = disabled
    monitor.libcamera = disabled
  }
}
"""


class AudioInstance:
    def __init__(
        self,
        runtime_dir: Path,
        sink: str,
        log_file: BinaryIO,
        name: str = "",
    ) -> None:
        # A named instance must not reap or reuse another instance's runtime dir.
        folder = f"cage-audio-{name}" if name else "cage-audio"
        self.root = runtime_dir / folder
        self.sink = sink
        self.log_file = log_file
        self.procs: list[subprocess.Popen[bytes]] = []

    @property
    def pulse_socket(self) -> Path:
        return self.root / "pulse" / "native"

    def child_env(self) -> dict[str, str]:
        """Environment cage and Sunshine need to stay inside this instance."""
        return {
            "PULSE_SERVER": f"unix:{self.pulse_socket}",
            "PIPEWIRE_RUNTIME_DIR": str(self.root),
        }

    def start(self) -> bool:
        if shutil.which("pipewire") is None or shutil.which("pipewire-pulse") is None:
            warn("audio", "pipewire or pipewire-pulse is missing")
            return False
        if not PIPEWIRE_CONF.is_file():
            warn("audio", "stock config %s is missing", PIPEWIRE_CONF)
            return False
        try:
            self._reap_leftovers()
            self._write_config()
        except OSError as exc:
            warn("audio", "cannot prepare %s: %s", self.root, exc)
            return False
        self._spawn()
        if self._wait_for_socket():
            return True
        warn("audio", "private pulse socket did not appear: %s", self.pulse_socket)
        self.stop()
        return False

    def stop(self) -> None:
        # Keep self.procs populated until the daemons are dead. A second
        # Ctrl-C calls force-kill from the signal handler and must still see them.
        procs = list(self.procs)
        for proc in procs:
            if proc.poll() is None:
                signal_group(proc.pid, signal.SIGTERM)
        for proc in procs:
            if proc.poll() is None and not wait_gone(proc.pid, 2.0):
                signal_group(proc.pid, signal.SIGKILL)
            reap(proc, 1.0)
        self.procs.clear()

    def _reap_leftovers(self) -> None:
        """Stop an earlier private instance. Host daemons lack the runtime dir."""
        matched: list[int] = []
        for name in _DAEMON_NAMES:
            for pid in pids_named(name):
                if process_has_env(pid, "PIPEWIRE_RUNTIME_DIR", str(self.root)):
                    matched.append(pid)
        if not matched:
            return
        debug("audio", "stopping %s leftover private audio process(es)", len(matched))
        # These are not necessarily process-group leaders (dbus-daemon is a
        # child). Signal the pid only, the same way the shell script did.
        for pid in matched:
            terminate(pid)
        for pid in matched:
            if not wait_gone(pid, 2.0):
                kill(pid)

    def _write_config(self) -> None:
        config = self.root / "config"
        wp = self.root / "wp"
        (config / "pipewire.conf.d").mkdir(parents=True, exist_ok=True)
        (wp / "wireplumber.conf.d").mkdir(parents=True, exist_ok=True)
        (self.root / "state").mkdir(parents=True, exist_ok=True)
        (self.root / "pulse").mkdir(parents=True, exist_ok=True)
        # PIPEWIRE_CONFIG_DIR replaces the search path, so the stock file has
        # to live inside it and our drop-in is merged from there.
        _symlink(PIPEWIRE_CONF, config / "pipewire.conf")
        _symlink(WIREPLUMBER_CONF, wp / "wireplumber.conf")
        (config / "pipewire.conf.d" / "10-cage-audio.conf").write_text(
            pipewire_dropin(self.sink), encoding="utf-8"
        )
        (wp / "wireplumber.conf.d" / "99-isolated.conf").write_text(
            wireplumber_dropin(), encoding="utf-8"
        )
        for name in (
            "pipewire-0",
            "pipewire-0.lock",
            "pipewire-0-manager",
            "pipewire-0-manager.lock",
        ):
            (self.root / name).unlink(missing_ok=True)
        self.pulse_socket.unlink(missing_ok=True)

    def _spawn(self) -> None:
        base = os.environ.copy()
        self.procs.append(
            _popen(
                _own_session(["pipewire"]),
                base
                | {
                    "PIPEWIRE_RUNTIME_DIR": str(self.root),
                    "PIPEWIRE_CONFIG_DIR": str(self.root / "config"),
                },
                self.log_file,
            )
        )
        self.procs.append(
            _popen(
                _own_session(["pipewire-pulse"]),
                base
                | {
                    "PIPEWIRE_RUNTIME_DIR": str(self.root),
                    "PULSE_RUNTIME_PATH": str(self.root / "pulse"),
                },
                self.log_file,
            )
        )
        if shutil.which("wireplumber") and shutil.which("dbus-run-session"):
            self.procs.append(
                _popen(
                    _own_session(["wireplumber"]),
                    base
                    | {
                        "PIPEWIRE_RUNTIME_DIR": str(self.root),
                        "WIREPLUMBER_CONFIG_DIR": str(self.root / "wp"),
                        "XDG_STATE_HOME": str(self.root / "state"),
                        "XDG_CONFIG_HOME": str(self.root / "state" / "cfg"),
                    },
                    self.log_file,
                )
            )
        else:
            debug("audio", "wireplumber not started")

    def _wait_for_socket(self) -> bool:
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            if self.pulse_socket.is_socket():
                return True
            if any(proc.poll() is not None for proc in self.procs[:2]):
                return False
            time.sleep(0.1)
        return self.pulse_socket.is_socket()


def _own_session(argv: list[str]) -> list[str]:
    """Run a daemon on a session bus of its own.

    pipewire-pulse registers org.pulseaudio.Server on the bus it inherits, and
    the desktop session already owns that name, so every instance that shares
    the host bus fails to register. wireplumber has always been started this
    way; the other daemons now are too.
    """
    if shutil.which("dbus-run-session"):
        return ["dbus-run-session", "--", *argv]
    return argv


def _symlink(target: Path, link: Path) -> None:
    if link.is_symlink() or link.exists():
        link.unlink()
    link.symlink_to(target)


def _popen(
    argv: list[str], env: dict[str, str], log_file: BinaryIO
) -> subprocess.Popen[bytes]:
    return subprocess.Popen(
        argv,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=log_file,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
