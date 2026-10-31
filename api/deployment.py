"""Public deployment metadata; never expose credentials embedded in a URL."""
from collections.abc import Mapping
from urllib.parse import urlsplit


def canonical_url(environ: Mapping[str, str]) -> str | None:
    value = environ.get('KURA_CANONICAL_URL', '').strip()
    try:
        parsed = urlsplit(value)
        _ = parsed.port
        valid = (parsed.scheme in {'http', 'https'} and parsed.hostname
                 and not parsed.username and not parsed.password
                 and not parsed.query and not parsed.fragment
                 and not any(ord(char) < 32 for char in value))
        return value.rstrip('/') if valid else None
    except ValueError:
        return None


def replica_refusal(environ: Mapping[str, str], role: str = 'replica') -> dict:
    if role == 'rehearsal':
        return {'detail': 'this kura-api serves a rehearsal copy (KURA_DB_ROLE=rehearsal): '
                          'only signing in and out are accepted; make changes on the canonical Kura',
                'canonicalUrl': canonical_url(environ)}
    return {'detail': 'this kura-api is a read-only replica (KURA_DB_ROLE=replica); '
                      'send writes to the canonical Kura API',
            'canonicalUrl': canonical_url(environ)}
