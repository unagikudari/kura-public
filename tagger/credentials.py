"""The sole wire format for a Kura user's raw service token."""
import base64


def token_headers(username: str, token: str) -> dict[str, str]:
    if not username or not token:
        raise ValueError('KURA_USER and KURA_TOKEN are required')
    encoded = base64.b64encode(f'{username}:{token}'.encode()).decode('ascii')
    return {'Authorization': 'Token ' + encoded}
