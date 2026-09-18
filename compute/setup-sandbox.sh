#!/bin/sh
# Ubuntu sandbox prerequisites from https://learn.chatgpt.com/docs/sandboxing
# Run once as root on the Ubuntu server. No global sysctl is changed.
set -eu
if [ "$(id -u)" -ne 0 ]; then
    echo 'Run this installer with sudo on the Ubuntu server.' >&2
    exit 1
fi
apt-get update
apt-get install -y bubblewrap apparmor-profiles apparmor-utils
profile=/etc/apparmor.d/bwrap-userns-restrict
source_profile=/usr/share/apparmor/extra-profiles/bwrap-userns-restrict
if [ -f "$source_profile" ]; then
    if [ -f "$profile" ] && ! cmp -s "$source_profile" "$profile"; then
        backup_dir=/var/backups/compute-cluster
        install -d -m 0700 "$backup_dir"
        cp -p "$profile" "$backup_dir/bwrap-userns-restrict.$(date +%Y%m%d-%H%M%S)"
    fi
    install -m 0644 "$source_profile" "$profile"
fi
if [ ! -f "$profile" ]; then
    echo 'The packaged AppArmor profile is unavailable; no global restrictions were changed.' >&2
    exit 1
fi
apparmor_parser -r "$profile"
echo 'Ubuntu sandbox helper and profile are installed. No reboot is required.'
