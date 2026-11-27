"""Read-only Web deployment observations and their dashboard view."""
from __future__ import annotations

import json
import re
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit

REPO = Path(__file__).resolve().parents[2]
RELEASE_ROOT = Path.home() / '.local/share/kura-web'
UNIT = 'kura-web-release-observe.service'
PREFIX = 'kura-web-observation '
STATES = {'current', 'pending', 'mismatch', 'unobserved'}
COMMIT = re.compile(r'[a-f0-9]{7,40}')
VERSION = re.compile(r'[A-Za-z0-9_.-]{1,80}')
EXTENSION_PAGES = {
    'lora': '/lora',
}
INPUT_PATHS = ('web/', 'release/extension_sources.json', 'scripts/compose_extensions.py',
               ':(exclude)web/tests/**', ':(exclude)web/src/lib/build_info.ts',
               ':(exclude)web/scripts/web-release.py')


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def read_http(url):
    opener = urllib.request.build_opener(NoRedirect)
    try:
        response = opener.open(url, timeout=3)
    except urllib.error.HTTPError as error:
        response = error
    with response:
        return response.status, response.read(32768)


def git(repo, *args):
    return subprocess.check_output(['git', '-C', str(repo), *args], text=True,
                                   stderr=subprocess.DEVNULL, timeout=8).strip()


def observe(repo=REPO, root=RELEASE_ROOT):
    snapshot = {'state': 'unobserved', 'observed_at': int(time.time()),
                'active_commit': None, 'main_commit': None, 'reason': 'receipt'}
    try:
        receipt = json.loads((root / 'active.json').read_text())
        active = receipt['commit']
        version = receipt['build_version']
        if (receipt['format'] != 'kura-web-release-v1' or not COMMIT.fullmatch(active)
                or not VERSION.fullmatch(version)):
            return snapshot
        release = Path(receipt['release']).resolve()
        if release.parent != (root / 'releases').resolve():
            return snapshot
        manifest = json.loads((release / 'release.json').read_text())
        if manifest['commit'] != active or manifest['build_version'] != version:
            return snapshot
        origin = manifest['origin']
        parts = urlsplit(origin)
        if (parts.scheme != 'https' or not parts.hostname or parts.username is not None
                or parts.path or parts.query or parts.fragment):
            return snapshot
        snapshot.update(active_commit=active, reason='edge')
        code, body = read_http(origin + '/_app/version.json')
        if code != 200:
            return snapshot
        served = json.loads(body)['version']
        if not VERSION.fullmatch(served):
            return snapshot
        if served != version:
            return dict(snapshot, state='mismatch', reason='edge_version')
        snapshot['reason'] = 'extensions'
        code, body = read_http('http://127.0.0.1:8001/api/info')
        if code != 200:
            return snapshot
        installed = json.loads(body)['extensions']
        if not isinstance(installed, dict):
            return snapshot
        for name, path in EXTENSION_PAGES.items():
            if name not in installed:
                continue
            code, _ = read_http(origin + path)
            if code == 404:
                return dict(snapshot, state='mismatch', reason='extension_route')
            if code != 303:
                return snapshot
        snapshot['reason'] = 'repository'
        line = git(repo, 'ls-remote', '--exit-code', 'origin', 'refs/heads/main')
        target, ref = line.split()
        if ref != 'refs/heads/main' or not re.fullmatch(r'[a-f0-9]{40}', target):
            return snapshot
        snapshot['main_commit'] = target
        changed = git(repo, 'diff', '--name-only', active, target, '--', *INPUT_PATHS)
        return dict(snapshot, state='pending' if changed else 'current',
                    reason='build_inputs' if changed else 'matched')
    except (OSError, ValueError, TypeError, KeyError, subprocess.SubprocessError):
        return snapshot


def latest_observation():
    try:
        lines = subprocess.check_output(
            ['journalctl', '--user', '-u', UNIT, '-n', '40', '-o', 'cat', '--no-pager'],
            text=True, stderr=subprocess.DEVNULL, timeout=2)
        for line in reversed(lines.splitlines()):
            if line.startswith(PREFIX):
                row = json.loads(line[len(PREFIX):])
                return row if isinstance(row, dict) else None
    except (OSError, ValueError, subprocess.SubprocessError):
        pass
    return None


def dashboard_status(now=None):
    now = time.time() if now is None else now
    row = latest_observation()
    if not isinstance(row, dict):
        return {'state': 'unobserved'}
    observed = row.get('observed_at')
    state = row.get('state')
    if (not isinstance(state, str) or state not in STATES or type(observed) is not int
            or not -60 <= now - observed <= 720):
        return {'state': 'unobserved'}
    return {'state': state, 'observed_at': observed}


def main():
    snapshot = observe()
    previous = latest_observation() or {}
    keys = ('state', 'active_commit', 'reason')
    snapshot['changed'] = (
        any(previous.get(key) != snapshot[key] for key in keys)
        or snapshot['state'] == 'pending' and previous.get('main_commit') != snapshot['main_commit']
    )
    print(PREFIX + json.dumps(snapshot, sort_keys=True))


if __name__ == '__main__':
    main()
