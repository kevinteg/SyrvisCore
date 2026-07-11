"""Config probe — surfaces which components are enabled + the domain.

Always cheap and local; reports NOT_CONFIGURED if there's no ``.env`` yet.
"""

import asyncio

from .base import ProbeResult, Status


def _config_sync(settings) -> ProbeResult:
    from syrviscore.config_reader import read_config

    cfg = read_config()
    if not cfg.values:
        return ProbeResult("config", Status.NOT_CONFIGURED, "no .env found — run `syrvis setup`")
    enabled = [name for name, on in cfg.enabled_components.items() if on]
    return ProbeResult(
        "config",
        Status.OK,
        "domain={}".format(cfg.domain or "unset"),
        extra={
            "domain": cfg.domain,
            "enabled_components": cfg.enabled_components,
            "active_version": cfg.active_version,
            "enabled": enabled,
        },
    )


async def probe_config(settings) -> ProbeResult:
    return await asyncio.to_thread(_config_sync, settings)
