"""Routes view — every hostname this instance routes through Traefik, with live health.

The declared routes come from the core library's hostnames report
(``syrviscore.hostnames.build_report``): the core UIs, any enabled Synology
passthrough services, and the Layer 2 services. Each entry is then checked
against reality two ways:

1. *Router present* — is a router matching the hostname in Traefik's live
   ``/api/http/routers`` list?
2. *Reachability* — an end-to-end probe through Traefik's own entrypoints
   (``https://<traefik>/`` with the ``Host`` header set; routing is by Host, so
   the SNI/cert mismatch is fine with verification off). Auth-gated apps
   answering 401/403 are alive. If HTTPS fails at the transport level we fall
   back to plain HTTP, where the https-redirect middleware's 301/308 still
   proves the router is live.

Important product nuance: the Synology entries are *routed via Syrvis, not
managed by it* — we affect their reachability by routing them, so we report
route health and nothing more (``managed: false``).

Read-only and never 500s: every layer degrades to an ``error``/``note`` field
or per-entry ``unknown`` reachability.
"""

import asyncio
import re
from typing import Any, Dict, Optional, Set, Tuple
from urllib.parse import urlsplit

import httpx
from fastapi import APIRouter, Depends

from ..deps import get_settings_dep
from ..settings import DashboardSettings

router = APIRouter(prefix="/api", tags=["routes"])

# Backticked hostnames inside a Traefik Host(...) rule matcher.
_HOST_RULE_RE = re.compile(r"\bHost\(([^)]*)\)")
_BACKTICKED_RE = re.compile(r"`([^`]+)`")

# 401/403 mean the app answered and is gating access — that's alive.
_AUTH_GATED = {401, 403}


def _hostnames_report() -> Dict[str, Any]:
    """The declared-routes report; degrades to an error field, never raises."""
    try:
        from syrviscore.hostnames import build_report

        return build_report()
    except Exception as exc:  # noqa: BLE001 - config unreadable, import failure, ...
        return {"domain": None, "traefik_ip": None, "entries": [], "error": str(exc)}


def _rule_hosts(rule: str) -> Set[str]:
    """Extract the hostnames a Traefik router rule matches on."""
    hosts: Set[str] = set()
    for match in _HOST_RULE_RE.finditer(rule or ""):
        hosts.update(h.lower() for h in _BACKTICKED_RE.findall(match.group(1)))
    return hosts


async def _live_router_hosts(settings: DashboardSettings) -> Tuple[bool, Set[str], Optional[str]]:
    """Fetch Traefik's live routers -> (api_ok, hostnames routed, note on failure)."""
    url = settings.traefik_url.rstrip("/") + "/api/http/routers"
    try:
        timeout = httpx.Timeout(settings.probe_timeout_s)
        async with httpx.AsyncClient(timeout=timeout) as http:
            resp = await http.get(url)
        if resp.status_code != 200:
            return False, set(), "traefik API returned {}".format(resp.status_code)
        hosts: Set[str] = set()
        for entry in resp.json():
            if isinstance(entry, dict):
                hosts.update(_rule_hosts(entry.get("rule", "")))
        return True, hosts, None
    except Exception as exc:  # noqa: BLE001 - unreachable, bad JSON, ...
        return False, set(), "traefik API unreachable: {}".format(exc.__class__.__name__)


async def _probe(
    http: httpx.AsyncClient, hostname: str, https_url: str, http_url: str
) -> Tuple[str, Optional[int], str]:
    """End-to-end probe of one hostname through Traefik's entrypoints.

    Returns ``(level, http_code, detail)`` where level is:
    - ``backend`` — the route answered through Traefik (2xx/3xx/401/403 over https)
    - ``router``  — https transport failed but plain http answered (e.g. the
      301/308 https-redirect middleware): the router is live, the backend unproven
    - ``failed``  — a response came back but not a healthy one (404/5xx)
    - ``none``    — nothing answered on either entrypoint
    """
    headers = {"Host": hostname}
    try:
        resp = await http.get(https_url, headers=headers)
    except Exception as exc:  # noqa: BLE001 - transport-level failure -> http fallback
        https_err = exc.__class__.__name__
        try:
            resp = await http.get(http_url, headers=headers)
        except Exception:  # noqa: BLE001
            return "none", None, "no response through traefik (https: {})".format(https_err)
        code = resp.status_code
        if 200 <= code < 400 or code in _AUTH_GATED:
            detail = "https failed ({}); http entrypoint answered HTTP {}".format(https_err, code)
            return "router", code, detail
        return "failed", code, "https failed ({}); http returned HTTP {}".format(https_err, code)
    code = resp.status_code
    if 200 <= code < 400 or code in _AUTH_GATED:
        return "backend", code, "HTTP {} via traefik".format(code)
    return "failed", code, "HTTP {} via traefik".format(code)


def _classify(
    level: str, code: Optional[int], detail: str, router_present: bool, api_ok: bool
) -> Dict[str, Any]:
    """Fold the probe result + live-router knowledge into one reachability verdict."""
    if level == "backend":
        return {"status": "ok", "http_code": code, "detail": detail}
    if level == "router":
        return {"status": "degraded", "http_code": code, "detail": detail}
    # The probe failed outright — decide between degraded/down/unknown.
    if not api_ok:
        return {
            "status": "unknown",
            "http_code": code,
            "detail": detail + "; traefik API unreachable, router state unknown",
        }
    if router_present:
        return {
            "status": "degraded",
            "http_code": code,
            "detail": detail + "; router present but backend probe failed",
        }
    return {"status": "down", "http_code": code, "detail": detail + "; no router for this host"}


@router.get("/routes")
async def routes(settings: DashboardSettings = Depends(get_settings_dep)) -> dict:
    """Every routed hostname with router presence + end-to-end reachability."""
    report = await asyncio.to_thread(_hostnames_report)
    api_ok, live_hosts, api_note = await _live_router_hosts(settings)

    out: Dict[str, Any] = {
        "domain": report.get("domain"),
        "traefik_ip": report.get("traefik_ip"),
        "traefik_api_ok": api_ok,
        "entries": [],
    }
    if report.get("error"):
        out["error"] = report["error"]
    if api_note:
        out["note"] = api_note

    declared = report.get("entries") or []
    if not declared:
        return out

    # Probe every hostname through Traefik's own entrypoints, concurrently.
    entry_host = urlsplit(settings.traefik_url).hostname or "traefik"
    https_url = "https://{}/".format(entry_host)
    http_url = "http://{}/".format(entry_host)
    timeout = httpx.Timeout(settings.probe_timeout_s)
    async with httpx.AsyncClient(timeout=timeout, verify=False, follow_redirects=False) as http:
        probes = await asyncio.gather(
            *(_probe(http, e.get("hostname", ""), https_url, http_url) for e in declared)
        )

    for entry, (level, code, detail) in zip(declared, probes):
        enriched = dict(entry)
        hostname = str(entry.get("hostname", "")).lower()
        present = api_ok and hostname in live_hosts
        # Synology services are routed via Syrvis but NOT managed by it — we
        # only affect (and therefore only report on) their route health.
        enriched["managed"] = entry.get("kind") != "synology"
        enriched["router_present"] = present
        enriched["reachability"] = _classify(level, code, detail, present, api_ok)
        out["entries"].append(enriched)

    return out
