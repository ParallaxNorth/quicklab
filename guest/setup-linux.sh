#!/usr/bin/env bash
set -Eeuo pipefail
umask 022

usage() {
    cat <<'EOF'
Usage: sudo bash /path/to/guest/setup-linux.sh app|soc
  app  ql-app  10.77.0.30/24  52:54:00:77:00:30  HTTP/80, sends logs
  soc  ql-soc  10.77.0.40/24  52:54:00:77:00:40  receives TCP/5514
Run from the Ubuntu 24.04 VM console. If packages are missing, attach
single-VM NAT first. After setup, power off before switching to the lab LAN.
EOF
}

die() { printf 'Quicklab: %s\n' "$*" >&2; exit 1; }

if [[ $# == 1 && ( $1 == -h || $1 == --help ) ]]; then
    usage
    exit 0
fi
[[ $# == 1 ]] || { usage >&2; exit 2; }
case $1 in
    app) host=ql-app; address=10.77.0.30; mac=52:54:00:77:00:30 ;;
    soc) host=ql-soc; address=10.77.0.40; mac=52:54:00:77:00:40 ;;
    *) usage >&2; exit 2 ;;
esac
role=$1
[[ $EUID == 0 ]] || die 'Run with sudo.'
. /etc/os-release
[[ $ID == ubuntu && $VERSION_ID == 24.04 ]] || die 'Expected Ubuntu 24.04.'

nic=''
for file in /sys/class/net/*/address; do
    [[ $(<"$file") == "$mac" ]] || continue
    [[ -z $nic ]] || die "Multiple interfaces match $mac."
    nic=${file%/address}
    nic=${nic##*/}
done
[[ -n $nic ]] || die "No NIC with $mac; check the VM role and MAC."
printf 'Setting up %s on %s (%s)\n' "$host" "$nic" "$mac"

scratch=$(mktemp -d)
unchecked=()
cleanup() {
    if ((${#unchecked[@]})); then rm -f -- "${unchecked[@]}"; fi
    rm -rf -- "$scratch"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
trap 'printf "Quicklab: setup failed at line %s (status %s).\n" "$LINENO" "$?" >&2' ERR

# Do not replace admin edits (or follow symlinks) at a managed path.
check_managed() {
    local source=$1 target=$2
    if [[ -e $target || -L $target ]]; then
        [[ ! -L $target && -f $target ]] && cmp -s -- "$source" "$target" ||
            die "Not overwriting $target; review it and move it aside if appropriate."
    fi
}

install_managed() {
    local source=$1 target=$2 mode=$3
    check_managed "$source" "$target"
    if [[ ! -e $target ]]; then
        install -o root -g root -m "$mode" -- "$source" "$target"
    fi
}

# Delete only newly installed service snippets if their syntax check fails.
install_unchecked() {
    local source=$1 target=$2
    check_managed "$source" "$target"
    if [[ ! -e $target ]]; then
        unchecked+=("$target")
        install_managed "$source" "$target" 0644
    fi
}

packages=(netplan.io python3-yaml rsyslog)
case $role in
    app) packages+=(nginx curl) ;;
    soc) packages+=(logrotate) ;;
esac
missing=()
for package in "${packages[@]}"; do
    status=$(dpkg-query -W -f='${Status}' "$package" 2>/dev/null) || status=''
    [[ $status == 'install ok installed' || $status == 'hold ok installed' ]] || missing+=("$package")
done
if ((${#missing[@]})); then
    printf 'Installing %s (requires temporary Ubuntu repository access via NAT)\n' "${missing[*]}"
    apt-get -o APT::Update::Error-Mode=any update
    DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends "${missing[@]}"
fi

# A later Netplan file does not reliably replace another definition of the
# same NIC. Only retire recognized installer files, after inspecting them.
netplan_file=/etc/netplan/90-quicklab.yaml
install -d -m 0700 "$scratch/netplan/etc/netplan"
netplan_candidate=$scratch/netplan/etc/netplan/90-quicklab.yaml
cat > "$netplan_candidate" <<EOF
network:
  version: 2
  renderer: networkd
  ethernets:
    quicklab-lan:
      match:
        macaddress: "$mac"
      addresses: [$address/24]
      dhcp4: false
      dhcp6: false
      accept-ra: false
      link-local: []
      optional: true
      nameservers:
        addresses: [10.77.0.10]
        search: [lab.test]
EOF
chmod 0600 "$netplan_candidate"
check_managed "$netplan_candidate" "$netplan_file"
shopt -s nullglob
installer_files=()
for file in /lib/netplan/*.yaml /etc/netplan/*.yaml /run/netplan/*.yaml; do
    case $file in
        "$netplan_file") continue ;;
        /etc/netplan/00-installer-config.yaml|/etc/netplan/50-cloud-init.yaml) ;;
        *) die "Unmanaged Netplan file $file; reconcile it manually from the VM console." ;;
    esac
    [[ -f $file && ! -L $file ]] || die "Not moving non-regular Netplan file $file."
    backup=$file.quicklab-backup
    [[ ! -e $backup && ! -L $backup ]] || die "Backup $backup already exists; reconcile $file manually."
    installer_files+=("$file")
done
shopt -u nullglob
if ((${#installer_files[@]})); then
    python3 - "$nic" "$mac" "${installer_files[@]}" <<'PY'
import sys
import yaml

nic, mac, *paths = sys.argv[1:]


def is_installer_dhcp(data):
    if not isinstance(data, dict) or set(data) != {'network'}:
        return False
    network = data['network']
    if (not isinstance(network, dict) or network.get('version') != 2
            or network.get('renderer', 'networkd') != 'networkd'
            or set(network) - {'version', 'renderer', 'ethernets'}):
        return False
    ethernets = network.get('ethernets')
    if not isinstance(ethernets, dict) or len(ethernets) != 1:
        return False
    (name, config), = ethernets.items()
    if (not isinstance(config, dict) or config.get('dhcp4') is not True
            or config.get('renderer', 'networkd') != 'networkd'
            or config.get('set-name', nic) != nic
            or set(config) - {'dhcp4', 'dhcp6', 'match', 'set-name', 'optional', 'renderer'}):
        return False
    if 'match' not in config:
        return name == nic
    match = config['match']
    if not isinstance(match, dict) or not match or set(match) - {'macaddress', 'name'}:
        return False
    return str(match.get('macaddress', mac)).lower() == mac and match.get('name', nic) == nic


for path in paths:
    try:
        with open(path, encoding='utf-8') as source:
            recognized = is_installer_dhcp(yaml.safe_load(source))
    except (OSError, yaml.YAMLError) as exc:
        sys.exit(f'Quicklab: cannot inspect {path}: {exc}')
    if not recognized:
        sys.exit(f'Quicklab: {path} is not simple DHCP for {nic}; reconcile it manually.')
PY
fi
netplan generate --root-dir "$scratch/netplan"

# Keep cloud-init from restoring DHCP or replacing the hostname on reboot.
if [[ -d /etc/cloud/cloud.cfg.d ]]; then
    cat > "$scratch/cloud.cfg" <<'EOF'
preserve_hostname: true
manage_etc_hosts: false
network:
  config: disabled
EOF
    install_managed "$scratch/cloud.cfg" /etc/cloud/cloud.cfg.d/99-quicklab.cfg 0644
fi

# Keep installer aliases/comments while making the new name locally resolvable.
# Refuse to duplicate a short name or FQDN that already maps to another address.
python3 - "$host" <<'PY'
from pathlib import Path
import sys

path = Path('/etc/hosts')
if path.is_symlink() or not path.is_file():
    sys.exit('Quicklab: expected a regular /etc/hosts; update it manually.')
host, = sys.argv[1:]
names = [f'{host}.lab.test', host]
original = path.read_text(encoding='utf-8')
lines = original.splitlines(keepends=True)
loopback = None
for index, line in enumerate(lines):
    entry = line.partition('#')[0].split()
    if not entry:
        continue
    if entry[0] == '127.0.1.1':
        if loopback is None:
            loopback = index
    elif set(names).intersection(alias.lower() for alias in entry[1:]):
        sys.exit(f'Quicklab: conflicting /etc/hosts entry for {host}; reconcile it manually.')
if loopback is None:
    updated = original + ('\n' if original and not original.endswith('\n') else '')
    updated += '127.0.1.1\t' + ' '.join(names) + '\n'
else:
    body, marker, comment = lines[loopback].partition('#')
    aliases = [alias for alias in body.split()[1:] if alias.lower() not in names]
    lines[loopback] = '127.0.1.1\t' + ' '.join(names + aliases)
    lines[loopback] += '\t' + marker + comment.rstrip('\n') + '\n' if marker else '\n'
    updated = ''.join(lines)
if updated != original:
    path.write_text(updated, encoding='utf-8')
PY
if [[ $(< /etc/hostname) != "$host" || $(hostname) != "$host" ]]; then
    hostnamectl set-hostname "$host"
fi

if [[ $role == app ]]; then
    install -d -m 0755 /var/www/quicklab
    cat > "$scratch/index.html" <<'EOF'
<!doctype html>
<html lang="en">
<head><meta charset="utf-8"><title>Quicklab Portal</title></head>
<body>
  <h1>Quicklab Portal</h1>
  <p>ql-app is serving this static page on the isolated lab LAN.</p>
</body>
</html>
EOF
    install_managed "$scratch/index.html" /var/www/quicklab/index.html 0644
    cat > "$scratch/nginx.conf" <<'EOF'
server {
    listen 80;
    server_name ql-app ql-app.lab.test 10.77.0.30;
    root /var/www/quicklab;
    index index.html;
    access_log /var/log/nginx/quicklab-access.log;
    error_log /var/log/nginx/quicklab-error.log warn;
}
EOF
    # Use the Ubuntu nginx rotation policy, which already covers these files.
    for stream in access error; do
        file=/var/log/nginx/quicklab-$stream.log
        if [[ ! -e $file && ! -L $file ]]; then
            install -o www-data -g adm -m 0640 /dev/null "$file"
        fi
    done
    install_unchecked "$scratch/nginx.conf" /etc/nginx/conf.d/quicklab-portal.conf
    nginx -t
    unchecked=()

    cat > "$scratch/rsyslog.conf" <<'EOF'
module(load="imfile")

ruleset(name="quicklab_forward") {
    action(type="omfwd" target="10.77.0.40" port="5514" protocol="tcp"
           template="RSYSLOG_SyslogProtocol23Format"
           action.resumeRetryCount="-1"
           queue.type="LinkedList" queue.filename="quicklab-forward"
           queue.size="10000" queue.maxDiskSpace="64m" queue.saveOnShutdown="on")
}

input(type="imfile" File="/var/log/nginx/quicklab-access.log"
      Tag="quicklab-nginx-access:" Facility="local6" Severity="info"
      PersistStateInterval="1" Ruleset="quicklab_forward")
input(type="imfile" File="/var/log/nginx/quicklab-error.log"
      Tag="quicklab-nginx-error:" Facility="local6" Severity="warning"
      PersistStateInterval="1" Ruleset="quicklab_forward")

# Local syslog also proceeds to Ubuntu's normal /var/log destinations.
call quicklab_forward
EOF
    install_unchecked "$scratch/rsyslog.conf" /etc/rsyslog.d/40-quicklab-forward.conf
else
    install -d -o syslog -g adm -m 0750 /var/log/quicklab/remote/ql-app
    cat > "$scratch/rsyslog.conf" <<'EOF'
module(load="imtcp")

ruleset(name="quicklab_receive") {
    # Use the socket peer, not the sender-supplied hostname, to select storage.
    if ($fromhost-ip != "10.77.0.30") then stop

    if ($programname == "quicklab-nginx-access") then {
        action(type="omfile" file="/var/log/quicklab/remote/ql-app/nginx-access.log"
               fileCreateMode="0640" fileGroup="adm")
    } else if ($programname == "quicklab-nginx-error") then {
        action(type="omfile" file="/var/log/quicklab/remote/ql-app/nginx-error.log"
               fileCreateMode="0640" fileGroup="adm")
    } else if ($syslogfacility-text == "auth" or $syslogfacility-text == "authpriv") then {
        action(type="omfile" file="/var/log/quicklab/remote/ql-app/auth.log"
               fileCreateMode="0640" fileGroup="adm")
    } else {
        action(type="omfile" file="/var/log/quicklab/remote/ql-app/syslog.log"
               fileCreateMode="0640" fileGroup="adm")
    }
}

# Listen independently of link readiness; the dedicated ruleset accepts only ql-app.
input(type="imtcp" Address="0.0.0.0" Port="5514" Ruleset="quicklab_receive")
EOF
    install_unchecked "$scratch/rsyslog.conf" /etc/rsyslog.d/40-quicklab-collector.conf

    cat > "$scratch/logrotate" <<'EOF'
/var/log/quicklab/remote/ql-app/*.log {
    su syslog adm
    daily
    maxsize 20M
    rotate 7
    missingok
    notifempty
    compress
    delaycompress
    create 0640 syslog adm
    sharedscripts
    postrotate
        /usr/bin/systemctl kill --kill-who=main -s HUP rsyslog.service
    endscript
}
EOF
    install_unchecked "$scratch/logrotate" /etc/logrotate.d/quicklab-remote
    logrotate --debug /etc/logrotate.d/quicklab-remote
fi
rsyslogd -N1
unchecked=()
systemctl enable rsyslog.service
systemctl restart rsyslog.service
if [[ $role == app ]]; then
    systemctl enable nginx.service
    systemctl reload-or-restart nginx.service
fi

# Activate the isolated network last. Don't remove routes by hand: Netplan
# must own the complete configuration and bring it back up on the next boot.
network_ready() {
    local addresses ipv4_default ipv6_default
    addresses=$(ip -4 -o addr show dev "$nic" scope global) || return 1
    ipv4_default=$(ip -4 route show table all default) || return 1
    ipv6_default=$(ip -6 route show table all default) || return 1
    awk -v expected="$address/24" 'END { exit !(NR == 1 && $4 == expected) }' <<< "$addresses" &&
        [[ -z $ipv4_default && -z $ipv6_default ]]
}

apply=false
if [[ ! -e $netplan_file ]] || ((${#installer_files[@]})); then apply=true; fi
install_managed "$netplan_candidate" "$netplan_file" 0600
for file in "${installer_files[@]}"; do
    mv -T -- "$file" "$file.quicklab-backup"
    printf 'Preserved installer configuration as %s.quicklab-backup\n' "$file"
done
netplan generate
if [[ $apply == true ]] || ! network_ready; then
    netplan apply
    for ((attempt = 0; attempt < 15; attempt++)); do
        if network_ready; then break; fi
        sleep 1
    done
fi
network_ready || die "Expected $address/24 on $nic and no default route; inspect 'ip addr' and 'ip route' from the console."

printf '\n%s is configured on %s at %s/24, with DNS 10.77.0.10 (lab.test) and no default route.\n' "$host" "$nic" "$address"
if [[ $role == soc ]]; then
    printf 'Remote logs: /var/log/quicklab/remote/ql-app/ (TCP/5514, unencrypted)\n'
fi
printf 'Power off before switching from temporary NAT to the isolated lab LAN.\n'
