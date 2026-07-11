"""Health endpoints — the aggregate snapshot and per-component detail."""

from fastapi import APIRouter, Depends, HTTPException, Query

from ..aggregator import HealthAggregator
from ..deps import get_aggregator

router = APIRouter(prefix="/api", tags=["health"])


@router.get("/health")
async def health(
    refresh: bool = Query(False, description="bypass the TTL cache"),
    agg: HealthAggregator = Depends(get_aggregator),
) -> dict:
    return await agg.get_snapshot(force=refresh)


@router.get("/health/{component}")
async def health_component(component: str, agg: HealthAggregator = Depends(get_aggregator)) -> dict:
    snapshot = await agg.get_snapshot()
    result = snapshot["components"].get(component)
    if result is None:
        raise HTTPException(status_code=404, detail="unknown component: {}".format(component))
    return result


@router.get("/drift")
async def drift(agg: HealthAggregator = Depends(get_aggregator)) -> dict:
    """Core desired-vs-actual drift (pulled from the cached core probe)."""
    snapshot = await agg.get_snapshot()
    core = snapshot["components"].get("core", {})
    report = core.get("extra", {}).get("drift")
    return report or {"in_sync": None, "detail": "drift not available (docker/compose unreadable)"}


@router.get("/verify")
async def verify(smoke: bool = False) -> dict:
    """Full read-only verify report (validators + drift). Heavier than /api/health."""
    import asyncio

    def _run() -> dict:
        from syrviscore import verify as verify_lib

        try:
            return verify_lib.build_report(smoke=smoke)
        except Exception as exc:  # noqa: BLE001
            return {"error": str(exc), "healthy": False}

    return await asyncio.to_thread(_run)
