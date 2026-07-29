# Handoff — the "app" model: self-contained, locatable services (data on any volume)

> **Prepared 2026-07-28.** Pick this up in a fresh session in `~/code/SyrvisCore`.
> **Design (the WHAT/WHY):** `~/code/home-tech/design/26-per-service-data-volume.md`
> (read it first — this handoff is the HOW + current state). Companion pattern to
> reuse: `~/code/home-tech/design/22-privileged-l2-tier.md` (the `tier: infra`
> authorship-gate + adversarial-review model).

## 1. Mission

Make a SyrvisCore **service a fully self-contained "app"** — its manifest, compose,
data, config, env, and **location** all together in one movable home — and let an
app's **home live on any DSM volume** (not just `SYRVIS_HOME`/`/volume4`).
SyrvisCore's **deployment manifest becomes the registry of where each app lives**.
Immediate driver: run the estate's Postgres + OpenSearch (Onyx stores) on the
**RS1221+ NVMe RAID-1 `/volume6`** — the hardware was bought for exactly this. The
current symlink workaround (`onyx-nvme-flip`) is correctly rejected by 0.5.0's
path-containment check; this replaces it with a declared, known placement.

## 2. Locked design decisions (owner, 2026-07-28)

- **"app" = service-level.** Each L2 service is an app (not a stack grouping;
  home-tech's `stacks/` already groups at deploy time).
- **Full self-containment.** An app's home holds manifest + compose + data + config
  + env/bindings **together**, so "move an app" = move one dir + update the registry.
- **Location = a declared, per-app field.** Default = `SYRVIS_HOME` (`/volume4`);
  override to any real DSM volume (e.g. `/volume6`). Value validated `^/volume\d+$`,
  must be an existing **mounted** volume.
- **Authorship-gated** (the load-bearing security rule — reuse design/22's
  `OPERATOR_AUTHORED_PREFIXES` gate): only an operator-authored declaration
  (`services.d` / a `deploy` bundle) may set a location. A git-repo (`service add`),
  image-first (`service run`), or catalog manifest carrying it is **rejected at
  install** — a third-party repo can never redirect its home onto an arbitrary host
  path. Same trust anchor as `tier`, `enabled`, `critical`.
- **Adversarial review MANDATORY before release** — this loosens the path-traversal
  containment boundary (same bar as the infra tier + the deploy verb). Spawn an
  independent skeptic agent to break it.

## 3. Why — the exact failure

`service_manager.py:84-115` `_service_paths(name)` computes the service/data/compose
paths and asserts containment:

```python
parent_real = os.path.dirname(os.path.realpath(str(target)))   # follows a symlink → /volume6
if parent_real != base_real:                                   # /volume6 != /volume4/syrviscore/data
    raise "Service {!r} escapes the {} directory"              # ← the deploy blocker
```

It realpaths the target but compares against the **literal** base, so it can't tell
a legit operator volume-placement from a `../..` traversal — it rejects both. The
guard is *right*; the mechanism (an invisible symlink) was wrong. Fix: make the
location **declared + known**, and base every path check off the app's declared home.

## 4. Current on-disk model (what to evolve)

- Layout (`service_manager.py:75-77`, `paths.py`): `SYRVIS_HOME` (`/volume4/syrviscore`)
  `/ {services/<svc>` (manifest), `compose/<svc>.yaml`, `data/<svc>` (data),
  `config/}` — **split, all on `/volume4`, no location concept.**
- 0.5.0 already has a **"workload"** concept (`lifecycle.py` — a managed unit:
  service / vm / `@core`; state is DERIVED, never stored) and a **deployment
  manifest** (revision history keyed by workload — `syrvis history --json` →
  `{workloads: {...}}`; `deployments.py`). **This is the registry to extend with a
  per-app `location`.** `data/state/runstate.json` holds the instance runstate.

## 5. Code sites (from a 2026-07-28 grep — verify against current)

- `service_manager.py:75-77` — base dirs (`services_dir`, `compose_dir`, `data_dir`).
- `service_manager.py:84-115` — **`_service_paths(name)`** — central path calc + the
  containment check that rejects (:104-114). Make it location-aware.
- `service_manager.py` **~6 direct `self.data_dir / name`**: 574, 789, 825/827, 1768,
  2096 — route through one location-aware helper.
- `service_schema.py` — add the `location` field to `ALLOWED_TOP_LEVEL_KEYS`,
  `_validate_location` (`^/volume\d+$`), dataclass field + `to_dict` round-trip (so
  the materialized manifest carries it — lifecycle ops read it back), and hook the
  authorship gate. `OPERATOR_AUTHORED_PREFIXES` + the gate live in
  `service_manager._install_from_definition` (search "tier: infra").
- `lifecycle.py` / `deployments.py` — the deployment manifest/registry: record +
  query per-app location.
- Compose emit + `_ensure_volume_dir` (0777 rw data dirs; DSM won't auto-create bind
  sources) — honor the app home.
- `paths.py` (`get_data_dir` etc.) — location-aware.

## 6. Implementation plan

1. **Schema**: `location` field + validation + dataclass/to_dict + authorship gate.
2. **Path centralization**: one helper `_app_home(service_or_name) -> Path` that
   resolves the app's home from its declared location (default `SYRVIS_HOME`); route
   `_service_paths` + all direct `self.data_dir / name` through it; the containment
   assertion bases off the app's home (so a real dir under the declared volume passes,
   a `../..` still fails).
3. **Thread location to lifecycle ops** (stop/remove/logs have only the name): read
   it from the **materialized manifest** in the app home (always present).
4. **Registry**: record app → location in the deployment manifest; surface in
   `status`/`history`.
5. **Dir creation + mount validation**: pre-create the app home; at install confirm
   `location` is a mounted volume.
6. **Adversarial review** (see §2).
7. **Release + install** (0.5.x → probably 0.6.0 given the scope).

### ⚠ Open design question — the layout + migrating the existing 19

Two viable shapes for the app home; **decide + record in design/26**:
- **(A) One dir per app:** `<location>/apps/<app>/{manifest,compose,data}` — the
  "fully self-contained" ideal. Cleanest, but **restructures all 19 running services'
  on-disk layout = a real, risky data migration** (stop → move → restart, per service,
  reversible).
- **(B) Location-scoped current split:** `<location>/syrviscore/{services,compose,data}/<app>`
  — the **default (`/volume4`) keeps today's exact layout (zero migration for the 19)**;
  only a **relocated** app (Onyx stores → `/volume6`) materializes under the new
  location. Less "one dir per app," but ships Onyx-on-NVMe with **no migration risk**.
- **RECOMMENDATION:** ship **(B) first** (unblocks `/volume6` safely + immediately),
  then evolve toward (A)'s one-dir-per-app as a follow-on migration once the location
  plumbing is proven. The owner wants full self-containment as the end state — (B) is
  the safe first increment of it, not a different design.

## 7. Companion deploy fixes (fold in — same Onyx deploy hit them)

- **Deploy up-timeout too short for huge image pulls.** `onyx-model-server:v4.4.2`
  timed out on `docker compose up` (deploy uses `up -d`, so it's the *pull*, not the
  healthcheck — smaller `onyx-backend` made it). Fix: raise/allow-config the up
  timeout in `deploy_bundle`/`_compose`, or pre-pull before the timed start.
- **`deploy-stack` (home-tech) ignores `--only` ORDER.** It deployed nginx-first,
  stores-last → app started before DB. Fix in `~/code/home-tech/scripts/deploy-stack`
  (honor `--only` order) or have deploy use the declared `shutdown.priority` bands
  (design/25 D4) in reverse for START ordering (symmetric with graceful stop).

## 8. Also-surfaced SyrvisCore work (separate; the reboot test defined these)

- **Self-healing boot hook (operator seam).** DSM regenerates `/etc/passwd` on boot,
  resetting `syrvis-operator`'s shell to `/sbin/nologin` → **the operator seam breaks
  on EVERY reboot** (verified: a plain reboot broke it 2026-07-28). Fix: the rc.d
  boot hook (`privileged_ops.py` `ensure_startup_script` → `syrvis-startup.sh`, runs
  as root at boot) should **re-assert the operator shell `nologin → /bin/sh`** on
  every boot so the seam self-heals. (NB: `syno_hdd_db` is NOT needed at boot —
  `/volume6` mounts itself; it's only needed after a DSM *update* reverts the compat
  DB. Running it at boot would *re-break* the shell, so keep it out of the boot path.)
- **Graceful-shutdown rc.d hook (owner asked).** The rc.d `stop` case
  (`privileged_ops.py`, `S99syrviscore.sh`) currently only deletes the macvlan shim.
  Extend it to run `syrvis shutdown` so a DSM shutdown/reboot gracefully halts the
  estate (ordered, stores-last). ⚠ Needs a **new `reason`** that **auto-resumes on
  next boot** (`maintenance` holds across reboots — wrong for a plain reboot; `ups`
  auto-resumes — add `reason: system`/`reboot`). Verify DSM's rc.d-stop timeout
  accommodates the ~70s ordered stop (stores get 120s grace). Unifies with design/25's
  UPS path (which already planned DSM/NUT → `syrvis shutdown --reason ups`).

## 9. Current estate/NAS state (context)

- **RS1221+ migration DONE**: DSM 7.4.1-90080, estate green (4 core + 19 L2 running),
  `/volume4` = SYRVIS_HOME, **`/volume6`** = NVMe RAID-1 Btrfs (reboot-verified: it
  mounts on boot on its own).
- **Onyx**: 10 declarations, all `enabled: false` (reverted to pause — commit `bb4e0e3`
  in home-tech). **4 partial-deployed on `/volume4`** (`onyx-nginx`, `onyx-web`,
  `onyx-api`, `onyx-background` — installed but **STOPPED**); 5 not deployed (the 2
  stores hit the containment blocker; 3 never reached). **Symlinks left in place**:
  `/volume4/syrviscore/data/{onyx-relational-db,onyx-opensearch}` →
  `/volume6/{…}` (superseded by the `location` field — remove during the Onyx
  migration). `/volume6/{onyx-relational-db,onyx-opensearch}` dirs exist (empty).
- **Seam breaks on every reboot** (§8) — re-provision when it does:
  `cd ~/code/SyrvisCore && ~/.pyenv/versions/3.8.12/envs/syrviscore/bin/python -m
  syriscore.seam.gen provision --home /volume4/syrviscore --pubkey
  ~/.ssh/syrvis_mcp_ed25519.pub > /tmp/provision.sh` → `scp /tmp/provision.sh
  ds:/tmp/` → owner runs `ssh -t ds 'sudo sh /tmp/provision.sh'` (needs sudo pw).
  (Typo guard: the module is `syrviscore.seam.gen`.)

## 10. Onyx migration (home-tech, AFTER the field ships)

1. Owner (root shell): `rm` the two symlinks under `/volume4/syrviscore/data/`.
2. Add `location: /volume6` to `services.d/onyx-relational-db.yaml` +
   `onyx-opensearch.yaml`; re-enable the 9 (`onyx-mcp` stays stage-2).
3. Fix the deploy order/timeout (§7), then
   `deploy-stack onyx --apply --only <stores first, then models, then app, then web/nginx>`.
4. Verify `docker inspect onyx-relational-db | grep Source` → `/volume6/…`; app up;
   `verify-all` green. Retire `wiki/runbooks/onyx-nvme-flip.md`.
5. Owner steps still open (design/24): the `chat A 192.168.8.4` SRM record;
   register Kevin-first at `https://chat.konsume.org`; the in-app config (§8).

## 11. Dev facts

- Repo `~/code/SyrvisCore`, version 0.5.0 (NAS + repo agree). Venv:
  `~/.pyenv/versions/3.8.12/envs/syrviscore/bin/python`. Tests: `pytest tests/ -q`.
  Release: `build-tools/build-service.sh` + `release-service.sh` (read
  `__version__.py`; PATH needs the venv + the `build` module). Install to the NAS:
  MCP `install`, or seam `sudo -n /var/packages/syrviscore/target/venv/bin/syrvisctl
  install -y --path /volume4/syrviscore -- <ver>`.
- **Adversarial-review practice**: for a boundary-loosening change, spawn an
  independent general-purpose skeptic agent to try to break it (git-repo `location`
  rejected? only `/volume<N>` mounts pass? no `..`/symlink-in-value/unmounted escape?
  prune/backup/drift resolve to the RIGHT — relocated — path, never a stale one? a
  prune that resolved wrong could `rmtree` the wrong tree). Prior examples: the infra
  tier (design/22) and the deploy verb both went through this.
- **Never cerebrate for routine ops** (it pages the owner); the operator seam is the
  path (except its own re-provision, which needs root). Digest-pin any new images.
