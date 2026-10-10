#!/usr/bin/env python3
"""Install the Arch user daemon and the root network helper.

The user service is clippond. The system service is clippon-net: it creates
one network namespace per instance and DNATs the host GameStream ports onto
the instance clipponctl has exposed. Starting clippon-net sets
net.ipv4.ip_forward to 1. The foreground `clippon` command is unchanged.

Nothing here is distro-agnostic on purpose. Other systems are rejected up front.
"""

from __future__ import annotations

import json
import os
import pwd
import shlex
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from clippon.config import SHIM_PATH, UDEV_RULE_PATH, VHID_SHIM_PATH
from clippon.log import error, note, setup_logging, warn
from clippon.units import system_unit, user_unit
from install import InstallError, arch_name, as_root, as_user, run, user_home

ADDR_SOURCE = ROOT / "assets" / "sunshine_addr.c"
ADDR_INSTALL = Path("/usr/local/lib/libsunshine_addr.so")
CURSOR_SOURCE = ROOT / "assets" / "wlr_cursor_map.c"
CURSOR_INSTALL = Path("/usr/local/lib/libwlr_cursor_map.so")
SEAT_SHIM_SOURCE = ROOT / "assets" / "seat_shim.c"
SEAT_SHIM_INSTALL = SHIM_PATH
SEAT_SHIM_SOURCE_INSTALL = Path("/usr/local/src/libseat-seat-cage/seat_shim.c")
VHID_SOURCE = ROOT / "assets" / "vhid_name.c"
VHID_INSTALL = VHID_SHIM_PATH
SEAT_TAG_SOURCE = ROOT / "assets" / "clippon-seat-tag"
SEAT_TAG_INSTALL = Path("/usr/local/libexec/clippon-seat-tag")
UDEV_SOURCE = ROOT / "assets" / "99-sunshine-cage.rules"
UDEV_INSTALL = UDEV_RULE_PATH
OWNER_PATH = Path("/var/lib/clippon/owner.json")
SYSTEM_UNIT_PATH = Path("/etc/systemd/system/clippon-net.service")
HELPER = ROOT / "bin" / "clippon-net"
DAEMON = ROOT / "bin" / "clippond"
CTL = ROOT / "bin" / "clipponctl"

HELP = """\
Usage: install-daemon.py [--dry-run]

Install clippon-net (system) and clippond (systemd --user), and the
getsockname shim Sunshine needs inside a network namespace.

  --dry-run   print the commands and stop before changing the system
  -h, --help  show this help and exit
"""


def main(argv: list[str] | None = None) -> int:
    setup_logging(False)
    args = list(sys.argv[1:] if argv is None else argv)
    dry_run = False
    for item in args:
        if item in ("-h", "--help"):
            print(HELP, end="")
            return 0
        if item == "--dry-run":
            dry_run = True
            continue
        error("inst", "unknown argument: %s", item)
        print(HELP, end="", file=sys.stderr)
        return 2
    try:
        install(dry_run=dry_run)
    except InstallError as exc:
        error("inst", "%s", exc)
        return 1
    return 0


def install(dry_run: bool) -> None:
    note("inst", "system  %s", arch_name())
    account = target_account()
    note("inst", "owner   %s (uid %s)", account.pw_name, account.pw_uid)
    ensure_tools(dry_run)
    install_seat_shim(dry_run)
    install_addr_shim(dry_run)
    install_cursor_shim(dry_run)
    install_vhid_shim(dry_run)
    install_seat_tag(dry_run)
    install_udev(dry_run)
    install_owner(account, dry_run)
    install_units(account, dry_run)
    fix_checkout_modes(dry_run)
    link_user_launchers(dry_run)
    if dry_run:
        note("inst", "dry-run, nothing was changed")
        return
    note("inst", "clippon-net and clippond are enabled")
    note("inst", "next: clipponctl start NAME -- <command>")


def target_account() -> pwd.struct_passwd:
    if os.geteuid() == 0:
        name = os.environ.get("SUDO_USER", "")
        if not name or name == "root":
            raise InstallError(
                "run install-daemon.py as your user, or via sudo from that user"
            )
        try:
            return pwd.getpwnam(name)
        except KeyError as exc:
            raise InstallError(f"SUDO_USER {name} is not in passwd") from exc
    return pwd.getpwuid(os.geteuid())


def ensure_tools(dry_run: bool) -> None:
    missing: list[str] = []
    if shutil.which("nft") is None:
        missing.append("nftables")
    if shutil.which("iptables") is None:
        missing.append("iptables")
    if shutil.which("gcc") is None:
        missing.append("gcc")
    # The gateway screenshots each instance for its box art.
    if shutil.which("grim") is None:
        missing.append("grim")
    if not _has_cryptography():
        missing.append("python-cryptography")
    if not _has_module("OpenSSL"):
        missing.append("python-pyopenssl")
    if not _has_module("PIL"):
        missing.append("python-pillow")
    for binary in ("ip", "nsenter", "setpriv", "sysctl", "openssl"):
        if shutil.which(binary) is None:
            raise InstallError(f"{binary} is not on PATH")
    if not missing:
        note("inst", "nft, iptables, gcc, grim, cryptography, pyOpenSSL and Pillow present")
        return
    note("inst", "pacman will install %s", ", ".join(missing))
    run(as_root(["pacman", "-S", "--needed", "--noconfirm", *missing]), dry_run=dry_run)


def _has_cryptography() -> bool:
    return _has_module("cryptography")


def _has_module(name: str) -> bool:
    """Whether the interpreter that will run the services can import *name*."""
    return (
        subprocess.run(
            [sys.executable, "-c", f"import {name}"],
            check=False,
            capture_output=True,
        ).returncode
        == 0
    )


def install_seat_shim(dry_run: bool) -> None:
    """Build the cage-side seat shim.

    install.py installs this shim as well. Repeating it here keeps the daemon
    install self-contained, which matters because the shim reads CLIPPON_SEAT
    and a stale copy silently puts every instance back on one seat.
    """
    if not SEAT_SHIM_SOURCE.is_file():
        raise InstallError(f"shim source is missing: {SEAT_SHIM_SOURCE}")
    note("inst", "building libseat seat shim")
    if dry_run:
        note("inst", "$ gcc -shared -fPIC -O2 -o libseat_seat_cage.so %s", SEAT_SHIM_SOURCE)
        note("inst", "would install %s", SEAT_SHIM_INSTALL)
        return
    with tempfile.TemporaryDirectory(prefix="clippon-seat-") as temporary:
        built = Path(temporary) / "libseat_seat_cage.so"
        run(
            ["gcc", "-shared", "-fPIC", "-O2", "-o", str(built), str(SEAT_SHIM_SOURCE)],
            dry_run=False,
        )
        _require_symbol(built, "libseat_seat_name")
        staged = SEAT_SHIM_INSTALL.with_name(SEAT_SHIM_INSTALL.name + ".new")
        run(as_root(["install", "-Dm644", str(built), str(staged)]), dry_run=False)
        run(as_root(["mv", "-f", str(staged), str(SEAT_SHIM_INSTALL)]), dry_run=False)
        run(
            as_root(
                ["install", "-Dm644", str(SEAT_SHIM_SOURCE), str(SEAT_SHIM_SOURCE_INSTALL)]
            ),
            dry_run=False,
        )
    note("inst", "shim  %s", SEAT_SHIM_INSTALL)


def install_addr_shim(dry_run: bool) -> None:
    if not ADDR_SOURCE.is_file():
        raise InstallError(f"shim source is missing: {ADDR_SOURCE}")
    note("inst", "building sunshine address shim")
    if dry_run:
        note("inst", "$ gcc -shared -fPIC -O2 -o libsunshine_addr.so %s", ADDR_SOURCE)
        note("inst", "would install %s", ADDR_INSTALL)
        return
    if shutil.which("gcc") is None:
        raise InstallError("gcc is not on PATH")
    with tempfile.TemporaryDirectory(prefix="clippon-addr-") as temporary:
        built = Path(temporary) / "libsunshine_addr.so"
        run(
            ["gcc", "-shared", "-fPIC", "-O2", "-o", str(built), str(ADDR_SOURCE)],
            dry_run=False,
        )
        _require_getsockname(built)
        staged = ADDR_INSTALL.with_name(ADDR_INSTALL.name + ".new")
        run(as_root(["install", "-Dm644", str(built), str(staged)]), dry_run=False)
        run(as_root(["mv", "-f", str(staged), str(ADDR_INSTALL)]), dry_run=False)
    note("inst", "shim  %s", ADDR_INSTALL)


def install_cursor_shim(dry_run: bool) -> None:
    if not CURSOR_SOURCE.is_file():
        raise InstallError(f"shim source is missing: {CURSOR_SOURCE}")
    note("inst", "building cursor mapping shim")
    if dry_run:
        note("inst", "$ gcc -shared -fPIC -O2 -o libwlr_cursor_map.so %s", CURSOR_SOURCE)
        note("inst", "would install %s", CURSOR_INSTALL)
        return
    with tempfile.TemporaryDirectory(prefix="clippon-cursor-") as temporary:
        built = Path(temporary) / "libwlr_cursor_map.so"
        run(
            ["gcc", "-shared", "-fPIC", "-O2", "-o", str(built), str(CURSOR_SOURCE)],
            dry_run=False,
        )
        _require_symbol(built, "wlr_cursor_attach_input_device")
        staged = CURSOR_INSTALL.with_name(CURSOR_INSTALL.name + ".new")
        run(as_root(["install", "-Dm644", str(built), str(staged)]), dry_run=False)
        run(as_root(["mv", "-f", str(staged), str(CURSOR_INSTALL)]), dry_run=False)
    note("inst", "shim  %s", CURSOR_INSTALL)


def install_vhid_shim(dry_run: bool) -> None:
    if not VHID_SOURCE.is_file():
        raise InstallError(f"shim source is missing: {VHID_SOURCE}")
    note("inst", "building virtual input name shim")
    if dry_run:
        note("inst", "$ gcc -shared -fPIC -O2 -o libclippon_vhid_name.so %s", VHID_SOURCE)
        note("inst", "would install %s", VHID_INSTALL)
        return
    with tempfile.TemporaryDirectory(prefix="clippon-vhid-") as temporary:
        built = Path(temporary) / "libclippon_vhid_name.so"
        run(
            ["gcc", "-shared", "-fPIC", "-O2", "-o", str(built), str(VHID_SOURCE)],
            dry_run=False,
        )
        _require_symbol(built, "libevdev_set_name")
        staged = VHID_INSTALL.with_name(VHID_INSTALL.name + ".new")
        run(as_root(["install", "-Dm644", str(built), str(staged)]), dry_run=False)
        run(as_root(["mv", "-f", str(staged), str(VHID_INSTALL)]), dry_run=False)
    note("inst", "shim  %s", VHID_INSTALL)


def install_seat_tag(dry_run: bool) -> None:
    if not SEAT_TAG_SOURCE.is_file():
        raise InstallError(f"seat tag helper is missing: {SEAT_TAG_SOURCE}")
    note("inst", "installing udev seat helper %s", SEAT_TAG_INSTALL)
    run(
        as_root(["install", "-Dm755", str(SEAT_TAG_SOURCE), str(SEAT_TAG_INSTALL)]),
        dry_run=dry_run,
    )


def install_udev(dry_run: bool) -> None:
    if not UDEV_SOURCE.is_file():
        raise InstallError(f"udev rule is missing: {UDEV_SOURCE}")
    note("inst", "installing udev rule %s", UDEV_INSTALL)
    run(as_root(["install", "-Dm644", str(UDEV_SOURCE), str(UDEV_INSTALL)]), dry_run=dry_run)
    run(as_root(["udevadm", "control", "--reload-rules"]), dry_run=dry_run)
    run(
        as_root(["udevadm", "trigger", "--subsystem-match=input", "--action=change"]),
        dry_run=dry_run,
    )


def _require_getsockname(path: Path) -> None:
    _require_symbol(path, "getsockname")


def _require_symbol(path: Path, symbol: str) -> None:
    if shutil.which("nm") is None:
        warn("inst", "nm is not on PATH; not checking %s for %s", path, symbol)
        return
    proc = subprocess.run(["nm", "-D", str(path)], check=False, capture_output=True, text=True)
    if proc.returncode != 0 or symbol not in proc.stdout:
        raise InstallError(f"{path} does not export {symbol}")


def install_owner(account: pwd.struct_passwd, dry_run: bool) -> None:
    payload = {
        "uid": account.pw_uid,
        "gid": account.pw_gid,
        "user": account.pw_name,
        "home": account.pw_dir,
    }
    note("inst", "owner file %s", OWNER_PATH)
    if dry_run:
        return
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False) as handle:
        json.dump(payload, handle)
        handle.write("\n")
        temporary = Path(handle.name)
    try:
        run(as_root(["install", "-Dm644", str(temporary), str(OWNER_PATH)]), dry_run=False)
    finally:
        temporary.unlink(missing_ok=True)


def install_units(account: pwd.struct_passwd, dry_run: bool) -> None:
    python = sys.executable
    system = system_unit(python, str(HELPER))
    user = user_unit(python, str(DAEMON))
    note("inst", "system unit %s", SYSTEM_UNIT_PATH)
    user_unit_path = user_home() / ".config" / "systemd" / "user" / "clippond.service"
    note("inst", "user unit   %s", user_unit_path)
    if dry_run:
        note("inst", "would systemctl enable --now clippon-net")
        note("inst", "would systemctl --user enable --now clippond")
        return
    # Restart only when the unit text changed. enable --now covers the first
    # install, and a restart drops the gateway for a moment. The KDE wallet
    # dialog is not caused by this restart; see docs/OPEN-ISSUES.md.
    system_changed = _read_text(SYSTEM_UNIT_PATH) != system
    user_changed = _read_text(user_unit_path) != user
    with tempfile.TemporaryDirectory(prefix="clippon-units-") as temporary:
        system_file = Path(temporary) / "clippon-net.service"
        user_file = Path(temporary) / "clippond.service"
        system_file.write_text(system, encoding="utf-8")
        user_file.write_text(user, encoding="utf-8")
        run(as_root(["install", "-Dm644", str(system_file), str(SYSTEM_UNIT_PATH)]), dry_run=False)
        script = (
            f"mkdir -p {shlex.quote(str(user_unit_path.parent))} && "
            f"cp {shlex.quote(str(user_file))} {shlex.quote(str(user_unit_path))}"
        )
        run(as_user(["sh", "-c", script]), dry_run=False)
    if system_changed:
        run(as_root(["systemctl", "daemon-reload"]), dry_run=False)
    run(as_root(["systemctl", "enable", "--now", "clippon-net"]), dry_run=False)
    if system_changed:
        note("inst", "clippon-net unit changed, restarting it")
        run(as_root(["systemctl", "restart", "clippon-net"]), dry_run=False)
    if user_changed:
        run(user_systemctl(account, ["daemon-reload"]), dry_run=False)
    run(user_systemctl(account, ["enable", "--now", "clippond"]), dry_run=False)
    if user_changed:
        note("inst", "clippond unit changed, restarting it")
        run(user_systemctl(account, ["restart", "clippond"]), dry_run=False)


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


def fix_checkout_modes(dry_run: bool) -> None:
    """Repair the execute bits a clone, tarball or rsync can drop."""
    for path in (HELPER, DAEMON, CTL, ROOT / "bin" / "clippon", ROOT / "clipponctl.py"):
        if dry_run:
            note("inst", "would ensure the execute bit on %s", path)
            continue
        _make_executable(path)


def _make_executable(path: Path) -> None:
    """A checkout or archive copy can lose the execute bit.

    which(1) then reports "not found" while the shell says "permission denied".
    """
    try:
        mode = path.stat().st_mode
    except OSError as exc:
        raise InstallError(f"cannot stat {path}: {exc}") from exc
    if mode & 0o111:
        return
    try:
        path.chmod(mode | 0o755)
    except OSError as exc:
        warn("inst", "cannot add the execute bit to %s: %s", path, exc)
        return
    note("inst", "added the execute bit to %s", path)


def user_systemctl(account: pwd.struct_passwd, args: list[str]) -> list[str]:
    """Talk to the user manager even when install-daemon.py was started with sudo."""
    command = [
        "env",
        f"XDG_RUNTIME_DIR=/run/user/{account.pw_uid}",
        "systemctl",
        "--user",
        *args,
    ]
    if os.geteuid() == 0:
        if shutil.which("runuser") is None:
            raise InstallError("runuser is required to reach the user systemd manager")
        return ["runuser", "-u", account.pw_name, "--", *command]
    return command


def link_user_launchers(dry_run: bool) -> None:
    for name, source, marker in (
        ("clipponctl", CTL, "clippon.ctl"),
        ("clippond", DAEMON, "clippon.daemon"),
    ):
        dest = user_home() / ".local" / "bin" / name
        note("inst", "linking %s -> %s", dest, source)
        if dry_run:
            continue
        _make_executable(source)
        if dest.exists() and not dest.is_symlink():
            try:
                text = dest.read_text(encoding="utf-8", errors="replace")
            except OSError as exc:
                raise InstallError(f"cannot read {dest}: {exc}") from exc
            if marker not in text:
                warn("inst", "%s exists and is not a clippon launcher; left it in place", dest)
                continue
        script = (
            f"mkdir -p {shlex.quote(str(dest.parent))} && "
            f"ln -sfn {shlex.quote(str(source))} {shlex.quote(str(dest))}"
        )
        run(as_user(["sh", "-c", script]), dry_run=False)


if __name__ == "__main__":
    raise SystemExit(main())
