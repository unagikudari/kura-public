"""Resolve an explicit Kura API target without a deployment-specific fallback."""
import os
from collections.abc import Mapping
from urllib.parse import urlsplit


def api_origin(value: str | None = None, *, environ: Mapping[str, str] | None = None,
               required: bool = True) -> str:
    environ = os.environ if environ is None else environ
    target = (value or environ.get('KURA_URL') or environ.get('KURA_API') or '').strip().rstrip('/')
    if not target:
        if not required:
            return ''
        raise ValueError('set KURA_URL or KURA_API to the canonical Kura endpoint before running the worker')
    try:
        parts = urlsplit(target)
        _ = parts.port
        valid = (parts.scheme in {'http', 'https'} and parts.hostname
                 and not parts.username and not parts.password
                 and not parts.query and not parts.fragment
                 and not any(ord(char) <= 32 for char in target))
    except ValueError:
        valid = False
    if not valid:
        raise ValueError('KURA_URL/KURA_API must be an HTTP(S) endpoint without embedded credentials, query, or fragment')
    return target.removesuffix('/api')


def api_base(value: str | None = None, *, environ: Mapping[str, str] | None = None,
             required: bool = True) -> str:
    origin = api_origin(value, environ=environ, required=required)
    return origin + '/api' if origin else ''
