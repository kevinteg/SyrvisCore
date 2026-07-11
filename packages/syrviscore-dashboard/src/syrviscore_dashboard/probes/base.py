"""Probe result type + a guard that guarantees a probe never raises.

Every component probe returns a :class:`ProbeResult`. The aggregator runs each
through :func:`guard`, which times it and converts any exception into a clean
``DOWN`` result — so one flaky component can never 500 the whole snapshot.
"""

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Awaitable, Callable, Dict, Optional


class Status(str, Enum):
    OK = "ok"
    DEGRADED = "degraded"
    DOWN = "down"
    NOT_CONFIGURED = "not_configured"


# Severity used to fold component statuses into one overall status.
# NOT_CONFIGURED is not a failure — an absent optional component is fine.
_SEVERITY = {
    Status.OK: 0,
    Status.NOT_CONFIGURED: 0,
    Status.DEGRADED: 1,
    Status.DOWN: 2,
}


@dataclass
class ProbeResult:
    component: str
    status: Status
    detail: str = ""
    latency_ms: Optional[float] = None
    extra: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "component": self.component,
            "status": self.status.value,
            "detail": self.detail,
            "latency_ms": self.latency_ms,
            "extra": self.extra,
        }


def severity(status: Status) -> int:
    return _SEVERITY.get(status, 0)


async def guard(
    component: str, fn: Callable[..., Awaitable[ProbeResult]], *args: Any
) -> ProbeResult:
    """Run a probe, timing it and never letting it raise."""
    start = time.perf_counter()
    try:
        result = await fn(*args)
    except Exception as exc:  # noqa: BLE001 - the whole point is to never propagate
        return ProbeResult(
            component=component,
            status=Status.DOWN,
            detail="probe error: {}".format(exc),
            latency_ms=round((time.perf_counter() - start) * 1000, 1),
        )
    if result.latency_ms is None:
        result.latency_ms = round((time.perf_counter() - start) * 1000, 1)
    return result
