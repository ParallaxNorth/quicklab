#!/usr/bin/env python3
"""Local Quickemu lab launcher. Run as the desktop user, not root."""
import argparse
import fcntl
import json
import os
import platform
import shlex
import shutil
import socket
import stat
import subprocess
import sys
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DEFAULT_STATE = ROOT / ".quicklab"
QEMU = "qemu-system-x86_64"
CONTROLLER_DIR = ROOT / "automation"
CONTROLLER_IMAGE = "quicklab-ansible:local"
CONTROLLER_SOCKET = "/run/quicklab-agent.sock"


@dataclass(frozen=True)
class VM:
    os: str
    media: str
    host: int
    ram: int
    cores: int
    disk: int

    @property
    def mac(self):
        return f"52:54:00:77:00:{self.host:02d}"

    @property
    def windows(self):
        return self.os.startswith("windows")


# Insertion order is the default start order, not a guest-readiness check.
VMS = {
    "dc": VM("windows-server", "server2022", 10, 4, 2, 64),
    "soc": VM("linux", "ubuntu", 40, 2, 2, 32),
    "app": VM("linux", "ubuntu", 30, 2, 2, 24),
    "win11": VM("windows", "win11", 20, 4, 2, 80),
}
MEDIA = {
    "ubuntu": ("ubuntu-server", "24.04"),
    "server2022": ("windows-server", "2022", "English (United States)"),
    "win11": ("windows", "11", "English (United States)"),
    "virtio-win": (),
}
VIRTIO = "https://fedorapeople.org/groups/virt/virtio-win/direct-downloads/stable-virtio/virtio-win.iso"
HUB = "hub"
START_TOOLS = ("quickemu", QEMU, "qemu-img", "swtpm", "socat")
ROLE_LABELS = {
    "dc": "Windows Server 2022 · Active Directory and DNS",
    "soc": "Ubuntu 24.04 · log collector",
    "app": "Ubuntu 24.04 · intranet and log forwarder",
    "win11": "Windows 11 Pro · employee workstation",
}


class LabError(Exception):
    pass


def cli(*args, state=None):
    location = ["--state", str(state)] if state is not None and state != DEFAULT_STATE else []
    return shlex.join(["./quicklab.py", *location, *args])


def progress(message):
    print(message, flush=True)


def tool(name):
    if not shutil.which(name):
        raise LabError(f"Missing executable: {name}")
    return name


def iso_valid(path):
    # Shallow ISO/UDF signature check, not a checksum or a bootability test.
    try:
        with path.open("rb") as image:
            image.seek(16 * 2048)
            descriptors = image.read(32 * 2048)
    except (FileNotFoundError, IsADirectoryError):
        return False
    return any(descriptors[i+1:i+6] in (b"CD001", b"NSR02", b"NSR03")
               for i in range(0, len(descriptors), 2048))


def download_media():
    for key, spec in MEDIA.items():
        dest = Path(f"media/{key}.iso")
        if dest.exists():
            if not iso_valid(dest):
                raise LabError(f"{dest} is not recognized as an ISO; replace it before retrying")
            progress(f"  Reusing {dest.name}")
            continue
        stage = Path(f"media/.downloads/{key}")
        stage.mkdir(parents=True, exist_ok=True)
        if key == "virtio-win":
            command = [tool("curl"), "--fail", "--location", "--proto", "=https",
                       "--continue-at", "-", "--output", "virtio-win.iso", VIRTIO]
        else:
            command = [tool("quickget"), "--download", *spec]
        progress(f"  Downloading {key} (official installation media)…")
        try:
            subprocess.run(command, cwd=stage, check=True)
        except subprocess.CalledProcessError as error:
            raise LabError(f"Download for {key} failed. Retry or place the official ISO at {dest.resolve()} (see README.md).") from error
        images = [p for p in stage.rglob("*") if p.suffix.lower() == ".iso"
                  and p.name.lower() != "unattended.iso" and iso_valid(p)]
        if len(images) != 1:
            raise LabError(f"Expected one downloaded ISO in {stage}; manually place the official ISO at {dest}")
        images[0].rename(dest)


def qmp_path(role):
    return f"r/{role}.qmp"


def stream(role, server):
    # Only the QEMU hub and the guest connect to these Unix-domain sockets.
    return (f"stream,id={role if server else 'lab'},server={'on' if server else 'off'},"
            f"addr.type=unix,addr.path=r/{role}.net" + ("" if server else ",reconnect-ms=1000"))


def seed_file(role):
    return Path(f"media/{role}-seed.iso")


def guest_disk(role):
    return Path(f"v/{role}/ql-{role}.qcow2")


def require_fresh(role):
    if VMS[role].windows:
        raise LabError("Ubuntu autoinstall is only available for app and soc")
    if running(role) or guest_disk(role).exists():
        raise LabError(f"{role}: autoinstall requires a new VM with no existing disk; use manual recovery for an interrupted install")


def ubuntu_seed(role):
    from automation.ubuntu_seed import make_seed

    require_fresh(role)
    dest = seed_file(role)
    dest.parent.mkdir(exist_ok=True)
    progress(f"Creating a Ubuntu installer seed for {role}. The prompt is for the guest's labadmin account.")
    try:
        make_seed(role, dest)
    except RuntimeError as error:
        raise LabError(str(error)) from error
    print(f"Created private Ubuntu seed: {dest.resolve()}")
    print(f"Next: {cli('up', role, '--nat', '--autoinstall', state=Path.cwd())}")


def config(role, nat, autoinstall=False):
    vm = VMS[role]
    backend = "user,id=lab,ipv6=off" if nat else stream(role, False)
    extra = (f"-netdev {backend} -device virtio-net-pci,netdev=lab,mac={vm.mac} "
             f"-qmp unix:{qmp_path(role)},server=on,wait=off")
    if vm.windows:
        extra += " -drive media=cdrom,index=2,file=media/virtio-win.iso,format=raw,readonly=on"
    elif autoinstall:
        extra += f" -drive media=cdrom,index=2,file={seed_file(role)},format=raw,readonly=on"
    values = {
        "guest_os": vm.os,
        "disk_img": str(guest_disk(role)),
        "disk_size": f"{vm.disk}G",
        "ram": f"{vm.ram}G",
        "cpu_cores": str(vm.cores),
        "boot": "efi",
        "secureboot": "on" if role == "win11" else "off",
        "tpm": "on" if role == "win11" else "off",
        "iso": f"media/{vm.media}.iso",
        "fixed_iso": "media/quicklab.iso",
        "network": "none",
        "bridge": "",
        "gl": "off",
        "extra_args": extra,
    }
    return "# Generated by QuickLab. Edit the inventory in quicklab.py instead.\n" + "".join(
        f"{key}={shlex.quote(value)}\n" for key, value in values.items())


def prepare(download):
    if any(running(role) for role in VMS):
        raise LabError("Stop the lab before rebuilding the guest payload ISO")
    Path("media").mkdir(exist_ok=True)
    mkiso = shutil.which("xorriso") or shutil.which("genisoimage")
    if not mkiso:
        raise LabError("Install xorriso or genisoimage to create the guest payload ISO")
    progress(f"Preparing media in {Path('media').resolve()}")
    if download:
        progress("Checking installation media. Missing ISOs may take several minutes to download.")
        download_media()
    progress("Building the read-only QUICKLAB guest setup CD…")
    dest = Path("media/quicklab.iso")
    pending = dest.with_suffix(".tmp")
    pending.unlink(missing_ok=True)
    command = [mkiso, *(["-as", "mkisofs"] if Path(mkiso).name == "xorriso" else []),
               "-quiet", "-R", "-J", "-V", "QUICKLAB", "-o", str(pending),
               "-graft-points", f"guest={ROOT / 'guest'}"]
    try:
        subprocess.run(command, check=True)
        pending.replace(dest)
    finally:
        pending.unlink(missing_ok=True)
    print(f"Guest setup ISO: {dest.resolve()}")
    missing = False
    for name in MEDIA:
        path = Path(f"media/{name}.iso")
        state = media_state(path)
        missing |= state != "ready"
        print(f"  {state:<8} {path.name}")
    if missing:
        print(f"Next: {cli('prepare', '--download', state=Path.cwd())} or add the official ISOs manually (README.md).")
    else:
        print(f"Next: {cli('guide', state=Path.cwd())} for installation steps.")


def pidfile(role):
    return Path("r/hub.pid" if role == HUB else f"v/{role}/ql-{role}.pid")


def process_args(role):
    # Match the lab cwd and exact QMP endpoint; a stale pidfile is not a process handle.
    try:
        pid = int(pidfile(role).read_text().strip())
        if pid <= 1:
            return []
        proc = Path(f"/proc/{pid}")
        if not (proc / "cwd").samefile(Path.cwd()):
            return []
        args = (proc / "cmdline").read_bytes().split(b"\0")
        endpoint = f"unix:{qmp_path(role)},server=on,wait=off".encode()
        for option, value in zip(args, args[1:]):
            if option == b"-qmp" and value == endpoint and Path(os.fsdecode(args[0])).name == QEMU:
                return args
    except (OSError, ValueError):
        pass
    return []


def running(role):
    return bool(process_args(role))


def is_nat(role):
    return b"user,id=lab,ipv6=off" in process_args(role)


def qmp(role, command):
    with socket.socket(socket.AF_UNIX) as connection:
        connection.settimeout(2)
        connection.connect(qmp_path(role))
        with connection.makefile("rb") as wire:
            greeting = json.loads(wire.readline())
            if "QMP" not in greeting:
                raise LabError(f"{role}: invalid QMP greeting")
            for action in ("qmp_capabilities", command):
                connection.sendall(json.dumps({"execute": action, "id": action}).encode() + b"\n")
                while True:
                    message = json.loads(wire.readline())
                    if message.get("id") != action:
                        continue  # QMP may interleave asynchronous events.
                    if "error" in message:
                        raise LabError(f"{role}: {action}: {message['error']['desc']}")
                    break
            return message.get("return")


def ready(role):
    until = time.monotonic() + 8
    while time.monotonic() < until:
        if running(role):
            try:
                return qmp(role, "query-status")
            except (OSError, ValueError):
                pass
        time.sleep(0.2)
    log = "r/hub.log" if role == HUB else f"v/{role}/ql-{role}.log"
    raise LabError(f"{role}: QEMU did not become ready; see {Path(log).resolve()}")


def start_hub():
    if running(HUB):
        ready(HUB)
        return
    # Existing guests must reconnect after a hub failure; don't remove live guest sockets.
    for path in [Path(qmp_path(HUB)), pidfile(HUB), *(Path(f"r/{r}.net") for r in VMS)]:
        path.unlink(missing_ok=True)
    command = [tool(QEMU), "-machine", "none", "-nodefaults", "-display", "none",
               "-pidfile", str(pidfile(HUB)),
               "-qmp", f"unix:{qmp_path(HUB)},server=on,wait=off"]
    for role in VMS:
        command += ["-netdev", stream(role, True), "-netdev",
                    f"hubport,id=port_{role},hubid=0,netdev={role}"]
    log_path = Path("r/hub.log")
    with log_path.open("w") as log:
        # Keep QEMU's cwd: both relative Unix socket paths and PID validation depend on it.
        process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=log,
                                   stderr=subprocess.STDOUT, start_new_session=True)
    try:
        ready(HUB)
    except (LabError, OSError, ValueError) as error:
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=5)
        detail = log_path.read_text(errors="replace").strip()
        raise LabError(f"{error}{': ' + detail if detail else ''}") from error
    return process


def stop_hub():
    if running(HUB):
        qmp(HUB, "quit")
        until = time.monotonic() + 5
        while running(HUB) and time.monotonic() < until:
            time.sleep(0.1)
        if running(HUB):
            raise LabError("The private hub did not stop")


def qemu_network_supported():
    help_text = subprocess.check_output([QEMU, "-netdev", "help"], text=True,
                                        stderr=subprocess.STDOUT, timeout=5)
    return {"stream", "hubport"} <= set(help_text.split())


def check_host():
    if platform.system() != "Linux" or platform.machine() != "x86_64":
        raise LabError("This lab requires Linux on x86_64")
    for name in START_TOOLS:
        tool(name)
    if not qemu_network_supported():
        raise LabError("QEMU must support the stream and hubport network backends")
    if not os.access("/dev/kvm", os.R_OK | os.W_OK):
        raise LabError("/dev/kvm is missing or not accessible. Enable virtualization (or nested KVM) and grant your user KVM access.")


def up(roles, nat, autoinstall=False):
    if autoinstall:
        if not nat or len(roles) != 1:
            raise LabError("--autoinstall requires --nat and exactly one Ubuntu role (app or soc)")
        require_fresh(roles[0])
        if not iso_valid(seed_file(roles[0])):
            raise LabError(f"Missing or invalid seed; run seed {roles[0]} first")
    if nat and (len(roles) != 1 or any(running(role) for role in VMS)):
        raise LabError("--nat requires exactly one role and all lab VMs stopped")
    if not nat and any(is_nat(role) for role in VMS):
        raise LabError("Stop the provisioning VM before starting the private lab")
    state = Path.cwd()
    progress(f"Starting {', '.join(roles)} on {'temporary NAT' if nat else 'the private lab LAN'}.")
    progress("Checking host tools, hardware virtualization and required media…")
    check_host()
    pending = [r for r in roles if not running(r)]
    media = [(path.name, media_state(path)) for path in required_media(pending)]
    missing = [f"{name} ({condition})" for name, condition in media if condition != "ready"]
    if missing:
        raise LabError(f"Media not ready in {state / 'media'}: {', '.join(missing)}.\n"
                       f"  Next: {cli('prepare', '--download', state=state)} or add official ISOs (README.md).")
    if nat:
        stop_hub()
        progress("Temporary NAT: this guest may reach the host, LAN and internet. Only use trusted media.")
    else:
        progress("Checking the private Ethernet hub…" if running(HUB) else "Starting the private Ethernet hub…")
        start_hub()
    for index, role in enumerate(roles, 1):
        prefix = f"[{index}/{len(roles)}] {role}"
        if role not in pending:
            progress(f"{prefix}: already running; leaving it alone.")
            continue
        disk = guest_disk(role)
        progress(f"{prefix}: starting Quickemu ({'reusing the VM disk' if disk.exists() else 'creating a new VM disk'})…")
        disk.parent.mkdir(parents=True, exist_ok=True)
        qmp_file = Path(qmp_path(role))
        qmp_file.unlink(missing_ok=True)
        pidfile(role).unlink(missing_ok=True)
        conf = Path(f"ql-{role}.conf")
        temporary = conf.with_suffix(".tmp")
        temporary.write_text(config(role, nat, autoinstall), encoding="utf-8")
        temporary.replace(conf)
        try:
            with launch_log(role).open("w") as output:
                subprocess.run(["quickemu", "--vm", str(conf), "--display", "spice", "--access", "local",
                                "--viewer", "none", "--public-dir", "none", "--monitor", "socket",
                                "--serial", "none", "--sound-card", "none"], check=True, stdin=subprocess.DEVNULL,
                               stdout=output, stderr=subprocess.STDOUT, start_new_session=True)
            ready(role)
        except (subprocess.CalledProcessError, LabError, OSError) as error:
            reason = f"Quickemu exited with status {error.returncode}" if isinstance(error, subprocess.CalledProcessError) else str(error)
            raise LabError(f"{role}: {reason}. Other VMs already started were left running.\n"
                           f"  Inspect: {cli('logs', role, state=state)}\n"
                           f"  Diagnose: {cli('doctor', role, state=state)}") from error
        progress(f"{prefix}: QEMU is running. Open: {cli('console', role, state=state)}")
    print("A running VM may still be booting or waiting at its installer; guest services were not checked.")
    if autoinstall:
        print("In Ubuntu's GRUB menu press e, add autoinstall before --- on the linux line, then press Ctrl+X.")
    print(f"View lab: {cli('status', state=state)}")


def down(roles, wait, force):
    targets = [role for role in reversed(roles) if running(role)]
    if not targets:
        if not any(running(role) for role in VMS):
            if running(HUB):
                progress("No guests are running. Stopping the idle private hub…")
            stop_hub()
        print("Selected VMs are already stopped. No disks were changed.")
        return
    progress(f"Shutting down {', '.join(targets)}. Guest disks will be kept.")
    for role in targets:
        try:
            qmp(role, "system_powerdown")
            print(f"{role}: requested ACPI shutdown", flush=True)
        except (OSError, ValueError, LabError) as error:
            print(f"{role}: could not request shutdown: {error}", file=sys.stderr)
    until = time.monotonic() + wait
    announce = time.monotonic() + 10
    if wait:
        progress(f"Waiting up to {wait}s for the guests to shut down…")
    while (remaining := [role for role in targets if running(role)]) and time.monotonic() < until:
        if time.monotonic() >= announce:
            progress(f"Still waiting for: {', '.join(remaining)}")
            announce = time.monotonic() + 10
        time.sleep(0.25)
    if force:
        for role in targets:
            if running(role):
                print(f"{role}: forcing power off (guest data may be lost)", flush=True)
                qmp(role, "quit")
        until = time.monotonic() + 5
        while any(running(role) for role in targets) and time.monotonic() < until:
            time.sleep(0.1)
    survivors = [role for role in targets if running(role)]
    if not any(running(role) for role in VMS) and running(HUB):
        progress("Stopping the private hub (no lab VMs running)…")
        stop_hub()
    if survivors:
        state = Path.cwd()
        raise LabError(f"Still running after {wait}s: {', '.join(survivors)}. No data was deleted.\n"
                       f"  Open the guest and shut it down there: {cli('console', survivors[0], state=state)}\n"
                       f"  Last resort (may lose unsaved data): {cli('down', *survivors, '--force', state=state)}")
    print("Selected VMs stopped; disks preserved.")


def required_media(roles):
    names = {VMS[role].media for role in roles}
    if any(VMS[role].windows for role in roles):
        names.add("virtio-win")
    if roles:
        names.add("quicklab")
    return [Path(f"media/{name}.iso") for name in sorted(names)]


def media_state(path):
    if not path.exists():
        return "missing"
    try:
        return "ready" if path.is_file() and iso_valid(path) else "invalid"
    except OSError:
        return "unreadable"


def launch_log(role):
    return Path(f"v/{role}/ql-{role}.launch.log")


def log_sources(role):
    if role == HUB:
        return [Path("r/hub.log")]
    return [launch_log(role), Path(f"v/{role}/ql-{role}.log")]


def agent_socket(role):
    return Path(f"v/{role}/ql-{role}-agent.sock")


def show_logs(state, role, lines):
    found = False
    for source in log_sources(role):
        path = state / source
        if not path.is_file():
            continue
        found = True
        print(f"== {path} (last {lines} lines) ==")
        with path.open(errors="replace") as handle:
            excerpt = deque(handle, maxlen=lines)
        print("".join(excerpt).rstrip() if excerpt else "(empty)")
    if not found:
        print(f"No logs for {role} in {state}. Logs appear after its first launch attempt.")
        print(f"Next: {cli('doctor', *([] if role == HUB else [role]), state=state)}")


def qemu_state(role):
    try:
        result = qmp(role, "query-status")
        return result["status"]
    except (OSError, ValueError, LabError, KeyError, TypeError):
        return "unresponsive"


def status(state, roles):
    existing = state.is_dir()
    print(f"Lab state: {state}" + (" (not created yet)" if not existing else ""))
    print(f"{'ROLE':<7} {'QEMU':<14} {'NETWORK':<14} {'PLANNED IP':<15} {'VM DISK':<9} PURPOSE")
    active = [role for role in roles if existing and running(role)]
    for role in roles:
        live = role in active
        mode = ("temporary NAT" if is_nat(role) else "private LAN") if live else "—"
        disk = "present" if (state / guest_disk(role)).is_file() else "not made"
        print(f"{role:<7} {(qemu_state(role) if live else 'stopped'):<14} {mode:<14} "
              f"{('10.77.0.' + str(VMS[role].host)):<15} {disk:<9} {ROLE_LABELS[role]}")
    media = required_media(roles)
    ready_count = sum(media_state(state / path) == "ready" for path in media)
    hub = qemu_state(HUB) if existing and running(HUB) else "stopped"
    print(f"\nPrivate hub: {hub}  |  Required media: {ready_count}/{len(media)} available")
    print("Addresses are planned, not observed. QEMU and disk status do not verify installation or guest services.")
    if len(roles) == 1:
        role = roles[0]
        vm = VMS[role]
        print(f"Allocation: {vm.ram} GiB RAM, {vm.cores} vCPUs, {vm.disk} GiB disk cap")
        print(f"Disk path: {state / guest_disk(role)}")
        if active:
            print(f"Console: {cli('console', role, state=state)}")
        print(f"Logs: {cli('logs', role, state=state)}")
    else:
        print(f"Show one guest's allocation and disk path: {cli('status', roles[0], state=state)}")
    print(f"Diagnose: {cli('doctor', *([] if roles == list(VMS) else roles), state=state)}")
    if ready_count < len(media):
        print(f"Prepare media: {cli('prepare', '--download', state=state)}")


def probe_agent(role):
    from automation.qga_setup import AgentError, agent_session

    path = agent_socket(role)
    if not path.is_socket():
        return "socket missing"
    deadline = time.monotonic() + 1.5
    try:
        with agent_session(deadline, str(path)) as agent:
            if agent.call("guest-ping", deadline) == {}:
                return "responding"
    except (AgentError, OSError):
        pass
    return "not responding"


def agent_status(role):
    # QEMU's agent socket only handles one client. Don't contend with Ansible.
    try:
        with Path("r/lock").open() as lock:
            fcntl.flock(lock, fcntl.LOCK_SH | fcntl.LOCK_NB)
            return probe_agent(role)
    except FileNotFoundError:
        return "not checked (no launcher lock)"
    except BlockingIOError:
        return "not checked (a lab command is in progress)"


def docker_status(state):
    if not shutil.which("docker"):
        return "WARN", "Docker CLI missing; install it for Ansible provisioning."
    path = Path("/var/run/docker.sock")
    if not path.is_socket():
        return "WARN", "Rootful Docker socket missing; check that the system Docker daemon is running."
    if not os.access(path, os.W_OK):
        return "WARN", f"Docker requires elevated access. Use {cli('build-controller', '--sudo-docker', state=state)} in your terminal if the image is not built."
    base = ["docker", "--host", "unix:///var/run/docker.sock"]
    try:
        daemon = subprocess.run([*base, "version", "--format", "{{.Server.Version}}"],
                                capture_output=True, text=True, timeout=3)
        if daemon.returncode:
            return "WARN", "Could not reach the local Docker daemon; check Docker permissions and service status."
        image = subprocess.run([*base, "image", "inspect", CONTROLLER_IMAGE],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=3)
    except (OSError, subprocess.TimeoutExpired):
        return "WARN", "Local Docker query failed or timed out."
    if image.returncode:
        return "WARN", f"Docker is reachable, but the controller image was not found. Run {cli('build-controller', state=state)}."
    return "OK", f"Docker {daemon.stdout.strip()} and controller image {CONTROLLER_IMAGE} available."


def doctor(state, roles):
    errors = warnings = 0

    def report(level, message):
        nonlocal errors, warnings
        errors += level == "FAIL"
        warnings += level == "WARN"
        progress(f"  [{level:<4}] {message}")

    progress(f"QuickLab diagnostics · {state}\nRead-only checks; no guests are started and sudo is not invoked.")
    progress("\nHost")
    system = f"{platform.system()} {platform.machine()}"
    report("OK" if system == "Linux x86_64" else "FAIL",
           system if system == "Linux x86_64" else f"{system}; required: Linux x86_64")
    missing = [name for name in START_TOOLS if not shutil.which(name)]
    report("FAIL" if missing else "OK", f"Missing launcher tools: {', '.join(missing)}" if missing else "Launcher tools found")
    kvm = os.access("/dev/kvm", os.R_OK | os.W_OK)
    report("OK" if kvm else "FAIL", "/dev/kvm is accessible" if kvm else "Cannot use /dev/kvm. Check virtualization support and user permissions.")
    if QEMU not in missing:
        try:
            network_ok = qemu_network_supported()
        except (OSError, subprocess.SubprocessError):
            network_ok = False
        report("OK" if network_ok else "FAIL", "QEMU supports the private Ethernet hub" if network_ok else "QEMU hub support could not be verified; check stream and hubport backends.")
    if not shutil.which("spicy"):
        report("WARN", "SPICE viewer (spicy) missing; install it to open guest consoles.")
    if not (shutil.which("xorriso") or shutil.which("genisoimage")):
        report("WARN", "Install xorriso or genisoimage to build guest setup and seed CDs.")
    storage = next(path for path in (state, *state.parents) if path.is_dir())
    free = shutil.disk_usage(storage).free / (1024 ** 3)
    report("WARN" if free < 10 else "INFO", f"{free:.1f} GiB free on the state filesystem" + ("; space may be too low for ISOs or growing VM disks" if free < 10 else ""))
    report(*docker_status(state))

    progress("\nInstallation media")
    unavailable = False
    for item in required_media(roles):
        condition = media_state(state / item)
        unavailable |= condition != "ready"
        report("OK" if condition == "ready" else "FAIL", f"{item.name}: {condition}")
    if unavailable:
        print(f"  Next: {cli('prepare', '--download', state=state)} or add official ISOs in {state / 'media'}.")

    progress("\nVMs and private network")
    existing = state.is_dir()
    live = [r for r in VMS if existing and running(r)]
    private = [r for r in live if not is_nat(r)]
    hub = qemu_state(HUB) if existing and running(HUB) else "stopped"
    hub_problem = hub == "unresponsive" or (bool(private) and hub != "running")
    report("FAIL" if hub_problem else "INFO", f"Private hub: {hub}")
    if hub_problem:
        print(f"  Inspect: {cli('logs', HUB, state=state)}")
    for role in roles:
        if role not in live:
            disk = "disk present, OS not verified" if (state / guest_disk(role)).is_file() else "no VM disk yet"
            report("INFO", f"{role}: stopped ({disk})")
            continue
        qemu = qemu_state(role)
        mode = "temporary NAT" if role not in private else "private LAN"
        report("OK" if qemu == "running" else "FAIL" if qemu == "unresponsive" else "WARN", f"{role}: QEMU {qemu}, {mode}")
        if mode == "temporary NAT":
            report("WARN", f"{role}: temporary NAT may reach the host, LAN and internet.")
        if qemu != "running":
            print(f"  Inspect: {cli('logs', role, state=state)}")
            continue
        reply = agent_status(role)
        level = "OK" if reply == "responding" else "INFO" if reply.startswith("not checked") else "WARN"
        report(level, f"{role}: guest agent {reply}")
        if level == "WARN":
            print(f"  If setup is expected, boot the installed OS and check the guest agent: {cli('console', role, state=state)}")
    print(f"\nSummary: {errors} blocking issue(s), {warnings} warning(s). Guest applications and AD were not tested.")
    return 1 if errors else 0


def docker_command(sudo_docker):
    if os.geteuid() == 0:
        raise LabError("Run QuickLab as your desktop user; pass --sudo-docker to elevate only Docker")
    command = [tool("docker"), "--host", "unix:///var/run/docker.sock"]
    return [tool("sudo"), "--", *command] if sudo_docker else command


def build_controller(sudo_docker):
    command = docker_command(sudo_docker)
    progress(f"Building {CONTROLLER_IMAGE} on the local Docker daemon. The first build downloads the base image and Ansible.")
    subprocess.run([*command, "build", "--tag", CONTROLLER_IMAGE, str(CONTROLLER_DIR)], check=True)
    print("Controller ready. After installing and booting a guest, run provision ROLE (add --sudo-docker if needed).")


def docker_provision_command(role, endpoint, sudo_docker, tty):
    if any(char in str(endpoint) for char in ',"\r\n'):
        raise LabError("Docker guest-agent bind mounts do not support commas, quotes or line breaks in the state path")
    return [*docker_command(sudo_docker), "run", "--rm", "--pull=never", "--network=none",
            "--userns=host", "--user", f"{os.getuid()}:{os.getgid()}", "--read-only", "--cap-drop=ALL",
            "--security-opt=no-new-privileges", "--tmpfs", "/tmp:rw,nosuid,nodev,mode=1777,size=64m",
            "--mount", f"type=bind,source={endpoint},target={CONTROLLER_SOCKET},readonly",
            "--env", f"QUICKLAB_ROLE={role}", *(["-it"] if tty else []), CONTROLLER_IMAGE]


def provision(role, sudo_docker, local):
    if not running(role):
        raise LabError(f"{role} is not running; start it and boot into the installed OS first")
    if VMS[role].windows and is_nat(role):
        raise LabError("Stop NAT and start this Windows guest on the private LAN before setup")
    if role == "win11" and (not running("dc") or is_nat("dc")):
        raise LabError("Start and provision the private domain controller before joining win11")
    tty = sys.stdin.isatty() and sys.stdout.isatty()
    if VMS[role].windows and not tty:
        raise LabError("Run Windows provisioning in an interactive terminal for credential prompts")
    endpoint = agent_socket(role).absolute()
    if not endpoint.is_socket():
        raise LabError(f"{role}: Quickemu guest-agent socket is missing: {endpoint}")
    progress(f"Provisioning {role} via {'local Ansible' if local else 'a temporary Docker controller'} and the private guest-agent socket.")
    if VMS[role].windows:
        progress("Ansible will prompt for lab credentials. Windows may reboot several times; leave the VM on.")
    else:
        progress("The guest may install missing packages before applying the static lab network.")
    if local:
        env = {**os.environ, "QUICKLAB_ROLE": role, "QUICKLAB_AGENT_SOCKET": str(endpoint)}
        subprocess.run([tool("ansible-playbook"), "-i", "localhost,", str(CONTROLLER_DIR / "site.yml")],
                       check=True, env=env)
    else:
        subprocess.run(docker_provision_command(role, endpoint, sudo_docker, tty), check=True)
    state = Path.cwd()
    print(f"Provisioning finished for {role}.")
    if is_nat(role):
        print(f"Return to the private LAN: {cli('down', role, state=state)} then {cli('up', role, state=state)}")
    elif role == "dc":
        print(f"Next: {cli('guide', 'win11', state=state)} to install or join the employee workstation.")
    else:
        print(f"Inspect: {cli('status', role, state=state)}")


def guide(state, role=None):
    print("QuickLab — your local Quickemu lab")
    print(f"State: {state}\n")
    commands = lambda *args: cli(*args, state=state)
    if role:
        print(f"{role}: {ROLE_LABELS[role]} (planned IP: 10.77.0.{VMS[role].host})")
        if not VMS[role].windows:
            print(f"  1. Fresh disk: {commands('seed', role)}, then {commands('up', role, '--nat', '--autoinstall')}")
            print(f"  2. Open {commands('console', role)}. In GRUB, press e, add autoinstall before --- and press Ctrl+X.")
            print(f"  3. After Ubuntu powers off, boot the installed disk: {commands('up', role, '--nat')}")
            print(f"  4. Apply the workload: {commands('provision', role, '--sudo-docker')} (Docker controller must be built).")
            print(f"  5. Stop with {commands('down', role)}, then {commands('up', role)} to return to the private LAN.")
        else:
            if role == "win11":
                print("  Finish provisioning dc before joining this guest. If Windows OOBE needs NAT, use it only with all guests stopped; restart privately for the join.")
            print(f"  1. Boot: {commands('up', role)}; install Windows using {commands('console', role)}.")
            print("  2. Boot the installed OS and install the signed QEMU Guest Agent MSI from the VirtIO CD.")
            print(f"  3. Apply {'AD and DNS' if role == 'dc' else 'the domain join'}: {commands('provision', role, '--sudo-docker')} (Docker controller must be built).")
        print("\nSee README.md for OS editions, drivers, and the manual setup alternative.")
        return
    print("First time (run as your desktop user, not root)")
    print(f"  1. Check host prerequisites: {commands('check')}")
    print(f"  2. Get the installers and guest setup CD: {commands('prepare', '--download')}")
    print(f"  3. Build the Ansible container: {commands('build-controller', '--sudo-docker')}")
    print(f"  4. Install and provision each guest. Start with {commands('guide', 'soc')}; finish dc before joining win11.")
    for key, label in ROLE_LABELS.items():
        print(f"       {key:<6} {label}")
    print("     Omit --sudo-docker if your user already has access to rootful Docker.")
    print("\nEveryday use after setup")
    print(f"  Start: {commands('up')}  |  Inspect: {commands('status')}  |  Stop: {commands('down')}")
    print(f"  Diagnose: {commands('doctor')}  |  Startup logs: {commands('logs', 'soc')}")
    print("\nConsole access is local. The default LAN has no gateway or port forwards. See README.md for the full walkthrough.")


def main():
    parser = argparse.ArgumentParser(
        description="QuickLab: install, run, and inspect a local Quickemu lab. Run as your desktop user.",
        epilog="New here? Run ./quicklab.py with no arguments for the setup guide. "
               "For a problem, start with ./quicklab.py doctor; for a failed launch, use ./quicklab.py logs ROLE.",
    )
    parser.add_argument("--state", type=Path, default=DEFAULT_STATE, help="lab data directory (before subcommand; default: .quicklab beside the launcher)")
    sub = parser.add_subparsers(dest="command", metavar="COMMAND")
    intro = sub.add_parser("guide", help="read the getting-started guide or steps for one role")
    intro.add_argument("role", nargs="?", choices=VMS, help="optional role: dc, soc, app, win11")
    sub.add_parser("check", help="check host tools and KVM without starting anything")
    prep = sub.add_parser("prepare", help="build the guest scripts ISO and enumerate install media")
    prep.add_argument("--download", action="store_true", help="fetch missing official install media (large downloads)")
    seed = sub.add_parser("seed", help="create private NoCloud install media for a fresh Ubuntu VM")
    seed.add_argument("role", choices=("app", "soc"))
    build = sub.add_parser("build-controller", help="build the disposable Docker Ansible controller")
    build.add_argument("--sudo-docker", action="store_true", help="elevate only the Docker CLI")
    setup = sub.add_parser("provision", help="configure a guest with the Docker Ansible controller")
    setup.add_argument("role", choices=VMS)
    backend = setup.add_mutually_exclusive_group()
    backend.add_argument("--sudo-docker", action="store_true", help="elevate only the Docker CLI")
    backend.add_argument("--local", action="store_true", help="use an installed ansible-playbook instead of Docker")
    start = sub.add_parser("up", help="start all guests or specific roles (dc, soc, app, win11)")
    start.add_argument("roles", nargs="*", metavar="ROLE", help="dc, soc, app, win11; omit to start all")
    start.add_argument("--nat", action="store_true", help="temporary outbound connectivity for one provisioning VM only")
    start.add_argument("--autoinstall", action="store_true", help="attach a NoCloud seed to one fresh Ubuntu VM; requires --nat")
    stop = sub.add_parser("down", help="request guest shutdown and retain disks")
    stop.add_argument("roles", nargs="*", metavar="ROLE", help="dc, soc, app, win11; omit to stop all")
    stop.add_argument("--wait", type=int, default=60, metavar="SECONDS")
    stop.add_argument("--force", action="store_true", help="cut guest power after the grace period; may lose data")
    overview = sub.add_parser("status", help="show VM, network, disk and media status")
    overview.add_argument("roles", nargs="*", metavar="ROLE", help="dc, soc, app, win11; omit for all")
    diagnosis = sub.add_parser("doctor", help="diagnose host tools, media, Docker, QEMU and guest agents")
    diagnosis.add_argument("roles", nargs="*", metavar="ROLE", help="dc, soc, app, win11; omit for all")
    logs = sub.add_parser("logs", help="show recent launch and QEMU logs, or the hub log")
    logs.add_argument("role", choices=(*VMS, HUB))
    logs.add_argument("--lines", type=int, default=25, metavar="N", help="lines per file, 1–1000 (default: 25)")
    console = sub.add_parser("console", help="open a running VM's local graphical console")
    console.add_argument("role", choices=VMS)
    args = parser.parse_args()
    roles = list(dict.fromkeys(getattr(args, "roles", []) or VMS))
    if invalid := set(roles) - VMS.keys():
        parser.error(f"unknown role(s): {', '.join(sorted(invalid))}; choose {', '.join(VMS)}")
    if getattr(args, "wait", 0) < 0:
        parser.error("--wait must be non-negative")
    if not 1 <= getattr(args, "lines", 25) <= 1000:
        parser.error("--lines must be between 1 and 1000")
    state = args.state.expanduser().resolve()
    if args.command in (None, "guide"):
        guide(state, getattr(args, "role", None))
        return
    if args.command == "check":
        progress("Checking Quickemu, QEMU networking and hardware virtualization…")
        try:
            check_host()
        except (LabError, OSError, subprocess.SubprocessError) as error:
            raise LabError(f"{error}\n  For a full report: {cli('doctor', state=state)}") from error
        print(f"Host launch prerequisites passed. Next: {cli('prepare', '--download', state=state)}")
        return
    if args.command == "build-controller":
        build_controller(args.sudo_docker)
        return
    os.umask(0o077)
    read_only = args.command in ("status", "doctor", "logs", "console")
    if not read_only:
        state.mkdir(mode=0o700, parents=True, exist_ok=True)
    if state.exists():
        info = state.stat()
        if not state.is_dir() or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
            raise LabError(f"State must be a directory owned by you with private permissions (chmod 700): {state}")
        os.chdir(state)
    if args.command == "status":
        status(state, roles)
        return
    if args.command == "doctor":
        return doctor(state, roles)
    if args.command == "logs":
        show_logs(state, args.role, args.lines)
        return
    if args.command == "console":
        if not state.is_dir() or not running(args.role):
            raise LabError(f"{args.role} is stopped. Start it with {cli('up', args.role, state=state)}.")
        endpoint = state / f"v/{args.role}/ql-{args.role}.sock"
        if not endpoint.is_socket():
            raise LabError(f"Console socket missing for {args.role}. Inspect {cli('logs', args.role, state=state)}.")
        progress(f"Opening the local console for {args.role}. Closing it will not stop the guest.")
        try:
            subprocess.run([tool("spicy"), "--uri", f"spice+unix://{endpoint}", "--title", f"ql-{args.role}"], check=True)
        except subprocess.CalledProcessError as error:
            raise LabError(f"Could not open the graphical viewer. Check your desktop session and {cli('doctor', args.role, state=state)}.") from error
        return
    Path("r").mkdir(exist_ok=True)
    with Path("r/lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if args.command == "prepare":
            prepare(args.download)
        elif args.command == "seed":
            ubuntu_seed(args.role)
        elif args.command == "provision":
            provision(args.role, args.sudo_docker, args.local)
        elif args.command == "up":
            up(roles, args.nat, args.autoinstall)
        elif args.command == "down":
            down(roles, args.wait, args.force)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except BlockingIOError:
        sys.exit("QuickLab: another command is using this lab. Wait for it to finish; status and logs are still available.")
    except (LabError, OSError, ValueError, subprocess.SubprocessError) as error:
        sys.exit(f"QuickLab: {error}")
    except KeyboardInterrupt:
        sys.exit(130)
