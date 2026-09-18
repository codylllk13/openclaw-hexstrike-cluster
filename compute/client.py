"""Private, SSH-authenticated transport to the coordinator."""
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys


def load_config():
    path = Path(os.environ.get('CLUSTER_CONFIG', '~/.config/compute-cluster/config.json')).expanduser()
    with path.open() as stream:
        config = json.load(stream)
    if not isinstance(config, dict):
        raise RuntimeError('Cluster configuration must be an object')
    return config


def rpc(action, **kwargs):
    config = load_config()
    script = config.get('coordinator_script', str(Path(__file__).with_name('coordinator.py')))
    if config.get('coordinator_ssh'):
        alias = config['coordinator_ssh']
        if not isinstance(alias, str) or alias.startswith('-') or any(c.isspace() for c in alias):
            raise RuntimeError('Invalid SSH alias')
        command = ['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=8',
                   '-o', 'StrictHostKeyChecking=yes', alias,
                   'python3 ' + shlex.quote(script) + ' rpc']
    else:
        command = [sys.executable, script, 'rpc']
    request = json.dumps(dict(kwargs, action=action))
    timeout = 120 if action in ('project_put', 'project_get') else 15
    try:
        result = subprocess.run(command, input=request, text=True, capture_output=True,
                                timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(f'Coordinator unavailable: {type(exc).__name__}') from exc
    try:
        envelope = json.loads(result.stdout)
    except (ValueError, TypeError) as exc:
        raise RuntimeError('Coordinator unavailable or invalid response; check SSH and services') from exc
    if not isinstance(envelope, dict) or type(envelope.get('ok')) is not bool:
        raise RuntimeError('Coordinator returned an invalid response envelope')
    if result.returncode or not envelope.get('ok'):
        raise RuntimeError(str(envelope.get('error', 'Coordinator request failed')))
    return envelope.get('data')
