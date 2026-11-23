#!/usr/bin/env python3
"""Run QuickLab's fixed guest setup commands over the local QEMU Guest Agent."""

import argparse
import base64

from contextlib import contextmanager
from dataclasses import dataclass
import json
import os
import re
import secrets
import socket
import sys
import time


SOCKET_PATH = os.environ.get("QUICKLAB_AGENT_SOCKET", "/run/quicklab-agent.sock")
RPC_TIMEOUT = 5
BOOT_PROBE_TIMEOUT = 15
SETUP_TIMEOUT = 30 * 60
REBOOT_TIMEOUT = 6 * 60
MAX_REBOOTS = 4
REBOOT_POLL_INTERVAL = 2
MAX_REPLY = 64 * 1024 * 1024
MAX_INPUT = 64 * 1024

LINUX_ROLES = ("app", "soc")
WINDOWS_ROLES = {"dc": "DC", "win11": "Employee"}
CREDENTIALS = {
    "dc": {"dsrm_password": "QUICKLAB_DSRM_PASSWORD"},
    "win11": {"join_user": "QUICKLAB_JOIN_USER", "join_password": "QUICKLAB_JOIN_PASSWORD"},
}
ONBOARDING = (
    "Finish manual guest onboarding, boot the installed OS, and check that the "
    f"QEMU Guest Agent is running on the virtio channel wired to {SOCKET_PATH}."
)

LINUX_SETUP = r"""
set -euo pipefail
media=$(mktemp -d /tmp/quicklab.XXXXXXXX)
mounted=0
cleanup() {
    local status=$? failed=0
    if (( mounted )); then umount -- "$media" || failed=1; fi
    rmdir -- "$media" || failed=1
    if (( status == 0 )); then status=$failed; fi
    exit "$status"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
mount -t iso9660 -o ro,nodev,nosuid,noexec -L QUICKLAB "$media"
mounted=1
/bin/bash "$media/guest/setup-linux.sh" "$1"
"""

WINDOWS_SETUP = r"""
$ErrorActionPreference = 'Stop'
try {
    $volumes = @(Get-Volume -ErrorAction Stop | Where-Object {
        $_.FileSystemLabel -eq 'QUICKLAB' -and $_.DriveType -eq 'CD-ROM' -and $_.DriveLetter
    })
    if ($volumes.Count -ne 1) { exit 20 }
    $scriptPath = '{0}:\guest\setup-windows.ps1' -f $volumes[0].DriveLetter
    if (-not (Test-Path -LiteralPath $scriptPath -PathType Leaf)) { exit 20 }
    $script = Get-Command -Name $scriptPath -CommandType ExternalScript
    if (-not $script.Parameters.ContainsKey('Automation')) { exit 21 }
    $global:LASTEXITCODE = 0
    & $scriptPath -Role __ROLE__ -Automation *> $null
    exit $LASTEXITCODE
} catch {
    exit 1
}
"""

WINDOWS_BOOT = r"""
$ErrorActionPreference = 'Stop'
$os = Get-CimInstance -ClassName Win32_OperatingSystem
$bootId = $os.LastBootUpTime.ToUniversalTime().Ticks.ToString([Globalization.CultureInfo]::InvariantCulture)
$bytes = [Text.Encoding]::UTF8.GetBytes("$bootId`n$env:SystemRoot`n")
$output = [Console]::OpenStandardOutput()
$output.Write($bytes, 0, $bytes.Length)
$output.Flush()
"""


class SetupError(Exception):
    """An error whose message is safe to show to the operator."""


class AgentError(SetupError):
    pass


def _remaining(deadline):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise AgentError("Timed out waiting for the QEMU Guest Agent.")
    return remaining


@dataclass(frozen=True)
class ExecResult:
    returncode: int
    stdout: str
    stderr: str
    truncated: bool


class GuestAgent:
    def __init__(self, sock):
        self.sock = sock
        self.buffer = bytearray()

    def _send(self, command, arguments, deadline, *, prefix=b""):
        request = {"execute": command}
        if arguments is not None:
            request["arguments"] = arguments
        wire = prefix + json.dumps(request, ensure_ascii=True).encode("ascii") + b"\n"
        try:
            self.sock.settimeout(_remaining(deadline))
            self.sock.sendall(wire)
        except OSError:
            raise AgentError("Could not send a request to the QEMU Guest Agent.") from None

    def _read_line(self, deadline):
        searched = 0
        while True:
            _remaining(deadline)
            end = self.buffer.find(b"\n", searched)
            if end >= 0:
                if end > MAX_REPLY:
                    raise AgentError("QEMU Guest Agent response is too large.")
                line = bytes(self.buffer[:end])
                del self.buffer[:end + 1]
                return line
            searched = len(self.buffer)
            if searched > MAX_REPLY:
                raise AgentError("QEMU Guest Agent response is too large.")
            try:
                self.sock.settimeout(_remaining(deadline))
                chunk = self.sock.recv(65536)
            except OSError:
                raise AgentError("Could not read a response from the QEMU Guest Agent.") from None
            if not chunk:
                raise AgentError("The QEMU Guest Agent connection closed.")
            self.buffer.extend(chunk)

    def sync(self, deadline):
        nonce = secrets.randbits(63)
        self._send("guest-sync-delimited", {"id": nonce}, deadline, prefix=b"\xff")
        # The sent marker resets the guest's parser; the returned marker lets us
        # discard even an incomplete stale reply on the host side.
        while True:
            _, marker, payload = self._read_line(deadline).rpartition(b"\xff")
            if not marker:
                continue
            try:
                reply = json.loads(payload)
            except (ValueError, RecursionError):
                continue
            if isinstance(reply, dict) and type(reply.get("return")) is int and reply["return"] == nonce:
                return

    def call(self, command, deadline, arguments=None):
        deadline = min(deadline, time.monotonic() + RPC_TIMEOUT)
        self._send(command, arguments, deadline)
        try:
            reply = json.loads(self._read_line(deadline))
        except (ValueError, RecursionError):
            raise AgentError("Invalid JSON response from the QEMU Guest Agent.") from None
        if isinstance(reply, dict):
            if "error" in reply:
                # The agent's descriptions may quote request arguments.
                raise AgentError(f"The QEMU Guest Agent rejected {command}.")
            if "return" in reply:
                return reply["return"]
        raise AgentError("Unexpected response from the QEMU Guest Agent.")

    def run(self, path, args, deadline, *, env=None, capture=False):
        request = {"path": path, "arg": args, "capture-output": capture}
        if env is not None:
            request["env"] = env
        started = self.call("guest-exec", deadline, request)
        pid = started.get("pid") if isinstance(started, dict) else None
        if type(pid) is not int or pid <= 0:
            raise AgentError("QEMU Guest Agent did not return a valid guest process ID.")

        while True:
            status = self.call("guest-exec-status", deadline, {"pid": pid})
            if not isinstance(status, dict) or type(status.get("exited")) is not bool:
                raise AgentError("QEMU Guest Agent returned an invalid guest process status.")
            if status["exited"]:
                code = status.get("exitcode")
                if type(code) is not int:
                    signal = status.get("signal")
                    if type(signal) is not int or signal <= 0:
                        raise AgentError("QEMU Guest Agent did not report a guest exit status.")
                    code = -signal
                return ExecResult(
                    code,
                    _decode_output(status, "out-data") if capture else "",
                    _decode_output(status, "err-data") if capture else "",
                    bool(status.get("out-truncated") or status.get("err-truncated")),
                )
            # A timeout only stops polling; it does not kill the guest process.
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            time.sleep(min(0.5, remaining))
            if time.monotonic() >= deadline:
                break
        raise AgentError("Timed out waiting for a guest process to finish; it may still be running.")


def _decode_output(status, key):
    try:
        return base64.b64decode(status.get(key, ""), validate=True).decode("utf-8", errors="replace")
    except (ValueError, TypeError):
        raise AgentError("QEMU Guest Agent returned invalid captured output.") from None


@contextmanager
def agent_session(deadline, path=None):
    path = SOCKET_PATH if path is None else path
    try:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    except OSError:
        raise AgentError("Could not create a Unix socket for the QEMU Guest Agent.") from None
    with sock:
        try:
            sock.settimeout(_remaining(deadline))
            sock.connect(path)
        except OSError:
            raise AgentError(f"Could not connect to {path}.") from None
        agent = GuestAgent(sock)
        agent.sync(deadline)
        yield agent


def _credentials(role):
    expected = CREDENTIALS.get(role, {})
    if sys.stdin.isatty():
        if not expected:
            return []
        raise SetupError("Provide Windows credentials as a JSON object on stdin, not at a terminal.")
    try:
        raw = sys.stdin.read(MAX_INPUT + 1)
        if len(raw) > MAX_INPUT:
            raise SetupError("Setup input exceeds the size limit.")
        values = json.loads(raw) if raw.strip() else {}
    except (ValueError, OSError, RecursionError):
        raise SetupError("Setup input must be a JSON object on stdin.") from None
    if not isinstance(values, dict) or values.keys() != expected.keys():
        fields = ", ".join(expected) if expected else "no fields"
        raise SetupError(f"Expected a JSON object with {fields} for {role}.")
    env = []
    for field, variable in expected.items():
        value = values[field]
        if not isinstance(value, str) or not value or "\0" in value:
            raise SetupError(f"{field} must be a nonempty string without NUL characters.")
        try:
            value.encode("utf-8")
        except UnicodeError:
            raise SetupError(f"{field} must contain valid Unicode.") from None
        env.append(f"{variable}={value}")
    return env


def _linux_setup(role):
    with agent_session(time.monotonic() + RPC_TIMEOUT) as agent:
        result = agent.run(
            "/bin/bash", ["-c", LINUX_SETUP, "quicklab-setup", role],
            time.monotonic() + SETUP_TIMEOUT, capture=True,
        )
    sys.stdout.write(result.stdout)
    sys.stderr.write(result.stderr)
    if result.truncated:
        print("QuickLab: the guest agent truncated setup output.", file=sys.stderr)
    if result.returncode:
        raise SetupError(f"Linux setup failed (guest process status {result.returncode}).")


def _powershell_args(script, *, setup=False):
    args = ["-NoLogo", "-NoProfile", "-NonInteractive"]
    if setup:
        args += ["-ExecutionPolicy", "Bypass"]  # This process only.
    # EncodedCommand avoids Windows argv quoting issues. The script has no secrets.
    return args + ["-EncodedCommand", base64.b64encode(script.encode("utf-16-le")).decode("ascii")]


@dataclass(frozen=True)
class WindowsBoot:
    boot_id: str
    system_root: str


def _windows_boot(agent, deadline):
    result = agent.run("powershell.exe", _powershell_args(WINDOWS_BOOT), deadline, capture=True)
    lines = result.stdout.splitlines()
    if (result.returncode or result.truncated or len(lines) != 2
            or not re.fullmatch(r"[1-9][0-9]{0,18}", lines[0])
            or not re.fullmatch(r"[A-Za-z]:\\[^\x00-\x1f<>\"|?*]+", lines[1])):
        raise AgentError("Could not read Windows boot information through the guest agent.")
    return WindowsBoot(*lines)


def _wait_for_reboot(old_boot_id):
    deadline = time.monotonic() + REBOOT_TIMEOUT
    while time.monotonic() < deadline:
        try:
            with agent_session(min(deadline, time.monotonic() + RPC_TIMEOUT)) as agent:
                boot = _windows_boot(agent, min(deadline, time.monotonic() + BOOT_PROBE_TIMEOUT))
            if boot.boot_id != old_boot_id:
                return boot
        except AgentError:
            # QEMU's host socket may stay up while the agent inside Windows disappears.
            pass
        time.sleep(max(0, min(REBOOT_POLL_INTERVAL, deadline - time.monotonic())))
    raise SetupError("Timed out waiting for a verified Windows reboot. Check the guest console.")


def _windows_setup(role, credentials):
    boot = None
    script = WINDOWS_SETUP.replace("__ROLE__", WINDOWS_ROLES[role])
    for reboot_count in range(MAX_REBOOTS + 1):
        with agent_session(time.monotonic() + RPC_TIMEOUT) as agent:
            if boot is None:
                boot = _windows_boot(agent, time.monotonic() + BOOT_PROBE_TIMEOUT)
            # guest-exec's env replaces the process environment. Windows needs
            # SystemRoot for process startup, and absolute paths avoid relying on PATH.
            env = [f"SystemRoot={boot.system_root}", f"windir={boot.system_root}", *credentials]
            system32 = boot.system_root + r"\System32"
            result = agent.run(
                system32 + r"\WindowsPowerShell\v1.0\powershell.exe",
                _powershell_args(script, setup=True), time.monotonic() + SETUP_TIMEOUT, env=env,
            )
            if result.returncode == 0:
                return
            if result.returncode == 20:
                raise SetupError("Windows could not find setup-windows.ps1 on a single QUICKLAB CD.")
            if result.returncode == 21:
                raise SetupError("The QUICKLAB Windows script does not support -Automation. Use manual setup or attach compatible media.")
            if result.returncode != 3010:
                raise SetupError("Windows automation failed. Check setup from the guest console.")
            if reboot_count == MAX_REBOOTS:
                raise SetupError("Windows setup reached the reboot limit. Check the guest console.")
            try:
                shutdown = agent.run(system32 + r"\shutdown.exe", ["/r", "/t", "5"],
                                     time.monotonic() + RPC_TIMEOUT)
            except AgentError:
                # The connection can drop before we learn the command's status.
                # Don't send it again; require proof of a new boot either way.
                pass
            else:
                if shutdown.returncode != 0:
                    raise SetupError("Windows refused the requested restart. Check the guest console.")
        boot = _wait_for_reboot(boot.boot_id)


class _ArgumentParser(argparse.ArgumentParser):
    def error(self, message):
        # Do not echo mistaken command-line credential arguments.
        super().error("expected ROLE (app, soc, dc, or win11) and optional --ping")


def main(argv=None):
    parser = _ArgumentParser(description=__doc__, allow_abbrev=False, usage="%(prog)s ROLE [--ping]")
    parser.add_argument("role", metavar="ROLE", choices=(*LINUX_ROLES, *WINDOWS_ROLES))
    parser.add_argument("--ping", action="store_true", help="check guest-agent connectivity only")
    args = parser.parse_args(argv)

    try:
        if args.ping:
            deadline = time.monotonic() + RPC_TIMEOUT
            with agent_session(deadline) as agent:
                if agent.call("guest-ping", deadline) != {}:
                    raise AgentError("Unexpected guest-ping response.")
            print(f"QuickLab: guest agent reachable for {args.role}.")
        else:
            credentials = _credentials(args.role)
            if args.role in LINUX_ROLES:
                _linux_setup(args.role)
            else:
                _windows_setup(args.role, credentials)
            print(f"QuickLab: {args.role} setup complete.")
        return 0
    except SetupError as exc:
        print(f"QuickLab: {exc}", file=sys.stderr)
        if args.ping:
            print(ONBOARDING, file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("QuickLab: interrupted; any started guest command may still be running.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
