"""Start cage and Sunshine, and tear them down in an order both can survive.

Sunshine restores its default sink over the private pulse socket while it
shuts down, and hangs if that socket is already gone. Audio is stopped only
after Sunshine has exited. Cage is signalled first so a Ctrl-C unblocks the wait.
"""

from __future__ import annotations

import os
import signal
import subprocess
import time
from pathlib import Path
from typing import BinaryIO, NoReturn

from clippon.audio import AudioInstance
from clippon.cage import (
    current_mode,
    new_wayland_socket,
    socket_from_ss,
    start_cage,
    wayland_names,
)
from clippon.config import ClipponError, Config
from clippon.instance import instance_dir, state_home
from clippon.log import add_log_file, block, debug, error, note, warn
from clippon.preflight import check
from clippon.proc import RemoteProcess, kill, reap, shell_status, signal_group, terminate, wait_gone
from clippon.sunshine import (
    TrayPlan,
    fatal_lines,
    managed_env,
    start_sunshine,
    start_sunshine_in_netns,
    tray_plan,
)


class Session:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.cage: subprocess.Popen[bytes] | None = None
        self.sun: subprocess.Popen[bytes] | RemoteProcess | None = None
        self.audio: AudioInstance | None = None
        self._cleaning = False
        self._handles: dict[str, BinaryIO] = {}
        self._log_paths: dict[str, Path] = {}

    def run(self) -> int:
        self._install_signals()
        # Children must see the runtime dir the shell script would have exported.
        os.environ["XDG_RUNTIME_DIR"] = str(self.cfg.runtime_dir)
        try:
            self._open_logs()
            check(self.cfg)
            audio_env, audio_state = self._start_audio()
            socket_name = self._start_cage(audio_env)
            plan = self._start_sunshine(socket_name, audio_env)
            self._warn_if_sunshine_unhappy()
            self._announce(socket_name, plan, audio_state)
            status = self._wait_for_cage()
            note("cage", "exited (status %s), stopping sunshine", status)
            return status
        except ClipponError as exc:
            error("clip", "%s", exc)
            return exc.code
        finally:
            self._cleanup()

    def _install_signals(self) -> None:
        signal.signal(signal.SIGINT, self._on_signal)
        signal.signal(signal.SIGTERM, self._on_signal)

    def _on_signal(self, signum: int, _frame: object) -> None:
        code = 128 + signum
        if self._cleaning:
            self._force_kill()
            raise SystemExit(code)
        if self.cage is not None and self.cage.poll() is None:
            terminate(self.cage.pid)
        raise SystemExit(code)

    def _open_logs(self) -> None:
        try:
            self.cfg.log_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise ClipponError(f"cannot create log directory {self.cfg.log_dir}: {exc}") from exc
        stamp = time.strftime("%Y%m%d-%H%M%S")
        for kind in ("cage", "sunshine", "audio", "clippon"):
            path = self.cfg.log_dir / f"{kind}-{stamp}.log"
            self._log_paths[kind] = path
            _symlink(path, self.cfg.log_dir / f"{kind}-latest.log")
        add_log_file(self._log_paths["clippon"])
        try:
            for kind in ("cage", "sunshine", "audio"):
                self._handles[kind] = open(self._log_paths[kind], "ab", buffering=0)
        except OSError as exc:
            raise ClipponError(f"cannot open log file: {exc}") from exc
        note("clip", "logs  %s", self.cfg.log_dir)

    def _start_audio(self) -> tuple[dict[str, str], str]:
        if not self.cfg.audio_enabled:
            note("audio", "isolation disabled")
            return {}, "disabled"
        note("audio", "starting private instance, sink %s", self.cfg.sink)
        audio = AudioInstance(
            self.cfg.runtime_dir,
            self.cfg.sink,
            self._handles["audio"],
            name=self.cfg.instance,
        )
        self.audio = audio
        if not audio.start():
            return {}, "failed"
        note("audio", "ready")
        return audio.child_env(), "private"

    def _start_cage(self, audio_env: dict[str, str]) -> str:
        before = set(wayland_names(self.cfg.runtime_dir))
        note("cage", "starting")
        try:
            self.cage = start_cage(
                self.cfg.command,
                self.cfg.resolution,
                audio_env,
                self._handles["cage"],
                self.cfg.shim,
                self.cfg.cursor_shim,
            )
        except OSError as exc:
            raise ClipponError(f"cannot start cage: {exc}") from exc
        socket_name = self._wait_for_socket(before)
        note("cage", "display %s", socket_name)
        return socket_name

    def _wait_for_socket(self, before: set[str]) -> str:
        assert self.cage is not None
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            code = self.cage.poll()
            if code is not None:
                status = shell_status(code)
                self._fail_with_log(
                    "cage",
                    f"cage exited (status {status}) before creating a Wayland socket",
                    code=status,
                )
            # Directory diff is only the timeout fallback. Doing it on every
            # tick can latch onto a socket some other client just created.
            found = socket_from_ss(self.cage.pid)
            if found:
                return found
            time.sleep(0.1)
        found = new_wayland_socket(self.cfg.runtime_dir, before)
        if found:
            debug("cage", "ss did not see a socket; using new runtime entry %s", found)
            return found
        self._fail_with_log("cage", "timed out waiting for cage to create a Wayland socket")

    def _start_sunshine(self, socket_name: str, audio_env: dict[str, str]) -> TrayPlan:
        plan = tray_plan(self.cfg.tray)
        note("sun", "starting, tray %s", plan.note)
        if self._launch_sunshine(plan, socket_name, audio_env):
            return plan
        if plan.disabled:
            self._sunshine_failed()
        warn("sun", "sunshine exited immediately; retrying with system_tray=disabled")
        self._handles["sunshine"].write(b"\n--- retrying with system_tray=disabled ---\n")
        plan = TrayPlan(("system_tray=disabled",), "disabled (retry after immediate exit)", True)
        if not self._launch_sunshine(plan, socket_name, audio_env):
            self._sunshine_failed()
        return plan

    def _launch_sunshine(
        self, plan: TrayPlan, socket_name: str, audio_env: dict[str, str]
    ) -> bool:
        try:
            if self.cfg.instance:
                self.sun = start_sunshine_in_netns(
                    plan,
                    socket_name,
                    audio_env,
                    self._log_paths["sunshine"],
                    self.cfg.instance,
                    config_file=self._sunshine_config(),
                    extra_env=self._sunshine_env(),
                )
            else:
                self.sun = start_sunshine(
                    plan,
                    socket_name,
                    audio_env,
                    self._handles["sunshine"],
                    config_file=self._sunshine_config(),
                    extra_env=self._sunshine_env(),
                )
        except (OSError, ClipponError) as exc:
            raise ClipponError(f"cannot start sunshine: {exc}") from exc
        time.sleep(1.5)
        return self.sun.poll() is None

    def _sunshine_failed(self) -> NoReturn:
        self._fail_with_log("sunshine", "sunshine exited immediately")

    def _warn_if_sunshine_unhappy(self) -> None:
        path = self._log_paths["sunshine"]
        lines = fatal_lines(path)
        if not lines:
            return
        preview = "\n".join(lines[-5:])
        warn("sun", "sunshine reported a problem (see %s)\n%s", path, preview)

    def _announce(self, socket_name: str, plan: TrayPlan, audio_state: str) -> None:
        assert self.cage is not None and self.sun is not None
        mode = current_mode(socket_name)
        audio_line = {
            "private": (
                f"private instance, pre-session sink {self.cfg.sink} (host audio untouched)"
            ),
            "disabled": "isolation disabled, the app uses the host default sink",
            "failed": "private instance did not come up; the app uses the host default sink",
        }[audio_state]
        rows = [
            f"app      {command_text(self.cfg.command)}",
        ]
        if self.cfg.instance:
            rows.append(f"instance {self.cfg.instance}")
        rows.extend(
            [
                f"display  {socket_name}",
                f"cage     {self.cage.pid}",
                f"sunshine {self.sun.pid}",
            ]
        )
        if mode:
            rows.append(f"mode     {mode}")
        rows.append(f"audio    {audio_line}")
        rows.append(f"tray     {plan.note}")
        rows.append(f"logs     {self._log_paths['cage']}")
        rows.append(f"         {self._log_paths['sunshine']}")
        rows.append(f"stop     Ctrl-C  (or kill {self.cage.pid})")
        block("ready\n" + "\n".join(rows))

    def _wait_for_cage(self) -> int:
        assert self.cage is not None
        code = self.cage.wait()
        return shell_status(code)

    def _fail_with_log(self, kind: str, headline: str, code: int = 1) -> NoReturn:
        path = self._log_paths[kind]
        tail = _tail(path)
        detail = f"{headline}\nlog: {path}"
        if tail:
            detail = f"{detail}\n{tail}"
        raise ClipponError(detail, code=code)

    def _cleanup(self) -> None:
        if self._cleaning:
            return
        self._cleaning = True
        try:
            if self.cage is not None and self.cage.poll() is None:
                terminate(self.cage.pid)
            self._stop_sunshine()
            if self.audio is not None:
                self.audio.stop()
            if self.cage is not None:
                if self.cage.poll() is None and not wait_gone(self.cage.pid, 2.0):
                    kill(self.cage.pid)
                reap(self.cage, 1.0)
        finally:
            for handle in self._handles.values():
                handle.close()
            self._handles.clear()

    def _sunshine_config(self) -> Path | None:
        if not self.cfg.instance:
            return None
        # clippond writes sunshine.conf into the instance directory. Logs land
        # there too unless STREAM_LOG_DIR points somewhere else.
        candidates = (
            self.cfg.log_dir / "sunshine.conf",
            instance_dir(state_home(), self.cfg.instance) / "sunshine.conf",
        )
        for path in candidates:
            if path.is_file():
                return path
        raise ClipponError(
            f"missing {candidates[0]}; clippond writes it before starting the instance"
        )

    def _sunshine_env(self) -> dict[str, str]:
        if not self.cfg.instance:
            return {}
        return managed_env(self.cfg.addr_shim)

    def _stop_sunshine(self) -> None:
        if self.sun is None:
            return
        if self.sun.poll() is None:
            terminate(self.sun.pid)
            if not wait_gone(self.sun.pid, 5.0):
                kill(self.sun.pid)
        reap(self.sun, 1.0)

    def _force_kill(self) -> None:
        if self.cage is not None and self.cage.poll() is None:
            kill(self.cage.pid)
        if self.sun is not None and self.sun.poll() is None:
            kill(self.sun.pid)
        if self.audio is not None:
            for proc in list(self.audio.procs):
                if proc.poll() is None:
                    signal_group(proc.pid, signal.SIGKILL)


def command_text(command: tuple[str, ...]) -> str:
    import shlex

    return shlex.join(command)


def _symlink(target: Path, link: Path) -> None:
    temporary = link.with_name(link.name + ".tmp")
    temporary.unlink(missing_ok=True)
    temporary.symlink_to(target)
    temporary.replace(link)


def _tail(path: Path, lines: int = 20, width: int = 200) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    kept = text.splitlines()[-lines:]
    return "\n".join(line[:width] for line in kept)
