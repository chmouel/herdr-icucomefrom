import io
import sys
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

from icucomefrom import cli
from icucomefrom.backends import DetectError, Proc, parse_procargs2, parse_ss, peers_from_ss, run
from icucomefrom.detect import Result, detect

SOCK = "/run/h/herdr.sock"
NOW = 1000.0
LOCAL_CHAIN = ["zsh", "kitty", "systemd"]
SSH_CHAIN = ["zsh", "sshd-session", "sshd-session", "sshd", "systemd"]


def tty(pid, atime, ssh_from=None):
    env = {"HOME": "/h"}
    if ssh_from:
        env["SSH_CONNECTION"] = f"{ssh_from} 50000 10.0.0.1 22"
    return Proc(pid, ["herdr"], env, f"/dev/pts/{pid}", atime), SSH_CHAIN if ssh_from else LOCAL_CHAIN


def bridge(pid):
    env = {"SSH_CONNECTION": "192.168.1.70 50000 10.0.0.1 22"}
    return Proc(pid, ["herdr", "remote-client-bridge"], env, None, None), SSH_CHAIN


class Fake:
    def __init__(self, clients, error=None):
        self.clients = clients
        self.error = error

    def socket_peers(self, path):
        assert path == "/run/h/herdr-client.sock"
        if self.error:
            raise DetectError(self.error)
        return 9, sorted(self.clients)

    def proc(self, pid):
        return self.clients[pid][0]

    def ancestry(self, pid):
        return self.clients[pid][1]


def guess(clients, env=None, **kw):
    return detect(SOCK, Fake(clients, **kw), NOW, env or {})


class GuessTest(unittest.TestCase):
    def test_every_client_agrees(self):
        self.assertEqual(guess({10: tty(10, 1.0)}).client, "local")
        self.assertEqual(guess({10: tty(10, 1.0, "10.1.1.1"), 11: bridge(11)}).client, "ssh")

    def test_ssh_env_without_ssh_parent_counts_as_ssh(self):
        proc, _ = tty(10, 1.0, "10.1.1.1")
        self.assertEqual(guess({10: (proc, LOCAL_CHAIN)}).client, "ssh")

    def test_recent_terminal_input_wins(self):
        clients = {10: tty(10, NOW - 3), 11: tty(11, NOW - 200, "10.1.1.1"), 12: bridge(12)}
        self.assertEqual(guess(clients).client, "local")
        clients = {10: tty(10, NOW - 50), 11: tty(11, NOW - 2, "10.1.1.1")}
        self.assertEqual(guess(clients).client, "ssh")

    def test_bridge_wins_without_recent_terminal_input(self):
        clients = {10: tty(10, NOW - 60), 12: bridge(12)}
        self.assertEqual(guess(clients).client, "ssh")

    def test_latest_terminal_input_wins_when_nothing_is_recent(self):
        clients = {10: tty(10, NOW - 60), 11: tty(11, NOW - 300, "10.1.1.1")}
        self.assertEqual(guess(clients).client, "local")

    def test_unreadable_client_is_skipped(self):
        proc, _ = tty(11, NOW - 1)
        proc.env = None
        clients = {10: tty(10, NOW - 60, "10.1.1.1"), 11: (proc, LOCAL_CHAIN)}
        self.assertEqual(guess(clients).client, "ssh")

    def test_vanished_client_is_skipped(self):
        self.assertEqual(guess({10: tty(10, 1.0, "10.1.1.1"), 11: (None, None)}).client, "ssh")

    def test_failure_falls_back_to_caller_env(self):
        res = guess({}, error="ss is not installed")
        self.assertEqual((res.client, res.error), ("local", "ss is not installed"))
        self.assertEqual(guess({}, env={"SSH_TTY": "/dev/pts/3"}).client, "ssh")

    def test_outside_herdr_uses_caller_env(self):
        res = detect("", None, NOW, {"SSH_CONNECTION": "1.2.3.4 1 5.6.7.8 22"})
        self.assertEqual((res.client, res.error), ("ssh", ""))


class CliTest(unittest.TestCase):
    def call(self, argv, res):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(cli, "run_detect", return_value=res), redirect_stdout(out), redirect_stderr(err):
            code = cli.main(argv)
        return code, out.getvalue(), err.getvalue()

    def test_get_is_the_default_and_reports_errors_on_stderr(self):
        res = Result("local", "from this shell's environment", error="ss is not installed")
        self.assertEqual(self.call([], res), (0, "local\n", "herdr-icucomefrom: ss is not installed\n"))

    def test_exit_codes(self):
        ssh = Result("ssh", "every client agrees")
        self.assertEqual(self.call(["is-ssh"], ssh)[0], 0)
        self.assertEqual(self.call(["is-local"], ssh)[0], 1)


SS_TEXT = """\
u_str ESTAB  0 0 /run/h dir/herdr-client.sock 2407823  * 2404981  users:(("herdr",pid=9,fd=22),("herdr",pid=9,fd=15))
u_str LISTEN 0 4096 /run/h dir/herdr-client.sock 49701 * 0 users:(("herdr",pid=9,fd=5))
u_str ESTAB  0 0 * 2404981 * 2407823 users:(("herdr",pid=606628,fd=7))
u_str ESTAB  0 0 /run/h dir/herdr.sock 555 * 556 users:(("herdr",pid=9,fd=30))
u_str ESTAB  0 0 * 556 * 555 users:(("python3",pid=777,fd=3))
"""


class ParserTest(unittest.TestCase):
    def test_ss_maps_clients_of_the_client_socket_only(self):
        self.assertEqual(peers_from_ss(parse_ss(SS_TEXT), "/run/h dir/herdr-client.sock"), (9, [606628]))

    def test_ss_without_listener_is_an_error(self):
        with self.assertRaisesRegex(DetectError, "no herdr server"):
            peers_from_ss(parse_ss(SS_TEXT), "/elsewhere/herdr-client.sock")

    @unittest.skipIf(sys.byteorder != "little", "buffer built for little endian")
    def test_procargs2_splits_argv_and_env(self):
        buf = (2).to_bytes(4, "little") + b"/usr/bin/herdr\0\0\0herdr\0--session\0A=1\0SSH_TTY=/dev/x\0\0junk"
        self.assertEqual(parse_procargs2(buf), (["herdr", "--session"], {"A": "1", "SSH_TTY": "/dev/x"}))

    def test_missing_command_names_it(self):
        with self.assertRaisesRegex(DetectError, "no-such-cmd-xyz is not installed"):
            run(["no-such-cmd-xyz"], time.monotonic() + 1)


if __name__ == "__main__":
    unittest.main()
