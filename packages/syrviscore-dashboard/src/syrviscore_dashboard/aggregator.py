"""Health aggregator — one shared, TTL-cached snapshot of all component probes.

All callers (``/api/health``, the SSE loop, every browser tab) share one snapshot
per TTL window, so the Docker daemon and component APIs are hit at most once per
window regardless of load. Probes run concurrently; each is wrapped in ``guard``
so a single failure degrades one component rather than the whole snapshot.
"""

import asyncio
import time
from datetime import datetime, timezone
from typing import Any, Dict

import httpx

from .probes import PROBES, guard, severity

_OVERALL = {0: "ok", 1: "degraded", 2: "down"}


class HealthAggregator:
    def __init__(self, settings):
        self.settings = settings
        self._snapshot: Dict[str, Any] = None  # type: ignore[assignment]
        self._expires = 0.0
        self._lock = asyncio.Lock()

    async def get_snapshot(self, force: bool = False) -> Dict[str, Any]:
        now = time.monotonic()
        if not force and self._snapshot is not None and now < self._expires:
            return self._snapshot
        async with self._lock:
            now = time.monotonic()
            if not force and self._snapshot is not None and now < self._expires:
                return self._snapshot
            snapshot = await self._build()
            self._snapshot = snapshot
            self._expires = time.monotonic() + self.settings.aggregator_ttl_s
            return snapshot

    async def _build(self) -> Dict[str, Any]:
        timeout = httpx.Timeout(self.settings.probe_timeout_s)
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as http:
            tasks = [
                (
                    guard(name, fn, self.settings, http)
                    if needs_http
                    else guard(name, fn, self.settings)
                )
                for name, needs_http, fn in PROBES
            ]
            results = await asyncio.gather(*tasks)

        components = {r.component: r.to_dict() for r in results}
        worst = max((severity(r.status) for r in results), default=0)
        return {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "overall": _OVERALL[worst],
            "healthy": worst == 0,
            "components": components,
        }
