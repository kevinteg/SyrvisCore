"""Declarative service loading: the ``config/services.d/`` reconciler (phase 1).

``services.d/`` holds one validated ``syrvis-service.yaml`` declaration per file
(filename must equal ``name:``) — the *intent* every driver writes: home-tech's
IaC via rsync+ssh, the CLI's ``service run/add`` (which dual-write here), the
dashboard, and the MCP. ``syrvis reconcile`` converges the instance to it.

Failure isolation is the design's load-bearing requirement, and it is structural:

- LOAD:     every file parses/validates independently; a bad file marks only
            that service ``invalid`` — every other file proceeds.
- CONVERGE: every service converges independently (each is its own compose
            project); one failure is recorded and the loop continues.
- HEALTH:   a failing ``critical: true`` service makes the reconcile exit
            non-zero; a non-critical failure is reported but never fatal
            (``--strict`` promotes any failure to fatal; ``--boot`` demotes all).

Safety: installed-but-undeclared services are reported ``unmanaged`` and NEVER
touched unless an explicit prune policy (stop|remove|purge) is requested, and
destructive prune actions are flagged for caller-side confirmation gating.

See docs/service-loading-design.md for the full design.
"""

from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import yaml

from .errors import SyrvisError
from .service_schema import (
    DEPENDS_ON_HARD,
    DEPENDS_ON_HEALTHY,
    ServiceDefinition,
    load_service_definition,
)

DECLARATIONS_DIRNAME = "services.d"

PRUNE_POLICIES = ("stop", "remove", "purge")

# Keys that never affect the container itself — excluded when diffing a
# declaration against the installed manifest, so an orchestration-only change
# (e.g. flipping `critical`) can never trigger a container replace.
_ORCHESTRATION_KEYS = ("enabled", "critical")

# Docker statuses that mean "this container RAN and is now finished" — as
# opposed to `stopped` (no container exists at all: it was never started, or was
# removed) and `unknown` (the daemon could not be reached). Only these two can
# make a `restart: no` service TERMINAL; the other two are absence and
# ignorance, and neither is evidence of a completed run.
_TERMINAL_STATUSES = frozenset({"exited", "dead"})

# Action kinds, grouped by which direction they move a workload. Bring-up is
# ordered by REVERSED stop band (stores first); bring-down by the stop band
# itself, exactly like the shutdown walk.
_STOP_KINDS = frozenset({"stop", "prune_stop", "prune_remove", "prune_purge"})
_BRINGUP_KINDS = frozenset({"add", "replace", "start"})


class ReconcileError(SyrvisError):
    """A reconcile-level failure (not a per-service one — those are isolated)."""

    code = "reconcile_failed"


def get_declarations_dir(syrvis_home: Path) -> Path:
    return Path(syrvis_home) / "config" / DECLARATIONS_DIRNAME


def declaration_path(syrvis_home: Path, name: str) -> Path:
    return get_declarations_dir(syrvis_home) / "{}.yaml".format(name)


def load_declarations(
    syrvis_home: Path,
    tolerant: bool = False,
) -> Tuple[Dict[str, ServiceDefinition], List[Dict[str, str]]]:
    """Load every ``services.d/*.yaml`` with per-file failure isolation.

    Returns:
        (valid, invalid): ``valid`` maps name -> ServiceDefinition; ``invalid``
        is a list of ``{"file", "error"}`` rows — a broken file never blocks
        the others (the design's core requirement).

    ``tolerant`` (READ-ONLY callers only, e.g. the dashboard): before parsing,
    drop any TOP-LEVEL key this reader's schema doesn't recognise, so a
    declaration written for a NEWER schema field than this (possibly older,
    image-baked) reader knows still loads for display instead of being flagged
    "invalid". A real error (bad value on a known key, name mismatch) still
    surfaces. The strict default is for the deploy/reconcile path, which must
    NEVER silently ignore an unaudited key — that rejection is the trust boundary.
    """
    directory = get_declarations_dir(syrvis_home)
    valid: Dict[str, ServiceDefinition] = {}
    invalid: List[Dict[str, str]] = []
    if not directory.exists():
        return valid, invalid

    for path in sorted(directory.glob("*.yaml")):
        try:
            data = yaml.safe_load(path.read_text())
            if not isinstance(data, dict):
                raise ValueError("declaration must be a mapping")
            if tolerant:
                data = _drop_unknown_top_level_keys(data)
            service = ServiceDefinition.from_dict(data)
            if service.name != path.stem:
                raise ValueError(
                    "declares name {!r} — it must match its filename".format(service.name)
                )
            valid[service.name] = service
        except Exception as exc:  # noqa: BLE001 - isolation: report, keep loading
            invalid.append({"file": path.name, "error": str(exc)})

    # WHOLE-SET stage (design/63 D2): the per-file loop above cannot see cycles,
    # unknown targets, or whether an edge's target declares a healthcheck. A
    # service that fails it is moved out of `valid` — the DECLARING file goes
    # invalid, every other service proceeds, and isolation is preserved exactly
    # as it is for a syntax error.
    return _apply_graph_validation(valid, invalid)


def _drop_unknown_top_level_keys(data: Dict[str, Any]) -> Dict[str, Any]:
    """Return ``data`` minus top-level keys outside the current schema allowlist.

    Lets a READ-ONLY reader tolerate a declaration written for a newer schema (a
    field added after this reader was built) — the unknown field is simply not
    shown. NEVER call this on the deploy/install path: there, ``from_dict``'s
    rejection of an unaudited key is a deliberate trust boundary.
    """
    from .service_schema import ALLOWED_TOP_LEVEL_KEYS

    return {k: v for k, v in data.items() if k in ALLOWED_TOP_LEVEL_KEYS}


# ---------------------------------------------------------------------------
# The dependency graph (design/63 D1/D2/D3 — M1: validation + ordering only)
# ---------------------------------------------------------------------------


def build_dependency_graph(
    declarations: Dict[str, ServiceDefinition],
    invalid: Optional[List[Dict[str, str]]] = None,
) -> Dict[str, Any]:
    """Solve the declared ``depends_on`` edges over the whole declaration set.

    Returns::

        {"edges":   {name: [{"on": target, "readiness": class}, ...]},
         "errors":  {name: "why this declaration is invalid"},
         "depths":  {name: int},     # longest path from a root; the wave number
         "waves":   [[name, ...]],   # depths inverted, each wave sorted by name
         "blocked": {name: {"reason": ..., "dependency": ..., "why": ...}}}

    The FOUR whole-set rules, which is the whole of M1's D2 (there is no
    readiness gating here — that is M2; M1 is pure ordering):

    * **cycle** -> error on every member, naming the cycle. Detected over ALL
      edges, soft included: a soft edge still orders, so a soft cycle would
      still make the topological order undefined.
    * **unknown target** -> error on the DEPENDANT. Unknown is not the same as
      deliberately down: nothing on this instance declares that name at all,
      so the edge is a typo or a stale copy, and a typo must fail loudly.
    * **``healthy`` onto a target with no ``healthcheck:``** -> error on the
      dependant (design/63 D1, ``dep:F9``). Never a silent downgrade to
      ``started``: the declaration asks for a check the platform cannot make,
      and 2026-08-16 was a lesson in checks that report what they cannot see.
    * **hard edge onto a DISABLED / SHED / INVALID-FILE target** -> NOT an
      error. It is a plan-time ``blocked`` classification (design/63 D2 as
      AMENDED 2026-08-16, ``opc:F10``). As first written this was a validation
      error, which meant a partial load-shed made a RUNNING service's
      declaration invalid — reclassifying it ``unmanaged`` on the operator's own
      deliberate action, during exactly the window when the fleet is already
      degraded. And because "which services are disabled" flips under any apply
      (``opc:F9``), validity itself would have been non-deterministic.

    Shed is NOT read here — this function is pure over the declaration set, and
    the shed overlay is instance STATE. ``build_reconcile_plan`` folds it in
    (:func:`dependency_blocked`), which is also the only place it matters.
    """
    invalid_names = {
        row["file"][:-5] for row in (invalid or []) if row.get("file", "").endswith(".yaml")
    }

    edges: Dict[str, List[Dict[str, str]]] = {}
    errors: Dict[str, str] = {}
    for name, service in declarations.items():
        try:
            edges[name] = service.dependency_edges()
        except Exception as exc:  # noqa: BLE001 - a bad entry invalidates its own file
            edges[name] = []
            errors[name] = str(exc)

    # Rule 1+2: unknown targets and healthy-without-healthcheck.
    for name, service_edges in edges.items():
        for edge in service_edges:
            target, readiness = edge["on"], edge["readiness"]
            if target not in declarations:
                if target in invalid_names:
                    continue  # rule 4: blocked at plan time, not invalid here
                errors.setdefault(
                    name,
                    "depends_on target {!r} is not declared on this instance — an "
                    "edge onto a service that does not exist is a typo, not a "
                    "deliberately-down dependency".format(target),
                )
                continue
            if readiness == DEPENDS_ON_HEALTHY and not declarations[target].healthcheck:
                errors.setdefault(
                    name,
                    "depends_on {!r} asks for a 'healthy' gate but {!r} declares no "
                    "healthcheck: — declare one on the target, or write "
                    "'{}' honestly (design/63 D1: never a silent downgrade)".format(
                        "{}:{}".format(target, readiness), target, target
                    ),
                )

    # Rule 3: cycles. Every member of a cycle is invalid, and the message names
    # the ring — "a cycle exists" is useless at 3 a.m.
    for cycle in _find_cycles(edges):
        ring = " -> ".join(cycle + [cycle[0]])
        for member in cycle:
            errors.setdefault(member, "depends_on forms a dependency cycle: {}".format(ring))

    depths = _dependency_depths(edges, skip=set(errors))
    waves: List[List[str]] = []
    for name, depth in sorted(depths.items()):
        while len(waves) <= depth:
            waves.append([])
        waves[depth].append(name)

    return {
        "edges": edges,
        "errors": errors,
        "depths": depths,
        "waves": waves,
        "invalid_targets": sorted(invalid_names),
    }


def _find_cycles(edges: Dict[str, List[Dict[str, str]]]) -> List[List[str]]:
    """Every simple cycle's member list, via iterative DFS (3.8-clean, no recursion).

    Recursion would be fine at 39 services, but the declaration set is
    attacker-adjacent input and a stack overflow is not an error message.
    """
    color: Dict[str, int] = {}  # 0 unvisited, 1 on-stack, 2 done
    cycles: List[List[str]] = []
    seen_rings = set()
    for root in sorted(edges):
        if color.get(root, 0) != 0:
            continue
        stack: List[Tuple[str, int]] = [(root, 0)]
        path: List[str] = [root]
        color[root] = 1
        while stack:
            node, index = stack[-1]
            targets = [e["on"] for e in edges.get(node, [])]
            if index >= len(targets):
                color[node] = 2
                stack.pop()
                path.pop()
                continue
            stack[-1] = (node, index + 1)
            nxt = targets[index]
            if nxt not in edges:
                continue  # unknown target: rule 2 already reported it
            state = color.get(nxt, 0)
            if state == 1:
                ring = path[path.index(nxt) :]
                key = frozenset(ring)
                if key not in seen_rings:
                    seen_rings.add(key)
                    cycles.append(ring)
            elif state == 0:
                color[nxt] = 1
                path.append(nxt)
                stack.append((nxt, 0))
    return cycles


def _dependency_depths(
    edges: Dict[str, List[Dict[str, str]]], skip: Optional[set] = None
) -> Dict[str, int]:
    """Longest-path depth per node: 0 for a service that depends on nothing.

    This IS the topological order — if A depends on B then depth(A) > depth(B),
    so ascending depth is a valid topological sort AND a wave number (design/63
    D3's per-wave narration, M2). Nodes named in ``skip`` (already invalid) and
    unknown targets contribute nothing. Cycle members are bounded by the
    iteration cap rather than looping forever; they are invalid anyway.
    """
    skip = skip or set()
    depths = {name: 0 for name in edges}
    nodes = sorted(edges)
    for _ in range(len(nodes) + 1):
        changed = False
        for name in nodes:
            if name in skip:
                continue
            best = 0
            for edge in edges[name]:
                target = edge["on"]
                if target in edges and target != name:
                    best = max(best, depths[target] + 1)
            if best != depths[name]:
                depths[name] = best
                changed = True
        if not changed:
            break
    return depths


def dependency_blocked(
    declarations: Dict[str, ServiceDefinition],
    graph: Dict[str, Any],
    shed: Optional[Dict[str, Any]] = None,
) -> Dict[str, Dict[str, str]]:
    """Services whose bring-up is blocked by a HARD edge (design/63 D2 amended).

    ``{name: {"reason": "<D3 vocabulary>", "dependency": target, "why": kind}}``
    where ``kind`` is one of ``disabled`` / ``shed`` / ``invalid`` / ``blocked``
    (the last being the transitive case — a hard edge onto an already-blocked
    service).

    SOFT edges never block: "order when cheap, never gate" is the whole
    distinction the readiness class exists to carry. Propagation is transitive
    over hard edges only, and iterates to a fixed point so a chain of any depth
    resolves in one pass of the planner.
    """
    shed = shed or {}
    invalid_targets = set(graph.get("invalid_targets") or ())
    blocked: Dict[str, Dict[str, str]] = {}
    for _ in range(len(declarations) + 1):
        changed = False
        for name, service_edges in sorted(graph["edges"].items()):
            if name in blocked or name in graph["errors"]:
                continue
            for edge in service_edges:
                if edge["readiness"] not in DEPENDS_ON_HARD:
                    continue
                target = edge["on"]
                if target in shed:
                    why = "shed"
                elif target in invalid_targets:
                    why = "invalid"
                elif target in declarations and not declarations[target].enabled:
                    why = "disabled"
                elif target in blocked:
                    why = "blocked"
                else:
                    continue
                blocked[name] = {
                    "dependency": target,
                    "why": why,
                    "reason": "blocked (dependency {} {})".format(target, why),
                }
                changed = True
                break
        if not changed:
            break
    return blocked


def _apply_graph_validation(
    valid: Dict[str, ServiceDefinition], invalid: List[Dict[str, str]]
) -> Tuple[Dict[str, ServiceDefinition], List[Dict[str, str]]]:
    """Move graph-invalid declarations from ``valid`` into ``invalid``.

    Cheap short-circuit: an instance with no edges anywhere (every instance
    before design/63 M3's first subtree commit) pays one ``any()`` and returns
    the inputs untouched.
    """
    if not any(service.depends_on for service in valid.values()):
        return valid, invalid
    graph = build_dependency_graph(valid, invalid)
    if not graph["errors"]:
        return valid, invalid
    for name, error in sorted(graph["errors"].items()):
        valid.pop(name, None)
        invalid.append({"file": "{}.yaml".format(name), "error": error})
    return valid, invalid


def _content_dict(service: ServiceDefinition) -> Dict[str, Any]:
    """The container-affecting content of a definition (orchestration stripped)."""
    data = service.to_dict()
    for key in _ORCHESTRATION_KEYS:
        data.pop(key, None)
    return data


def _installed_manifests(manager) -> Dict[str, ServiceDefinition]:
    """Installed services by name; unloadable manifests surface as None entries."""
    installed: Dict[str, Optional[ServiceDefinition]] = {}
    services_dir = manager.services_dir
    if not services_dir.exists():
        return installed
    for service_dir in sorted(services_dir.iterdir()):
        if not service_dir.is_dir():
            continue
        manifest = service_dir / "syrvis-service.yaml"
        try:
            # A dir with NO manifest (crash mid-install, stray leftovers) is a
            # broken install: report it as present-but-unloadable so a matching
            # declaration plans a REPLACE instead of an ADD that would refuse
            # on the existing directory forever.
            installed[service_dir.name] = (
                load_service_definition(manifest) if manifest.exists() else None
            )
        except Exception:  # noqa: BLE001 - broken install -> replace candidate
            installed[service_dir.name] = None
    return installed


def _vanished_home_refusal(manager, name: str) -> Optional[str]:
    """Refusal message when an app home that ONCE held content is now gone.

    The pre-flight the convergence engine never had (incident 2026-08-16): the
    planner diffs declaration against manifest and never stats the filesystem
    under ``location:``, so a vanished ``<location>/syrviscore/apps/<name>`` was
    indistinguishable from a healthy one — and the compose generator then
    MANUFACTURED the tree plus an empty ``secrets.env`` and started a container
    against it. Absent means CREATE is the correct default for a declarative
    engine; it is the wrong default for a stateful app whose data is the thing
    being converged.

    Fires only on the high-water mark (:meth:`ServiceManager.mark_home_materialized`),
    so a genuinely new service is never blocked. Returns ``None`` when the home
    is fine, when nothing was ever recorded, or when the recorded state cannot
    be resolved (the ordinary per-service failure paths handle that, loudly).
    """
    try:
        state = manager.read_home_state(name)
    except Exception:  # noqa: BLE001 - a state read must never break planning
        return None
    if not state.get("home_materialized"):
        return None
    try:
        home = manager._app_home(name)
    except Exception:  # noqa: BLE001 - tampered manifest: the per-service path fails closed
        return None
    if home is None:
        # The manifest no longer resolves a home (removed location, unreadable
        # manifest); fall back to the path the mark itself recorded.
        home_str = state.get("home")
        if not home_str:
            return None
        home = Path(home_str)
    if manager._dir_nonempty(home):
        return None
    return (
        "app home {} has vanished (it previously held this app's data) — refusing "
        "to re-scaffold it and start {!r} against an empty tree. Almost certainly "
        "the volume root was RENAMED, not lost: run `ls -d /volume*/syrviscore*` "
        "and look for a 'syrviscore_1' sibling (DSM renames a volume root whose "
        "name collides with a shared folder at the first cold boot). Move it back "
        "before reconciling; nothing here will do it for you.".format(home, name)
    )


def _stop_band(service: Optional[ServiceDefinition]) -> int:
    """The declared ``shutdown.priority`` (lower stops FIRST; default 50).

    Read from the DECLARATION, not the installed manifest: the plan is about
    where a service is going, and an undeclared (prune) target has no intent to
    read, so it takes the default band.
    """
    from .lifecycle import DEFAULT_STOP_PRIORITY

    if service is None:
        return DEFAULT_STOP_PRIORITY
    try:
        return int((service.shutdown or {}).get("priority", DEFAULT_STOP_PRIORITY))
    except Exception:  # noqa: BLE001 - a malformed band never breaks planning
        return DEFAULT_STOP_PRIORITY


def _action_order_key(
    action: Dict[str, Any],
    declarations: Dict[str, ServiceDefinition],
    depths: Optional[Dict[str, int]] = None,
):
    """Sort key: TOPOLOGICAL order first, the shutdown band as the tie-breaker.

    Two orderings, folded into one key so there is exactly one sort (design/63
    D7: every ordering feature lands in this planner or it does not land).

    * **``depths``** (design/63 M1) is the dependency graph's longest-path depth
      — the topological primary. Bring-up ascends it (a store at depth 0 is
      issued before the consumer at depth 1); bring-down descends it (dependents
      drain before the store they use), which is the same reverse-topological
      walk D6 gives the shutdown side.
    * **the band** (dep:F23, SC-B's interim ordering) is what breaks ties WITHIN
      a wave. It is no longer the primary ordering, and it is not retired
      either: design/63 D6 keeps bands meaningful for the standalones and as the
      migration bridge, so an instance with NO edges sorts byte-identically to
      the band-only behaviour it had before the graph existed.

    Where an edge and a band disagree the EDGE wins (design/63 D6) — that is
    exactly what putting depth ahead of band in the tuple means.

    The interim ordering's own rationale, retained because it is still the
    no-edge behaviour:

    design/25 D6 claimed resume "starts in declaration order"; the reality was
    alphabetical-by-filename, because declarations load by sorted glob and the
    planner iterated them as-is. Alphabetical starts docker-health-exporter
    before docker-socket-proxy (its only data path), immich-legal-server before
    immich-legal-redis, and onyx-api before all three of its stores — and 15 of
    39 services have no healthcheck, so "healthchecks absorb that" was a hope,
    not a mechanism.

    The band order is the REVERSE of the shutdown walk, which is the one
    ordering the declarations already carry: stores stop last, so they start
    first. Bands are coarse and cross-subtree and cannot express "onyx-api needs
    onyx-relational-db *healthy*" — which is why they are now the tie-breaker
    and the graph is the primary. An UNDECLARED prune target has no depth (it
    has no declaration to read edges from) and takes depth 0.

    Bring-down actions keep the shutdown walk's own direction (consumers first),
    so a plan that both stops and starts things never tears a store out from
    under a consumer that is still running.
    """
    name = action["name"]
    band = _stop_band(declarations.get(name))
    depth = (depths or {}).get(name, 0)
    kind = action["kind"]
    if kind in _STOP_KINDS:
        return (0, -depth, band, name)
    if kind in _BRINGUP_KINDS:
        return (1, depth, -band, name)
    # `blocked` (and anything unknown): a report row, not an instruction — it
    # fails wherever it sits, so it sits last.
    return (2, 0, 0, name)


def build_reconcile_plan(
    manager,
    declarations: Dict[str, ServiceDefinition],
    invalid: List[Dict[str, str]],
    prune: Optional[str] = None,
) -> Dict[str, Any]:
    """Diff declared intent against installed/running state (read-only).

    Action kinds: ``add`` (materialize + start), ``replace`` (content differs;
    data dir preserved), ``start`` (declared, matching, not running), ``stop``
    (declared with ``enabled: false`` but running), ``blocked`` (a safety
    refusal — see :func:`_vanished_home_refusal`; never converted into any other
    action), and — only under an explicit prune policy — ``prune_stop`` /
    ``prune_remove`` / ``prune_purge`` for installed services with no
    declaration.

    THE SHED OVERLAY (design/65 D8, pulled forward): a service named in
    ``data/state/intent.json``'s ``shed[]`` is planned exactly as if its
    declaration said ``enabled: false``, whatever the declaration actually
    says. That is the whole mechanism by which a deliberate load-shed survives
    a GitOps apply: the apply rewrites ``enabled:``, the overlay outranks it,
    and the planner never emits a start for a shed service. A shed service that
    is somehow RUNNING is stopped — reconcile re-asserting the operator's
    decision is the point, not an accident. Shed names are reported in their
    own ``shed`` bucket rather than folded into ``disabled``, so "14 shed" and
    "14 turned off in git" never read the same.

    FOUR non-action buckets, all meaning "nothing owed here" for different
    reasons: ``disabled`` (git says off), ``shed`` (intent.json says off),
    ``terminal`` (a ``restart: no`` service that exited — see the branch below)
    and ``blocked`` (a hard ``depends_on`` edge onto a service that is
    deliberately down). None of them is drift.

    **``blocked`` the BUCKET vs ``blocked`` the ACTION.** They are different
    things and the distinction is load-bearing:

    * ``plan["blocked"]`` (design/63 D2 as amended, ``opc:F10``) — a hard edge
      whose target is disabled/shed/invalid. It is *not* an action and *not* a
      failure: a deliberate 14-service load-shed must not fail every hourly
      reconcile for every dependant in those subtrees. Exactly the reasoning
      that made ``terminal`` a bucket.
    * ``actions[kind == "blocked"]`` — the 0.9.x vanished-app-home SAFETY
      REFUSAL (:func:`_vanished_home_refusal`). That one IS a failed action, on
      purpose: converging it would destroy data.

    ACTION ORDER is topological over the dependency graph, with the shutdown
    band as the tie-breaker inside each wave (:func:`_action_order_key`,
    design/63 M1/D6). An instance with no declared edges orders exactly as the
    band-only interim did.
    """
    if prune is not None and prune not in PRUNE_POLICIES:
        raise ReconcileError(
            "prune policy must be one of {} (got {!r})".format(", ".join(PRUNE_POLICIES), prune)
        )

    from . import intent as intent_mod

    actions: List[Dict[str, Any]] = []
    in_sync: List[str] = []
    disabled_ok: List[str] = []
    shed_ok: List[str] = []
    terminal: List[Dict[str, Any]] = []
    shed = intent_mod.shed_map(manager.syrvis_home)
    installed = _installed_manifests(manager)

    # design/63 M1: the graph is solved ONCE per plan — it supplies the
    # topological order AND the blocked set. `load_declarations` already moved
    # every graph-INVALID declaration into `invalid`, so anything reaching here
    # is a valid edge set; re-solving is cheap and keeps the planner usable with
    # a hand-built declaration dict (tests, the dashboard, a future engine).
    graph = build_dependency_graph(declarations, invalid)
    blocked_by_dependency = dependency_blocked(declarations, graph, shed)

    # FLOOR CHECK (incident 2026-08-16). "Zero declarations" is a legitimate
    # state only for an instance with nothing installed. Zero declarations while
    # services ARE installed means the config tree is not where we are looking —
    # a mis-rooted SYRVIS_HOME, an unmounted volume, a renamed install root — and
    # `load_declarations` returns empty-and-valid for a missing directory, so the
    # engine would otherwise report a clean, converged, empty world. Refuse
    # instead: an empty config is never a reason to act on a populated instance.
    #
    # Scoped to the AMBIENT path (no prune policy) on purpose. That is the one
    # the boot hook, cron and every unattended reconcile take, and the one whose
    # emptiness is an inference. An explicit `--prune`/`on_undeclared: remove`
    # is an operator INSTRUCTION carrying its own policy word — it is not
    # "planning nothing", it is planning exactly what was asked, and it is
    # destructive-token-gated over the seam.
    if prune is None and installed and not declarations:
        raise ReconcileError(
            "0 declarations but {} installed service(s) — refusing to reconcile "
            "against an empty config/services.d. Check that SYRVIS_HOME points at "
            "the real install root (`ls -d /volume*/syrviscore*`) and that "
            "{} exists.".format(len(installed), get_declarations_dir(manager.syrvis_home))
        )

    for name, declared in declarations.items():
        current = installed.get(name)
        status = manager._get_service_status(declared.container_name or name)

        is_shed = name in shed
        if is_shed or not declared.enabled:
            # Declared-but-off (or SHED — the overlay is indistinguishable here
            # by design): stop anything alive (running, restarting, paused,
            # created — a crash-looping container is NOT stopped); never
            # materialize. Reached BEFORE the vanished-home pre-flight on
            # purpose: stopping is always safe, and a service on its way off is
            # not a service to block.
            if name in installed and status not in ("stopped", "exited", "unknown"):
                action = {
                    "kind": "stop",
                    "name": name,
                    "image": declared.image,
                    "critical": declared.critical,
                    "destructive": False,
                }
                if is_shed:
                    # A shed service found ALIVE is a resurrection — say so in
                    # the plan, because the interesting question is not "stop
                    # it" but "what started it".
                    action["shed"] = True
                    action["shed_reason"] = shed[name].get("reason")
                actions.append(action)
            elif is_shed:
                shed_ok.append(name)
            else:
                disabled_ok.append(name)
            continue

        # PRE-FLIGHT, ahead of every add/replace/start decision: a stateful app
        # whose home has vanished must be BLOCKED, not converged.
        refusal = _vanished_home_refusal(manager, name)
        if refusal:
            actions.append(
                {
                    "kind": "blocked",
                    "name": name,
                    "image": declared.image,
                    "critical": declared.critical,
                    "destructive": False,
                    "message": refusal,
                }
            )
            continue

        if name not in installed:
            actions.append(
                {
                    "kind": "add",
                    "name": name,
                    "image": declared.image,
                    "critical": declared.critical,
                    "destructive": False,
                }
            )
        elif current is None or _content_dict(current) != _content_dict(declared):
            actions.append(
                {
                    "kind": "replace",
                    "name": name,
                    # from_image = what's installed now, image = what it becomes —
                    # so the plan shows the version transition (e.g. a digest bump).
                    "from_image": current.image if current is not None else None,
                    "image": declared.image,
                    "critical": declared.critical,
                    "destructive": False,  # data dir is preserved across replace
                }
            )
        elif declared.restart == "no" and status in _TERMINAL_STATUSES:
            # TERMINAL, not drifted (dep:F11, design/60 §3.5 terminal precedence).
            # `restart: no` is how design/63 D4's hard-edge subtrees say "when
            # this exits, it STAYS exited" — Docker honors that, and until now
            # the reconciler did not: it emitted a `start` for anything not
            # running, so every hourly reconcile, every boot converge and every
            # resume resurrected a service the operator had deliberately let
            # stop. The only remaining way to hold it down was `enabled: false`,
            # the field a GitOps apply overwrites. Three mechanisms, one field,
            # cancelling out.
            #
            # It is a BUCKET, not an action: an action would report as a failure
            # on every pass (a `blocked` row grades ok=False), and a terminal
            # service is not a failure — it is a service with nothing owed. To
            # bring one back an operator starts it explicitly, or changes the
            # declaration (which plans a `replace`).
            terminal.append(
                {
                    "name": name,
                    "status": status,
                    "critical": declared.critical,
                    "reason": "restart: no — exited on purpose, reconcile will not restart it",
                }
            )
        elif status != "running" or manager.is_service_flapping(declared.container_name or name):
            # FLAPPING counts as not-in-sync (incident 2026-08-16). A
            # `restart: unless-stopped` container reads "running" between
            # crashes, so a crash-looping service was classed in_sync and
            # reconcile declared victory over six of them for ~15 minutes. The
            # emitted action is the ordinary `start` — an idempotent `up -d`
            # that also re-materializes compose (repairing host-side dir/perm
            # drift, a real cause of crash loops). It deliberately does NOT
            # force-recreate: repeatedly killing a container that may be slowly
            # recovering is worse than reporting the truth and letting an
            # operator run `syrvis service recreate`.
            actions.append(
                {
                    "kind": "start",
                    "name": name,
                    "image": declared.image,
                    "critical": declared.critical,
                    "destructive": False,
                    "flapping": status == "running",
                }
            )
        else:
            in_sync.append(name)

    unmanaged = sorted(set(installed) - set(declarations))
    if prune:
        for name in unmanaged:
            actions.append(
                {
                    "kind": "prune_{}".format(prune),
                    "name": name,
                    "image": installed[name].image if name in installed else None,
                    "critical": False,
                    # stop is reversible; remove drops config (data kept); purge drops data
                    "destructive": prune != "stop",
                }
            )

    # DEPENDENCY BLOCK (design/63 D2 amended): a bring-up whose hard edge points
    # at a deliberately-down service is withdrawn from the plan and reported in
    # its own bucket. Deliberately a POST-pass over the assembled actions rather
    # than a branch inside the classifier: only bring-up is suppressed, so a
    # blocked service that is RUNNING still reports `in_sync`, a blocked service
    # that is declared off still reports `disabled`, and a `stop` is never
    # withheld (stopping is always safe, and a service on its way down does not
    # need its dependency).
    blocked: List[Dict[str, Any]] = []
    if blocked_by_dependency:
        kept: List[Dict[str, Any]] = []
        for action in actions:
            row = blocked_by_dependency.get(action["name"])
            if row is None or action["kind"] not in _BRINGUP_KINDS:
                kept.append(action)
                continue
            blocked.append(
                {
                    "name": action["name"],
                    "critical": action.get("critical", False),
                    "withheld": action["kind"],
                    "dependency": row["dependency"],
                    "why": row["why"],
                    "reason": row["reason"],
                }
            )
        actions = kept

    depths = graph["depths"]
    actions.sort(key=lambda a: _action_order_key(a, declarations, depths))

    return {
        "changed": bool(actions),
        "actions": actions,
        "in_sync": in_sync,
        "disabled": disabled_ok,
        # Declared, but held down by intent.json — NOT drift, NOT "disabled".
        "shed": sorted(shed_ok),
        # Declared `restart: no` and exited — nothing owed, nothing to restart.
        "terminal": sorted(terminal, key=lambda r: r["name"]),
        # Declared and wanted, but a hard depends_on edge points at a service
        # that is deliberately down. NOT an action and NOT a failure.
        "blocked": sorted(blocked, key=lambda r: r["name"]),
        "unmanaged": unmanaged,
        "invalid": invalid,
        # The solved graph, for callers that order their own work (the bring-up
        # engine at M2, the dashboard, home-tech's dependency lint).
        "graph": {"depths": depths, "waves": graph["waves"], "edges": graph["edges"]},
        "summary": {
            "declared": len(declarations),
            "invalid": len(invalid),
            "shed": len(shed_ok),
            "terminal": len(terminal),
            "blocked": len(blocked),
            "total_actions": len(actions),
            "destructive": sum(1 for a in actions if a["destructive"]),
        },
    }


def apply_reconcile_plan(
    manager,
    declarations: Dict[str, ServiceDefinition],
    plan: Dict[str, Any],
    trigger: str = "reconcile",
    allow_halted: bool = False,
) -> List[Dict[str, Any]]:
    """Execute a reconcile plan with per-service failure isolation.

    Every action reports its own outcome; a failure never stops later actions.
    ``trigger`` is threaded into the deployment-history records the manager
    verbs write ("reconcile", "converge", ...). While the instance is HALTED
    (graceful shutdown), applying is refused — a cron/MCP reconcile must not
    restart just-stopped workloads; ``syrvis resume`` passes ``allow_halted``.
    Planning (:func:`build_reconcile_plan`) stays pure and always allowed.
    """
    from . import lifecycle

    lifecycle.guard_not_halted("reconcile", allow_halted=allow_halted)

    results: List[Dict[str, Any]] = []

    for action in plan.get("actions", []):
        kind, name = action["kind"], action["name"]
        try:
            if kind == "blocked":
                # A planner-level SAFETY REFUSAL, never an instruction. It is
                # deliberately handled first and can never fall through into
                # add/start: the whole point is that converging this service
                # would destroy or shadow its data (incident 2026-08-16). It
                # reports as a failed action, so a critical service's refusal
                # fails the reconcile instead of passing silently.
                ok, msg = False, action.get("message") or "blocked"
            elif kind == "add":
                ok, msg = manager.install_declaration(
                    declarations[name], start=True, trigger=trigger
                )
            elif kind == "replace":
                # design/26: a location change on an installed service that
                # still has data is REFUSED before anything is torn down — a
                # naive replace would materialize an EMPTY home at the new
                # location (a DB would re-init: presents as data loss). The
                # refusal surfaces as this service's own failed action; the
                # rest of the plan proceeds (failure isolation). The bypass is
                # the app-move procedure (empty/absent old data root proceeds).
                refusal = manager._location_change_refusal(
                    name, declarations[name].location
                ) or manager._volume_location_change_refusal(
                    name, declarations[name].volume_locations
                )
                if refusal:
                    ok, msg = False, refusal
                else:
                    # keep_declaration=True also keeps the history silent: the
                    # reinstall below records the ONE logical deploy (with the
                    # image transition), never a remove+add pair.
                    # fire_hooks=False for the same reason — a replace fires
                    # one pre/post-deploy pair (from the reinstall), never a
                    # spurious stop quiesce.
                    ok, msg = manager.remove(
                        name, purge=False, keep_declaration=True, fire_hooks=False
                    )
                    if ok:
                        # The data dir predates this replace: a failed
                        # re-install must roll back the new artifacts WITHOUT
                        # destroying it.
                        ok, msg = manager.install_declaration(
                            declarations[name],
                            start=True,
                            preserve_data_on_rollback=True,
                            trigger=trigger,
                            previous_image=action.get("from_image"),
                        )
            elif kind == "start":
                ok, msg = manager.start(name)
            elif kind == "stop":
                ok, msg = manager.stop(name)
            elif kind == "prune_stop":
                ok, msg = manager.stop(name)
            elif kind == "prune_remove":
                ok, msg = manager.remove(name, purge=False, trigger=trigger)
            elif kind == "prune_purge":
                ok, msg = manager.remove(name, purge=True, trigger=trigger)
            else:
                ok, msg = False, "unknown action kind {!r}".format(kind)
        except Exception as exc:  # noqa: BLE001 - isolation: record, continue
            ok, msg = False, str(exc)
        results.append(
            {
                "kind": kind,
                "name": name,
                "ok": ok,
                "critical": action.get("critical", False),
                "message": msg,
            }
        )

    # Any applied action changed the deployed image set; drop the updates cache
    # so a just-reconciled service stops showing an "available" update it no
    # longer has (its "current" is frozen in that cache otherwise).
    if any(r["ok"] for r in results):
        from . import image_updates

        image_updates.invalidate_cache()

    return results


def verdict(
    plan: Dict[str, Any],
    results: Optional[List[Dict[str, Any]]],
    strict: bool = False,
) -> Tuple[bool, str]:
    """(ok, reason) for the exit decision.

    Defaults:
    - An INVALID declaration file is fatal: corruption of intent must never
      pass silently (a truncated critical service's file has no readable
      ``critical`` flag, so criticality cannot exempt it). Isolation is
      preserved regardless — every other service was still converged.
    - A FAILED action is fatal only for a ``critical: true`` service;
      non-critical failures degrade but never block the rest.
    ``strict`` promotes any failure to fatal. ``--boot`` callers ignore the
    verdict entirely (best-effort).
    """
    failures = [r for r in (results or []) if not r["ok"]]
    invalid = plan.get("invalid") or []

    if strict and (failures or invalid):
        return False, "{} invalid declaration(s), {} failed action(s) (strict)".format(
            len(invalid), len(failures)
        )

    if invalid:
        return False, "invalid declaration(s): {}".format(", ".join(row["file"] for row in invalid))

    critical_failures = [r for r in failures if r.get("critical")]
    if critical_failures:
        return False, "critical service(s) failed: {}".format(
            ", ".join(r["name"] for r in critical_failures)
        )
    return True, "ok"


def adopt(manager, name: str) -> Path:
    """Generate a declaration from an existing install (migration helper)."""
    manifest = manager.services_dir / name / "syrvis-service.yaml"
    if not manifest.exists():
        raise ReconcileError("Service '{}' is not installed (nothing to adopt)".format(name))
    try:
        service = load_service_definition(manifest)  # validates before we bless it
    except Exception as exc:  # noqa: BLE001 - typed error for per-row isolation
        raise ReconcileError("Cannot adopt '{}': {}".format(name, exc))
    return write_declaration(manager.syrvis_home, service)


def build_declaration(
    name: str,
    image: str,
    subdomain: Optional[str] = None,
    exposure: Optional[str] = None,
    port: int = 80,
    environment: Optional[List[str]] = None,
    description: str = "",
    enabled: bool = True,
    critical: bool = False,
) -> ServiceDefinition:
    """Author a declaration from image-first vocabulary (the trust boundary applies).

    The builder behind ``syrvis service declare`` and the MCP ``service_declare``
    tool: it only AUTHORS intent — nothing is installed or started until a
    reconcile applies it.
    """
    from . import exposure as exposure_mod
    from .service_manager import _image_tag  # lazy: service_manager imports us

    manifest: Dict[str, Any] = {
        "name": name,
        "version": _image_tag(image),
        "image": image,
        "traefik": {
            "enabled": True,
            "subdomain": (subdomain or name).strip().lower(),
            "port": port,
            "exposure": exposure_mod.normalize(exposure),
        },
        "enabled": enabled,
        "critical": critical,
    }
    if description:
        manifest["description"] = description
    if environment:
        manifest["environment"] = list(environment)
    return ServiceDefinition.from_dict(manifest)


def write_declaration(syrvis_home: Path, service: ServiceDefinition) -> Path:
    """Persist a declaration file for ``service`` verbatim (orchestration kept)."""
    from .service_schema import dump_definition

    path = declaration_path(syrvis_home, service.name)
    path.parent.mkdir(parents=True, exist_ok=True)
    return dump_definition(service, path, include_orchestration=True)


def write_declaration_from_install(syrvis_home: Path, service: ServiceDefinition) -> Path:
    """The dual-write used by imperative installs/updates AND bundle deploys.

    Writes the service CONTENT while preserving the operator's orchestration
    keys from any existing declaration — a git/catalog manifest can therefore
    never set or reset ``enabled``/``critical``; only the operator (editing the
    declaration) or the reconcile layer owns them.

    SHED wins over both (incident 2026-08-16): if the service is in
    ``intent.json``'s shed set, the declaration is written ``enabled: false``
    regardless of what the incoming manifest or the prior file said. A
    per-service deploy of a shed service is legitimate — you often want the new
    bits staged while the service stays down — but it must not be able to write
    intent that contradicts the shed.
    """
    from . import intent as intent_mod

    existing_path = declaration_path(syrvis_home, service.name)
    enabled, critical = True, False
    if existing_path.exists():
        try:
            existing = ServiceDefinition.from_dict(yaml.safe_load(existing_path.read_text()))
            enabled, critical = existing.enabled, existing.critical
        except Exception:  # noqa: BLE001 - unreadable prior declaration: defaults
            pass
    if intent_mod.is_shed(syrvis_home, service.name):
        enabled = False
    import copy

    to_write = copy.copy(service)
    to_write.enabled = enabled
    to_write.critical = critical
    return write_declaration(syrvis_home, to_write)


def remove_declaration(syrvis_home: Path, name: str) -> bool:
    """Delete a declaration (imperative `service remove` must not leave intent
    behind, or the next reconcile would resurrect the service)."""
    path = declaration_path(syrvis_home, name)
    if path.exists():
        path.unlink()
        return True
    return False


def set_declared_enabled(syrvis_home: Path, name: str, enabled: bool) -> bool:
    """Flip ``enabled`` on an existing declaration (imperative start/stop as
    file authors). Returns False when no declaration exists (nothing to edit).
    """
    path = declaration_path(syrvis_home, name)
    if not path.exists():
        return False
    try:
        service = ServiceDefinition.from_dict(yaml.safe_load(path.read_text()))
    except Exception:  # noqa: BLE001 - don't let a broken file block stop/start
        return False
    if service.enabled == enabled:
        # No-op flips never rewrite the file: reconcile's own start actions and
        # repeated stops must not churn (re-serialize/re-chmod/re-own) the
        # IaC-authored declarations they were planned from.
        return True
    service.enabled = enabled
    write_declaration(syrvis_home, service)
    return True
