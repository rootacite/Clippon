#!/usr/bin/env python3
"""Install Clippon's Arch Linux dependencies, seat shim, and udev rule.

Official packages come from pacman. Sunshine is not packaged there; when
`sunshine` is missing from PATH this installs the AUR package sunshine-beta-bin
with yay or paru, as the user who invoked sudo (AUR helpers refuse to run as
root). The seat shim and the udev rule are what keep Moonlight's virtual input
on seat-cage instead of the host desktop.

Nothing here is distro-agnostic on purpose. Other systems are rejected up front.
"""

from __future__ import annotations

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

from clippon.config import SHIM_PATH, UDEV_RULE_PATH
from clippon.log import error, note, setup_logging, warn

# Runtime tools the session actually execs, plus the toolchain that builds the
# seat shim and (via base-devel) an AUR package.
OFFICIAL_PACKAGES = (
    "cage",
    "wlr-randr",
    "pipewire",
    "pipewire-pulse",
    "wireplumber",
    "seatd",
    "iproute2",
    "dbus",
    "gcc",
    "procps-ng",
    "python",
    "git",
    "base-devel",
)
AUR_PACKAGE = "sunshine-beta-bin"
SHIM_SOURCE = ROOT / "assets" / "seat_shim.c"
UDEV_SOURCE = ROOT / "assets" / "99-sunshine-cage.rules"
SHIM_INSTALL = SHIM_PATH
UDEV_INSTALL = UDEV_RULE_PATH
SOURCE_INSTALL = Path("/usr/local/src/libseat-seat-cage/seat_shim.c")
LAUNCHER = ROOT / "bin" / "clippon"

HELP = """\
Usage: install.py [--dry-run]

Install the Arch Linux packages, libseat seat shim, and udev rule Clippon needs.

  --dry-run   print the commands and stop before changing the system
  -h, --help  show this help and exit
"""


class InstallError(Exception):
    pass


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
    pretty = arch_name()
    note("inst", "system  %s", pretty)
    missing = [name for name in OFFICIAL_PACKAGES if not package_installed(name)]
    if missing:
        note("inst", "pacman will install %s", ", ".join(missing))
    else:
        note("inst", "official packages already installed")
    run(as_root(["pacman", "-S", "--needed", "--noconfirm", *OFFICIAL_PACKAGES]), dry_run=dry_run)

    if shutil.which("sunshine"):
        note("inst", "sunshine already on PATH: %s", shutil.which("sunshine"))
    else:
        helper = aur_helper()
        note("inst", "installing %s with %s", AUR_PACKAGE, helper)
        run(
            as_user([helper, "-S", "--needed", "--noconfirm", AUR_PACKAGE]),
            dry_run=dry_run,
        )

    install_shim(dry_run)
    install_udev(dry_run)
    install_launcher(dry_run)
    if dry_run:
        note("inst", "dry-run, nothing was changed")
        return
    summary()


def arch_name() -> str:
    path = Path("/etc/os-release")
    if not path.is_file():
        raise InstallError("only Arch Linux is supported (/etc/os-release is missing)")
    data: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        data[key] = value.strip().strip('"')
    ident = data.get("ID", "")
    like = data.get("ID_LIKE", "").split()
    if ident != "arch" and "arch" not in like:
        raise InstallError(
            f"only Arch Linux is supported (detected ID={ident or 'unknown'})"
        )
    return data.get("PRETTY_NAME") or ident


def package_installed(name: str) -> bool:
    proc = subprocess.run(
        ["pacman", "-Q", name],
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return proc.returncode == 0


def aur_helper() -> str:
    for name in ("yay", "paru"):
        if shutil.which(name):
            return name
    raise InstallError(
        f"sunshine is not installed and neither yay nor paru is on PATH; "
        f"install {AUR_PACKAGE} or an AUR helper, then re-run"
    )


def as_root(cmd: list[str]) -> list[str]:
    if os.geteuid() == 0:
        return cmd
    if shutil.which("sudo") is None:
        raise InstallError("sudo is required to install system packages and files")
    return ["sudo", *cmd]


def as_user(cmd: list[str]) -> list[str]:
    """AUR helpers must run as a normal user. They call sudo for pacman themselves."""
    if os.geteuid() != 0:
        return cmd
    user = os.environ.get("SUDO_USER", "")
    if not user or user == "root":
        raise InstallError(
            "AUR installs have to run as a normal user; re-run install.py without root"
        )
    if shutil.which("runuser") is None:
        raise InstallError("runuser is required to drop root before the AUR helper")
    return ["runuser", "-u", user, "--", *cmd]


def run(cmd: list[str], *, dry_run: bool) -> None:
    note("inst", "$ %s", shlex.join(cmd))
    if dry_run:
        return
    proc = subprocess.run(cmd, check=False)
    if proc.returncode != 0:
        raise InstallError(f"command failed ({proc.returncode}): {shlex.join(cmd)}")


def install_shim(dry_run: bool) -> None:
    if not SHIM_SOURCE.is_file():
        raise InstallError(f"shim source is missing: {SHIM_SOURCE}")
    note("inst", "building libseat seat shim")
    if dry_run:
        note("inst", "$ gcc -shared -fPIC -O2 -o libseat_seat_cage.so %s", SHIM_SOURCE)
        note("inst", "would install %s", SHIM_INSTALL)
        note("inst", "would install %s", SOURCE_INSTALL)
        return
    if shutil.which("gcc") is None:
        raise InstallError("gcc is not on PATH; the official package install did not provide it")
    with tempfile.TemporaryDirectory(prefix="clippon-shim-") as temporary:
        built = Path(temporary) / "libseat_seat_cage.so"
        run(
            [
                "gcc",
                "-shared",
                "-fPIC",
                "-O2",
                "-o",
                str(built),
                str(SHIM_SOURCE),
            ],
            dry_run=False,
        )
        _require_symbol(built)
        # Rename into place so a running cage keeps the inode it already mapped.
        staged = SHIM_INSTALL.with_name(SHIM_INSTALL.name + ".new")
        run(as_root(["install", "-Dm644", str(built), str(staged)]), dry_run=False)
        run(as_root(["mv", "-f", str(staged), str(SHIM_INSTALL)]), dry_run=False)
        run(as_root(["install", "-Dm644", str(SHIM_SOURCE), str(SOURCE_INSTALL)]), dry_run=False)
    note("inst", "shim  %s", SHIM_INSTALL)


def _require_symbol(path: Path) -> None:
    if shutil.which("nm") is None:
        warn("inst", "nm is not on PATH; not checking the shim's exported symbol")
        return
    proc = subprocess.run(
        ["nm", "-D", str(path)],
        check=False,
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0 or "libseat_seat_name" not in proc.stdout:
        raise InstallError(f"{path} does not export libseat_seat_name")


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


def user_home() -> Path:
    """Home of the invoking user, including when install.py was started with sudo."""
    if os.geteuid() == 0:
        user = os.environ.get("SUDO_USER", "")
        if user and user != "root":
            return Path(pwd.getpwnam(user).pw_dir)
    return Path.home()


def install_launcher(dry_run: bool) -> None:
    dest = user_home() / ".local" / "bin" / "clippon"
    note("inst", "linking %s -> %s", dest, LAUNCHER)
    if dry_run:
        return
    if dest.exists() and not dest.is_symlink():
        try:
            text = dest.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            raise InstallError(f"cannot read {dest}: {exc}") from exc
        if "clippon.cli" not in text:
            warn("inst", "%s exists and is not a clippon launcher; left it in place", dest)
            return
    # mkdir and ln run as the user so a sudo invocation cannot leave
    # ~/.local/bin owned by root.
    script = (
        f"mkdir -p {shlex.quote(str(dest.parent))} && "
        f"ln -sfn {shlex.quote(str(LAUNCHER))} {shlex.quote(str(dest))}"
    )
    run(as_user(["sh", "-c", script]), dry_run=False)


def summary() -> None:
    for name in ("cage", "sunshine", "wlr-randr", "pipewire", "pipewire-pulse", "wireplumber"):
        found = shutil.which(name)
        if found:
            note("inst", "%-16s %s", name, found)
        else:
            warn("inst", "%-16s missing", name)
    note("inst", "%-16s %s", "shim", SHIM_INSTALL if SHIM_INSTALL.is_file() else "missing")
    note("inst", "%-16s %s", "udev", UDEV_INSTALL if UDEV_INSTALL.is_file() else "missing")
    launcher = user_home() / ".local" / "bin" / "clippon"
    note("inst", "%-16s %s", "command", launcher if launcher.exists() else "not linked")
    if os.geteuid() != 0 and not _dir_on_path(launcher.parent):
        warn("inst", "%s is not on PATH; open a new login shell or add it", launcher.parent)


def _dir_on_path(directory: Path) -> bool:
    return any(Path(entry) == directory for entry in os.environ.get("PATH", "").split(os.pathsep))


if __name__ == "__main__":
    raise SystemExit(main())
