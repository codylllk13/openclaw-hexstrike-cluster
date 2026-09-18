#!/usr/bin/env python3
"""Submit coding and command jobs to the owner's two-machine cluster."""
import argparse
import base64
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time

from client import load_config, rpc

os.umask(0o077)


def state_dir():
    directory = Path(os.environ.get('CLUSTER_STATE', '~/.local/state/compute-cluster')).expanduser()
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    return directory


def registry():
    file = state_dir() / 'projects.json'
    return json.loads(file.read_text()) if file.exists() else {}


def add_project(name, source):
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,63}', name):
        raise ValueError('Use a short project name containing letters, numbers, dash or underscore')
    root = Path(source).expanduser().resolve()
    def git(*args):
        return subprocess.check_output(['git', '-C', str(root), *args], text=True).strip()
    root = Path(git('rev-parse', '--show-toplevel'))
    if git('status', '--porcelain'):
        raise ValueError('Commit or set aside project changes first; project snapshots use a clean HEAD')
    revision = git('rev-parse', 'HEAD')
    # Bundle only the current tree via a fresh temporary root commit. This avoids
    # copying credentials or private data removed from older Git history.
    tracked = git('ls-tree', '-r', '--full-tree', '-z', '--name-only', revision).split('\0')
    forbidden = [p for p in tracked if any(
        part == '.env' or (part.startswith('.env.') and part != '.env.example') or
        part.endswith(('.pem', '.key', '.p12', '.pfx')) or
        part.startswith(('id_rsa', 'id_ed25519')) or part in ('credentials.json', 'auth.json')
        for part in Path(p).parts)]
    if forbidden:
        raise ValueError('Snapshot contains credential-like paths; remove them from the project before submitting')
    if any(line.startswith('160000 ') for line in git('ls-tree', '-r', '--full-tree', '-z', revision).split('\0')):
        raise ValueError('Submodules are not copied automatically; use a self-contained project snapshot')
    with tempfile.TemporaryDirectory(prefix='cluster-bundle-') as tmp:
        temp = Path(tmp)
        subprocess.run(['git', 'init', '--bare', '-q', str(temp / 'snapshot.git')], check=True)
        env = dict(os.environ, GIT_OBJECT_DIRECTORY=git('rev-parse', '--path-format=absolute', '--git-path', 'objects'),
                   GIT_AUTHOR_NAME='Cluster snapshot', GIT_AUTHOR_EMAIL='cluster@localhost',
                   GIT_COMMITTER_NAME='Cluster snapshot', GIT_COMMITTER_EMAIL='cluster@localhost')
        tree = git('rev-parse', revision + '^{tree}')
        # commit-tree object is intentionally placed in temporary object storage;
        # original source object directory is only an alternate, never written.
        env['GIT_ALTERNATE_OBJECT_DIRECTORIES'] = env.pop('GIT_OBJECT_DIRECTORY')
        env['GIT_OBJECT_DIRECTORY'] = str(temp / 'snapshot.git' / 'objects')
        snapshot = subprocess.check_output(['git', '--git-dir', str(temp / 'snapshot.git'), 'commit-tree', tree],
                                           input='Cluster snapshot\n', text=True, env=env).strip()
        subprocess.run(['git', '--git-dir', str(temp / 'snapshot.git'), 'update-ref', 'refs/heads/snapshot', snapshot], env=env, check=True)
        bundle = temp / 'source.bundle'
        subprocess.run(['git', '--git-dir', str(temp / 'snapshot.git'), 'bundle', 'create', str(bundle), 'refs/heads/snapshot'], env=env, check=True, capture_output=True)
        if bundle.stat().st_size > 48 * 1024 * 1024:
            raise ValueError('Project exceeds the 48 MiB snapshot limit; keep large datasets outside the source tree')
        result = rpc('project_put', name=name, bundle_b64=base64.b64encode(bundle.read_bytes()).decode(), revision=snapshot)
    projects = registry()
    projects[name] = dict(result, path=str(root), original_revision=revision)
    destination = state_dir() / 'projects.json'
    temporary = destination.with_suffix('.tmp')
    temporary.write_text(json.dumps(projects, indent=2) + '\n')
    temporary.replace(destination)
    print(f'Registered {name}: {revision[:12]} (immutable source snapshot)')
    return result


def project_fields(name):
    if not name:
        return {'project': None, 'source_id': None, 'revision': None}
    project = registry().get(name)
    if not project:
        raise ValueError(f'Unknown project {name}; use clusterctl project add first')
    return {key: project[key] for key in ('project', 'source_id', 'revision')}


def spec_for(args, kind):
    spec = dict(kind=kind, title=args.title or (args.prompt[:80] if kind == 'agent' else ' '.join(args.argv)[:80]),
                target=args.node, role=args.role, depends_on=args.after or [],
                inherit_from=args.inherit, timeout_seconds=args.timeout, **project_fields(args.project))
    if kind == 'agent':
        spec['prompt'] = args.prompt
    else:
        spec['argv'] = args.argv[1:] if args.argv[:1] == ['--'] else args.argv
    return spec


def print_status(data):
    print('MACHINES')
    for node in data['nodes']:
        print(json.dumps(node, ensure_ascii=False))
    print('\nRECENT JOBS')
    for job in data['jobs']:
        spec = job.get('spec', {})
        print(f"{job['id']}  {job.get('status', '?'):16} {job.get('node') or spec.get('target', ''):12} {spec.get('title', job.get('title', ''))}")


def wait_for(ids):
    terminal = {'succeeded', 'failed', 'cancelled', 'interrupted', 'blocked'}
    previous = {}
    while True:
        jobs = [rpc('get', id=job_id) for job_id in ids]
        for job in jobs:
            status = job['status']
            if previous.get(job['id']) != status:
                print(f"{job['id']}  {status}", flush=True)
                previous[job['id']] = status
        if all(job['status'] in terminal for job in jobs):
            return 0 if all(job['status'] == 'succeeded' for job in jobs) else 1
        time.sleep(3)


def submit_team(args):
    source = project_fields(args.project)
    common = dict(kind='agent', timeout_seconds=args.timeout, **source)
    research = rpc('submit', spec=dict(common, title='Research: ' + args.prompt[:65], target='workstation', role='research',
                   prompt='Inspect the project and propose a focused implementation and tests for this objective. Do not edit files.\n' + args.prompt,
                   depends_on=[], inherit_from=None))
    build = rpc('submit', spec=dict(common, title='Build: ' + args.prompt[:65], target='server', role='build',
                prompt='Implement the objective. Use the research report supplied in dependencies. Run focused tests.\n' + args.prompt,
                depends_on=[research['id']], inherit_from=None))
    review = rpc('submit', spec=dict(common, title='Review: ' + args.prompt[:65], target='workstation', role='review',
                 prompt='Review the inherited implementation for correctness and regressions. Do not edit files. Report actionable findings and remaining risks.\n' + args.prompt,
                 depends_on=[build['id']], inherit_from=build['id']))
    test = rpc('submit', spec=dict(common, title='Test: ' + args.prompt[:65], target='server', role='test',
               prompt='Test the inherited implementation. Run appropriate existing tests and focused behavioral checks. Do not modify source files. Report evidence and any failures.\n' + args.prompt,
               depends_on=[build['id']], inherit_from=build['id']))
    ids = [job['id'] for job in (research, build, review, test)]
    print('Team submitted. Research → build → review and test. No source merge is automatic.')
    for role, job_id in zip(('research', 'build (patch)', 'review', 'test'), ids):
        print(f'{role}: {job_id}')
    return wait_for(ids) if args.wait else 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    status = sub.add_parser('status'); status.add_argument('--json', action='store_true')
    project = sub.add_parser('project').add_subparsers(dest='project_command', required=True)
    add = project.add_parser('add'); add.add_argument('name'); add.add_argument('path')
    project.add_parser('list')
    for kind in ('run', 'agent'):
        cmd = sub.add_parser(kind)
        cmd.add_argument('--project', required=kind == 'agent')
        cmd.add_argument('--node', default='any')
        cmd.add_argument('--role', choices=['compute', 'research', 'build', 'review', 'test'], default='build' if kind == 'agent' else 'compute')
        cmd.add_argument('--title')
        cmd.add_argument('--timeout', type=int, default=1800)
        cmd.add_argument('--after', action='append')
        cmd.add_argument('--inherit')
        cmd.add_argument('--wait', action='store_true')
        cmd.add_argument('prompt' if kind == 'agent' else 'argv', **({} if kind == 'agent' else {'nargs': argparse.REMAINDER}))
    team = sub.add_parser('team'); team.add_argument('--project', required=True); team.add_argument('--timeout', type=int, default=1800); team.add_argument('--wait', action='store_true'); team.add_argument('prompt')
    raw = sub.add_parser('submit'); raw.add_argument('file', help='JSON spec file, or - for stdin')
    wait = sub.add_parser('wait'); wait.add_argument('ids', nargs='+')
    for name in ('show', 'logs', 'patch', 'cancel', 'retry'):
        cmd = sub.add_parser(name); cmd.add_argument('id')
        if name == 'patch': cmd.add_argument('--output', required=True)
    args = parser.parse_args()
    try:
        if args.command == 'status':
            result = rpc('status')
            print(json.dumps(result, indent=2)) if args.json else print_status(result)
        elif args.command == 'project':
            if args.project_command == 'add': add_project(args.name, args.path)
            else: print('\n'.join(f"{name}: {p['path']}" for name, p in registry().items()) or 'No projects registered')
        elif args.command in ('run', 'agent'):
            job = rpc('submit', spec=spec_for(args, 'command' if args.command == 'run' else 'agent'))
            print(job['id'])
            if args.wait: return wait_for([job['id']])
        elif args.command == 'team': return submit_team(args)
        elif args.command == 'wait': return wait_for(args.ids)
        elif args.command == 'submit':
            content = sys.stdin.read() if args.file == '-' else Path(args.file).read_text()
            print(rpc('submit', spec=json.loads(content))['id'])
        elif args.command in ('cancel', 'retry'):
            job = rpc(args.command, id=args.id)
            print(f"{job['id']}  {job['status']}")
        else:
            job = rpc('get', id=args.id)
            result = job.get('result') or {}
            if args.command == 'logs': print(result.get('log', 'No completed log yet'))
            elif args.command == 'patch':
                if not result.get('patch_b64'): raise ValueError('Job has no patch')
                path = Path(args.output).expanduser()
                with path.open('xb') as output: output.write(base64.b64decode(result['patch_b64'], validate=True))
                print(f'Patch saved to {path.resolve()}')
            else:
                display = dict(job)
                if result: display['result'] = {k: v for k, v in result.items() if k not in ('log', 'patch_b64', 'artifacts_b64')}
                print(json.dumps(display, indent=2, ensure_ascii=False))
        return 0
    except (RuntimeError, ValueError, OSError, subprocess.CalledProcessError) as exc:
        print(f'clusterctl: {exc}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
