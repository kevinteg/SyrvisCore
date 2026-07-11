"""Cloudflare Access JWT verification.

Verifies the ``Cf-Access-Jwt-Assertion`` header (or ``CF_Authorization`` cookie)
against the team's JWKS, enforcing signature + ``aud`` + ``iss`` + expiry in one
``jwt.decode`` call. ``PyJWKClient`` fetches and caches the signing keys and picks
the right one by the token's ``kid``.
"""

from typing import Optional

import jwt
from jwt import PyJWKClient


class CloudflareAccessVerifier:
    def __init__(self, team: str, aud: str, certs_url: Optional[str] = None):
        self.team = team
        self.aud = aud
        self.issuer = "https://{}.cloudflareaccess.com".format(team)
        self.certs_url = certs_url or (self.issuer + "/cdn-cgi/access/certs")
        # Lazy network: no request is made until the first token is verified.
        self._jwk_client = PyJWKClient(self.certs_url)

    def verify(self, token: str) -> dict:
        """Return the token claims, or raise ``jwt.InvalidTokenError`` if invalid."""
        signing_key = self._jwk_client.get_signing_key_from_jwt(token)
        return jwt.decode(
            token,
            signing_key.key,
            algorithms=["RS256"],
            audience=self.aud,
            issuer=self.issuer,
        )

    @staticmethod
    def extract_token(request) -> Optional[str]:
        token = request.headers.get("Cf-Access-Jwt-Assertion")
        if token:
            return token
        return request.cookies.get("CF_Authorization")
