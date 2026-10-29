#!/usr/bin/env python3
"""Install explicitly selected, pinned extension sources into a Kura runtime tree.

Core source remains a normal Git checkout. Extension files are untracked runtime
artifacts; export core with git archive, never by copying the working directory.
All conflicts are checked before files are changed. No service is restarted here.
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
from pathlib import Path, PurePosixPath
import subprocess
import tempfile

STATE = '.kura-extensions.json'
# an extension's id is its own; core knows none of them by name (same rule as api/extension_hooks.py)
EXTENSION_ID = re.compile(r'[a-z][a-z0-9_]{0,31}')
# 1: helpers imported from routers; 2: api/extension_api.py is the interface
CORE_APIS = {1, 2, 3, 4}


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def git(root: Path, *args: str) -> str:
    return subprocess.check_output(['git', '-C', str(root), *args], text=True).strip()


def destination(root: Path, name: str) -> Path:
    path = PurePosixPath(name)
    if (not path.parts or path.is_absolute() or any(p in {'.', '..', '.git'} for p in path.parts)
            or str(path) != name or '\\' in name or name == STATE):
        raise ValueError(f'unsafe extension path: {name!r}')
    out = root.joinpath(*path.parts)
    for parent in (out, *out.parents):
        if parent == root:
            break
        if parent.is_symlink():
            raise ValueError(f'symlink in extension destination: {name}')
    return out


def read_extension(source: Path) -> dict:
    source = source.resolve()
    if git(source, 'status', '--porcelain'):
        raise ValueError(f'extension checkout must be clean: {source}')
    commit = git(source, 'rev-parse', 'HEAD')
    def blob(path):
        return subprocess.check_output(['git', '-C', str(source), 'show', f'{commit}:{path}'])
    # Read committed objects, not checkout files that can change after preflight.
    manifest = json.loads(blob('extension.json'))
    if (manifest.get('schema') != 1 or not EXTENSION_ID.fullmatch(str(manifest.get('id')))
            or manifest.get('core_api') not in CORE_APIS):
        raise ValueError('unsupported extension manifest')
    tree = {}
    for line in git(source, 'ls-tree', '-r', commit).splitlines():
        metadata, path = line.split('\t', 1)
        tree[path] = metadata.split()[0]
    files = {}
    for name in manifest['files']:
        destination(source, name)
        rel = 'overlay/' + name
        if tree.get(rel) not in {'100644', '100755'}:
            raise ValueError(f'extension file is not a tracked regular file: {name}')
        files[name] = (blob(rel), 0o755 if tree[rel] == '100755' else 0o644)
    if len(files) != len(manifest['files']):
        raise ValueError('duplicate extension paths')
    # units the extension runs and the paths whose change restarts each (kura_checkout_pull.sh)
    units = manifest.get('units', {})
    if not isinstance(units, dict) or not all(
            re.fullmatch(r'[\w@.-]+\.service', u) and isinstance(p, str) and p.strip() and '\n' not in p
            for u, p in units.items()):
        raise ValueError('unsupported extension units')
    return {'id': manifest['id'], 'version': manifest['version'],
            'commit': commit, 'source': str(source), 'units': units, 'files': files}


def atomic_write(path: Path, data: bytes, mode: int = 0o644):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix='.kura-compose-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(data)
        os.chmod(temp, mode)
        os.replace(temp, path)
    finally:
        Path(temp).unlink(missing_ok=True)


def install(root: Path, sources: list[Path], *, check: bool = False) -> dict:
    root = root.resolve()
    lock = root / '.kura-extensions.lock'
    fd = os.open(lock, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'w') as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        return _install_locked(root, sources, check=check)


def _install_locked(root: Path, sources: list[Path], *, check: bool = False) -> dict:
    root = root.resolve()
    if git(root, 'rev-parse', '--show-toplevel') != str(root):
        raise ValueError('runtime must be the root of a Kura Git checkout')
    tracked = set(git(root, 'ls-files').splitlines())
    state_path = root / STATE
    if state_path.is_symlink():
        raise ValueError('extension state may not be a symlink')
    previous = json.loads(state_path.read_text()) if state_path.exists() else {'schema': 1, 'extensions': []}
    if previous.get('schema') != 1:
        raise ValueError('unsupported installed extension state')
    old = {}
    for ext in previous['extensions']:
        for name, sha in ext['files'].items():
            destination(root, name)
            if name in old:
                raise ValueError('duplicate installed ownership')
            old[name] = sha
    lock_path = root / 'release/extension_sources.json'
    pins = json.loads(lock_path.read_text())['extensions'] if lock_path.exists() else {}
    incoming, extensions = {}, []
    for source in sources:
        ext = read_extension(source)
        if pins and (ext['id'] not in pins or pins[ext['id']]['commit'] != ext['commit']):
            raise ValueError(f"extension is not pinned by this core release: {ext['id']}")
        if any(e['id'] == ext['id'] for e in extensions):
            raise ValueError('duplicate extension id')
        hashes = {}
        for name, value in ext['files'].items():
            destination(root, name)
            if name in incoming or name in tracked:
                raise ValueError(f'extension collides with core or another extension: {name}')
            incoming[name] = value
            hashes[name] = digest(value[0])
        extensions.append({k: v for k, v in ext.items() if k != 'files'} | {'files': hashes})
    # Check the entire transition before writing or removing anything. Adopt an
    # old untracked file only when its bytes are identical to the pinned source.
    for name in old.keys() | incoming.keys():
        target = destination(root, name)
        if name in tracked:
            raise ValueError(f'installed path is now core-owned: {name}')
        if target.exists():
            if not target.is_file():
                raise ValueError(f'extension destination is not a file: {name}')
            actual = digest(target.read_bytes())
            allowed = {old.get(name)}
            if name in incoming:
                allowed.add(digest(incoming[name][0]))
            if actual not in allowed:
                raise ValueError(f'local changes or unmanaged file would be overwritten: {name}')
    state = {'schema': 1, 'extensions': extensions}
    if check:
        return state
    for name, (data, mode) in incoming.items():
        target = destination(root, name)
        if not target.exists() or digest(target.read_bytes()) != digest(data):
            atomic_write(target, data, mode)
        elif target.stat().st_mode & 0o777 != mode:
            os.chmod(target, mode)
    for name in old.keys() - incoming.keys():
        destination(root, name).unlink(missing_ok=True)
    atomic_write(state_path, (json.dumps(state, indent=2, sort_keys=True) + '\n').encode())
    return state


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--runtime', type=Path, required=True)
    parser.add_argument('--extension', type=Path, action='append', default=[],
                        help='complete desired extension set')
    parser.add_argument('--remove-all', action='store_true', help='explicitly remove all installed extensions')
    parser.add_argument('--check', action='store_true', help='validate a transition without changing files')
    args = parser.parse_args()
    if bool(args.extension) == args.remove_all:
        parser.error('specify --extension or --remove-all')
    try:
        result = install(args.runtime, args.extension, check=args.check)
    except (ValueError, OSError, subprocess.CalledProcessError) as error:
        parser.exit(1, str(error) + '\n')
    print(json.dumps({'check': args.check, 'extensions': [
        {k: e[k] for k in ('id', 'version', 'commit')} for e in result['extensions']]}))


if __name__ == '__main__':
    main()
