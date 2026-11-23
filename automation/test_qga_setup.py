import base64
from collections import deque
from contextlib import contextmanager, redirect_stderr, redirect_stdout
import io
import json
import socket
import time
import unittest
from unittest import mock

from automation import qga_setup as qga


def wire(reply):
    return json.dumps(reply).encode("ascii") + b"\r\n"


def exited(code=0, *, stdout="", stderr=""):
    return {
        "exited": True, "exitcode": code,
        "out-data": base64.b64encode(stdout.encode("utf-8")).decode("ascii"),
        "err-data": base64.b64encode(stderr.encode("utf-8")).decode("ascii"),
    }


def process(status, pid=27):
    return [("guest-exec", {"pid": pid}), ("guest-exec-status", status)]


class GuestAgentTests(unittest.TestCase):
    @contextmanager
    def server(self, *sessions):
        """Script the QGA byte stream, including fragmentation and EOF."""
        requests = []
        pending = deque(None if session is None else deque(session) for session in sessions)
        opened = []
        socket_type = socket.socket

        def connect(family, kind):
            self.assertEqual((family, kind), (socket.AF_UNIX, socket.SOCK_STREAM))
            script = pending.popleft()
            if script is not None:
                opened.append(script)
            conn = mock.MagicMock(spec=socket_type)
            replies = deque()

            def send(data):
                self.assertFalse(replies, "Client started a request before reading its reply")
                if data.startswith(b"\xff"):
                    request = json.loads(data[1:])
                    self.assertEqual(request["execute"], "guest-sync-delimited")
                    if script is not None:
                        good = wire({"return": request["arguments"]["id"]})
                        replies.extend((b'{"return":{}}\n\xff{"return":-1}\npartial\xff' + good[:4], good[4:]))
                    return
                command, result = script.popleft()
                request = json.loads(data)
                self.assertEqual(request["execute"], command)
                requests.append((command, request.get("arguments")))
                if result is not None:
                    replies.append(wire({"return": result}))

            conn.sendall.side_effect = send
            conn.recv.side_effect = lambda size: replies.popleft() if replies else b""
            conn.connect.side_effect = lambda path: self.assertEqual(path, "/run/quicklab-agent.sock")
            return conn

        with mock.patch.object(qga.socket, "socket", side_effect=connect):
            yield requests
        self.assertFalse(pending, "Mock connections were left unused")
        self.assertFalse(any(opened), "Mock requests were left unused")

    def cli(self, argv, stdin=""):
        out, err = io.StringIO(), io.StringIO()
        with (mock.patch.object(qga.sys, "stdin", io.StringIO(stdin)),
              redirect_stdout(out), redirect_stderr(err)):
            code = qga.main(argv)
        return code, out.getvalue(), err.getvalue()

    def test_ping_exchanges_no_setup_commands_or_stdin(self):
        with self.server([("guest-ping", {})]) as requests:
            with mock.patch.object(qga.sys, "stdin", None), redirect_stdout(io.StringIO()):
                self.assertEqual(qga.main(["dc", "--ping"]), 0)
        self.assertEqual(requests, [("guest-ping", None)])

    def test_unreachable_ping_explains_onboarding(self):
        conn = mock.MagicMock()
        conn.connect.side_effect = FileNotFoundError
        with mock.patch.object(qga.socket, "socket", return_value=conn):
            code, _, err = self.cli(["app", "--ping"])
        self.assertEqual(code, 1)
        self.assertIn("manual guest onboarding", err)
        self.assertIn("virtio", err)

    def test_linux_mount_and_poll_captured_output(self):
        with self.server([
            ("guest-exec", {"pid": 27}),
            ("guest-exec-status", {"exited": False}),
            ("guest-exec-status", exited(3, stdout="héllo\n", stderr="script error\n")),
        ]) as requests:
            code, out, err = self.cli(["soc"], "{}")
        self.assertEqual(code, 1)
        self.assertEqual(out, "héllo\n")
        self.assertIn("script error\n", err)
        spec = requests[0][1]
        self.assertEqual(spec["path"], "/bin/bash")
        self.assertTrue(spec["capture-output"])
        self.assertNotIn("env", spec)
        self.assertIn("-o ro,", spec["arg"][1])
        self.assertIn("-L QUICKLAB", spec["arg"][1])
        self.assertIn('guest/setup-linux.sh" "$1"', spec["arg"][1])
        self.assertEqual(spec["arg"][-1], "soc")
        self.assertEqual(requests[1:], [("guest-exec-status", {"pid": 27})] * 2)

    def test_windows_retries_only_after_changed_boot_id(self):
        old_boot = process(exited(stdout="123\r\nC:\\Windows\r\n"))
        new_boot = process(exited(stdout="124\r\nC:\\Windows\r\n"))
        setup_returns_reboot = process(exited(3010, stdout="do not print me"))
        shutdown_drops_connection = [("guest-exec", {"pid": 28}), ("guest-exec-status", None)]
        with (mock.patch.object(qga, "REBOOT_POLL_INTERVAL", 0),
              self.server(old_boot + setup_returns_reboot + shutdown_drops_connection,
                          old_boot, None, new_boot, process(exited())) as requests):
            code, out, err = self.cli(["win11"], json.dumps({
                "join_user": "LAB\\user", "join_password": "fake=pass with spaces!",
            }))
        self.assertEqual(code, 0, err)
        self.assertNotIn("do not print me", out + err)
        specs = [args for command, args in requests if command == "guest-exec"]
        self.assertEqual(len(specs), 6)
        for i in (0, 3, 4):
            self.assertIn("Win32_OperatingSystem", self.decoded_script(specs[i]))
            self.assertIn("LastBootUpTime", self.decoded_script(specs[i]))
            self.assertTrue(specs[i]["capture-output"])
        self.assertTrue(specs[2]["path"].endswith("\\shutdown.exe"))
        self.assertEqual(specs[2]["arg"], ["/r", "/t", "5"])
        for i in (1, 5):
            spec = specs[i]
            self.assertFalse(spec["capture-output"])
            self.assertIn("-ExecutionPolicy", spec["arg"])
            script = self.decoded_script(spec)
            self.assertIn("Get-Volume", script)
            self.assertIn("-Role Employee -Automation", script)
            self.assertEqual([e for e in spec["env"] if e.startswith("QUICKLAB_")], [
                "QUICKLAB_JOIN_USER=LAB\\user", "QUICKLAB_JOIN_PASSWORD=fake=pass with spaces!",
            ])
            self.assertNotIn("fake=pass", str(spec["arg"]) + script)

    def test_windows_failure_does_not_expose_guest_output(self):
        with self.server(process(exited(stdout="123\r\nC:\\Windows\r\n")) +
                         process(exited(1, stdout="fake-dsrm", stderr="fake-dsrm"))) as requests:
            code, out, err = self.cli(["dc"], '{"dsrm_password":"fake-dsrm"}')
        self.assertEqual(code, 1)
        self.assertIn("Windows automation failed", err)
        self.assertNotIn("fake-dsrm", out + err)
        spec = requests[2][1]
        self.assertIn("-Role DC -Automation", self.decoded_script(spec))
        self.assertIn("QUICKLAB_DSRM_PASSWORD=fake-dsrm", spec["env"])
        self.assertNotIn("fake-dsrm", str(spec["arg"]))

    def test_invalid_stdin_is_not_echoed(self):
        code, out, err = self.cli(["dc"], '{"dsrm_password": "fake-secret", "extra": true}')
        self.assertEqual(code, 1)
        self.assertIn("dsrm_password", err)
        self.assertNotIn("fake-secret", out + err)

    def test_incomplete_process_times_out(self):
        agent = qga.GuestAgent(None)
        with mock.patch.object(agent, "call", side_effect=[{"pid": 1}, {"exited": False}]):
            with self.assertRaisesRegex(qga.AgentError, "may still be running"):
                agent.run("/bin/bash", [], time.monotonic() + 0.03)

    def test_reboot_limit_never_requests_an_extra_restart(self):
        boot = process(exited(stdout="123\r\nC:\\Windows\r\n"))
        with (mock.patch.object(qga, "MAX_REBOOTS", 0),
              self.server(boot + process(exited(3010))) as requests):
            code, _, err = self.cli(["dc"], '{"dsrm_password":"fake-dsrm"}')
        self.assertEqual(code, 1)
        self.assertIn("reboot limit", err)
        self.assertEqual(len(requests), 4)

    @staticmethod
    def decoded_script(spec):
        return base64.b64decode(spec["arg"][-1]).decode("utf-16-le")


if __name__ == "__main__":
    unittest.main()
