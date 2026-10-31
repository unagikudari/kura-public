#!/usr/bin/env python3
"""Reject Tailscale routes that publish Kura backends outside its web boundary."""
import argparse
import json
import subprocess
from pathlib import Path
from urllib.parse import urlsplit


def violations(config, origin, upstream):
    host = urlsplit(origin).netloc
    handlers = config.get('Web', {}).get(host, {}).get('Handlers', {})
    errors = []
    if handlers.get('/', {}).get('Proxy', '').rstrip('/') != upstream.rstrip('/'):
        errors.append('root must use the authenticated web service')
    for path, handler in handlers.items():
        if any(path == prefix or path.startswith(prefix + '/') for prefix in ('/api', '/data', '/m')):
            expected = upstream.rstrip('/') + path
            if handler.get('Proxy', '').rstrip('/') != expected.rstrip('/'):
                errors.append(f'{path} bypasses the authenticated web service')
    return errors


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--origin', required=True)
    parser.add_argument('--upstream')
    args = parser.parse_args()
    config = json.loads(subprocess.check_output(['tailscale','serve','status','--json'],text=True))
    upstream=args.upstream
    if upstream is None:
        state=Path.home()/'.local/share/kura-web/active.json'
        if state.exists():
            receipt=json.loads(state.read_text());port=receipt.get('port')
            if receipt.get('format')!='kura-web-release-v1' or port not in (3002,3003):
                raise ValueError('Invalid private ingress release receipt')
            upstream=f'http://127.0.0.1:{port}'
        else:upstream='http://127.0.0.1:3000'
    errors = violations(config, args.origin, upstream)
    print(json.dumps({'private_ingress':not errors,'errors':errors}))
    raise SystemExit(bool(errors))


if __name__ == '__main__': main()
