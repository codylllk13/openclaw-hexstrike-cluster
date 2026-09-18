#!/usr/bin/env bash
# Finish a previously-started Kali tools install and restore the HexStrike API.
set -euo pipefail

container="${HEXSTRIKE_CONTAINER:-kali-hexstrike}"
install_unit="${HEXSTRIKE_INSTALL_UNIT:-hexstrike-kali-install.service}"
report="${HEXSTRIKE_FINALIZE_REPORT:-$HOME/.local/state/hexstrike-agent/finalize-status}"
policy="/usr/sbin/policy-rc.d"
policy_backup="/usr/sbin/policy-rc.d.hexstrike-before-install"
policy_marker="# hexstrike-install-temporary-policy"

mkdir -p "$(dirname "$report")"
chmod 700 "$(dirname "$report")"

while systemctl --user is-active --quiet "$install_unit"; do
    sleep 15
done

restore_api() {
    if lxc exec "$container" -- test -e "$policy_backup"; then
        lxc exec "$container" -- mv -f "$policy_backup" "$policy" || true
    elif lxc exec "$container" -- grep -Fqx "$policy_marker" "$policy" 2>/dev/null; then
        lxc exec "$container" -- rm -f "$policy" || true
    fi
    lxc exec "$container" -- systemctl daemon-reload || true
    lxc exec "$container" -- systemctl restart hexstrike-api.service || true
}
trap restore_api EXIT

lxc exec "$container" -- env \
    DEBIAN_FRONTEND=noninteractive APT_LISTCHANGES_FRONTEND=none NEEDRESTART_MODE=a \
    dpkg --configure -a
lxc exec "$container" -- env \
    DEBIAN_FRONTEND=noninteractive APT_LISTCHANGES_FRONTEND=none NEEDRESTART_MODE=a \
    apt-get -y --no-download --no-install-recommends -f install
lxc exec "$container" -- apt-get check

meta_status="$(lxc exec "$container" -- dpkg-query -W -f='${db:Status-Abbrev}' kali-linux-everything)"
if [[ "$meta_status" != "ii " ]]; then
    printf 'kali-linux-everything status is %q\n' "$meta_status" >&2
    exit 1
fi
if [[ -n "$(lxc exec "$container" -- dpkg --audit)" ]]; then
    printf 'dpkg audit reported unfinished package state\n' >&2
    exit 1
fi

restore_api
trap - EXIT

for _ in {1..30}; do
    if curl -fsS http://127.0.0.1:8888/health >/dev/null; then
        printf 'complete\n' >"$report"
        chmod 600 "$report"
        exit 0
    fi
    sleep 2
done

printf 'HexStrike API did not become healthy after package installation\n' >&2
exit 1
