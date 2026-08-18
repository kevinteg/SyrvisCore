# The declaration tree (what flows over apply/deploy)

## What this slice is

`~/code/home-tech/syrvis/` is the desired-state tree for the NAS. Only *part* of it ever reaches SyrvisCore. Two artifacts cross the operator seam:

- **`syrvis/services.d/*.yaml`** — 39 L2 service manifests, shipped whole as the `declarations` map of a `syrvis-instance/v1` bundle to `sudo -n syrvis apply` (`scripts/apply-instance:251-254`), *or* one at a time joined with configs+secrets into a `syrvis-bundle` for `sudo -n syrvis deploy <name>` (`scripts/deploy-stack:195-243`).
- **`syrvis/deployment.yaml`** — rendered by `render/nas_env.py:render_env()/render_stack()` into the bundle's `env:` (the NAS `.env`) and `stack:` (core-tier `stack.yaml`) sections.

Everything else in the tree — `volumes.yaml`, `shares.d/` (21), `data.d/` (41), `backup-tasks.d/` (12) — is a **home-tech-only registry the platform never sees**. It exists because SyrvisCore has no data/share/backup model at all; the repo carries the whole storage-doctrine plane out-of-band and reconciles it against reality with `scripts/verify-all` and DSM, not with a verb. `jobs.d/` (29) is a third category: declarative, but delivered by `schedule sync` + `config set`, not by apply/deploy.

`syrvis/stacks/<stack>/deploy.yaml` (11 files) is **not a SyrvisCore schema** — it is home-tech's own join table (service → config sources → sops secret refs), consumed exclusively by `scripts/deploy-stack` laptop-side.

## Verb-usage inventory

| Artifact | Verb / argv shape | Channel | Caller | Cadence | Data flow | Result use |
|---|---|---|---|---|---|---|
| all 39 `services.d/*.yaml` + rendered `.env` + `stack.yaml` | `sudo -n $SYRVIS apply [--dry-run\|--apply] [--allow-secret-change] --json` | seam (stdin bundle) | `scripts/apply-instance:298` (`run_apply`) | per-change; the single deploy path | stdin = one `syrvis-instance/v1` JSON (`{apiVersion, env, stack, declarations}`); stdout JSON report | parsed; non-JSON stdout surfaced **verbatim** as a platform refusal (0.5.15 `guard_enable_change`) |
| — pre-flight for the above | `$SYRVIS status --json` (non-sudo read) | seam | `apply-instance:require_active_runstate()` | every `--converge` | reads `.runstate.state` | refuses to converge while `halted`, prints the `syrvis resume` remedy |
| — live declaration read | `$SYRVIS ... --json` → `.declarations` | seam | `apply-instance:139` | every run | JSON read | **merged into the outgoing bundle**: live `enabled:false` is preserved into git's declaration unless `--force-enable` |
| one service's manifest + configs + secrets | `sudo -n $SYRVIS deploy <name> --json` | seam (stdin bundle) | `scripts/deploy-stack` | per-stack, manual | stdin = `syrvis-bundle` `{service: <manifest verbatim>, configs:[{dest,content,secret?}], secrets:{ENV:value}}` | revision recorded; script prints it |
| revision reporting | `$SYRVIS history --json` (non-sudo read) | seam | `deploy-stack:fetch_history()` | after each deploy, 30s timeout | `{"workloads": {...}, "invalid": []}` | prints revision + a valid `--to` rollback target; **failure is swallowed** — must never fail a deploy |
| `jobs.d/*.conf.tmpl` (17 of 29 jobs) | `sudo -n $SYRVIS config set <job>` | seam (stdin) | `scripts/apply-jobs` | per-change | stdin = rendered conf | job conf written 0600 |
| `jobs/<name>/` scripts | `sudo -n $SYRVIS schedule sync` / `schedule apply` | seam | `scripts/apply-jobs` | per-change | exit-code gate | managed crontab block reconciled |
| `services.d` reconcile at runtime | `syrvis reconcile` | local-cron (root, `$SYRVIS_HOME/bin/syrvis`) | `jobs.d/hourly-reconcile.yaml` (`47 * * * *`, **enabled: false**) | hourly (currently off) | — | declared-but-disabled |
| — | boot-hook reconcile of declared services | local (S99) | SyrvisCore boot hook | per-boot | — | brings declared set up |
| located services after a bind-path change | `sudo -n $SYRVIS service recreate <name>` | seam | `scripts/recreate-located` | incident / halt-window only | plan built from `service list --json` | serial recreate, stores first |
| `romm-db` repair | `sudo -n $SYRVIS service task --task reinit-datadir -- romm-db` | seam | declared in `services.d/romm-db.yaml:45` | incident-only (2026-08-15) | argv only — **task argv may not carry `$` expansion** | wipes a half-initialized datadir |
| routing intent (9 services) | `syrvis stack hostnames --json` | seam / MCP | `syrvis/README.md:23` | per DNS/tunnel change | JSON report per hostname | home-tech reconciles DNS via router-DNS + Cloudflare MCP; **never re-derives** |
| image pins | `syrvis updates --json` | seam | `verify-all` check `nas.updates` | verify runs | JSON | report-only |
| platform version | reads `deployment.yaml instance.version: "0.5.16"` vs live | seam | `verify-all` check `nas.version` | verify runs | — | FAILs on drift; **no verb bumps this line** — it is edited by hand |

## Fleet statistics

**39 services / 11 stacks, 1:1 coverage** — every manifest belongs to exactly one `deploy.yaml`, and every stack entry has a manifest. Largest stacks: `monitoring` (12), `onyx` (10); seven stacks hold 1–4.

**Key usage across the 39:**

| Key | N | Notes |
|---|---|---|
| `name`/`version`/`image`/`container_name`/`restart` | 39 | `restart: unless-stopped` in **all 39** — the field is a constant |
| `networks: [proxy]` | 38 | `romm-db` alone omits it |
| `resources` | 38 | `memory:` is the **only** sub-key ever used; no cpu limits anywhere |
| `shutdown` | 35 | `{priority, stop_timeout}` ×20, `{priority}` only ×13, `{stop_timeout}` only ×2 |
| `critical` | 34 | `true` ×3 (`docker-socket-proxy`, `docker-health-exporter`, `ntfy-alertmanager`), `false` ×31, absent ×5 |
| `volumes` | 31 | 42 string entries, **2 `fileplane:` entries** (romm→Gaming/ROMS ro, syncthing→Gaming/installs/android ro) |
| `healthcheck` | 29 (74%) | `CMD-SHELL` ×18, `CMD` ×11; `start_period` on only 15 |
| `environment` | 28 | list-of-strings form |
| `command` | 22 | argv-driven exporters/TSDBs |
| `enabled` | 20 | explicit `true` ×19, `false` ×1 (`unpoller`); absent on 19 |
| `env_file` | 18 | value is `secrets.env` in **all 18** — another constant |
| `location` | 15 | `/volume5` ×11, `/volume6` ×4; 24 unlocated (legacy `$SYRVIS_HOME/data/<name>` layout) |
| `traefik` | 9 | `{enabled, subdomain, port, exposure}` ×9, `domain` on 3 |
| `tier: infra` | 2 | `docker-socket-proxy`, `node-exporter` |
| `tasks` | 1 | `romm-db.reinit-datadir` |
| `ports` | 1 | `syncthing` (3 host mappings) |
| **`depends_on`** | **0** | see below |
| **`volume_locations`** | **0** | |
| **`hooks`** | **0** | |
| `config_templates` | 0 | configs ride `deploy.yaml`, not the manifest |

**Shutdown bands:** 20→18 services, 90→7, 70→5, 30→2, 60→1, none→6. Six services (`alertmanager`, `cyberquill`, `grafana`, `node-exporter`, `ntfy-alertmanager`, `snmp-exporter`) carry no band at all — and two of those are `critical: true`.

**Exposure:** all 9 routed L2 services are `exposure: tunnel`. **`internal` is never used at L2** — it appears only in `deployment.yaml` for core (dashboard) and Synology-native (DSM/Photos/WebDAV) services. The declared "internal vs tunnel" duality is, in practice, half-dead in the L2 tree.

**Configs & secrets:** 32 config pushes total — 29 in `monitoring` alone (16 of them Grafana dashboard JSON), 2 in `onyx`, 1 in `litellm`. Exactly **2 are `secret: true`** (`vmagent/scrape.yml`, `ntfy-alertmanager/config.scfg`). 37 sops secret refs across 12 services, drawn from 3 sops files.

**The registries (never seen by the platform):** `data.d` 41 entries — kinds `database` 11, `config` 8, `cache` 7, `documents` 7, `media` 6, `metrics` 2; `regenerable: false` on 25; backup methods `none` ×19, `hb:<task>` ×14, `pg-dump+hb:backups-sensitive` ×4; **`restore.method` is null on all 41** (only a runbook path, `verified: null` everywhere). `shares.d` 21 (volume4 ×11, volume5 ×6, volume3 ×3, volume1 ×1). `backup-tasks.d` 12, all → `s3://konsume-nas-backup/*`. `jobs.d` 29 (25 enabled, 4 declared-off).

## The graph shape

**There are zero `depends_on` edges in the tree.** The 0.5.16 feature is written but ungated-in (dashboard repin required), so ordering lives in **three separate, uncoordinated client-side mechanisms**:

1. **Shutdown band, reversed** — the platform's only ordering signal. Store 90 → broker 70/60 → shipper 30 → app 20.
2. **Declaration order inside `stacks/*/deploy.yaml`** — `deploy-stack` iterates the map in file order, and the comments say so explicitly ("Declaration order = deploy order", immich/romm/immich-legal).
3. **A hardcoded override in the client** — `ALERTING_PAIR_LAST = ("alertmanager", "ntfy-alertmanager")` (`deploy-stack:69,514-517`), a stable-partition that force-moves two services to the end. Born from the 32-minute undelivered-alerts window of 2026-08-11. The `deploy.yaml` carries a ⚠ warning that its own file order is a lie for those two.

Everything else is left to `restart: unless-stopped` + application-level retry. The dependency structure is real and legible from bands — `*-db` (90) ← `*-redis`/`*-valkey` (70/60) ← app (20); `docker-socket-proxy` (70) ← `docker-health-exporter`+`vector` ← `vmagent` (30) ← `victoria-metrics` (90, inverted: it is a store) ← `vmalert` (20) → `alertmanager` (unbanded) → `ntfy-alertmanager` (unbanded) — but the platform is told none of it.

## Interaction patterns

**Bundle assembly is entirely laptop-side.** `deploy-stack.build_bundle()` reads the manifest verbatim, reads each config source from disk, and only at `--apply` decrypts sops and (for `secret: true` configs) renders `${VAR}` placeholders in memory. Secret values never touch laptop disk or argv — sops → memory → ssh-stdin. Dry-run deliberately omits secrets, which is why **secret-rendering failures surface only at `--apply`, on the live path**.

**The client re-implements platform limits.** `SEAM_CONFIG_MAX_BYTES = 65536` is a hand-copy of SyrvisCore's `ServiceManager._SECRET_MAX_BYTES`, enforced client-side purely so a dry-run fails instead of an `--apply` half-completing. That cap has already forced `rules-critical.yml` to split **five times** (2026-08-10 ×2, -08-14, -08-17) and split `nas.json` → `io-array.json`.

**Apply reads the live world before writing it.** `apply-instance` fetches live `declarations`, computes the live-disabled set, and *rewrites git's declaration to `enabled:false`* before sending — so a manual shed is not clobbered. Divergence with no matching row in `config/maintenance-state.yaml` is flagged and fails `verify-all nas.enabled-drift`.

**Results are consumed as guidance, not just gates.** `report_revision` re-derives the rollback target with the same rule the platform's `resolve_rollback_target` uses, and prints the exact seam argv.

## Workarounds & missing verbs (the negative space)

- **`depends_on` absent → three ordering hacks** (above), plus `onyx-nginx.yaml:4` running a bespoke retry wrapper "because SyrvisCore has no depends_on", and `stacks/onyx/config/default.conf:8` saying the same.
- **No schema for `ulimits`, `shm_size`, `user:`, `sysctls`, per-service networks.** `onyx-opensearch.yaml:4` — memlock -1 "unrepresentable"; `onyx-relational-db.yaml:7` + `authentik-server.yaml:130` — `shm_size` "not expressible"; `docker-socket-proxy.yaml:200` — "own network is not expressible in a declaration today". The `user:` gap (design/05 §P5) is why **alertmanager's config can never be `secret: true`** — 0600-root would crash-loop a `USER nobody` image, and `_place_config` refuses to downgrade 0600→0644, making it a one-way door.
- **Configs are write-only.** "A bundle only WRITES configs; SyrvisCore never prunes a config that has left the bundle" — seven retired Grafana boards kept serving from disk. The workaround was a *storage relocation* (fresh empty config slot) standing in for a missing `config prune` verb.
- **Nothing validates dest↔mount.** `stacks/monitoring/deploy.yaml:29-31`: "deploy-stack checks size and renderability, not whether a dest lands where a mount reads; a mismatch deploys green and serves nothing." This is why `alertmanager` and `vector` cannot be flipped to `location:` without collapsing their `config/<file>` dests.
- **`service recreate` shipped and nobody called it** — `scripts/recreate-located` exists solely because the share-rename runbook required hand-deriving 11–17 names from `service list --json` inside a halt window. The script re-adds a plan, a shed filter, a store-first order and a budget the verb lacks.
- **No `restore` verb, and no restore verification.** All 41 `data.d` entries have `restore.verified: null` and point at a runbook.
- **`instance.version` drift is hand-maintained** — the release runbook has no bump step; it sat stale at 0.5.9 through the 0.5.14 install, making `nas.version` a standing FAIL.
- **`syncthing-identity`** must be "re-paired by hand" after restore — no identity-preserving restore path.
- **Break-glass credentials as declaration.** Grafana regained `GF_SECURITY_ADMIN_PASSWORD` via sops→env because the console sits behind authentik and an authentik outage would lock the watchers out of the watcher.

## Observations for a v2 agent design

1. **Ship the dependency graph, and make the band derivable from it, not parallel to it.** Today three ordering mechanisms disagree by construction, and the highest-consequence one (`ALERTING_PAIR_LAST`) lives in a client script born from an outage. A consolidated agent should own topological order and let `shutdown.priority` be a *derived* tie-breaker.
2. **The tree's real bulk is registry, not manifest.** 74 files (`shares.d`+`data.d`+`backup-tasks.d`) describe storage the platform cannot see or act on. A device-lifecycle agent that modeled shares, datasets, placement and backup selection would absorb `volumes.yaml`, `data.d`, `backup-tasks.d`, the `synocheckshare`/Hyper-Backup coupling, and `recreate-located` in one stroke.
3. **Move the seam's own limits server-side and make them queryable.** The 64 KiB cap is duplicated as a magic constant in `deploy-stack`; a capability/limits read verb would end the copy.
4. **Plan must include rendered content.** Dry-run blindness to secret-config rendering is the single sharpest edge in this slice: the failure mode is discovered on the live alerting path.
5. **Make config delivery converge, not append.** Declared set = on-disk set, with prune; and validate `dest` against the manifest's declared mounts at plan time.
6. **Constants that are always the same value are schema smells**: `restart: unless-stopped` (39/39), `env_file: secrets.env` (18/18), `networks: [proxy]` (38/39), `resources.memory` as the only resource key. Collapse them to defaults and free the surface for `user:`, `ulimits:`, `shm_size:` — the three gaps that repeatedly force declarations to lie about upstream's requirements.
7. **Two-thirds of the declared schema is exercised; one-third is aspiration.** Heavily used: `image`/`shutdown`/`healthcheck`/`location`/`env_file`/`traefik`/`command`/`resources.memory`. Declared once or never: `tasks` (1), `ports` (1), `tier` (2), `fileplane` (2), `depends_on`/`hooks`/`volume_locations` (0). A v2 agent should treat the zero-use set as unproven, not as shipped.