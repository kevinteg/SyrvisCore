"""The ``require_user`` dependency + one-time auth wiring for the app.

``require_user`` accepts either a local OIDC session or a Cloudflare Access JWT.
``setup_auth`` fails closed at startup if a selected provider is misconfigured,
adds the session middleware, and builds the provider objects onto ``app.state``.
"""

import secrets

from fastapi import HTTPException, Request
from starlette.middleware.sessions import SessionMiddleware

from .cloudflare import CloudflareAccessVerifier

_DEV_USER = {"email": "lan-dev", "sub": "dev", "via": "none"}


def _session_user(request: Request):
    try:
        return request.session.get("user")
    except Exception:  # noqa: BLE001 - session middleware may be absent
        return None


async def require_user(request: Request) -> dict:
    """Authenticate the request or raise 401. Result is cached per-request."""
    settings = request.app.state.settings
    mode = settings.auth_mode

    if mode == "none":
        return dict(_DEV_USER)

    if settings.oidc_enabled:
        user = _session_user(request)
        if user:
            return {**user, "via": "oidc"}

    if settings.cloudflare_enabled:
        verifier = getattr(request.app.state, "cf_verifier", None)
        if verifier is not None:
            token = verifier.extract_token(request)
            if token:
                try:
                    claims = verifier.verify(token)
                    return {
                        "email": claims.get("email"),
                        "sub": claims.get("sub"),
                        "via": "cloudflare",
                    }
                except Exception:  # noqa: BLE001 - any verify failure => unauthenticated
                    pass

    raise HTTPException(status_code=401, detail="authentication required")


def setup_auth(app, settings) -> None:
    """Fail-closed startup checks + session middleware + provider objects."""
    if settings.cloudflare_enabled and not (
        settings.cloudflare_access_team and settings.cloudflare_access_aud
    ):
        raise RuntimeError(
            "auth mode '{}' requires CLOUDFLARE_ACCESS_TEAM and CLOUDFLARE_ACCESS_AUD".format(
                settings.auth_mode
            )
        )
    if settings.oidc_enabled and not (settings.oidc_issuer and settings.oidc_client_id):
        raise RuntimeError(
            "auth mode '{}' requires OIDC_ISSUER and OIDC_CLIENT_ID".format(settings.auth_mode)
        )

    secret = settings.dashboard_session_secret or secrets.token_hex(32)
    app.add_middleware(SessionMiddleware, secret_key=secret, same_site="lax", https_only=False)

    if settings.cloudflare_enabled:
        app.state.cf_verifier = CloudflareAccessVerifier(
            settings.cloudflare_access_team, settings.cloudflare_access_aud
        )
    if settings.oidc_enabled:
        from .oidc import build_oauth

        app.state.oauth = build_oauth(settings)
