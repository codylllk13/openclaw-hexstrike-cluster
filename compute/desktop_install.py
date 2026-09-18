#!/usr/bin/env python3
"""Install the workstation's native control window and local console service."""
import os
from pathlib import Path
import shutil
import subprocess
import time


def main():
    os.umask(0o077)
    source = Path(__file__).parent
    home = Path.home()
    app = home / '.local/share/compute-cluster/app'
    backup = home / '.local/state/compute-cluster/install-backups' / ('desktop-' + time.strftime('%Y%m%d-%H%M%S'))
    app.mkdir(parents=True, exist_ok=True)
    def install(src, dst, mode=0o600):
        dst.parent.mkdir(parents=True, exist_ok=True)
        if dst.exists():
            backup.mkdir(parents=True, exist_ok=True, mode=0o700)
            shutil.copy2(dst, backup / dst.name)
        shutil.copy2(src, dst)
        dst.chmod(mode)
    for name in ('console.py', 'desktop.py', 'client.py', 'clusterctl.py'):
        install(source / name, app / name)
    for src in (source / 'desktop').iterdir():
        if src.is_file(): install(src, app / 'desktop' / src.name)
    units = home / '.config/systemd/user'
    units.mkdir(parents=True, exist_ok=True)
    unit = units / 'compute-cluster-console.service'
    if unit.exists():
        backup.mkdir(parents=True, exist_ok=True, mode=0o700)
        shutil.copy2(unit, backup / unit.name)
    unit.write_text('''[Unit]
Description=Cluster Desk private local control service
After=network-online.target
[Service]
ExecStart=/usr/bin/python3 %h/.local/share/compute-cluster/app/console.py
Environment=PYTHONUNBUFFERED=1
Restart=always
RestartSec=5
UMask=0077
MemoryMax=512M
TasksMax=64
[Install]
WantedBy=default.target
''')
    launcher = home / '.local/bin/cluster-desk'
    launcher.parent.mkdir(parents=True, exist_ok=True)
    launcher.write_text('#!/bin/sh\nexec /usr/bin/python3 "$HOME/.local/share/compute-cluster/app/desktop.py" "$@"\n')
    launcher.chmod(0o700)
    entry = f'''[Desktop Entry]
Type=Application
Name=Cluster Desk
Comment=Chat with your cluster, run commands, and follow both machines
Exec={launcher}
Icon={app / 'desktop/icon.svg'}
Terminal=false
Categories=Development;System;
StartupWMClass=ClusterDesk
StartupNotify=true
'''
    entries = [home / '.local/share/applications/compute-cluster.desktop']
    desktop = subprocess.run(['xdg-user-dir', 'DESKTOP'], capture_output=True, text=True, check=False)
    folder = Path(desktop.stdout.strip()) if desktop.returncode == 0 and desktop.stdout.strip() else home / 'Desktop'
    if folder.is_dir() and folder != home:
        entries.append(folder / 'Cluster Desk.desktop')
    for path in entries:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            backup.mkdir(parents=True, exist_ok=True, mode=0o700)
            shutil.copy2(path, backup / path.name)
        path.write_text(entry)
        path.chmod(0o700)
        if path.parent == folder:
            subprocess.run(['gio', 'set', str(path), 'metadata::trusted', 'true'], capture_output=True)
    subprocess.run(['systemctl', '--user', 'daemon-reload'], check=True)
    subprocess.run(['systemctl', '--user', 'enable', '--now', 'compute-cluster-console'], check=True)
    subprocess.run(['systemctl', '--user', 'restart', 'compute-cluster-console'], check=True)
    print('Cluster Desk installed in the applications menu and on the desktop.')


if __name__ == '__main__':
    main()
