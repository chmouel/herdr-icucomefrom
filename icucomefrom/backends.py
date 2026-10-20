"""Read process and unix socket data on Linux and macOS.

Backends report what they can't read as errors. Detection turns those errors
into "unknown" instead of guessing.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass

SSH_ANCESTORS = frozenset({"sshd", "sshd-session", "mosh-server"})


class DetectError(Exception):
    """Detection can't produce a trustworthy answer."""


@dataclass
class Proc:
    pid: int
    argv: list
    env: dict | None
    tty: str | None
    tty_atime: float | None


def remaining(deadline):
    left = deadline - time.monotonic()
    if left <= 0:
        raise DetectError("ran out of time")
    return left


def run(cmd, deadline):
    try:
        out = subprocess.run(
            cmd,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=remaining(deadline),
            check=False,
        )
    except FileNotFoundError as exc:
        raise DetectError(f"{cmd[0]} is not installed") from exc
    except subprocess.TimeoutExpired as exc:
        raise DetectError(f"{cmd[0]} timed out") from exc
    if out.returncode != 0:
        raise DetectError(f"{cmd[0]} exited with {out.returncode}")
    return out.stdout.decode(errors="replace")


def path_forms(path):
    """The path as given and with symlinks resolved, for matching socket names."""
    return {path, os.path.realpath(path)}


SS_ROW = re.compile(
    r"^\S+\s+(?P<state>\S+)\s+\d+\s+\d+\s+(?P<local>.*?)\s+(?P<inode>\d+)"
    r"\s+(?P<peer>.*?)\s+(?P<peer_inode>\d+)(?:\s+users:\((?P<users>.*)\))?\s*$"
)
SS_PID = re.compile(r"pid=(\d+)")


def parse_ss(text):
    """Return (state, local_path, inode, peer_inode, pids) for each `ss -xapH` row."""
    rows = []
    for line in text.splitlines():
        m = SS_ROW.match(line)
        if not m:
            continue
        pids = {int(p) for p in SS_PID.findall(m["users"] or "")}
        rows.append((m["state"], m["local"], int(m["inode"]), int(m["peer_inode"]), pids))
    return rows


def one_server(pids, sock_path):
    if not pids:
        raise DetectError(f"no herdr server listens on {sock_path}")
    if len(pids) > 1:
        raise DetectError(f"several processes listen on {sock_path}")
    return next(iter(pids))


def peers_from_ss(rows, sock_path):
    """Find the server pid and the client pids connected to sock_path."""
    names = path_forms(sock_path)
    servers = set()
    accepted = set()
    for state, local, _inode, peer, pids in rows:
        if local not in names:
            continue
        if state == "LISTEN":
            servers |= pids
        elif peer:
            accepted.add(peer)
    server = one_server(servers, sock_path)
    owners = {}
    for _state, _local, inode, _peer, pids in rows:
        if inode in accepted:
            owners.setdefault(inode, set()).update(pids)
    clients = set()
    for inode in accepted:
        pids = owners.get(inode, set()) - {server}
        if not pids:
            raise DetectError(f"no process owns client socket {inode}")
        clients |= pids
    return server, sorted(clients)


def ssh_env(env):
    return bool(env and (env.get("SSH_CONNECTION") or env.get("SSH_TTY")))


class LinuxBackend:
    supports_tty_atime = True

    def __init__(self, deadline, proc_root="/proc"):
        self.deadline = deadline
        self.root = proc_root

    def socket_peers(self, sock_path):
        return peers_from_ss(parse_ss(run(["ss", "-xapH"], self.deadline)), sock_path)

    def _stat(self, pid):
        """Return (comm, ppid) or None when the process is gone."""
        try:
            with open(f"{self.root}/{pid}/stat", encoding="utf-8", errors="replace") as f:
                data = f.read()
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise DetectError(f"can't read stat of {pid}: {exc.strerror}") from exc
        head, _, tail = data.rpartition(")")
        comm = head.partition("(")[2]
        # fields[0] is the state, so ppid is fields[1].
        return comm, int(tail.split()[1])

    def proc(self, pid):
        remaining(self.deadline)
        base = f"{self.root}/{pid}"
        try:
            with open(f"{base}/cmdline", "rb") as f:
                argv = [a.decode(errors="replace") for a in f.read().split(b"\0") if a]
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise DetectError(f"can't read argv of {pid}: {exc.strerror}") from exc
        env = None
        try:
            with open(f"{base}/environ", "rb") as f:
                env = dict(
                    kv.decode(errors="replace").partition("=")[::2]
                    for kv in f.read().split(b"\0")
                    if b"=" in kv
                )
        except FileNotFoundError:
            return None
        except OSError:
            pass
        tty = atime = None
        try:
            target = os.readlink(f"{base}/fd/0")
            if target.startswith(("/dev/pts/", "/dev/tty")):
                tty = target
                atime = os.stat(target).st_atime
        except OSError:
            pass
        return Proc(pid, argv, env, tty, atime)

    def ancestry(self, pid):
        """Return parent comms up to pid 1, or None when the chain breaks."""
        st = self._stat(pid)
        if st is None:
            return None
        names = []
        ppid = st[1]
        seen = set()
        while ppid > 1:
            remaining(self.deadline)
            if ppid in seen:
                return None
            seen.add(ppid)
            st = self._stat(ppid)
            if st is None:
                return None
            names.append(st[0])
            ppid = st[1]
        return names


def parse_procargs2(data):
    """Split a KERN_PROCARGS2 buffer into (argv, env)."""
    if len(data) < 4:
        raise DetectError("short KERN_PROCARGS2 buffer")
    argc = int.from_bytes(data[:4], sys.byteorder)
    rest = data[4:]
    end = rest.find(b"\0")
    if end < 0:
        raise DetectError("bad KERN_PROCARGS2 buffer")
    rest = rest[end:].lstrip(b"\0")
    parts = rest.split(b"\0")
    argv = [p.decode(errors="replace") for p in parts[:argc]]
    env = {}
    for p in parts[argc:]:
        if not p:
            break
        k, sep, v = p.decode(errors="replace").partition("=")
        if sep:
            env[k] = v
    return argv, env


def parse_lsof(text):
    """Return (pid, address, name) tuples from `lsof -U -F pdn` output."""
    rows = []
    pid = None
    addr = None
    for line in text.splitlines():
        if not line:
            continue
        tag, val = line[0], line[1:]
        if tag == "p":
            pid = int(val)
        elif tag == "f":
            addr = None
        elif tag == "d":
            addr = val
        elif tag == "n" and pid is not None:
            rows.append((pid, addr, val))
    return rows


def is_client_argv(argv):
    if not argv or os.path.basename(argv[0]) != "herdr":
        return False
    rest = argv[1:]
    return not rest or rest[0] in ("--session", "remote-client-bridge") or rest[:2] == [
        "session",
        "attach",
    ]


class MacBackend:
    supports_tty_atime = False

    def __init__(self, deadline):
        self.deadline = deadline
        self._table = None

    def _ps(self):
        if self._table is None:
            text = run(["ps", "-axo", "pid=,ppid=,tty=,comm="], self.deadline)
            table = {}
            for line in text.splitlines():
                parts = line.split(None, 3)
                if len(parts) < 4:
                    continue
                table[int(parts[0])] = (int(parts[1]), parts[2], os.path.basename(parts[3].strip()))
            self._table = table
        return self._table

    def _procargs(self, pid):
        import ctypes
        import ctypes.util

        libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
        mib = (ctypes.c_int * 3)(1, 49, pid)  # CTL_KERN, KERN_PROCARGS2
        size = ctypes.c_size_t(0)
        if libc.sysctl(mib, 3, None, ctypes.byref(size), None, 0) != 0:
            raise DetectError(f"can't read argv of {pid}")
        buf = ctypes.create_string_buffer(size.value)
        if libc.sysctl(mib, 3, buf, ctypes.byref(size), None, 0) != 0:
            raise DetectError(f"can't read argv of {pid}")
        return parse_procargs2(buf.raw[: size.value])

    def socket_peers(self, sock_path):
        rows = parse_lsof(run(["lsof", "-U", "-n", "-F", "pdn"], self.deadline))
        names = path_forms(sock_path)
        servers = {pid for pid, _a, name in rows if name in names}
        server = one_server(servers, sock_path)
        server_addrs = {a for pid, a, _n in rows if pid == server and a}
        candidates = {
            pid
            for pid, _a, name in rows
            if pid != server and name.startswith("->") and name[2:] in server_addrs
        }
        # lsof can't tell herdr.sock and herdr-client.sock connections apart
        # here, so keep only processes that look like attached clients.
        clients = []
        for pid in sorted(candidates):
            argv, env = self._procargs(pid)
            if is_client_argv(argv) and env.get("HERDR_ENV") != "1":
                clients.append(pid)
        return server, clients

    def proc(self, pid):
        remaining(self.deadline)
        row = self._ps().get(pid)
        if row is None:
            return None
        argv, env = self._procargs(pid)
        tty = None if row[1] in ("??", "-") else f"/dev/{row[1]}"
        atime = None
        if tty:
            try:
                atime = os.stat(tty).st_atime
            except OSError:
                pass
        return Proc(pid, argv, env, tty, atime)

    def ancestry(self, pid):
        table = self._ps()
        row = table.get(pid)
        if row is None:
            return None
        names = []
        ppid = row[0]
        seen = set()
        while ppid > 1:
            if ppid in seen:
                return None
            seen.add(ppid)
            row = table.get(ppid)
            if row is None:
                return None
            names.append(row[2])
            ppid = row[0]
        return names if ppid == 1 else None


def default_backend(deadline):
    if sys.platform == "darwin":
        return MacBackend(deadline)
    return LinuxBackend(deadline)
