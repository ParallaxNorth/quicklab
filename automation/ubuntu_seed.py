"""Build a private NoCloud seed for a new QuickLab Ubuntu Server VM."""

import getpass
import re
import shutil
import subprocess
import tempfile
import uuid
import warnings
from pathlib import Path


_SHA512_CRYPT = re.compile(rb"\$6\$[./0-9A-Za-z]{1,16}\$[./0-9A-Za-z]{86}")


def _require_terminal():
    try:
        with open("/dev/tty", "rb", buffering=0) as tty:
            if tty.isatty():
                return
    except OSError:
        pass
    raise RuntimeError("Creating an Ubuntu seed requires a controlling terminal")


def _password_hash(openssl: str, hostname: str) -> str:
    with warnings.catch_warnings():
        # Even with a TTY, getpass must not fall back if it cannot disable echo.
        warnings.simplefilter("error", getpass.GetPassWarning)
        try:
            password = getpass.getpass(f"Password for labadmin on {hostname}: ")
            confirmation = getpass.getpass("Confirm password: ")
        except getpass.GetPassWarning:
            raise RuntimeError("Cannot read a password with terminal echo disabled") from None
    if password != confirmation:
        raise ValueError("Passwords do not match")
    encoded = password.encode("utf-8")
    if not 1 <= len(encoded) <= 256 or any(c in encoded for c in (b"\x00", b"\n", b"\r")):
        raise ValueError("Use a password of 1–256 UTF-8 bytes with no NUL or line breaks")

    # OpenSSL chooses a random salt when -salt is omitted. Never send a password
    # via argv or disk, or allow OpenSSL to silently truncate a long password.
    result = subprocess.run([openssl, "passwd", "-6", "-stdin"], input=encoded + b"\n",
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    digest = result.stdout.rstrip(b"\r\n")
    if result.returncode or not _SHA512_CRYPT.fullmatch(digest):
        raise RuntimeError("OpenSSL failed to generate a SHA-512 crypt password hash")
    return digest.decode("ascii")


def make_seed(role: str, destination: Path) -> None:
    """Prompt for labadmin's password and atomically publish a 0600 seed ISO.

    The destination's parent must exist. Use only for a new app/soc VM with
    a single fresh disk: Subiquity's direct layout repartitions the whole disk.
    Attach the CIDATA ISO alongside the stock Ubuntu Server 24.04 ISO. At the
    stock GRUB menu, edit the install entry and add ``autoinstall`` to the
    kernel line before ``---`` to skip Subiquity's confirmation prompt. No
    ``ds=`` argument or boot ISO remaster is needed for a local CIDATA CD.

    NoCloud expects root-level meta-data and user-data; when delivered by
    cloud-init, Subiquity needs the #cloud-config header and an autoinstall
    wrapper. The installer uses its stock ISO source and default single-NIC
    DHCP; the launcher must provide NAT for the apt mirror and package install.
    On success Subiquity powers off instead of rebooting. Detach the seed before
    booting the installed VM, and keep it private (it contains a password hash).
    """
    if role not in ("app", "soc"):
        raise ValueError("Ubuntu seeds are only supported for app and soc")
    _require_terminal()
    openssl = shutil.which("openssl")
    if not openssl:
        raise RuntimeError("Creating an Ubuntu seed requires openssl")
    if xorriso := shutil.which("xorriso"):
        iso_command = [xorriso, "-as", "mkisofs"]
    elif genisoimage := shutil.which("genisoimage"):
        iso_command = [genisoimage]
    else:
        raise RuntimeError("Creating an Ubuntu seed requires xorriso or genisoimage")

    # Staging next to the destination keeps the final replace on one filesystem.
    with tempfile.TemporaryDirectory(prefix=".ubuntu-seed-", dir=destination.parent) as tmp:
        stage = Path(tmp)
        hostname = f"ql-{role}"
        password_hash = _password_hash(openssl, hostname)
        meta_data = f"instance-id: {hostname}-{uuid.uuid4()}\nlocal-hostname: {hostname}\n"
        user_data = f"""#cloud-config
autoinstall:
  version: 1
  identity:
    hostname: {hostname}
    username: labadmin
    password: '{password_hash}'
  storage:
    layout:
      name: direct
  ssh:
    install-server: false
    allow-pw: false
  apt:
    fallback: abort
  packages:
    - qemu-guest-agent
  shutdown: poweroff
"""
        for name, data in (("meta-data", meta_data), ("user-data", user_data)):
            path = stage / name
            path.touch(mode=0o600, exist_ok=False)
            path.write_text(data, encoding="utf-8")

        image = stage / "seed.iso"
        image.touch(mode=0o600, exist_ok=False)
        subprocess.run(iso_command + ["-quiet", "-V", "CIDATA", "-J", "-R", "-o", image.name,
                                      "meta-data", "user-data"], cwd=stage,
                       stdin=subprocess.DEVNULL, check=True)
        image.chmod(0o600)
        image.replace(destination)
