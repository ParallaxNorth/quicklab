"""Smoke tests; the optional hub test needs Unix sockets, not KVM or ISOs."""
import fcntl
import os
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import quicklab as lab


class LauncherTest(unittest.TestCase):
    def invoke(self, state, *args):
        return subprocess.run([sys.executable, "-B", str(lab.ROOT / "quicklab.py"), "--state", str(state), *args],
                              text=True, capture_output=True, timeout=20)

    def test_first_run_guide_and_status_do_not_initialize_state(self):
        with tempfile.TemporaryDirectory() as temp:
            state = Path(temp) / "new lab"
            intro = self.invoke(state)
            self.assertEqual(intro.returncode, 0, intro.stderr)
            self.assertIn("First time", intro.stdout)
            self.assertIn("build-controller --sudo-docker", intro.stdout)
            steps = self.invoke(state, "guide", "soc")
            self.assertIn("up soc --nat --autoinstall", steps.stdout)
            self.assertIn("'" + str(state) + "'", steps.stdout)
            report = self.invoke(state, "status", "app")
            self.assertEqual(report.returncode, 0, report.stderr)
            self.assertIn("not created yet", report.stdout)
            self.assertIn("10.77.0.30", report.stdout)
            self.assertNotIn("10.77.0.10", report.stdout)
            self.assertIn("Required media: 0/2", report.stdout)
            self.assertFalse(state.exists())

    def test_doctor_scopes_media_and_marks_new_guest_as_information(self):
        with tempfile.TemporaryDirectory() as temp:
            state = Path(temp) / "not made"
            result = self.invoke(state, "doctor", "win11")
            self.assertEqual(result.returncode, 1, result.stderr)
            self.assertIn("quicklab.iso: missing", result.stdout)
            self.assertIn("win11.iso: missing", result.stdout)
            self.assertIn("virtio-win.iso: missing", result.stdout)
            self.assertNotIn("ubuntu.iso", result.stdout)
            self.assertIn("[INFO] win11: stopped (no VM disk yet)", result.stdout)
            self.assertFalse(state.exists())

    def test_logs_and_status_work_while_state_is_locked(self):
        with tempfile.TemporaryDirectory() as temp:
            state = Path(temp)
            (state / "r").mkdir()
            vm = state / "v" / "dc"
            vm.mkdir(parents=True)
            (vm / "ql-dc.launch.log").write_text("old\nsetup detail\ninit failed\n")
            (vm / "ql-dc.log").write_text("QEMU detail\n")
            with (state / "r/lock").open("w") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                result = self.invoke(state, "logs", "dc", "--lines", "2")
                status = self.invoke(state, "status")
                diagnosis = self.invoke(state, "doctor", "dc")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(status.returncode, 0, status.stderr)
            self.assertIn("dc: stopped", diagnosis.stdout)
            self.assertIn("init failed", result.stdout)
            self.assertIn("QEMU detail", result.stdout)
            self.assertNotIn("old\n", result.stdout)

    def test_failed_launcher_saves_output_and_suggests_next_commands(self):
        def fail(command, *, stdout, **kwargs):
            stdout.write("Firmware configuration failed\n")
            raise subprocess.CalledProcessError(42, command)

        previous = Path.cwd()
        with tempfile.TemporaryDirectory() as temp:
            os.chdir(temp)
            try:
                Path("r").mkdir()
                with patch.object(lab, "running", return_value=False), \
                     patch.object(lab, "check_host"), patch.object(lab, "start_hub"), \
                     patch.object(lab, "media_state", return_value="ready"), \
                     patch.object(lab, "progress"), patch.object(lab.subprocess, "run", side_effect=fail):
                    with self.assertRaisesRegex(lab.LabError, r"status 42[\s\S]+logs soc[\s\S]+doctor soc"):
                        lab.up(["soc"], False)
                self.assertEqual(lab.launch_log("soc").read_text(), "Firmware configuration failed\n")
            finally:
                os.chdir(previous)

    def test_doctor_guest_agent_probe_defers_to_provisioning_lock(self):
        previous = Path.cwd()
        with tempfile.TemporaryDirectory() as temp:
            os.chdir(temp)
            try:
                Path("r").mkdir()
                with Path("r/lock").open("w") as lock, patch.object(lab, "probe_agent") as probe:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    self.assertIn("not checked", lab.agent_status("dc"))
                    probe.assert_not_called()
            finally:
                os.chdir(previous)

    def test_configs_disable_quickemu_network(self):
        for role, vm in lab.VMS.items():
            with self.subTest(role=role):
                private = lab.config(role, False)
                nat = lab.config(role, True)
                self.assertIn("network=none\n", private)
                self.assertIn(f"addr.path=r/{role}.net", private)
                self.assertNotIn("user,id=lab", private)
                self.assertIn("user,id=lab,ipv6=off", nat)
                self.assertNotIn("hostfwd=", nat)
                self.assertIn(vm.mac, private)
                self.assertIn(vm.mac, nat)

    def test_rootful_docker_controller_mounts_only_one_agent(self):
        endpoint = Path("/tmp/a private lab/v/dc/ql-dc-agent.sock")
        with patch.object(lab, "tool", side_effect=lambda name: name), \
             patch.object(lab.os, "geteuid", return_value=1000), \
             patch.object(lab.os, "getuid", return_value=1000), \
             patch.object(lab.os, "getgid", return_value=1200):
            command = lab.docker_provision_command("dc", endpoint, True, True)
            direct = lab.docker_provision_command("app", endpoint, False, False)
        self.assertEqual(command[:6], ["sudo", "--", "docker", "--host", "unix:///var/run/docker.sock", "run"])
        self.assertEqual(direct[:4], ["docker", "--host", "unix:///var/run/docker.sock", "run"])
        self.assertIn("--network=none", command)
        self.assertIn("--userns=host", command)
        self.assertIn("--read-only", command)
        self.assertIn("--cap-drop=ALL", command)
        self.assertIn("--rm", command)
        self.assertEqual(command[command.index("--user") + 1], "1000:1200")
        self.assertEqual(command.count("--mount"), 1)
        self.assertEqual(command[command.index("--mount") + 1],
                         f"type=bind,source={endpoint},target=/run/quicklab-agent.sock,readonly")
        self.assertIn("-it", command)
        self.assertNotIn("-it", direct)

    def test_autoinstall_seed_is_explicit_and_readonly(self):
        self.assertNotIn("app-seed.iso", lab.config("app", True))
        self.assertIn("media/app-seed.iso,format=raw,readonly=on", lab.config("app", True, True))
        with self.assertRaisesRegex(lab.LabError, "requires --nat"):
            lab.up(["app"], False, True)
        with self.assertRaisesRegex(lab.LabError, "only available for app and soc"):
            lab.up(["dc"], True, True)
        with patch.object(lab, "running", return_value=False), \
             patch.object(lab, "guest_disk", return_value=Path(__file__)):
            with self.assertRaisesRegex(lab.LabError, "existing disk"):
                lab.up(["app"], True, True)

    @unittest.skipUnless(os.environ.get("QUICKLAB_TEST_HUB") == "1" and shutil.which(lab.QEMU),
                         "set QUICKLAB_TEST_HUB=1 outside the editor sandbox")
    def test_qemu_hub_forwards_and_allows_reconnect(self):
        original = Path.cwd()
        with tempfile.TemporaryDirectory() as state:
            os.chdir(state)
            process = None
            try:
                Path("r").mkdir()
                process = lab.start_hub()
                self.assertTrue(lab.running(lab.HUB))
                frame = bytes.fromhex("ffffffffffff5254007700300800") + bytes(range(46))
                packet = struct.pack("!I", len(frame)) + frame
                with socket.socket(socket.AF_UNIX) as receiver:
                    receiver.connect("r/soc.net")
                    receiver.settimeout(2)
                    with receiver.makefile("rb") as reader:
                        # Connect the same peer twice, as happens after a guest restart.
                        for _ in range(2):
                            with socket.socket(socket.AF_UNIX) as sender:
                                sender.connect("r/app.net")
                                time.sleep(0.25)
                                sender.sendall(packet)
                                self.assertEqual(reader.read(len(packet)), packet)
                            time.sleep(0.25)
                lab.stop_hub()
                self.assertFalse(lab.running(lab.HUB))
            finally:
                if lab.running(lab.HUB):
                    lab.stop_hub()
                if process is not None:
                    process.wait(timeout=5)
                os.chdir(original)


if __name__ == "__main__":
    unittest.main()
