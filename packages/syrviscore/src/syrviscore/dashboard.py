"""Auto-generated Grafana dashboard for the SyrvisCore layers.

A deterministic projection, the same class as :func:`hostnames.build_report`:
the set of containers THIS instance declares (the core tier + every Layer 2
service) becomes a Grafana dashboard with a top-level overview row scoped to the
syrvis-managed set, then one row per service keyed by its ``container_name``.
Each service row carries the STANDARD container measurements (running, health,
restarts, uptime); a service may add domain-specific panels via the optional
``dashboard:`` block in its manifest (see service_schema ``_validate_dashboard``).

SyrvisCore projects its own intent; a deployment repo provisions the result into
Grafana (design/00 renderer pattern; design/15/16 keep the collector + Grafana
in the estate). To stay generic this module names NOTHING estate-specific — the
collector metric contract and the datasource UID are parameters on
:class:`Collector`, whose defaults match the common docker-health-exporter +
VictoriaMetrics reference stack. Override them for a different collector.

Kept import-light and Python 3.8-clean (it runs on the DSM 3.8 CLI).
"""

import json
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

# Additive-safe contract version for the generated model. Bump only on a
# breaking shape change (consumers may gate on it).
DASHBOARD_SCHEMA_VERSION = 1

_GRID_W = 24  # Grafana's fixed grid width


@dataclass
class Collector:
    """The metric contract the generated panels query.

    Defaults match docker-health-exporter (metrics keyed by ``name=``) scraped
    into VictoriaMetrics with datasource UID ``victoriametrics``. Every field is
    overridable so the generator names no estate-specific policy.
    """

    datasource_uid: str = "victoriametrics"
    name_label: str = "name"
    running: str = "docker_container_running"
    health: str = "docker_container_health_status"
    unhealthy_status: str = "unhealthy"
    restarts: str = "docker_container_restart_count"
    started: str = "docker_container_started_at_seconds"
    collector_job: str = "docker-health"


@dataclass
class DashService:
    """One SyrvisCore-managed container, as the dashboard sees it."""

    name: str  # display / declaration name
    container_name: str  # the metric `name=` label value
    kind: str = "service"  # "core" | "service"
    critical: bool = False
    tier: str = ""
    enabled: bool = True
    panels: List[Dict[str, Any]] = field(default_factory=list)  # custom dashboard: panels
    # For the per-service deep-dive header (the "what is this + links" text panel).
    description: str = ""  # one-liner (manifest description / built-in for core)
    homepage: str = ""  # the service's homepage/repo
    about: str = ""  # optional longer markdown blurb (dashboard.about)
    links: List[Dict[str, str]] = field(default_factory=list)  # dashboard.links [{title,url}]


# Built-in blurbs + homepages for the core tier (which has no manifest to read).
_CORE_ABOUT = {
    "traefik": (
        "Reverse proxy + TLS terminator — the routing substrate every "
        "service is reached through.",
        "https://traefik.io",
    ),
    "portainer": (
        "Container-management UI — the fallback console for the Docker " "host.",
        "https://www.portainer.io",
    ),
    "cloudflared": (
        "Cloudflare Tunnel daemon — the outbound-only connector that "
        "publishes `tunnel`-exposed services without open ports.",
        "https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/",
    ),
    "dashboard": (
        "The SyrvisCore web dashboard — a thin adapter over the same "
        "deterministic core the CLI uses.",
        "https://github.com/kevinteg/SyrvisCore",
    ),
    "cloudflare_ddns": (
        "Keeps a DNS record pointed at the home public IP (the " "pre-tunnel remote-access path).",
        "https://github.com/favonia/cloudflare-ddns",
    ),
}


# ---------------------------------------------------------------------------
# Enumeration — best-effort, never raises (mirrors hostnames.build_report).
# ---------------------------------------------------------------------------
def collect_services(env_path: Optional[str] = None) -> List[DashService]:
    """Enumerate the containers this instance declares, core tier first.

    Any unreadable source degrades to fewer entries, never an exception — the
    same contract as the read-only report/endpoints.
    """
    services: List[DashService] = []

    # Core tier (from the declared stack). Primordial traefik+portainer are the
    # routing substrate → treated as critical; optional ones only when enabled.
    try:
        from . import stack as stack_mod

        stack = stack_mod.load_stack()
        for name in stack_mod.ALL_SERVICES:
            if not stack.is_enabled(name):
                continue
            about, homepage = _CORE_ABOUT.get(name, ("", ""))
            services.append(
                DashService(
                    name=name,
                    container_name=stack_mod.CONTAINER_NAME.get(name, name),
                    kind="core",
                    critical=name in stack_mod.PRIMORDIAL,
                    enabled=True,
                    description=about,
                    homepage=homepage,
                )
            )
    except Exception:  # noqa: BLE001 - SYRVIS_HOME unresolved, bad file, etc.
        pass

    # Layer 2 services (each manifest carries container_name/critical/tier and an
    # optional dashboard: block). Load definitions directly so we see fields the
    # summary list() omits.
    try:
        from .service_manager import ServiceManager
        from .service_schema import load_service_definition

        mgr = ServiceManager()
        services_dir = mgr.services_dir
        if services_dir.exists():
            for service_dir in sorted(services_dir.iterdir(), key=lambda p: p.name):
                if not service_dir.is_dir():
                    continue
                manifest = service_dir / "syrvis-service.yaml"
                if not manifest.exists():
                    continue
                try:
                    sd = load_service_definition(manifest)
                except Exception:  # noqa: BLE001 - a broken manifest just drops out
                    continue
                dash = sd.dashboard or {}
                panels = dash.get("panels") if isinstance(dash.get("panels"), list) else []
                links = dash.get("links") if isinstance(dash.get("links"), list) else []
                services.append(
                    DashService(
                        name=sd.name,
                        container_name=sd.container_name or sd.name,
                        kind="service",
                        critical=bool(sd.critical),
                        tier=sd.tier or "",
                        enabled=bool(sd.enabled),
                        panels=panels,
                        description=sd.description or "",
                        homepage=sd.homepage or "",
                        about=str(dash.get("about") or ""),
                        links=links,
                    )
                )
    except Exception:  # noqa: BLE001
        pass

    return services


# ---------------------------------------------------------------------------
# Grafana model builders
# ---------------------------------------------------------------------------
class _Layout:
    """Sequential panel id + y-cursor bookkeeping for a flat panel list."""

    def __init__(self) -> None:
        self._id = 0
        self.y = 0
        self.panels: List[Dict[str, Any]] = []

    def next_id(self) -> int:
        self._id += 1
        return self._id

    def add(self, panel: Dict[str, Any]) -> None:
        self.panels.append(panel)


def _ds(col: Collector) -> Dict[str, str]:
    return {"type": "prometheus", "uid": col.datasource_uid}


def _target(col: Collector, expr: str, instant: bool, fmt: Optional[str] = None) -> Dict[str, Any]:
    t: Dict[str, Any] = {
        "refId": "A",
        "datasource": _ds(col),
        "expr": expr,
        "legendFormat": "{{%s}}" % col.name_label,
    }
    if instant:
        t["instant"] = True
    if fmt:
        t["format"] = fmt
    return t


def _row(lay: _Layout, title: str) -> None:
    lay.add(
        {
            "id": lay.next_id(),
            "type": "row",
            "title": title,
            "collapsed": False,
            "gridPos": {"h": 1, "w": _GRID_W, "x": 0, "y": lay.y},
        }
    )
    lay.y += 1


def _stat(
    lay: _Layout,
    col: Collector,
    title: str,
    expr: str,
    x: int,
    w: int,
    *,
    y: Optional[int] = None,
    mappings: Optional[List[Dict[str, Any]]] = None,
    thresholds: Optional[Dict[str, Any]] = None,
    unit: str = "short",
    color_mode: str = "value",
    fixed_color: Optional[str] = None,
) -> None:
    defaults: Dict[str, Any] = {"unit": unit, "decimals": 0}
    if mappings:
        defaults["mappings"] = mappings
    if thresholds:
        defaults["thresholds"] = thresholds
    if fixed_color:
        defaults["color"] = {"mode": "fixed", "fixedColor": fixed_color}
    lay.add(
        {
            "id": lay.next_id(),
            "type": "stat",
            "title": title,
            "gridPos": {"h": 4, "w": w, "x": x, "y": lay.y if y is None else y},
            "datasource": _ds(col),
            "targets": [_target(col, expr, instant=True)],
            "options": {
                "reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
                "colorMode": color_mode,
                "graphMode": "none",
                "textMode": "auto",
            },
            "fieldConfig": {"defaults": defaults, "overrides": []},
        }
    )


def _timeseries(
    lay: _Layout, col: Collector, title: str, expr: str, x: int, w: int, y: int, unit: str = "short"
) -> None:
    lay.add(
        {
            "id": lay.next_id(),
            "type": "timeseries",
            "title": title,
            "gridPos": {"h": 8, "w": w, "x": x, "y": y},
            "datasource": _ds(col),
            "targets": [_target(col, expr, instant=False)],
            "fieldConfig": {
                "defaults": {
                    "unit": unit,
                    "custom": {
                        "drawStyle": "line",
                        "fillOpacity": 10,
                        "lineWidth": 1,
                        "showPoints": "auto",
                    },
                },
                "overrides": [],
            },
            "options": {
                "legend": {"displayMode": "list", "placement": "bottom", "showLegend": True},
                "tooltip": {"mode": "multi", "sort": "desc"},
            },
        }
    )


def _table(lay: _Layout, col: Collector, title: str, expr: str, x: int, w: int, y: int) -> None:
    lay.add(
        {
            "id": lay.next_id(),
            "type": "table",
            "title": title,
            "gridPos": {"h": 8, "w": w, "x": x, "y": y},
            "datasource": _ds(col),
            "targets": [_target(col, expr, instant=True, fmt="table")],
            "transformations": [
                {
                    "id": "organize",
                    "options": {
                        "excludeByName": {
                            "Time": True,
                            "job": True,
                            "instance": True,
                            "instance_role": True,
                            "__name__": True,
                        },
                        "renameByName": {"Value": "running", col.name_label: "container"},
                    },
                }
            ],
            "fieldConfig": {
                "defaults": {"custom": {"align": "auto", "filterable": True}},
                "overrides": [
                    {
                        "matcher": {"id": "byName", "options": "running"},
                        "properties": [
                            {
                                "id": "custom.cellOptions",
                                "value": {"type": "color-background", "mode": "basic"},
                            },
                            {
                                "id": "mappings",
                                "value": [_UPDOWN_MAP],
                            },
                            {"id": "thresholds", "value": _UPDOWN_THRESH},
                        ],
                    }
                ],
            },
            "options": {"showHeader": True, "sortBy": [{"displayName": "running", "desc": False}]},
        }
    )


# Shared value mappings / thresholds.
_UPDOWN_MAP = {
    "type": "value",
    "options": {"0": {"text": "down", "color": "red"}, "1": {"text": "up", "color": "green"}},
}
_UPDOWN_THRESH = {
    "mode": "absolute",
    "steps": [{"color": "red", "value": None}, {"color": "green", "value": 1}],
}
_ALARM_THRESH = {
    "mode": "absolute",
    "steps": [{"color": "green", "value": None}, {"color": "red", "value": 1}],
}
_RESTART_THRESH = {
    "mode": "absolute",
    "steps": [
        {"color": "green", "value": None},
        {"color": "yellow", "value": 1},
        {"color": "red", "value": 5},
    ],
}


def _selector(col: Collector, container: str, extra: str = "") -> str:
    """A single-container label selector, e.g. ``{name="traefik"}``."""
    inner = '{}="{}"'.format(col.name_label, container)
    if extra:
        inner += "," + extra
    return "{" + inner + "}"


def _set_selector(col: Collector, containers: List[str], extra: str = "") -> str:
    """A regex selector scoping to the whole syrvis set, e.g. ``{name=~"a|b"}``."""
    if not containers:
        # Match nothing rather than everything, so the overview never silently
        # aggregates unrelated estate containers when the set is empty.
        alt = "$^"
    else:
        alt = "|".join(containers)
    inner = '{}=~"{}"'.format(col.name_label, alt)
    if extra:
        inner += "," + extra
    return "{" + inner + "}"


def build_dashboard(
    services: List[DashService],
    collector: Optional[Collector] = None,
    title: str = "SyrvisCore — service metrics",
    uid: str = "syrvis-overview",
) -> Dict[str, Any]:
    """Project the declared services into a Grafana dashboard model (a dict)."""
    col = collector or Collector()
    lay = _Layout()
    containers = [s.container_name for s in services]

    # ── Overview row (scoped to the syrvis-managed set) ──────────────────────
    _row(lay, "SyrvisCore — overview")
    total = len(services)
    _stat(
        lay,
        col,
        "Collector up",
        'up{{job="{}"}}'.format(col.collector_job),
        x=0,
        w=6,
        mappings=[
            {
                "type": "value",
                "options": {
                    "0": {"text": "DOWN", "color": "red"},
                    "1": {"text": "UP", "color": "green"},
                },
            }
        ],
        thresholds=_UPDOWN_THRESH,
        color_mode="background",
    )
    _stat(
        lay,
        col,
        "Services running (of {})".format(total),
        "sum({}{})".format(col.running, _set_selector(col, containers)),
        x=6,
        w=6,
        fixed_color="green",
        color_mode="value",
    )
    _stat(
        lay,
        col,
        "Unhealthy",
        "sum({}{})".format(
            col.health, _set_selector(col, containers, 'status="{}"'.format(col.unhealthy_status))
        ),
        x=12,
        w=6,
        thresholds=_ALARM_THRESH,
        color_mode="background",
    )
    _stat(
        lay,
        col,
        "Restarts (1h)",
        "sum(increase({}{}[1h]))".format(col.restarts, _set_selector(col, containers)),
        x=18,
        w=6,
        thresholds=_RESTART_THRESH,
        color_mode="background",
    )
    lay.y += 4
    _table(
        lay,
        col,
        "Service state (1=up / 0=down)",
        "{}{}".format(col.running, _set_selector(col, containers)),
        x=0,
        w=_GRID_W,
        y=lay.y,
    )
    lay.y += 8

    # ── Per-service rows ─────────────────────────────────────────────────────
    for svc in services:
        cn = svc.container_name
        tags = []
        if svc.kind == "core":
            tags.append("core")
        if svc.critical:
            tags.append("critical")
        if svc.tier:
            tags.append(svc.tier)
        if not svc.enabled:
            tags.append("disabled")
        suffix = "  [{}]".format(", ".join(tags)) if tags else ""
        _row(lay, "{} — {}{}".format(svc.name, cn, suffix))
        _stat(
            lay,
            col,
            "Running",
            "{}{}".format(col.running, _selector(col, cn)),
            x=0,
            w=6,
            mappings=[_UPDOWN_MAP],
            thresholds=_UPDOWN_THRESH,
            color_mode="background",
        )
        _stat(
            lay,
            col,
            "Unhealthy",
            "sum({}{})".format(
                col.health, _selector(col, cn, 'status="{}"'.format(col.unhealthy_status))
            ),
            x=6,
            w=6,
            thresholds=_ALARM_THRESH,
            color_mode="background",
        )
        _stat(
            lay,
            col,
            "Restarts (1h)",
            "increase({}{}[1h])".format(col.restarts, _selector(col, cn)),
            x=12,
            w=6,
            thresholds=_RESTART_THRESH,
            color_mode="background",
        )
        _stat(
            lay,
            col,
            "Uptime",
            "time() - {}{}".format(col.started, _selector(col, cn)),
            x=18,
            w=6,
            unit="s",
            color_mode="value",
        )
        lay.y += 4

        # Optional domain-specific panels declared by the service manifest,
        # laid out two-per-band below the standard stats. ${container} expands
        # to this service's container_name so exprs are reusable across services.
        if svc.panels:
            x = 0
            band_y = lay.y
            for panel in svc.panels:
                expr = str(panel.get("expr", "")).replace("${container}", cn)
                ptitle = str(panel.get("title", "panel"))
                kind = panel.get("kind", "timeseries")
                unit = panel.get("unit", "short")
                w = 12
                if kind == "stat":
                    _stat(lay, col, ptitle, expr, x=x, w=w, y=band_y, unit=unit)
                elif kind == "table":
                    _table(lay, col, ptitle, expr, x=x, w=w, y=band_y)
                else:  # timeseries | gauge → a timeseries panel
                    _timeseries(lay, col, ptitle, expr, x=x, w=w, y=band_y, unit=unit)
                x += w
                if x >= _GRID_W:  # band full → wrap to the next 8-high band
                    x = 0
                    band_y += 8
            # leave the cursor below the last (possibly partial) band
            lay.y = band_y + (8 if x > 0 else 0)

    return _envelope(uid, title, lay.panels)


def _envelope(uid: str, title: str, panels: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Wrap a panel list in the standard Grafana dashboard object."""
    return {
        "uid": uid,
        "title": title,
        "tags": ["syrviscore", "generated"],
        "editable": True,
        "schemaVersion": 39,
        "version": 1,
        "refresh": "1m",
        "time": {"from": "now-6h", "to": "now"},
        "timezone": "browser",
        "templating": {"list": []},
        "annotations": {"list": []},
        "panels": panels,
        # Provenance marker so a human (and a drift check) can tell this file is
        # machine-generated and should be regenerated, not hand-edited.
        "__syrviscore": {"generated": True, "schema": DASHBOARD_SCHEMA_VERSION},
    }


def _uid_for(name: str, prefix: str = "syrvis") -> str:
    """A Grafana-safe uid (a-zA-Z0-9-_, <=40 chars) for a service dashboard."""
    safe = "".join(c if (c.isalnum() or c in "-_") else "-" for c in name.lower())
    return "{}-{}".format(prefix, safe)[:40]


def _text_panel(lay: _Layout, markdown: str, height: int) -> None:
    lay.add(
        {
            "id": lay.next_id(),
            "type": "text",
            "title": "",
            "gridPos": {"h": height, "w": _GRID_W, "x": 0, "y": lay.y},
            "options": {"mode": "markdown", "content": markdown},
        }
    )
    lay.y += height


def _header_markdown(svc: DashService) -> str:
    """The 'what is this + links' blurb for a service dashboard's top panel."""
    lines = ["## {}".format(svc.name)]
    tags = []
    if svc.kind == "core":
        tags.append("core")
    if svc.critical:
        tags.append("critical")
    if svc.tier:
        tags.append(svc.tier)
    if tags:
        lines.append("_{}_ · container `{}`".format(", ".join(tags), svc.container_name))
    else:
        lines.append("container `{}`".format(svc.container_name))
    if svc.description:
        lines.append("")
        lines.append(svc.description)
    if svc.about:
        lines.append("")
        lines.append(svc.about)
    link_md = []
    if svc.homepage:
        link_md.append("[homepage]({})".format(svc.homepage))
    for link in svc.links:
        link_md.append("[{}]({})".format(link.get("title", "link"), link.get("url", "")))
    if link_md:
        lines.append("")
        lines.append("**Links:** " + " · ".join(link_md))
    return "\n".join(lines)


def build_service_dashboard(
    svc: DashService, collector: Optional[Collector] = None
) -> Dict[str, Any]:
    """A per-service deep-dive: a header (what it is + links), the standard
    container measurements, then the service's own declared panels."""
    col = collector or Collector()
    lay = _Layout()
    cn = svc.container_name

    # Header text panel.
    md = _header_markdown(svc)
    _text_panel(lay, md, height=max(3, min(8, md.count("\n") + 2)))

    # Standard container measurements (this container only).
    _row(lay, "Health")
    _stat(
        lay,
        col,
        "Running",
        "{}{}".format(col.running, _selector(col, cn)),
        x=0,
        w=6,
        mappings=[_UPDOWN_MAP],
        thresholds=_UPDOWN_THRESH,
        color_mode="background",
    )
    _stat(
        lay,
        col,
        "Unhealthy",
        "sum({}{})".format(
            col.health, _selector(col, cn, 'status="{}"'.format(col.unhealthy_status))
        ),
        x=6,
        w=6,
        thresholds=_ALARM_THRESH,
        color_mode="background",
    )
    _stat(
        lay,
        col,
        "Restarts (1h)",
        "increase({}{}[1h])".format(col.restarts, _selector(col, cn)),
        x=12,
        w=6,
        thresholds=_RESTART_THRESH,
        color_mode="background",
    )
    _stat(
        lay,
        col,
        "Uptime",
        "time() - {}{}".format(col.started, _selector(col, cn)),
        x=18,
        w=6,
        unit="s",
        color_mode="value",
    )
    lay.y += 4
    _timeseries(
        lay,
        col,
        "Restarts over time",
        "increase({}{}[1h])".format(col.restarts, _selector(col, cn)),
        x=0,
        w=24,
        y=lay.y,
    )
    lay.y += 8

    # Service-specific panels (domain metrics from the manifest dashboard: block).
    if svc.panels:
        _row(lay, "{} metrics".format(svc.name))
        x = 0
        band_y = lay.y
        for panel in svc.panels:
            expr = str(panel.get("expr", "")).replace("${container}", cn)
            ptitle = str(panel.get("title", "panel"))
            kind = panel.get("kind", "timeseries")
            unit = panel.get("unit", "short")
            w = 12
            if kind == "stat":
                _stat(lay, col, ptitle, expr, x=x, w=w, y=band_y, unit=unit)
            elif kind == "table":
                _table(lay, col, ptitle, expr, x=x, w=w, y=band_y)
            else:
                _timeseries(lay, col, ptitle, expr, x=x, w=w, y=band_y, unit=unit)
            x += w
            if x >= _GRID_W:
                x = 0
                band_y += 8
        lay.y = band_y + (8 if x > 0 else 0)

    return _envelope(_uid_for(svc.name), "SyrvisCore — {}".format(svc.name), lay.panels)


def build_all(
    services: List[DashService], collector: Optional[Collector] = None
) -> List[Dict[str, Any]]:
    """The overview dashboard plus one deep-dive per service (in that order)."""
    col = collector or Collector()
    out = [build_dashboard(services, col)]
    out.extend(build_service_dashboard(svc, col) for svc in services)
    return out


def generate(
    env_path: Optional[str] = None,
    collector: Optional[Collector] = None,
    title: str = "SyrvisCore — service metrics",
    uid: str = "syrvis-overview",
) -> Dict[str, Any]:
    """Collect the declared services and build the overview dashboard model."""
    return build_dashboard(collect_services(env_path), collector, title=title, uid=uid)


def generate_all(
    env_path: Optional[str] = None, collector: Optional[Collector] = None
) -> List[Dict[str, Any]]:
    """Collect the declared services and build the overview + per-service models."""
    return build_all(collect_services(env_path), collector)


def to_json(model: Dict[str, Any]) -> str:
    """Serialize the model to Grafana-ready JSON (stable key order)."""
    return json.dumps(model, indent=2, sort_keys=False)
