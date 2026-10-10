"""Root helper: one network namespace per clippon instance, and DNAT onto one of them.

Cage and Sunshine stay in the host user namespace so they keep the video,
render and input groups. This process creates the netns, moves a veth into it,
then nsenter + setpriv drops to the owning user before exec.
"""

from __future__ import annotations

import json
import os
import pwd
import shutil
import signal
import socket
import stat
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from clippon.instance import seat_name, validate_name
from clippon.log import error, note, warn, setup_logging
from clippon.proc import all_pids, process_env_value
from clippon.ports import (
    BRIDGE,
    BRIDGE_IP,
    SUBNET,
    allocate_host,
    docker_user_checks,
    in_subnet,
    lan_from_ip_json,
    netns_name,
    nft_table,
    veth_names,
)
from clippon.protocol import HELPER_SOCK, recv_obj, send_obj

STATE_PATH = Path("/var/lib/clippon/state.json")
OWNER_PATH = Path("/var/lib/clippon/owner.json")
SUNSHINE_BIN = Path("/usr/bin/sunshine")
SUNSHINE_SPAWN = Path("/usr/local/lib/clippon/sunshine")


def sunshine_spawn_path() -> str:
    """The Sunshine binary to exec: a copy without file capabilities.

    The packaged binary carries cap_sys_admin=p. The kernel marks every exec of
    a capable binary secure, and the loader then ignores LD_PRELOAD, so the
    device-name shim never loads. A headless cage session keeps capture on
    wlr-screencopy instead of KMS, so that capability buys nothing here and the
    copy is what lets each instance's devices carry their instance tag.

    Falls back to PATH lookup when the packaged binary is somewhere else.
    """
    if not SUNSHINE_BIN.is_file():
        return "sunshine"
    try:
        source = SUNSHINE_BIN.stat()
        stale = True
        if SUNSHINE_SPAWN.is_file():
            copy = SUNSHINE_SPAWN.stat()
            stale = (copy.st_size, copy.st_mtime_ns) != (source.st_size, source.st_mtime_ns)
        if stale:
            SUNSHINE_SPAWN.parent.mkdir(parents=True, exist_ok=True)
            staged = SUNSHINE_SPAWN.with_name(SUNSHINE_SPAWN.name + ".new")
            # copyfile, not copy2: copystat would carry security.capability over.
            shutil.copyfile(SUNSHINE_BIN, staged)
            os.chmod(staged, 0o755)
            # Keep the source's timestamp so the next spawn sees it as current,
            # and rename into place so a running instance keeps its own inode.
            os.utime(staged, ns=(source.st_atime_ns, source.st_mtime_ns))
            os.replace(staged, SUNSHINE_SPAWN)
            note("net", "refreshed %s from %s", SUNSHINE_SPAWN, SUNSHINE_BIN)
    except OSError as exc:
        warn(
            "net",
            "cannot prepare %s: %s; this instance's devices stay untagged and its "
            "cage will not see them",
            SUNSHINE_SPAWN,
            exc,
        )
        return "sunshine"
    return str(SUNSHINE_SPAWN)


class HelperError(Exception):
    pass


@dataclass
class InstanceRec:
    ip: str
    pid: int | None = None      # clippon, on the host network
    sun_pid: int | None = None  # sunshine, inside the instance netns
    exited: bool = False

    def to_json(self) -> dict[str, object]:
        return {"ip": self.ip, "pid": self.pid, "sun_pid": self.sun_pid, "exited": self.exited}


@dataclass
class NetState:
    exposed: str | None = None
    instances: dict[str, InstanceRec] = field(default_factory=dict)

    def to_json(self) -> dict[str, object]:
        return {
            "exposed": self.exposed,
            "instances": {name: rec.to_json() for name, rec in self.instances.items()},
        }

    @classmethod
    def from_json(cls, data: object) -> NetState:
        state = cls()
        if not isinstance(data, dict):
            return state
        exposed = data.get("exposed")
        state.exposed = exposed if isinstance(exposed, str) else None
        raw = data.get("instances")
        if not isinstance(raw, dict):
            return state
        for name, rec in raw.items():
            if not isinstance(name, str) or not isinstance(rec, dict):
                continue
            try:
                validate_name(name)
            except ValueError:
                continue
            ip = rec.get("ip")
            if not isinstance(ip, str) or not in_subnet(ip):
                continue
            pid = rec.get("pid")
            sun = rec.get("sun_pid")
            state.instances[name] = InstanceRec(
                ip=ip,
                pid=pid if isinstance(pid, int) and pid > 0 else None,
                sun_pid=sun if isinstance(sun, int) and sun > 0 else None,
                exited=bool(rec.get("exited")),
            )
        if state.exposed not in state.instances:
            state.exposed = None
        return state


@dataclass
class Owner:
    uid: int
    gid: int
    user: str
    home: str


def host_command(user: str, gid: int, argv: list[str]) -> list[str]:
    """Drop privileges, staying in whatever namespace the helper is in."""
    return [
        "setpriv",
        f"--reuid={user}",
        f"--regid={gid}",
        "--init-groups",
        "--inh-caps=-all",
        "--",
        *argv,
    ]


def spawn_command(ns_path: str, user: str, gid: int, argv: list[str]) -> list[str]:
    """nsenter -F keeps this pid through setpriv's exec, so the recorded pid is clippon.

    cage must NOT run inside the instance's network namespace: udev delivers
    device hotplug over a netlink group that only exists in init_net, so a
    netns-bound compositor never sees the virtual input Sunshine creates after
    it started. Only Sunshine goes in here.
    """
    return ["nsenter", "-F", f"--net={ns_path}", "--", *host_command(user, gid, argv)]


def orphan_namespaces(existing: list[str], live: set[str]) -> list[str]:
    """Namespace names that have no live instance behind them.

    Only names this helper would have created ("clippon-<valid name>") count,
    and *live* holds the namespaces of the instances still recorded.
    """
    found = []
    for ns in existing:
        if ns in live or not ns.startswith("clippon-"):
            continue
        try:
            validate_name(ns[len("clippon-") :])
        except ValueError:
            continue
        found.append(ns)
    return found


def kill_instance_leftovers(instance: str) -> int:
    """Stop what an instance left running when its clippon went away.

    cage, the streamed app and anything they spawned carry CLIPPON_SEAT; a
    plain command like `sleep infinity` outlives its cage, so the seat is what
    catches them. The private audio daemons carry the instance's
    PIPEWIRE_RUNTIME_DIR instead. Both markers are matched, the same way
    leftover_instances detects them. Returns how many were signalled.
    """
    seat = seat_name(instance)
    audio_suffix = f"/cage-audio-{instance}"
    stopped = 0
    for pid in all_pids():
        on_seat = process_env_value(pid, "CLIPPON_SEAT") == seat
        runtime = process_env_value(pid, "PIPEWIRE_RUNTIME_DIR")
        on_audio = runtime is not None and runtime.endswith(audio_suffix)
        if on_seat or on_audio:
            stopped += _kill_group(pid)
    if stopped:
        note("net", "stopped %s leftover process(es) of %s", stopped, instance)
    return stopped


def leftover_instances(live: set[str]) -> dict[str, int]:
    """Processes that belong to an instance no live record covers.

    Two markers name an instance: CLIPPON_SEAT for cage, the streamed app and
    anything they spawn, and the private PIPEWIRE_RUNTIME_DIR folder for the
    audio daemons. *live* holds the instances still recorded.
    """
    found: dict[str, int] = {}
    for pid in all_pids():
        names: set[str] = set()
        seat = process_env_value(pid, "CLIPPON_SEAT")
        if seat is not None and seat.startswith("seat-cage-"):
            names.add(seat[len("seat-cage-") :])
        runtime = process_env_value(pid, "PIPEWIRE_RUNTIME_DIR")
        if runtime is not None:
            folder = runtime.rsplit("/", 1)[-1]
            if folder.startswith("cage-audio-"):
                names.add(folder[len("cage-audio-") :])
        for name in names:
            if not name or name in live:
                continue
            found[name] = found.get(name, 0) + 1
    return found


def _kill_group(pid: int) -> int:
    """SIGKILL a process and the session it leads. 1 when something was sent."""
    try:
        os.killpg(pid, signal.SIGKILL)
        return 1
    except OSError:
        pass
    try:
        os.kill(pid, signal.SIGKILL)
        return 1
    except OSError:
        return 0


def _netns_names() -> list[str]:
    """Every network namespace on the host, by name."""
    proc = subprocess.run(
        ["ip", "-j", "netns", "list"],
        check=False,
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0 or not proc.stdout.strip():
        return []
    try:
        entries = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return []
    if not isinstance(entries, list):
        return []
    return [
        item["name"]
        for item in entries
        if isinstance(item, dict) and isinstance(item.get("name"), str)
    ]


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _signal_and_wait(pid: int) -> None:
    import signal as _signal
    import time as _time

    try:
        os.kill(pid, _signal.SIGTERM)
    except OSError:
        return
    deadline = _time.monotonic() + 5.0
    while _time.monotonic() < deadline:
        if not alive(pid):
            return
        _time.sleep(0.1)
    # It ignored SIGTERM. Take its whole session down: a clippon that had to be
    # force-killed still holds cage and the streamed app in its process group.
    try:
        os.killpg(pid, _signal.SIGKILL)
    except OSError:
        try:
            os.kill(pid, _signal.SIGKILL)
        except OSError:
            pass


class Helper:
    def __init__(self, owner: Owner, state: NetState) -> None:
        self.owner = owner
        self.state = state
        self._children: dict[str, list[subprocess.Popen[bytes]]] = {}
        self._stop = False

    @classmethod
    def from_disk(cls) -> Helper:
        if os.geteuid() != 0:
            raise HelperError("clippon-net must run as root")
        if not OWNER_PATH.is_file():
            raise HelperError(f"missing {OWNER_PATH}; run install-daemon.py")
        try:
            raw = json.loads(OWNER_PATH.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise HelperError(f"cannot read {OWNER_PATH}: {exc}") from exc
        owner = _owner(raw)
        state = NetState()
        if STATE_PATH.is_file():
            try:
                state = NetState.from_json(json.loads(STATE_PATH.read_text(encoding="utf-8")))
            except (OSError, json.JSONDecodeError) as exc:
                note("net", "ignoring unreadable state %s: %s", STATE_PATH, exc)
        return cls(owner, state)

    def _reclaim(self) -> None:
        """Drop what earlier runs left behind.

        Nothing needs reclaiming while a run is healthy: instances outlive
        clippond on purpose, and their namespaces belong to them. Leftovers come
        from a namespace whose instance record never reached the state file, or
        from an instance whose clippon process died while the helper was away.
        """
        self._refresh()
        for name, rec in list(self.state.instances.items()):
            if rec.exited:
                note("net", "reclaiming %s: its clippon process is gone", name)
                self.destroy(name)
        live = {netns_name(name) for name in self.state.instances}
        for ns in orphan_namespaces(_netns_names(), live):
            name = ns[len("clippon-") :]
            note("net", "removing leftover namespace %s", ns)
            self._destroy_links(name)
            kill_instance_leftovers(name)
        # Standalone single-instance runs use the shared seat and the unnamed
        # audio folder, so they never show up here.
        for name, count in sorted(leftover_instances(set(self.state.instances)).items()):
            note("net", "removing %s leftover process(es) of %s", count, name)
            kill_instance_leftovers(name)
        self._prepare_host()

    def serve(self) -> None:
        self._reclaim()
        path = HELPER_SOCK
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            path.unlink()
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(str(path))
        os.chown(path, 0, self.owner.gid)
        os.chmod(path, 0o660)
        server.listen(8)
        server.settimeout(2.0)
        signal.signal(signal.SIGTERM, self._on_signal)
        signal.signal(signal.SIGINT, self._on_signal)
        note("net", "listening on %s for uid %s", path, self.owner.uid)
        try:
            while not self._stop:
                self._refresh()
                try:
                    conn, _ = server.accept()
                except TimeoutError:
                    continue
                except OSError:
                    if self._stop:
                        break
                    raise
                self._client(conn)
        finally:
            server.close()
            path.unlink(missing_ok=True)

    def _on_signal(self, _signum: int, _frame: object) -> None:
        self._stop = True

    def _client(self, conn: socket.socket) -> None:
        with conn:
            uid = _peer_uid(conn)
            if uid != self.owner.uid:
                send_obj(conn, {"ok": False, "error": "permission denied"})
                return
            try:
                request = recv_obj(conn)
                response = self.handle(request)
            except (HelperError, ValueError, OSError) as exc:
                response = {"ok": False, "error": str(exc)}
            try:
                send_obj(conn, response)
            except OSError as exc:
                error("net", "reply failed: %s", exc)

    def handle(self, request: dict[str, object]) -> dict[str, object]:
        op = request.get("op")
        if op == "status":
            return self.status()
        if op == "create":
            ip = self.create(_name(request))
            return {"ok": True, "ip": ip}
        if op == "destroy":
            self.destroy(_name(request))
            return {"ok": True}
        if op == "expose":
            raw = request.get("name")
            if raw is None or raw == "":
                self.expose(None)
            elif isinstance(raw, str):
                self.expose(raw)
            else:
                raise HelperError("expose name must be a string or null")
            return self.status()
        if op == "spawn":
            pid = self.spawn(request, in_netns=False)
            return {"ok": True, "pid": pid}
        if op == "spawn-netns":
            pid = self.spawn(request, in_netns=True)
            return {"ok": True, "pid": pid}
        raise HelperError(f"unknown op {op!r}")

    def status(self) -> dict[str, object]:
        self._refresh()
        return {
            "ok": True,
            "exposed": self.state.exposed,
            "lan": self._lan(),
            "instances": self.state.to_json()["instances"],
        }

    def create(self, name: str) -> str:
        validate_name(name)
        self._prepare_host()
        existing = self.state.instances.get(name)
        if existing is not None and _links_up(name):
            return existing.ip
        if existing is not None:
            note("net", "recreating missing links for %s", name)
            self._destroy_links(name)
            ip = existing.ip
        else:
            used = {int(rec.ip.rsplit(".", 1)[1]) for rec in self.state.instances.values()}
            try:
                octet = allocate_host(used)
            except ValueError as exc:
                raise HelperError(str(exc)) from exc
            ip = f"10.200.0.{octet}"
        ns = netns_name(name)
        host_if, ns_if = veth_names(name)
        try:
            _run(["ip", "netns", "add", ns])
            _run(["ip", "link", "add", host_if, "type", "veth", "peer", "name", ns_if])
            _run(["ip", "link", "set", host_if, "master", BRIDGE])
            _run(["ip", "link", "set", host_if, "up"])
            _run(["ip", "link", "set", ns_if, "netns", ns])
            _run(["ip", "netns", "exec", ns, "ip", "link", "set", "lo", "up"])
            _run(["ip", "netns", "exec", ns, "ip", "link", "set", ns_if, "up"])
            _run(["ip", "netns", "exec", ns, "ip", "addr", "add", f"{ip}/24", "dev", ns_if])
            _run(["ip", "netns", "exec", ns, "ip", "route", "add", "default", "via", BRIDGE_IP])
        except HelperError:
            self._destroy_links(name)
            raise
        self.state.instances[name] = InstanceRec(ip=ip)
        self._save()
        note("net", "created %s at %s", name, ip)
        return ip

    def destroy(self, name: str) -> None:
        validate_name(name)
        if self.state.exposed == name:
            self.expose(None)
        rec = self.state.instances.get(name)
        for proc in self._children.pop(name, []):
            if proc.poll() is None:
                proc.terminate()
        if rec is not None and rec.sun_pid is not None and alive(rec.sun_pid):
            # Sunshine outlives a stopped clippon unless it is signalled too.
            _signal_and_wait(rec.sun_pid)
        # cage, the streamed app and the private audio daemons are not in this
        # process tree, so nothing else would stop them.
        kill_instance_leftovers(name)
        self._destroy_links(name)
        self.state.instances.pop(name, None)
        self._save()
        note("net", "destroyed %s", name)

    def expose(self, name: str | None) -> None:
        if name is not None:
            validate_name(name)
            if name not in self.state.instances:
                raise HelperError(f"unknown instance {name}")
            ip: str | None = self.state.instances[name].ip
        else:
            ip = None
        self.state.exposed = name
        self._apply_nft(ip)
        self._save()
        note("net", "exposed %s", name or "nothing")

    def spawn(self, request: dict[str, object], in_netns: bool) -> int:
        name = _name(request)
        if name not in self.state.instances:
            raise HelperError(f"unknown instance {name}")
        rec = self.state.instances[name]
        self._refresh()
        tracked = rec.sun_pid if in_netns else rec.pid
        if tracked is not None and alive(tracked):
            raise HelperError(f"{name} is already running as pid {tracked}")
        argv = _str_list(request.get("argv"), "argv")
        if not argv:
            raise HelperError("argv is empty")
        env = _str_dict(request.get("env"))
        stdout = request.get("stdout")
        if not isinstance(stdout, str) or not stdout.startswith("/"):
            raise HelperError("stdout must be an absolute path")
        ns_path = Path("/run/netns") / netns_name(name)
        if in_netns and not ns_path.exists():
            raise HelperError(f"missing netns {ns_path}")
        log = _open_user_log(stdout, self.owner.uid)
        if in_netns:
            if Path(argv[0]).name == "sunshine":
                argv = [sunshine_spawn_path(), *argv[1:]]
            cmd = spawn_command(str(ns_path), self.owner.user, self.owner.gid, argv)
        else:
            # A previous run of this instance may have left its app or audio
            # daemons behind; this one is about to take the same seat.
            kill_instance_leftovers(name)
            cmd = host_command(self.owner.user, self.owner.gid, argv)
        try:
            proc = subprocess.Popen(
                cmd,
                env=env,
                cwd=self.owner.home,
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        finally:
            log.close()
        if in_netns:
            rec.sun_pid = proc.pid
        else:
            rec.pid = proc.pid
            rec.exited = False
        self._children.setdefault(name, []).append(proc)
        self._save()
        note("net", "spawned %s pid %s%s", name, proc.pid, " in its netns" if in_netns else "")
        return proc.pid

    def _prepare_host(self) -> None:
        _enable_forwarding()
        self._ensure_bridge()
        ip = None
        if self.state.exposed and self.state.exposed in self.state.instances:
            ip = self.state.instances[self.state.exposed].ip
        self._apply_nft(ip)

    def _ensure_bridge(self) -> None:
        if _run_ok(["ip", "link", "show", BRIDGE]) != 0:
            _run(["ip", "link", "add", BRIDGE, "type", "bridge"])
        shown = subprocess.run(
            ["ip", "-4", "addr", "show", "dev", BRIDGE],
            check=False,
            capture_output=True,
            text=True,
        )
        if f"{BRIDGE_IP}/24" not in shown.stdout:
            _run(["ip", "addr", "add", f"{BRIDGE_IP}/24", "dev", BRIDGE])
        _run(["ip", "link", "set", BRIDGE, "up"])

    def _destroy_links(self, name: str) -> None:
        ns = netns_name(name)
        host_if, _ns_if = veth_names(name)
        subprocess.run(["ip", "link", "del", host_if], check=False, capture_output=True)
        subprocess.run(["ip", "netns", "del", ns], check=False, capture_output=True)

    def _apply_nft(self, ip: str | None) -> None:
        text = nft_table(ip)
        subprocess.run(
            ["nft", "delete", "table", "ip", "clippon"],
            check=False,
            capture_output=True,
            text=True,
        )
        proc = subprocess.run(
            ["nft", "-f", "-"],
            input=text,
            text=True,
            capture_output=True,
            check=False,
        )
        if proc.returncode != 0:
            raise HelperError(f"nft failed: {(proc.stderr or proc.stdout).strip()}")
        self._docker_user()

    def _docker_user(self) -> None:
        probe = subprocess.run(
            ["iptables", "-S", "DOCKER-USER"],
            check=False,
            capture_output=True,
            text=True,
        )
        if probe.returncode != 0:
            return
        for check, insert in docker_user_checks():
            present = subprocess.run(check, check=False, capture_output=True)
            if present.returncode != 0:
                _run(insert)

    def _refresh(self) -> None:
        changed = False
        for name, procs in list(self._children.items()):
            rec = self.state.instances.get(name)
            for proc in list(procs):
                if proc.poll() is None:
                    continue
                procs.remove(proc)
                if rec is None:
                    continue
                # The clippon pid defines the instance's lifetime; the sunshine
                # pid inside the netns does not.
                if rec.pid == proc.pid:
                    rec.pid = None
                    rec.exited = True
                    changed = True
                elif rec.sun_pid == proc.pid:
                    rec.sun_pid = None
                    changed = True
            if not procs:
                self._children.pop(name, None)
        for name, rec in self.state.instances.items():
            if rec.pid is not None and not alive(rec.pid):
                rec.pid = None
                rec.exited = True
                changed = True
            if rec.sun_pid is not None and not alive(rec.sun_pid):
                rec.sun_pid = None
                changed = True
        if changed:
            self._save()

    def _lan(self) -> list[str]:
        proc = subprocess.run(
            ["ip", "-4", "-json", "addr", "show"],
            check=False,
            capture_output=True,
            text=True,
        )
        if proc.returncode != 0 or not proc.stdout.strip():
            return []
        try:
            payload = json.loads(proc.stdout)
        except json.JSONDecodeError:
            return []
        if not isinstance(payload, list):
            return []
        return lan_from_ip_json(payload, SUBNET)

    def _save(self) -> None:
        STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        temporary = STATE_PATH.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(self.state.to_json(), indent=2) + "\n", encoding="utf-8")
        os.chmod(temporary, 0o644)
        temporary.replace(STATE_PATH)


def _owner(raw: object) -> Owner:
    if not isinstance(raw, dict):
        raise HelperError(f"{OWNER_PATH} is not an object")
    uid = raw.get("uid")
    gid = raw.get("gid")
    user = raw.get("user")
    home = raw.get("home")
    if not isinstance(uid, int) or not isinstance(gid, int):
        raise HelperError(f"{OWNER_PATH} needs numeric uid and gid")
    if not isinstance(user, str) or not isinstance(home, str):
        raise HelperError(f"{OWNER_PATH} needs user and home")
    try:
        record = pwd.getpwuid(uid)
    except KeyError as exc:
        raise HelperError(f"uid {uid} is not in passwd") from exc
    if record.pw_name != user:
        raise HelperError(f"owner user {user} does not match uid {uid}")
    return Owner(uid=uid, gid=gid, user=user, home=home)


def _peer_uid(conn: socket.socket) -> int:
    import struct

    creds = conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
    _pid, uid, _gid = struct.unpack("3i", creds)
    return uid


def _name(request: dict[str, object]) -> str:
    name = request.get("name")
    if not isinstance(name, str):
        raise HelperError("name must be a string")
    validate_name(name)
    return name


def _str_list(value: object, label: str) -> list[str]:
    if not isinstance(value, list) or not value or not all(isinstance(item, str) and item for item in value):
        raise HelperError(f"{label} must be a non-empty list of strings")
    if any("\x00" in item for item in value):
        raise HelperError(f"{label} contains NUL")
    return list(value)


def _str_dict(value: object) -> dict[str, str]:
    if not isinstance(value, dict):
        raise HelperError("env must be an object")
    env: dict[str, str] = {}
    for key, item in value.items():
        if not isinstance(key, str) or not isinstance(item, str):
            raise HelperError("env values must be strings")
        if "\x00" in key or "\x00" in item:
            raise HelperError("env contains NUL")
        env[key] = item
    return env


def _open_user_log(path: str, uid: int) -> object:
    try:
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_NOFOLLOW)
    except OSError as exc:
        raise HelperError(f"cannot open {path}: {exc}") from exc
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode) or info.st_uid != uid:
        os.close(fd)
        raise HelperError(f"{path} must be a regular file owned by uid {uid}")
    return os.fdopen(fd, "ab", buffering=0)


def _enable_forwarding() -> None:
    path = Path("/proc/sys/net/ipv4/ip_forward")
    try:
        current = path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise HelperError(f"cannot read {path}: {exc}") from exc
    if current == "1":
        return
    _run(["sysctl", "-w", "net.ipv4.ip_forward=1"])
    note("net", "enabled net.ipv4.ip_forward")


def _run(cmd: list[str]) -> None:
    note("net", "$ %s", _join(cmd))
    proc = subprocess.run(cmd, check=False, capture_output=True, text=True)
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()
        raise HelperError(f"{_join(cmd)} failed ({proc.returncode}): {detail}")


def _links_up(name: str) -> bool:
    ns = netns_name(name)
    host_if, _ns_if = veth_names(name)
    listed = subprocess.run(
        ["ip", "netns", "list"],
        check=False,
        capture_output=True,
        text=True,
    )
    names = [line.split()[0] for line in listed.stdout.splitlines() if line.split()]
    if ns not in names:
        return False
    return _run_ok(["ip", "link", "show", host_if]) == 0


def _run_ok(cmd: list[str]) -> int:
    return subprocess.run(cmd, check=False, capture_output=True).returncode


def _join(cmd: list[str]) -> str:
    import shlex

    return shlex.join(cmd)


def main() -> int:
    setup_logging(False)
    try:
        Helper.from_disk().serve()
    except HelperError as exc:
        error("net", "%s", exc)
        return 1
    return 0
