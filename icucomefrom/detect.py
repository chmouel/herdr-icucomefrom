"""Guess whether the herdr client you're using is attached over ssh."""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from .backends import SSH_ANCESTORS, DetectError, ssh_env, tty_size

# Linux moves tty atime at most every 8 seconds, so a terminal you typed in a
# moment ago can look up to 8 seconds old.
RECENT = 10
MOBILE_COLS = 100


@dataclass
class Client:
    pid: int
    transport: str
    kind: str
    tty: str | None = None
    tty_atime: float | None = None
    ssh_connection: str = ""
    evidence: str = ""
    cols: int | None = None
    rows: int | None = None


@dataclass
class Result:
    client: str
    reason: str
    clients: list = field(default_factory=list)
    error: str = ""
    picked: Client | None = None


def client_socket(api_socket):
    return os.path.join(os.path.dirname(api_socket), "herdr-client.sock")


def classify(backend, pid):
    """Return a Client, or None when the process went away."""
    proc = backend.proc(pid)
    if proc is None:
        return None
    ancestors = backend.ancestry(pid)
    kind = "remote-bridge" if "remote-client-bridge" in proc.argv else "tty"
    conn = (proc.env or {}).get("SSH_CONNECTION", "")
    hit = sorted(SSH_ANCESTORS.intersection(ancestors or ()))
    if hit:
        transport, evidence = "ssh", f"ancestor {hit[0]}"
    elif ssh_env(proc.env):
        transport, evidence = "ssh", "ssh variables in env"
    elif ancestors is None:
        transport, evidence = "unknown", "parent chain unreadable"
    elif proc.env is None:
        transport, evidence = "unknown", "env unreadable"
    else:
        transport, evidence = "local", "no ssh ancestor or variables"
    client = Client(pid, transport, kind, proc.tty, proc.tty_atime, conn, evidence)
    size = tty_size(proc.tty)
    if size:
        client.cols, client.rows = size
    return client


def guess(clients, now):
    """Return (transport, reason, client it relied on), or None when no client can be read."""
    known = [c for c in clients if c.transport != "unknown"]
    if not known:
        return None
    ttys = sorted(
        (c for c in known if c.kind == "tty" and c.tty_atime is not None),
        key=lambda c: c.tty_atime,
        reverse=True,
    )
    bridges = [c for c in known if c.kind == "remote-bridge"]
    if ttys and now - ttys[0].tty_atime <= RECENT:
        picked = ttys[0]
        transport, reason = picked.transport, f"pid {picked.pid} saw input recently"
    elif bridges:
        picked = bridges[0]
        transport, reason = "ssh", "no recent terminal input and a remote bridge is attached"
    elif ttys:
        picked = ttys[0]
        transport, reason = picked.transport, f"pid {picked.pid} saw input last"
    else:
        picked, transport, reason = known[0], "ssh", "no terminal input to compare"
    transports = {c.transport for c in known}
    if len(transports) == 1:
        return transports.pop(), "every client agrees", picked
    return transport, reason, picked


def is_mobile(client, cols=MOBILE_COLS):
    return client is not None and client.transport == "ssh" and client.cols is not None and client.cols < cols


def device(res, backend, cols=MOBILE_COLS):
    """Name the device behind the picked client: its ssh key, mobile, or the transport."""
    c = res.picked
    if c is not None and c.transport == "ssh" and c.ssh_connection:
        name = backend.ssh_key(c.pid, c.ssh_connection)
        if name:
            return name
    if is_mobile(c, cols):
        return "mobile"
    return res.client


def caller_env(env, error):
    return Result("ssh" if ssh_env(env) else "local", "from this shell's environment", error=error)


def detect(api_socket, backend, now, env):
    if not api_socket:
        return caller_env(env, "")
    try:
        _server, pids = backend.socket_peers(client_socket(api_socket))
        clients = [c for c in (classify(backend, pid) for pid in pids) if c is not None]
    except (DetectError, OSError, ValueError) as exc:
        return caller_env(env, str(exc))
    picked = guess(clients, now)
    if picked is None:
        res = caller_env(env, "no readable herdr client attached")
    else:
        transport, reason, client = picked
        res = Result(transport, reason, picked=client)
    res.clients = clients
    return res
