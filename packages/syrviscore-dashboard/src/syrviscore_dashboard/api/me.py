"""Who am I — returns the authenticated identity (and how they authenticated)."""

from fastapi import APIRouter, Depends

from ..auth.deps import require_user

router = APIRouter(prefix="/api", tags=["auth"])


@router.get("/me")
def me(user: dict = Depends(require_user)) -> dict:
    return user
