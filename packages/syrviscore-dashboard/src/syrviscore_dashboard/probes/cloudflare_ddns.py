"""Cloudflare DDNS probe — is the home IP in sync with the DNS records?

DDNS is "configured" only when a ``CLOUDFLARE_API_TOKEN`` is set. When it is, we
detect the current public IP and compare it against the A record(s) named in
``CLOUDFLARE_DDNS_RECORDS`` via the Cloudflare API (read-only GETs). The favonia
container is what actually updates the records; this probe tells you whether the
records currently match reality.
"""

from typing import List, Optional

import httpx

from .base import ProbeResult, Status
from ._config import component_enabled, raw_value


def _records() -> List[str]:
    raw = raw_value("CLOUDFLARE_DDNS_RECORDS") or ""
    return [r.strip() for r in raw.split(",") if r.strip()]


async def _public_ip(settings, http: httpx.AsyncClient) -> Optional[str]:
    resp = await http.get(settings.public_ip_url)
    resp.raise_for_status()
    text = resp.text.strip()
    return text or None


async def _zones(settings, http: httpx.AsyncClient, token: str) -> List[dict]:
    resp = await http.get(
        settings.cloudflare_api_url.rstrip("/") + "/zones",
        headers={"Authorization": "Bearer " + token},
        params={"per_page": 50},
    )
    resp.raise_for_status()
    return resp.json().get("result", []) or []


def _zone_for(host: str, zones: List[dict]) -> Optional[dict]:
    # Longest matching zone name that is a suffix of the record host.
    matches = [z for z in zones if host == z.get("name") or host.endswith("." + str(z.get("name")))]
    return max(matches, key=lambda z: len(z.get("name", ""))) if matches else None


async def _record_ip(settings, http, token, zone_id, host) -> Optional[str]:
    resp = await http.get(
        "{}/zones/{}/dns_records".format(settings.cloudflare_api_url.rstrip("/"), zone_id),
        headers={"Authorization": "Bearer " + token},
        params={"type": "A", "name": host},
    )
    resp.raise_for_status()
    result = resp.json().get("result", []) or []
    return result[0].get("content") if result else None


async def probe_ddns(settings, http: httpx.AsyncClient) -> ProbeResult:
    if not component_enabled("cloudflare_ddns"):
        return ProbeResult(
            "cloudflare_ddns", Status.NOT_CONFIGURED, "no CLOUDFLARE_API_TOKEN configured"
        )

    token = raw_value("CLOUDFLARE_API_TOKEN")
    records = _records()

    try:
        public_ip = await _public_ip(settings, http)
    except httpx.HTTPError as exc:
        return ProbeResult(
            "cloudflare_ddns", Status.DEGRADED, "could not detect public IP: {}".format(exc)
        )

    extra = {"public_ip": public_ip, "records": []}

    if not records:
        return ProbeResult(
            "cloudflare_ddns",
            Status.DEGRADED,
            "token set but CLOUDFLARE_DDNS_RECORDS is empty",
            extra=extra,
        )

    try:
        zones = await _zones(settings, http, token)
        checked = []
        for host in records:
            zone = _zone_for(host, zones)
            if not zone:
                checked.append({"name": host, "record_ip": None, "in_sync": False})
                continue
            record_ip = await _record_ip(settings, http, token, zone["id"], host)
            checked.append(
                {
                    "name": host,
                    "record_ip": record_ip,
                    "in_sync": record_ip == public_ip and record_ip is not None,
                }
            )
        extra["records"] = checked
    except httpx.HTTPError as exc:
        return ProbeResult(
            "cloudflare_ddns",
            Status.DEGRADED,
            "Cloudflare API error: {}".format(exc),
            extra=extra,
        )

    all_synced = bool(checked) and all(r["in_sync"] for r in checked)
    if all_synced:
        return ProbeResult(
            "cloudflare_ddns", Status.OK, "all records match {}".format(public_ip), extra=extra
        )
    return ProbeResult(
        "cloudflare_ddns",
        Status.DEGRADED,
        "one or more records do not match the public IP",
        extra=extra,
    )
