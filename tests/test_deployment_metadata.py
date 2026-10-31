import pytest
from api.deployment import canonical_url, replica_refusal


def test_replica_refusal_has_no_private_default():
    result = replica_refusal({})
    assert result['canonicalUrl'] is None
    assert 'read-only replica' in result['detail']


def test_explicit_canonical_endpoint_is_shown():
    assert canonical_url({'KURA_CANONICAL_URL':'https://archive.example.invalid/'}) == 'https://archive.example.invalid'


@pytest.mark.parametrize('url', ['https://user:secret@example.invalid','https://example.invalid/?token=secret',
                                 'https://example.invalid/#secret','file:///etc/passwd','//example.invalid',
                                 'https://example.invalid:bad','http://[broken','https://example.invalid/\nsecret'])
def test_refusal_never_echoes_embedded_credentials_or_invalid_endpoints(url):
    assert canonical_url({'KURA_CANONICAL_URL':url}) is None
