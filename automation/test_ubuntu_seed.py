import shutil
import stat
import subprocess
import tempfile
import unittest
import warnings
from pathlib import Path
from unittest.mock import patch

from automation import ubuntu_seed


class UbuntuSeedTest(unittest.TestCase):
    def test_rejects_invalid_role_and_missing_tty_before_prompt(self):
        with patch("builtins.open", side_effect=OSError), patch("getpass.getpass") as prompt:
            with self.assertRaises(ValueError):
                ubuntu_seed.make_seed("dc", Path("unused.iso"))
            with self.assertRaisesRegex(RuntimeError, "controlling terminal"):
                ubuntu_seed.make_seed("app", Path("unused.iso"))
            prompt.assert_not_called()

    def test_rejects_echo_fallback(self):
        def warn(*args):
            warnings.warn("echo is on", ubuntu_seed.getpass.GetPassWarning)
            self.fail("getpass fallback would read an echoed password")

        with patch("getpass.getpass", side_effect=warn), patch("subprocess.run") as run:
            with self.assertRaisesRegex(RuntimeError, "echo disabled"):
                ubuntu_seed._password_hash("openssl", "ql-app")
            run.assert_not_called()

    def test_rejects_password_mismatch(self):
        with patch("getpass.getpass", side_effect=["first", "second"]) as prompt, \
                patch("subprocess.run") as run:
            with self.assertRaisesRegex(ValueError, "do not match"):
                ubuntu_seed._password_hash("openssl", "ql-soc")
            self.assertEqual(prompt.call_count, 2)
            run.assert_not_called()

    def test_failed_iso_build_cleans_stage_and_preserves_destination(self):
        with tempfile.TemporaryDirectory() as parent:
            dest = Path(parent) / "seed.iso"
            dest.write_bytes(b"old")

            def run_command(command, **kwargs):
                if command[0] == "openssl":
                    self.assertEqual(command, ["openssl", "passwd", "-6", "-stdin"])
                    self.assertEqual(kwargs["input"], b"test password\n")
                    return subprocess.CompletedProcess(command, 0, b"$6$salt$" + b"x" * 86 + b"\n")
                self.assertEqual(command[:3], ["xorriso", "-as", "mkisofs"])
                stage = kwargs["cwd"]
                self.assertEqual(stage.parent, dest.parent)
                self.assertEqual(stat.S_IMODE(stage.stat().st_mode), 0o700)
                for path in stage.iterdir():
                    self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
                    self.assertNotIn(b"test password", path.read_bytes())
                raise subprocess.CalledProcessError(1, command)

            with patch.object(ubuntu_seed, "_require_terminal"), \
                    patch("shutil.which", side_effect=lambda name: name), \
                    patch("getpass.getpass", return_value="test password"), \
                    patch("subprocess.run", side_effect=run_command):
                with self.assertRaises(subprocess.CalledProcessError):
                    ubuntu_seed.make_seed("app", dest)
            self.assertEqual(dest.read_bytes(), b"old")
            self.assertEqual(list(dest.parent.iterdir()), [dest])

    @unittest.skipUnless(shutil.which("openssl") and shutil.which("isoinfo") and
                         (shutil.which("xorriso") or shutil.which("genisoimage")),
                         "requires openssl, isoinfo and an ISO builder")
    def test_real_iso_contents(self):
        hashes = []
        with tempfile.TemporaryDirectory() as parent, patch.object(ubuntu_seed, "_require_terminal"), \
                patch("getpass.getpass", return_value="test password"):
            dest = Path(parent) / "seed.iso"
            for role in ("app", "soc"):
                with self.subTest(role=role):
                    ubuntu_seed.make_seed(role, dest)
                    self.assertEqual(stat.S_IMODE(dest.stat().st_mode), 0o600)
                    info = ["isoinfo", "-i", str(dest)]
                    details = subprocess.check_output(info + ["-d"], text=True)
                    self.assertIn("Volume id: CIDATA\n", details)
                    tree = subprocess.check_output(info + ["-R", "-f"], text=True)
                    self.assertEqual(set(tree.splitlines()), {"/meta-data", "/user-data"})
                    meta = subprocess.check_output(info + ["-R", "-x", "/meta-data"], text=True)
                    self.assertIn(f"instance-id: ql-{role}-", meta)
                    self.assertIn(f"local-hostname: ql-{role}\n", meta)
                    user = subprocess.check_output(info + ["-R", "-x", "/user-data"], text=True)
                    self.assertTrue(user.startswith("#cloud-config\nautoinstall:\n  version: 1\n"))
                    for setting in (f"hostname: ql-{role}", "username: labadmin", "name: direct",
                                    "install-server: false", "- qemu-guest-agent", "shutdown: poweroff"):
                        self.assertIn(setting + "\n", user)
                    self.assertNotIn("test password", user)
                    hashes.append(user.split("password: '", 1)[1].split("'", 1)[0])
        self.assertNotEqual(*hashes)
