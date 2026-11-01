#!/usr/bin/env python3
"""Reject ingress routes that publish Kura backends outside its web boundary: Tailscale
Serve's, or a user-run nginx edge's (web/ops/edge-nginx.conf) when the origin is not on Serve."""
import argparse
import ipaddress
import json
import re
import shutil
import subprocess
from pathlib import Path
from urllib.parse import urlsplit

EDGE = Path.home() / '.local/share/kura-edge'


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


def _tokens(text):
    """nginx's tokens: words (quotes undone), ';', '{', '}'; comments dropped."""
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if c.isspace():
            i += 1
        elif c == '#':
            while i < n and text[i] != '\n':
                i += 1
        elif c in ';{}':
            yield c
            i += 1
        elif c in '"\'':
            j, word = i + 1, []
            while j < n and text[j] != c:
                if text[j] == '\\' and j + 1 < n:
                    j += 1
                word.append(text[j])
                j += 1
            yield ''.join(word)
            i = j + 1
        else:
            # As nginx reads a word: '#' starts a comment only where a token starts, and a
            # backslash takes the next character literally.
            j, word = i, []
            while j < n and not text[j].isspace() and text[j] not in ';{}':
                if text[j] == '\\' and j + 1 < n:
                    j += 1
                word.append(text[j])
                j += 1
            yield ''.join(word)
            i = j


def _parse(tokens, nested=False):
    """[(name, args, children or None)] for one block level; malformed text is refused."""
    out, words = [], []
    for t in tokens:
        if t in (';', '{') and not words:
            raise ValueError(f'{t!r} without a directive')
        if t == ';':
            out.append((words[0], words[1:], None)); words = []
        elif t == '{':
            out.append((words[0], words[1:], _parse(tokens, nested=True))); words = []
        elif t == '}':
            if words or not nested:
                raise ValueError("unexpected '}'")
            return out
        else:
            words.append(t)
    if words or nested:
        raise ValueError('the config ends inside a directive or block')
    return out


def _files(dump):
    """`nginx -T` prints each file under '# configuration file <path>:'; {path: text}, in order."""
    parts = re.split(r'^# configuration file (.+):$', dump, flags=re.M)
    return {parts[i]: parts[i + 1] for i in range(1, len(parts) - 1, 2)}


def _resolved(files, path, depth=0):
    """The parsed file with each include replaced by what it includes."""
    if depth > 8:
        raise ValueError('includes nest too deep')
    out = []
    for name, args, children in _parse(iter(list(_tokens(files[path])))):
        out.extend(_include(files, path, name, args, children, depth))
    return out


def _include(files, path, name, args, children, depth):
    if name == 'include':
        base = Path(path).parent
        targets = [p for p in files if Path(p) == (base / args[0]) or Path(p).match(args[0])]
        if not targets:
            raise ValueError(f'include {args[0]} is not in the dump')
        return [d for t in targets for d in _resolved(files, t, depth + 1)]
    if children is not None:
        children = [d for n, a, c in children for d in _include(files, path, n, a, c, depth)]
    return [(name, args, children)]


# The only directives each level may hold: the template's, and a few that change nothing
# about what is served or to whom. Anything else (if, root, return, another *_pass, a
# nested location, ...) is refused rather than interpreted.
_ALLOWED = {
    'main': {'worker_processes', 'pid', 'error_log', 'events', 'http'},
    'events': {'worker_connections'},
    'http': {'access_log', 'error_log', 'log_format', 'client_body_temp_path', 'proxy_temp_path',
             'fastcgi_temp_path', 'uwsgi_temp_path', 'scgi_temp_path', 'upstream', 'server'},
    'upstream': {'server'},
    'server': {'listen', 'http2', 'http3', 'server_name', 'allow', 'deny', 'ssl_certificate',
               'ssl_certificate_key', 'ssl_protocols', 'ssl_session_cache', 'ssl_session_timeout',
               'add_header', 'client_max_body_size', 'proxy_request_buffering', 'proxy_buffering',
               'access_log', 'error_log', 'location'},
    'location': {'proxy_pass', 'proxy_http_version', 'proxy_set_header', 'proxy_read_timeout',
                 'proxy_send_timeout', 'proxy_buffering', 'proxy_request_buffering',
                 'client_max_body_size', 'add_header'},
}
_BLOCKS = {'events', 'http', 'upstream', 'server', 'location'}


def _ipv4_listen(args):
    """`listen <IPv4 address>:<port> ...` and nothing looser (no IPv6, no host name, no wildcard)."""
    host, _, port = (args[0] if args else '').rpartition(':')
    try:
        ipaddress.IPv4Address(host)
    except ValueError:
        return False
    return port.isdigit()


def _shape(block, level, where, errors):
    for name, args, children in block:
        if name not in _ALLOWED[level]:
            errors.append(f'{where}: {name} is not part of the supported edge shape')
        elif (children is not None) != (name in _BLOCKS and level != 'upstream'):  # upstream's server is a line
            errors.append(f'{where}: {name} is not part of the supported edge shape')
        elif children is not None:
            _shape(children, name, f'{where} > {name} {" ".join(args)}'.rstrip(), errors)


def edge_violations(config, upstream):
    """What is wrong with an nginx edge, from `nginx -T` output; `upstream` is the web release
    that should serve. Only the shape web/ops/edge-nginx.conf has is accepted (see _ALLOWED):
    one upstream kura_web with that one server; servers whose access rule is exactly `allow
    100.64.0.0/10; deny all;`, with IPv4 listens and a `location /`; every location passing to
    kura_web and nothing else."""
    errors = []
    files = _files(config)
    if not files:
        return ['not nginx -T output']
    try:
        tree = _resolved(files, next(iter(files)))
    except (ValueError, IndexError) as e:
        return [f'the edge config cannot be read: {e}']
    _shape(tree, 'main', 'main', errors)
    https = [c for name, _, c in tree if name == 'http' and c is not None]
    if len(https) != 1:
        return errors + ['the edge config needs exactly one http block']
    http = https[0]
    ups = [(args, c) for name, args, c in http if name == 'upstream' and c is not None]
    want = 'server ' + upstream.removeprefix('http://').rstrip('/')
    if [args for args, _ in ups] != [['kura_web']] or \
            [' '.join([n, *a]) for n, a, _ in ups[0][1]] != [want]:
        errors.append(f'the edge upstream must be only kura_web with {want}')
    servers = [c for name, _, c in http if name == 'server' and c is not None]
    if not servers:
        errors.append('the edge has no server')
    for server in servers:
        if [(n, a) for n, a, _ in server if n in ('allow', 'deny')] != [('allow', ['100.64.0.0/10']), ('deny', ['all'])]:
            errors.append('a server must answer tailnet sources only: allow 100.64.0.0/10; deny all;')
        for name, args, _ in server:
            if name == 'listen' and not _ipv4_listen(args):
                errors.append(f'listen {" ".join(args)}: only IPv4 listens are supported '
                              '(tailscaled does not drop spoofed tailnet IPv6 sources)')
        locations = [(a, c) for n, a, c in server if n == 'location' and c is not None]
        if not any(a == ['/'] for a, _ in locations):
            errors.append('a server needs location / passing to kura_web')
        for args, block in locations:
            passes = [a for n, a, _ in block if n == 'proxy_pass']
            if passes != [['http://kura_web']]:
                errors.append(f'location {" ".join(args)}: must proxy_pass http://kura_web only, not {passes}')
    return errors


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--origin', required=True)
    parser.add_argument('--upstream')
    parser.add_argument('--edge', type=Path, default=EDGE)
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
    if not config.get('Web', {}).get(urlsplit(args.origin).netloc) and (args.edge / 'upstream.conf').is_file():
        nginx = shutil.which('nginx') or '/usr/sbin/nginx'
        dump = subprocess.run([nginx, '-T', '-p', str(args.edge), '-c', str(args.edge / 'nginx.conf')],
                              capture_output=True, text=True, check=True).stdout
        errors = edge_violations(dump, upstream)
    else:
        errors = violations(config, args.origin, upstream)
    print(json.dumps({'private_ingress':not errors,'errors':errors}))
    raise SystemExit(bool(errors))


if __name__ == '__main__': main()
