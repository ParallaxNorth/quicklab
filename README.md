# QuickLab: a local enterprise and SOC lab

QuickLab uses Quickemu to run four persistent VMs on a Linux desktop. Install the operating systems and apply the supplied guest scripts once; `up` and `down` then reuse the same disks. No Proxmox, cloud account, or privileged bridge is required.

| Launcher role | Hostname | Address | Guest and purpose | RAM / vCPU / disk cap |
| --- | --- | --- | --- | --- |
| `dc` | `ql-dc` | `10.77.0.10` | Windows Server 2022, Active Directory Domain Services and DNS for `lab.test` (`LAB`) | 4 GiB / 2 / 64 GiB |
| `win11` | `ql-win11` | `10.77.0.20` | Windows 11 Pro, domain-joined employee workstation | 4 GiB / 2 / 80 GiB |
| `app` | `ql-app` | `10.77.0.30` | Ubuntu Server 24.04 LTS, sample nginx intranet portal and log forwarder | 2 GiB / 2 / 24 GiB |
| `soc` | `ql-soc` | `10.77.0.40` | Ubuntu Server 24.04 LTS, rsyslog collector with per-stream files and rotation | 2 GiB / 2 / 32 GiB |

The default `/24` LAN runs through QEMU's userspace Ethernet hub over Unix sockets. It has no DHCP, gateway, host IP, LAN bridge, or forwarded ports. Access guests with local SPICE consoles; access the portal from another guest, not the host browser. This is a single flat segment, not an inter-VLAN firewall. The SOC ingests Linux app/auth/system logs; it does **not** collect Windows events by default and is not a full SIEM. Use trusted guests and lab-only data and credentials.

**New here?** Run `./quicklab.py` for a short setup guide, or `./quicklab.py guide soc` for the first Ubuntu guest. `./quicklab.py --help` lists all commands.

## 1. Check the host and prepare media

Install the prerequisites using your distribution's packages:

- **Host:** x86-64 Linux with hardware virtualization enabled and read/write access to `/dev/kvm` for your user. Run QuickLab as your regular user, not with `sudo`.
- **Virtualization and console:** Python 3.9+, Quickemu 4.9.9 or a compatible newer build, QEMU with SPICE and the `stream`/`hubport` backends (`reconnect-ms` support), OVMF firmware with Secure Boot support, `swtpm`, `socat`, and the `spicy` SPICE viewer.
- **Installation media:** `xorriso` or `genisoimage` to build setup CDs; `quickget` and `curl` if using `--download`; `openssl` if creating Ubuntu autoinstall seeds.
- **Resources:** The four guests use 12 GiB of RAM combined. Plan for at least 16 GiB of host RAM (24 GiB is more comfortable) and roughly 100 GiB of free disk space to get started. VM disks grow on demand.

From the project directory:

```bash
./quicklab.py check
./quicklab.py prepare --download
```

The ISOs are multi-gigabyte downloads. To supply your own media, or if a download fails, run `./quicklab.py prepare` without `--download`, then place the official ISOs in the reported media directory (`.quicklab/media/` by default):

| Filename | Source |
| --- | --- |
| `ubuntu.iso` | [Ubuntu Server 24.04 LTS amd64](https://releases.ubuntu.com/24.04/) (shared by both Linux guests) |
| `server2022.iso` | [Windows Server 2022 Evaluation](https://www.microsoft.com/en-us/evalcenter/evaluate-windows-server-2022) or licensed Windows Server 2022 media |
| `win11.iso` | [Windows 11 x64 multi-edition ISO](https://www.microsoft.com/software-download/windows11); install **Pro**, not Home or Pro N |
| `virtio-win.iso` | [Stable signed VirtIO Windows drivers](https://fedorapeople.org/groups/virt/virtio-win/direct-downloads/stable-virtio/virtio-win.iso) |

Compare each ISO against the vendor's published checksum when available, even if you used `--download`. QuickLab's own media check recognizes the file format; it does not verify checksums or signatures. Automated checksum validation is tracked in [TODO.md](TODO.md). Use evaluation and installation media within their license terms.

`prepare` also builds a read-only `QUICKLAB` CD containing the guest setup scripts. It does not install an operating system.

## 2. One-time guest installation and Docker provisioning

The primary provisioning method uses a **disposable Ansible container on the local rootful Docker daemon**. The container runs as your UID/GID with networking disabled and only the selected VM's QEMU Guest Agent socket mounted. It does not mount the Docker socket or VM disks, and does not open SSH or WinRM listeners.

```bash
./quicklab.py build-controller
```

If your user needs `sudo` to access the system Docker daemon (`/var/run/docker.sock`), add `--sudo-docker` to `build-controller` and each `provision` command. The flag elevates only Docker; Quickemu and the VM files stay under your regular account. The initial build downloads the Python base image and Ansible. Each provisioning run removes its container on exit. Rebuild after changes to `automation/`.

### Ubuntu app and SOC: NoCloud installation, then Ansible

Run this for `soc` first, then repeat for `app`. It requires `openssl` on the host and a new VM disk. If you already installed Ubuntu, skip the seed steps and install the guest agent from the guest console with `sudo apt update && sudo apt install qemu-guest-agent` while that guest is on temporary NAT.

```bash
./quicklab.py seed soc              # prompts privately for the labadmin password
./quicklab.py up soc --nat --autoinstall
./quicklab.py console soc
```

In the stock Ubuntu GRUB menu, highlight the install entry, press `e`, add `autoinstall` to the `linux` line **before** `---`, and press `Ctrl+X` or `F10`. From there Subiquity installs Ubuntu and the guest agent onto that VM's fresh disk and powers off when finished. `--autoinstall` refuses an existing VM disk; do not use it on disks you want to keep. The role's seed ISO contains a password hash; keep it private or remove it after installation. It is not attached on ordinary starts.

```bash
./quicklab.py up soc --nat           # boot the installed OS without the seed
./quicklab.py provision soc
./quicklab.py down soc
```

The guest needs NAT for any missing Ubuntu packages. Provisioning switches it to its static lab IP with no default route. Repeat for `app`, then use normal `up` (without `--nat`) for private operation. Use temporary NAT only with trusted install media: it allows the selected guest to reach the host, LAN, and internet.

### Windows: install from ISO, then provision with Ansible

Windows installation and first-run setup (OOBE) are **manual**. Follow the Windows installation notes below for edition, drivers, and any temporary NAT needs. In each installed guest, install the signed `guest-agent\qemu-ga-x86_64.msi` from the VirtIO CD as Administrator and verify its service is running (`Get-Service QEMU-GA`). If installing on an older QuickLab state, run `./quicklab.py prepare` with all VMs stopped to rebuild the `QUICKLAB` CD.

```bash
./quicklab.py up dc
./quicklab.py provision dc
./quicklab.py up win11
./quicklab.py provision win11
```

Run in an interactive terminal. The playbook prompts without echo for the DC recovery password or the domain join credentials. It handles the Windows renames, AD promotion/join, and required restarts. Keep both guests on the private LAN while provisioning Windows, and finish the DC before the employee machine. Only credentials for this lab belong in these prompts.

If the guest-agent check fails, confirm the **installed OS** is booted and that the agent is running. If Docker reports a socket permission error, use `--sudo-docker` from your terminal rather than changing socket permissions. `provision ROLE --local` is an optional fallback if `ansible-playbook` is installed on the host.

### Manual setup and Windows installation notes

Use `./quicklab.py console ROLE` in a second terminal or launch it after `up`. For each role, install onto its VM disk. During reboots, boot from the disk, not back into the installer. The guest scripts below can also be run directly to provision or troubleshoot without Ansible.

**Windows 11:** Start it on its own (`./quicklab.py up win11`). If your Windows build requires internet for OOBE, start it instead with `./quicklab.py up win11 --nat` while all VMs are stopped. At the disk selection screen load `viostor\w11\amd64` from the VirtIO CD if no drive appears. Load `NetKVM\w11\amd64` using the OOBE driver option or install it in Windows Device Manager if the NIC is missing. Use the Pro setup's work/school and local administrator options if offered; Microsoft changes OOBE across releases. Don't enroll the VM in a real organization. If you used NAT, shut down before configuring the lab address. You will join the domain after the DC is ready.

**Ubuntu app and SOC, one at a time:** Start with `./quicklab.py up soc --nat` or `./quicklab.py up app --nat`. Install a clean Ubuntu Server using installer DHCP on its one wired NIC, not a custom bridge, and boot into the installed OS. Mount the `QUICKLAB` CD, then run the matching command from the guest console:

```bash
sudo mkdir -p /mnt/quicklab
sudo mount -o ro /dev/disk/by-label/QUICKLAB /mnt/quicklab
sudo bash /mnt/quicklab/guest/setup-linux.sh soc  # use app on ql-app
sudo poweroff
```

The Linux script installs packages before switching to static addressing. If `by-label` is unavailable, use `lsblk -f` to identify the `QUICKLAB` CD. **NAT is for trusted installation and updates only:** the single guest can reach the host, LAN and internet. It has no inbound port forwards and cannot use the private hub at the same time. Shut down the guest before starting any other role or returning to private mode.

**Windows Server and AD:** Start the DC privately with `./quicklab.py up dc`. Use the **Desktop Experience** option for an easier first install. The Server disk uses a built-in storage driver. Install the VirtIO network driver (`NetKVM\2k22\amd64` on the driver CD) if the NIC is missing. Find the `QUICKLAB` CD in Explorer or with `Get-Volume`, open elevated 64-bit Windows PowerShell, and substitute its drive letter for `E:` below. `-ExecutionPolicy Bypass` applies only to this child process; it doesn't change machine policy.

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File E:\guest\setup-windows.ps1 -Role DC
```

When prompted, run `Restart-Computer` and rerun the same setup command until ready. After promotion sign in as `LAB\Administrator`. The script prompts for a separate Directory Services Restore Mode password. Optionally create a standard employee account for normal use:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File E:\guest\setup-windows.ps1 -Role DC -Action CreateLabUser -UserName lab-user
```

Once AD and DNS are ready, start `win11` privately (`./quicklab.py up win11`) and run this from elevated Windows PowerShell on the workstation. Use lab domain credentials authorized to join machines when prompted, then reboot and rerun until ready:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File E:\guest\setup-windows.ps1 -Role Employee
```

## 3. Everyday use and smoke checks

```bash
./quicklab.py up              # all roles, private LAN
./quicklab.py status
./quicklab.py console win11   # close the viewer without stopping the VM
./quicklab.py down            # guest ACPI shutdown, waits up to 60 seconds
```

`up app soc` and `down win11` operate on selected roles. `up` reports when QEMU starts, not when the guest OS or AD is ready. If `down` times out, shut down inside the console or use `down --wait 15 --force` as a last resort (unsaved data can be lost).

### Diagnose a lab

```bash
./quicklab.py status                # selected or all VM processes, network mode, disks and media
./quicklab.py status dc             # focus on one role
./quicklab.py doctor                # read-only host, Docker, media and runtime checks
./quicklab.py doctor win11          # focus media and guest checks on win11
./quicklab.py logs win11 --lines 40 # recent Quickemu launcher and QEMU output
./quicklab.py logs hub              # private network hub log
```

These commands also work before the lab exists and do not create state, start a VM, or prompt for sudo. `doctor` exits with status 1 for blocking findings (for example, missing required media or a broken hub while private guests are running). Missing Docker access and a nonresponsive guest agent are warnings. A stopped VM is not a failure; an existing disk does not prove the OS is installed, and a responsive agent does not prove an application or AD is ready. `doctor` skips the guest-agent ping if another lab command owns the state lock.

For a partial launch, start with `status`, then `logs` for the failed role. Startup logs sit beside the VM disks, and `logs` can read them after shutdown. This is host-side output, not guest event logs or Ansible credential prompts. After an interrupted OS install Quickemu may consider the disk bootable and omit its install ISO; use the guest UEFI boot menu if media is still present, or start again with a fresh disk only if you are sure no wanted data is on it.

- On `ql-win11`, `Resolve-DnsName -Type SRV _ldap._tcp.dc._msdcs.lab.test` and `Test-ComputerSecureChannel` check AD DNS and domain membership. Open `http://10.77.0.30/` to check the portal.
- On `ql-app`, run `logger -t quicklab-check 'test event from app'`. On `ql-soc`, check `sudo tail /var/log/quicklab/remote/ql-app/{syslog,nginx-access}.log`. Collector traffic on TCP/5514 is plaintext and intended only for this lab LAN.
- Run `python3 -B -m unittest -v`. To also test QEMU frame forwarding and reconnects without KVM or ISO downloads, run `env QUICKLAB_TEST_HUB=1 python3 -B -m unittest -v` in a shell that allows Unix sockets.

By default, lab data lives in `.quicklab/` next to `quicklab.py`. To choose another location, pass `--state` before the subcommand, for example `./quicklab.py --state /path/to/private-dir status`. Keep the state directory private (`0700`) and on a local filesystem. Back up the entire state with all VMs off to preserve disks, UEFI variables and the Windows 11 vTPM together. There is no destructive cleanup command.

## References

- [Ludus range configuration](https://docs.ludus.cloud/docs/configuration/) — explicit inventory, AD roles, resource sizing, and separate connected vs. isolated modes. QuickLab uses a smaller flat LAN and keeps the machine-local workflow.
- [DetectionLab](https://github.com/clong/DetectionLab) — a domain-based environment with dedicated centralized telemetry. It is no longer maintained; QuickLab takes the layout idea, not its images or credentials.
- [Quickemu](https://github.com/quickemu-project/quickemu) — guest configuration, upstream media tooling and the QEMU launcher.
