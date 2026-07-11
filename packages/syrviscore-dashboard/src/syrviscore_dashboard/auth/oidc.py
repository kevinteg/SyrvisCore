"""Local OIDC login — the dashboard as a generic OIDC client (Auth Code + PKCE).

Default IdP is the Synology SSO Server (fully local, no internet), but any OIDC
provider works. On successful login the ID token is verified by authlib and a
signed session cookie is issued; ``auth.deps.require_user`` then trusts it.
"""

from authlib.integrations.starlette_client import OAuth
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse

auth_router = APIRouter(prefix="/auth", tags=["auth"])


def build_oauth(settings) -> OAuth:
    oauth = OAuth()
    oauth.register(
        name="idp",
        server_metadata_url=settings.oidc_issuer.rstrip("/") + "/.well-known/openid-configuration",
        client_id=settings.oidc_client_id,
        client_secret=settings.oidc_client_secret or None,
        client_kwargs={"scope": "openid email profile", "code_challenge_method": "S256"},
    )
    return oauth


def _oauth(request: Request) -> OAuth:
    oauth = getattr(request.app.state, "oauth", None)
    if oauth is None:
        raise HTTPException(status_code=404, detail="OIDC login is not enabled")
    return oauth


@auth_router.get("/login")
async def login(request: Request):
    oauth = _oauth(request)
    settings = request.app.state.settings
    redirect_uri = settings.oidc_redirect_url or str(request.url_for("auth_callback"))
    return await oauth.idp.authorize_redirect(request, redirect_uri)


@auth_router.get("/callback", name="auth_callback")
async def auth_callback(request: Request):
    oauth = _oauth(request)
    token = await oauth.idp.authorize_access_token(request)
    userinfo = token.get("userinfo") or {}
    request.session["user"] = {
        "email": userinfo.get("email"),
        "sub": userinfo.get("sub"),
        "name": userinfo.get("name"),
    }
    return RedirectResponse(url="/")


@auth_router.get("/logout")
async def logout(request: Request):
    request.session.pop("user", None)
    return RedirectResponse(url="/")
