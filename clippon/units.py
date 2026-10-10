"""systemd unit text. install-daemon.py fills in the interpreter and the repo path."""

from __future__ import annotations


def system_unit(python: str, launcher: str) -> str:
    return f"""\
[Unit]
Description=Clippon network namespace helper
After=network.target

[Service]
Type=simple
ExecStart={python} {launcher}
Restart=on-failure
RestartSec=1
KillMode=process
RuntimeDirectory=clippon
StateDirectory=clippon

[Install]
WantedBy=multi-user.target
"""


def user_unit(python: str, launcher: str) -> str:
    return f"""\
[Unit]
Description=Clippon instance manager

[Service]
Type=simple
ExecStart={python} {launcher}
Restart=on-failure
RestartSec=1

[Install]
WantedBy=default.target
"""
