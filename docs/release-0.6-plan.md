# SyrvisCore 0.6 release train — scope and sequencing

Drafted 2026-08-11 from the home-tech research fan-out (design/handoffs/
2026-08-11-research-fanout.md §D5 + deep-dive findings). 0.5.6 is active on the
NAS; everything here waits for the 0.6 minor. Last-segment-only bumps stay the
rule for patches — 0.6.0 itself is an owner release ceremony
(`./build-tools/release-service.sh`).

## Scope (ratified 2026-08-11)

1. **`logging:` schema key** — audited L2 key restricted to `driver` +
   `options.{max-size,max-file}`; every generated service defaults to
   `json-file / 20m / 3` (~60 MB per container, ~2 GB fleet-wide). Emit in the
   service compose dict (`service_manager.py:989`) and the infra dict
   (`compose.py:278`, traefik/cloudflared). **Caveat to document in the release
   notes:** migrated containers leave DSM's `db` driver, so ContainerManager's
   GUI log viewer goes blank for them — VictoriaLogs/`syrvis logs` is the
   replacement surface. (Deep-dive `logrotate` #1, critical.)

2. **Traefik logs to stdout** — drop both `filePath:` lines from the
   `traefik_config.py` log block (app log + accesslog). One change fixes two
   audit findings: the files stop growing unbounded (they inherit #1's cap) and
   the logs become visible to vector → VictoriaLogs (`container_name:traefik`).
   Keep the `../data/traefik/logs` bind mount for one release so existing
   history stays readable, then drop it. (`logrotate` #2, high; fan-out A3.)

3. **`syrvis-run-job`** (design/13 Phase 1, unbuilt) — bin shim that rotates
   `logs/jobs/<name>.log` (5 MB × 5, self-contained rotation), appends
   stdout+stderr with a run header, pings ntfy on non-zero exit; crontab
   generator derives `syrvis-run-job <name>` keeping the no-operator-argv
   property. All 12 managed jobs currently discard output entirely.
   (`logrotate` #4, high.)

4. **`user:` schema field** — optional `user: <uid>[:<gid>]` on the service
   schema; when declared, `_ensure_volume_dir` does targeted
   `chown uid:gid` + `chmod 0750` instead of the blanket `0o777` the code
   itself apologizes for (`service_manager.py:974`). Fallback stays 0777 when
   absent. Populate first for immich-server / immich-legal-server, whose 224 G
   media root's "no external writes" invariant is currently contradicted at the
   filesystem layer (any non-root login, e.g. syrvis-reader, has a write path).
   (`cruft` #1, high; interim host-side tightening is a separate owner step
   tracked in home-tech.)

5. **share/user declare verbs** — the parked patch is preserved at
   `parked/syrvis-share-user-declare.patch` (14 files, spec included as
   `docs/share-user-declare-spec.md`; was 1102-tests-green when parked).
   Review, apply on a branch, re-run tests, land. Unlocks home-tech's
   `declare-shares` wrapper + share-drift verify.

6. **Managed-config-dirs — "data deployments"** (owner feature ask, verbatim:
   *deploy the data and optionally bounce the service*). The general mechanism
   that would have made Grafana dashboard pushes clean: a declared config dir
   whose contents deploy like code (diff → sync → optional bounce/prune).
   Spec alongside #5; they share the declare-verb plumbing style.

7. **`--no-l2-data` CLI flag** (deploy-time skip of L2 data sync).

8. **`syrvisctl backup create` relocated-apps bug** — misses relocated apps'
   config/secrets slots (home-tech HANDOFF item 5).

9. **docker-state-exporter: per-container memory series** — the memory plane is
   completely blind today (zero memory series) on a box where memory is the
   only enforceable limit; emit usage/limit gauges so the seven new home-tech
   `resources.memory` bounds become observable. (`consistency` #3, high.)

10. **Core-container memory bounds** — home-tech cannot declare `resources:`
    for the L1 four (traefik, cloudflared, portainer, dashboard); add bounds in
    `compose.py`. (`consistency` #2 tail.)

11. **Seam-readable `verify --smoke`** — the Configuration/"Required config"
    check reads a file the operator seam cannot, so `healthy:false` is a
    permanent seam artifact; make the check degrade honestly (SKIP with reason)
    when unreadable. (`cruft` info.)

12. **Seam `logs` verb argv mystery** — worked once, rejected elsewhere; audit
    the registered shape in `seam/registry.py` against the callers.

## Explicitly NOT in 0.6

- Compact syrvis-overview board — built home-tech-side (Grafana provisioning),
  landed with the 2026-08-11 fan-out.
- docker-socket-proxy stream-sever regex — shipped home-tech-side as a mounted
  haproxy.cfg override; no image change needed.
- Dashboard Portainer probe repoint (`/api/status` → `/api/system/status`) —
  rides a dashboard-package patch release before 0.6 (already coded).

## Sequencing

`#5 → #6` (shared plumbing), `#1 → #2` (stdout needs the cap to be safe),
`#3` independent, `#4` independent, `#9/#10` ride the exporter/compose images,
`#7/#8/#11/#12` small. Home-tech interim mitigations (jobs-owned logrotate
runner, per-service memory bounds) are already landing ahead of the train and
nothing here blocks on them.
