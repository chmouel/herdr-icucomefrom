"""Command line entry point."""

from __future__ import annotations

import argparse
import os
import sys
import time

from .backends import default_backend
from .detect import MOBILE_COLS, detect, device, is_mobile

DEADLINE = 3.0


def run_detect():
    """Return (backend, result); the backend keeps the deadline for later lookups."""
    backend = default_backend(time.monotonic() + DEADLINE)
    return backend, detect(os.environ.get("HERDR_SOCKET_PATH", ""), backend, time.time(), os.environ)


def warn(res):
    if res.error:
        print(f"herdr-icucomefrom: {res.error}", file=sys.stderr)


def cmd_get(_args):
    _backend, res = run_detect()
    warn(res)
    print(res.client)
    return 0


def cmd_is(want):
    def run(_args):
        return 0 if run_detect()[1].client == want else 1

    return run


def cmd_device(args):
    backend, res = run_detect()
    warn(res)
    print(device(res, backend, args.cols))
    return 0


def cmd_is_mobile(args):
    return 0 if is_mobile(run_detect()[1].picked, args.cols) else 1


def cmd_status(args):
    now = time.time()
    backend, res = run_detect()
    print(f"client: {res.client} ({res.reason})")
    print(f"device: {device(res, backend, args.cols)}")
    if res.error:
        print(f"error: {res.error}")
    sock = os.environ.get("HERDR_SOCKET_PATH", "")
    if not sock:
        return 0
    print(f"socket: {sock}")
    for c in res.clients:
        where = c.tty or "no tty"
        if c.cols:
            where += f" {c.cols}x{c.rows}"
        if c.tty_atime is not None:
            where += f", input {int(now - c.tty_atime)}s ago"
        if c.ssh_connection:
            where += f", from {c.ssh_connection.split(' ')[0]}"
            key = backend.ssh_key(c.pid, c.ssh_connection)
            if key:
                where += f", key {key}"
        print(f"  pid {c.pid}: {c.transport}, {c.kind}, {where} ({c.evidence})")
    return 0


def parser():
    p = argparse.ArgumentParser(
        prog="herdr-icucomefrom",
        description="Guess whether the herdr client you're using is attached over ssh or locally.",
    )
    sub = p.add_subparsers(dest="cmd", metavar="command")
    width = argparse.ArgumentParser(add_help=False)
    width.add_argument(
        "--cols",
        type=int,
        default=MOBILE_COLS,
        help=f"ssh clients narrower than this count as mobile (default {MOBILE_COLS})",
    )
    sub.add_parser("get", help="print ssh or local (the default)").set_defaults(func=cmd_get)
    sub.add_parser("is-ssh", help="exit 0 on ssh, 1 otherwise").set_defaults(func=cmd_is("ssh"))
    sub.add_parser("is-local", help="exit 0 on local, 1 otherwise").set_defaults(func=cmd_is("local"))
    sub.add_parser(
        "device", parents=[width], help="print the ssh key name of the client, or mobile, ssh or local"
    ).set_defaults(func=cmd_device)
    sub.add_parser("is-mobile", parents=[width], help="exit 0 on a narrow ssh client, 1 otherwise").set_defaults(
        func=cmd_is_mobile
    )
    sub.add_parser(
        "status", parents=[width], help="show every client and why the guess came out that way"
    ).set_defaults(func=cmd_status)
    return p


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        return getattr(args, "func", cmd_get)(args)
    except KeyboardInterrupt:
        return 130
    except BrokenPipeError:
        return 0
