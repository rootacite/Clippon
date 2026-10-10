"""Entry point. `python -m clippon` and the `clippon` console script both land here."""

from __future__ import annotations

import sys

from clippon.config import HELP, ClipponError, parse_args
from clippon.log import error, setup_logging
from clippon.session import Session


def main(argv: list[str] | None = None) -> int:
    if argv is None:
        argv = sys.argv[1:]
    setup_logging(False)
    try:
        cfg = parse_args(argv)
    except ClipponError as exc:
        error("clip", "%s", exc)
        if exc.code == 2:
            print(HELP, end="", file=sys.stderr)
        return exc.code
    if cfg.verbose:
        setup_logging(True)
    try:
        return Session(cfg).run()
    except KeyboardInterrupt:
        return 130
