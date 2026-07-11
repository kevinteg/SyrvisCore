"""Auth: Cloudflare Access JWT verification, the pluggable dependency, OIDC, guards."""

import time

import httpx
import jwt
import pytest
import respx
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

from syrviscore_dashboard.app import create_app
from syrviscore_dashboard.auth.cloudflare import CloudflareAccessVerifier


def _keypair():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key, key.public_key()


def _mint(private_key, aud, iss, exp_offset=3600, email="a@b.com"):
    now = int(time.time())
    payload = {
        "aud": aud,
        "iss": iss,
        "email": email,
        "sub": "user-1",
        "iat": now,
        "exp": now + exp_offset,
    }
    return jwt.encode(payload, private_key, algorithm="RS256", headers={"kid": "test"})


# --- Cloudflare verifier (unit) --------------------------------------------


def _verifier_with_key(monkeypatch, public_key):
    v = CloudflareAccessVerifier("team", "the-aud")

    class _SigningKey:
        key = public_key

    monkeypatch.setattr(v._jwk_client, "get_signing_key_from_jwt", lambda token: _SigningKey())
    return v


def test_cf_verify_valid(monkeypatch):
    key, pub = _keypair()
    v = _verifier_with_key(monkeypatch, pub)
    claims = v.verify(_mint(key, "the-aud", v.issuer))
    assert claims["email"] == "a@b.com"


def test_cf_verify_wrong_aud(monkeypatch):
    key, pub = _keypair()
    v = _verifier_with_key(monkeypatch, pub)
    with pytest.raises(jwt.InvalidAudienceError):
        v.verify(_mint(key, "other-aud", v.issuer))


def test_cf_verify_wrong_issuer(monkeypatch):
    key, pub = _keypair()
    v = _verifier_with_key(monkeypatch, pub)
    with pytest.raises(jwt.InvalidIssuerError):
        v.verify(_mint(key, "the-aud", "https://evil.example.com"))


def test_cf_verify_expired(monkeypatch):
    key, pub = _keypair()
    v = _verifier_with_key(monkeypatch, pub)
    with pytest.raises(jwt.ExpiredSignatureError):
        v.verify(_mint(key, "the-aud", v.issuer, exp_offset=-30))


# --- pluggable dependency (integration) ------------------------------------


class StubVerifier:
    def extract_token(self, request):
        return request.headers.get("Cf-Access-Jwt-Assertion") or request.cookies.get(
            "CF_Authorization"
        )

    def verify(self, token):
        if token == "good":
            return {"email": "a@b.com", "sub": "s"}
        raise ValueError("bad token")


def _cf_client(make_settings):
    s = make_settings(
        dashboard_auth_mode="cloudflare",
        cloudflare_access_team="team",
        cloudflare_access_aud="the-aud",
    )
    app = create_app(s)
    app.state.cf_verifier = StubVerifier()
    return TestClient(app)


def test_cloudflare_requires_valid_token(make_settings):
    client = _cf_client(make_settings)
    assert client.get("/api/me").status_code == 401
    assert client.get("/api/me", headers={"Cf-Access-Jwt-Assertion": "bad"}).status_code == 401
    ok = client.get("/api/me", headers={"Cf-Access-Jwt-Assertion": "good"})
    assert ok.status_code == 200
    assert ok.json() == {"email": "a@b.com", "sub": "s", "via": "cloudflare"}


def test_healthz_unauthenticated_in_cloudflare_mode(make_settings):
    client = _cf_client(make_settings)
    assert client.get("/healthz").status_code == 200  # liveness never gated


def test_none_mode_bypasses(client):
    assert client.get("/api/me").json()["via"] == "none"


# --- OIDC ------------------------------------------------------------------


def _oidc_client(make_settings):
    s = make_settings(
        dashboard_auth_mode="oidc",
        oidc_issuer="https://sso.example.com",
        oidc_client_id="cid",
        oidc_client_secret="sec",
    )
    return TestClient(create_app(s))


def test_oidc_requires_session(make_settings):
    client = _oidc_client(make_settings)
    assert client.get("/api/me").status_code == 401


def test_oidc_login_redirects_to_idp(make_settings):
    client = _oidc_client(make_settings)
    with respx.mock:
        respx.get("https://sso.example.com/.well-known/openid-configuration").mock(
            return_value=httpx.Response(
                200,
                json={
                    "issuer": "https://sso.example.com",
                    "authorization_endpoint": "https://sso.example.com/authorize",
                    "token_endpoint": "https://sso.example.com/token",
                    "jwks_uri": "https://sso.example.com/jwks",
                    "userinfo_endpoint": "https://sso.example.com/userinfo",
                    "response_types_supported": ["code"],
                },
            )
        )
        resp = client.get("/auth/login", follow_redirects=False)
    assert resp.status_code in (302, 307)
    assert "sso.example.com/authorize" in resp.headers["location"]


# --- fail-closed startup guards --------------------------------------------


def test_cloudflare_missing_aud_fails(make_settings):
    with pytest.raises(RuntimeError):
        create_app(
            make_settings(
                dashboard_auth_mode="cloudflare",
                cloudflare_access_team="team",
                cloudflare_access_aud="",
            )
        )


def test_oidc_missing_issuer_fails(make_settings):
    with pytest.raises(RuntimeError):
        create_app(make_settings(dashboard_auth_mode="oidc", oidc_issuer="", oidc_client_id="cid"))
