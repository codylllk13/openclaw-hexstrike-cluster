#!/usr/bin/env python3
"""Install per-user cluster services on the current machine; no sudo required."""
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--node-id', choices=['workstation', 'server'], required=True)
    p.add_argument('--coordinator-ssh')
    p.add_argument('--coordinator-script')
    p.add_argument('--backend', choices=['codex', 'openclaw'], required=True)
    p.add_argument('--agent-command', required=True)
    p.add_argument('--agent-config')
    p.add_argument('--dashboard', action='store_true')
    p.add_argument('--tunnel', action='store_true')
    p.add_argument('--start', action='store_true')
    a = p.parse_args()
    os.umask(0o077)
    home = Path.home()
    app = home / '.local/share/compute-cluster/app'
    configdir = home / '.config/compute-cluster'
    units = home / '.config/systemd/user'
    for directory in (app, configdir, units, home / '.local/state/compute-cluster', home / '.local/bin'):
        directory.mkdir(parents=True, exist_ok=True)
    configdir.chmod(0o700)
    backup = home / '.local/state/compute-cluster/install-backups' / time.strftime('%Y%m%d-%H%M%S')
    def write(path, content, mode=0o600):
        if path.exists():
            backup.mkdir(parents=True, exist_ok=True, mode=0o700)
            shutil.copy2(path, backup / path.name)
        path.write_text(content)
        path.chmod(mode)
    for name in ('client.py', 'coordinator.py', 'worker.py', 'github_context.py', 'clusterctl.py', 'dashboard.py'):
        write(app / name, Path(__file__).with_name(name).read_text())
    config = dict(node_id=a.node_id, nodes=['workstation', 'server'],
                  coordinator_script=a.coordinator_script or str(app / 'coordinator.py'),
                  backend=a.backend, agent_command=a.agent_command, model='gpt-5.6-sol',
                  poll_seconds=5, lease_seconds=90)
    if a.coordinator_ssh: config['coordinator_ssh'] = a.coordinator_ssh
    if a.agent_config: config['agent_config'] = a.agent_config
    write(configdir / 'config.json', json.dumps(config, indent=2) + '\n')
    write(home / '.local/bin/clusterctl', '#!/bin/sh\nexec /usr/bin/python3 "$HOME/.local/share/compute-cluster/app/clusterctl.py" "$@"\n', 0o700)
    cpu, memory = ('200%', '4G') if a.node_id == 'workstation' else ('300%', '6G')
    worker = f'''[Unit]
Description=Compute cluster worker ({a.node_id})
After=network-online.target

[Service]
Type=simple
ExecStart=/usr/bin/python3 %h/.local/share/compute-cluster/app/worker.py
Environment=PYTHONUNBUFFERED=1
Environment="PATH=%h/.local/share/compute-cluster/codex-path:%h/.local/bin:/usr/local/bin:/usr/bin:/bin"
Restart=always
RestartSec=8
TimeoutStopSec=20
KillMode=control-group
UMask=0077
CPUQuota={cpu}
MemoryMax={memory}
MemorySwapMax=1G
TasksMax=256
OOMPolicy=stop
Nice=5
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=default.target
'''
    write(units / 'compute-cluster-worker.service', worker)
    services = ['compute-cluster-worker.service']
    if a.dashboard:
        write(units / 'compute-cluster-dashboard.service', '''[Unit]
Description=Compute cluster private read-only dashboard
After=network-online.target
[Service]
ExecStart=/usr/bin/python3 %h/.local/share/compute-cluster/app/dashboard.py
Restart=always
RestartSec=5
UMask=0077
MemoryMax=256M
TasksMax=32
[Install]
WantedBy=default.target
''')
        services.append('compute-cluster-dashboard.service')
    if a.tunnel:
        if not a.coordinator_ssh or not all(c.isalnum() or c in '._-' for c in a.coordinator_ssh):
            raise ValueError('Tunnel requires a simple SSH alias')
        write(units / 'compute-cluster-tunnel.service', f'''[Unit]
Description=Compute cluster dashboard SSH tunnel
After=network-online.target
[Service]
ExecStart=/usr/bin/ssh -NT -o BatchMode=yes -o StrictHostKeyChecking=yes -o ConnectTimeout=8 -o ExitOnForwardFailure=yes -o ServerAliveInterval=20 -o ServerAliveCountMax=3 -L 127.0.0.1:18890:127.0.0.1:18890 {a.coordinator_ssh}
Restart=always
RestartSec=8
[Install]
WantedBy=default.target
''')
        services.append('compute-cluster-tunnel.service')
        applications = home / '.local/share/applications'
        applications.mkdir(parents=True, exist_ok=True)
        write(applications / 'compute-cluster.desktop', '''[Desktop Entry]
Type=Application
Name=Compute Cluster
Comment=View both machines and their queued work
Exec=xdg-open http://127.0.0.1:18890
Icon=network-workgroup
Terminal=false
Categories=Development;
''')
    subprocess.run(['systemctl', '--user', 'daemon-reload'], check=True)
    subprocess.run(['systemctl', '--user', 'enable', *services], check=True)
    if a.start: subprocess.run(['systemctl', '--user', 'restart', *services], check=True)
    print('Installed: ' + ', '.join(services))
    print('Enable user lingering for restart after boot if it is not already enabled.')


if __name__ == '__main__':
    main()
