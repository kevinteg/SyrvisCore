# SyrvisCore v1 — A Retroactive Design Document

> **Status:** retrospective, authored 2026-08-17 — at service `0.5.17` (branch
> `security-review-2026-08-17`, last tag `v0.5.7`), manager `0.3.6`, dashboard
> `0.5.11`, live on the NAS since 2026-07-28 (0.5.x line).
> **Scope:** the implemented v1 platform, the operator seam, the deployment
> plane, the app/service configuration model, the vision it serves, the gaps,
> and what a rewrite would change. Part I (§1–§9) *describes and judges* the
> v1 system. Part II (§10–§13, added 2026-08-18) inventories how the
> deployment repo actually consumes v1, records the owner's v2 mandate, and
> designs the v2 agent that answers it.
> **Method:** produced from a full-subsystem review of this repo (all four
> packages, SPK scripts, build tooling, tests, docs, git history), the
> home-tech design corpus (`~/code/home-tech/design/`) that SyrvisCore's 0.5.x
> line implements, and — for Part II — a full audit of home-tech's executable
> surfaces (scripts, jobs, runbooks, permission plane, incident records).
> Where a claim is load-bearing it was verified against the trees at the
> versions above.

---

## 0. Reading guide

- §1–§2 — what the system is and the history that shaped it.
- §3 — the **vision**: the data and operations doctrine SyrvisCore exists to
  execute (data verticality, the light payload, declare→reconcile→verify).
- §4 — the **implemented architecture**, subsystem by subsystem, including the
  seam (§4.7) and the deployment plane (§4.5).
- §5 — what v1 got right (the parts any successor must preserve).
- §6 — **gaps**, ranked by severity.
- §7 — cross-cutting tensions: the deliberate trades that now bind.
- §8 — **from scratch**: language verdict and a redesigned architecture.
- §9 — migration paths, with a recommendation.
- §10 — **the consumer in practice**: every verb/channel home-tech actually
  uses against the NAS, and the client-side burden that usage reveals.
- §11 — **the v2 mandate**: the owner's verdict on v1 and the requirements
  that bind v2, including the Home Kit deployment contract.
- §12 — **syrvisd**: the v2 device lifecycle manager and deployment
  orchestrator — drains, partial drains, server-side orchestration, rich
  status, and the consolidated API that replaces the 77-verb seam.
- §13 — **v2 packaging and the volume model**: the DSM-native install path
  and placement as a first-class concept.
- Appendices — numbers, the quote wall, and a home-tech design cross-reference.

A note on provenance: this codebase carries its own rationale unusually well.
Nearly every non-obvious constant, refusal, and ordering choice has a comment
naming the dated incident that produced it. That practice is itself a design
decision (§5.10), and this retrospective leans on it throughout — quotes below
are from the code and docs unless attributed otherwise.

---

## 1. What SyrvisCore is

SyrvisCore is a self-hosted infrastructure platform for a single Synology NAS
(DSM 7.0+). It packages a routing substrate (Traefik on a dedicated macvlan
IP, Portainer, optional Cloudflared tunnel) and a declarative Layer-2 service
plane (one validated `syrvis-service.yaml` per service in
`config/services.d/`), managed by two CLIs:

- **`syrvisctl`** (the manager, installed by an SPK to
  `/var/packages/syrviscore/target`): versioned installs of the service
  package under `$SYRVIS_HOME/versions/<v>/`, activation via a `current`
  symlink, backup/restore, and rootfs-level diagnosis (`doctor`).
- **`syrvis`** (the service, one venv per version): setup, compose generation,
  the reconcile engine, the deployment verbs, lifecycle (shutdown/resume),
  scheduling, VMs, verification.

Three adapters sit over one deterministic library layer: the CLI itself, an
MCP server that runs on the operator's Mac and speaks typed tools over SSH,
and a web dashboard that runs as a base-tier container and imports the library
in-process. All privileged remote operation flows through **the operator
seam**: a dedicated NAS account whose SSH key is pinned to a generated
forced-command shim and whose sudo rights are a generated enumerated
allowlist — both rendered from a single verb registry inside the platform.

A separate deployment repo (home-tech) owns everything estate-specific — the
domain, DNS, Cloudflare account, service catalog, secrets — and drives the
platform exclusively through the seam. SyrvisCore itself is generic and
public: it never names a domain, an IP, or an account.

The identity of the project in one sentence: **a validated, refusal-guarded
execution engine for declared intent, on a host that actively fights it.**

---

## 2. How it got here

### 2.1 Timeline

| Date | Event |
|---|---|
| 2025-11-29 | First commit. v1: Cline-authored single-package CLI + SPK. |
| 2025-11-30 | The macvlan decision — DSM's nginx owns 80/443; four alternatives tried and rejected; Traefik gets its own LAN IP. Survived every rewrite. |
| 2025-12-25 | v0.1.0 and, the same day, the v3 split-package architecture. |
| 2025-12-26 | v0.1.2–v0.1.21 ship **in one day**, each fixing what the previous broke on the production NAS. Layer 2 services added. |
| 2026-01 → 06 | Six months of dormancy. |
| 2026-07-09 | The code audit (`docs/archives/code-audit-2026-07.md`) and `v2-design.md`. Phases 0–3 (hygiene, manager rewrite, dev loop, security fixes) land in one day. |
| 2026-07-10 | Phases 4–5: verify engine, MCP server, same-day red team (10 confirmed findings, all fixed). Dashboard born. |
| 2026-07-11 | Exposure model, `services.d` declarative loading, the engineering wiki. |
| 2026-07-17 → 24 | The seam build-out: scheduled jobs, `secret set` / `config set` / `deploy` bundle, `tier: infra`, then the registry relocated into the platform (`syrviscore.seam`), `syrvis apply`, profiles, one Traefik routing mechanism (0.4.x). |
| 2026-07-27 → 31 | 0.5.0–0.5.4: the deployment system (revisions, rollback, hooks, `shutdown --reason ups`/`resume`), then boot durability (the deployed startup script "had drifted years behind the code"). |
| 2026-08-10 → 14 | 0.5.5–0.5.8: shim ifcfg (a DSM health-poller flood), change-aware Traefik reload, `/api/summary`, fileplane volumes + raw ports. The 2026-08-14 deploy outage (a `timeout`-wrapped deploy wedged dockerd; every seam verb hung). |
| 2026-08-16 | **The cold-boot share collision.** DSM's `synocheckshare` renamed the platform's volume roots to `syrviscore_1`, decapitating the wrapper, the seam, both self-healers, and all in-band detection at once — ~50 minutes to diagnosis by hand. 0.5.9–0.5.16 ship the same day: survive-the-rename hardening, `SYRVIS_APPS_ROOT_NAME`, durable intent (`shed`), the enable-resurrection guard, `depends_on`, the deploy journal, the breaker store, `volume_locations`. |
| 2026-08-17 | 0.5.17: `.env` write-time hazard guard (design/70) — a hazardous value is root RCE at next boot; a hazardous key name is reboot-free root RCE. |

286 commits: 29 in Nov 2025, 59 in Dec 2025, **173 in July 2026**, 25 in
August 2026. The project was effectively built twice — once naively, once
audit-driven — with the second build compressed into about five weeks of
part-time work plus two incident-response days.

### 2.2 The three architectures

"v1/v2/v3" name architectures, not release lines:

- **v1** — single package, wizard-driven SPK, no privilege model. Killed by
  DSM reality (below).
- **v2** — the July 2026 rewrite's operating principles, still the project's
  constitution: *declarative intent only; deterministic core, thin adapters;
  machine-readable everything; integrity chain; verify is first-class.* The
  audit's dispositions were surgical: keep the split-package/symlink
  architecture, rewrite the manager internals, refactor (not rewrite) the
  service, overhaul the SPK scripts, rebuild the test process first.
- **v3** — the implemented split: immutable SPK-installed manager, versioned
  venv-per-release service, seam-mediated operation. Landed as a *label*
  before v2's rewrite made it real.

### 2.3 The forces: DSM is a hostile substrate

Almost every odd-looking mechanism in SyrvisCore is a scar from a specific
DSM behavior:

- **Error 276 / the package-user model.** DSM 7 runs *every* SPK lifecycle
  script as an unprivileged package user — "never root, even when the admin
  ran `sudo synopkg install`." Hence: the SPK is a bootstrapper only; all root
  work happens later via a self-elevating CLI; the profile snippet instead of
  a `/usr/local/bin` symlink.
- **The SPK format's trap taxonomy** — 263 (outer tar must be uncompressed),
  261 (missing `start-stop-status`), 313 (`scripts/` must be a directory DSM
  can chmod), learned by repeated installs on the real box.
- **nginx owns 80/443** and the Application Portal refuses 443 proxy rules —
  hence macvlan, and hence the host-side shim interface (macvlan containers
  cannot reach their own host).
- **DSM regenerates `/etc/passwd` and `/etc/group` at every boot** — hence the
  seam-shell heal existing in three places, and the operator's docker-group
  membership being re-asserted at boot.
- **`synocheckshare` renames colliding volume-root directories at ~142.8s into
  boot, before any hook we can persist** — hence the reclaim guard, the
  collision census, the rootfs boot-env cache, and the rule "a DSM share name
  must never equal any plain volume-root directory name on any volume."
- **DSM offers no supervision for third parties** and its own tools are the
  only sanctioned surface (`synogroup`, `synowebapi`, `synoschedtask`) —
  hence adopt-first VMs, the read-only DSM task census, and the rejected
  Ansible/Salt/NixOS path ("they fight DSM — it owns its own config DB").
- **Python 3.8.12** is what DSM ships — hence the pin, and everything the pin
  costs (§7).

### 2.4 Incidents as design input

The distinguishing practice of this codebase is that incidents are converted
into *named, tested invariants* rather than fixes. The five that shaped the
most code:

1. **2026-07-30 boot-resume race** → the ordered startup script, the 600s
   Docker poll, retried boot reconcile, ntfy-on-failure.
2. **2026-08-10 exporter blinding** → "one sick container must never be able
   to blind the sensor for all the healthy ones" — bounded per-container
   timeouts, partial-collection visibility.
3. **2026-08-14 deploy outage** → design/60: change-scoped applies, the
   deploy journal, the backoff/breaker doctrine, "never wrap
   `deploy-stack --apply` in `timeout`."
4. **2026-08-16 cold-boot share collision** → the reclaim guard, the
   collision precheck on `install` ("prose is not a guard"), `syrvisctl
   doctor`, the rootfs alarm cache ("the alarm was stored inside the thing it
   was supposed to alarm about"), the red package status ("the cheapest
   out-of-band signal"), `SYRVIS_APPS_ROOT_NAME`.
5. **2026-08-16 shed resurrection** (same day, same incident window) —
   fourteen deliberately-stopped services re-enabled mid-array-rebuild by a
   repo apply → durable intent outside the declaration set (`intent.json`),
   the enable-change guard, the degraded-mutation guard, shed-aware drift.

---

## 3. Vision and goals

SyrvisCore is the *mechanism*; the doctrine lives in home-tech's design
corpus. The division is explicit (design/00 D10): "SyrvisCore stays generic…
Items that require SyrvisCore changes are spec'd as contracts and handed to
the SyrvisCore repo as its backlog." Understanding v1 requires understanding
what it was built to serve.

### 3.1 The division of authority

Two reconcilers, one boundary (design/51):

| Concern | SyrvisCore (mechanism) | home-tech (intent) |
|---|---|---|
| Routing, certs, macvlan, compose | owns | consumes |
| Which services exist, config + secrets | validates + applies | owns (bundles) |
| DNS / tunnel / Access | reports (`stack hostnames`) | owns (reconciles) |
| Data placement | executes (`location:`, `volume_locations:`) | declares |
| Scheduled jobs | derives + runs from a root-vetted pinned source | declares + confs |
| Seam enforcement | generates from the registry | provisions once |
| Backups | `syrvisctl backup`/`restore` (platform state) | Hyper Backup tasks (data) |

This split is why the repo is public: exposure is *declared intent* consumed
by a report, never a Cloudflare API call; the shares registry reads five keys
and deliberately ignores the rest "so a deployment repo may keep richer
semantics in the same files without coupling the engine to them." The panel
that reviewed the provisioning contract scored declarative-over-MCP "10/10 on
architectural fit precisely because it leans into the seam that already
exists," with the standing rule: **"AI is an accelerant, never a
dependency."**

### 3.2 The data doctrine: verticality, ownership, the light payload

The vision, in one line (design/45/50/53): **every byte on the NAS has
exactly one declared owner, one declared home, one declared backup story, and
one declared lifecycle stage** — and the platform is a validated placement
executor for those declarations, never an inferrer of semantics.

Its load-bearing ideas:

- **The vertical** (design/50) is the unit of reasoning: one domain's bundle
  across repo, NAS trees, edge site, services, corpora — declared by a
  colocated `vertical.yaml`, graded by a five-stage lifecycle
  (`retrieve → store-raw → process → present → interact`). Data verticality
  means the *photos* vertical, the *records* vertical, the *books* vertical
  each own their bytes end-to-end; nothing rests outside a vertical (the
  `Archive` share is the single sanctioned escape hatch: "a stray byte joins
  a vertical or goes to Archive — it never rests anywhere else").
- **Every byte is plane (a) or plane (b)** (design/45): service-owned
  (container data under a SyrvisCore-managed service, declared in `data.d/`)
  or repo-owned file-share data (declared owner repo + content-addressed
  manifest + ingest contract + GC class). "No orphan data… a verify failure,
  not a curiosity."
- **The light payload** (design/53 D1, owner-ruled): `SYRVIS_HOME` carries
  the platform, L1 core data, rendered configs, and bounded spools — target
  ≈1.5–2 GB — and **no L2 service keeps durable or bulky data under it**.
  Durable stores live in app homes on declared volumes
  (`<volume>/<apps-root>/apps/<name>/{data,config,secrets,logs}`), placed by
  the app model, visible to the backup plane via a hidden DSM share.
- **Placement is a first-class validated field, never convention** (53 D5):
  `location:` and `volume_locations:` are refusal-guarded platform mechanics
  (containment, mounted-volume checks, immutability-while-populated) — the
  symlink workaround was rejected because "the placement stays invisible to
  SyrvisCore — backups, prune, drift all reason about the wrong path."
- **Placement doctrine** (design/64): volumes are `fast` or `bulk`;
  service-critical state goes fast, bulk data bulk, and "degrading any bulk
  volume may slow DATA access but must leave every service alive."

The headline verdict for the platform section of that corpus (design/53
§6.1): *"What this design needs from the platform: nothing… The first-class
concept the data split needs — 'a service's data lives on a declared volume
outside SYRVIS_HOME' — already exists."* That is the platform working as
intended: home-tech's most consequential data migration (224 GB of family
photo originals with zero off-box copies) ran entirely on shipped mechanism.

### 3.3 The operational doctrine

design/00's spine — **declare → reconcile → verify, drift is loud** — plus
four newer axes the 0.5.x line implements or begins:

- **Deploy orchestration & velocity control** (design/60): change-scoped
  applies (G1: "a no-op apply of an unmodified stack restarts zero
  containers"), the deploy journal, one ratified backoff/breaker doctrine
  (base×2^n, 10-minute cap, full jitter, breaker at 3, half-open probe at the
  cap, the open transition pages exactly once, humans reset by intent).
- **Dependency-ordered lifecycle** (design/63): `depends_on` as orchestration
  intent (never compose), whole-set graph validation, topological waves,
  reserve-first shutdown budgets ("a consumer force-killed at 60s is an
  inconvenience, a postgres force-killed at 5s is a WAL replay or worse").
- **First-class intent** (design/65): device intent (`in-service`/`drained`)
  and per-service intent (`shed`) as durable state outside the declaration
  set; "boot converges to intent," never to a halt-reason inference.
- **The host agent** (design/61, unbuilt flagship): a rootfs-resident,
  zero-volume-dependency Go daemon that owns *when* (phase, gates, bring-up
  initiation, heals, flight recorder) while the platform owns *what order* —
  born from "nothing on the box could answer 'what phase is this machine in,
  and what is actually true right now?' from outside the failure domain."

### 3.4 Implemented vs still vision

| Axis | Shipped in 0.5.x | Still design |
|---|---|---|
| App model / placement | `location:`, `volume_locations:`, apps-root segment, fileplane binds, containment + refusals | `data_location:` (share-path homes), per-volume apps-root segment |
| Intent | `intent.json` (`device` + `shed[]`), shed/unshed verbs, enable guard, degraded guard | `drain`/`restore` verbs, rootfs intent mirror, boot-converges-to-intent table |
| Orchestration | `depends_on` schema + graph + topological plan ordering; reversed-band shutdown | `syrvis up` (async, locked), bring-up gates, `restart: no` flip, blocked-subtree policy |
| Velocity | deploy journal (recording), breaker store (recording), G1 digest compare | the breaker *engine* (skip/half-open/page), recovery loop, telemetry |
| Host agent | rootfs boot-env cache, reclaim guard, `syrvisctl doctor` | all of `syrvis-hostd` (design/61) |
| Data plane | everything §3.2 needs from the platform | share declare/list verbs; backup location-awareness; design/50's introspection (0 of 11) |

---

## 4. The implemented architecture

### 4.1 Shape

```
Operator Mac                          Synology NAS
┌───────────────┐  ssh + forced cmd  ┌──────────────────────────────────────────┐
│ MCP server    │───────────────────▶│ shim ▶ sudoers ▶ syrvis / syrvisctl      │
│ (py3.12)      │   (the seam)       │            │                             │
│ home-tech     │                    │  ┌─────────▼──────────────────────────┐  │
│ deploy-stack  │                    │  │ deterministic library (py3.8)      │  │
└───────────────┘                    │  │  paths / schema / services_d /     │  │
                                     │  │  service_manager / lifecycle / …   │  │
        LAN ┌────────────────────┐   │  └───────┬──────────────┬─────────────┘  │
        ────│ dashboard (container│──▶│ library │              │ docker compose │
            │  imports library)  │   │ in-proc │              ▼                │
            └────────────────────┘   │          │   traefik ─ portainer ─ …     │
                                     │          │   syrvis-<name> projects (L2) │
                                     └──────────┴───────────────────────────────┘
```

One library, three adapters, one privileged wire. "Anything an adapter can
do, `ssh nas && syrvis …` can do" — enforced, not aspirational: library
modules raise typed `SyrvisError`s with stable `code` strings and never
print; `cli.py` is the only presentation layer; `--json` is the machine
contract the MCP passes through untouched.

### 4.2 The manager plane (`syrvisctl`)

The manager owns exactly one job: get a service version onto the box, make
one active, and be able to put the box back. Its structural commitments:

- **The SPK installs only the manager** (~20 KB wheel + 6 pinned dependency
  wheels, fully offline `--no-index` install), run entirely as the
  unprivileged package user. The manager **never imports the service** —
  separate venvs; every cross-boundary call is a subprocess of the version's
  own venv. This is what lets `syrvisctl doctor` diagnose a tree that is
  itself broken.
- **Stage-then-swap, everywhere.** Installs build in `versions/.staging-<v>`,
  move any existing version *aside* (never delete-first), fix up relocated
  shebangs, verify the CLI executes, and roll back the directory swap on any
  failure. `postupgrade` builds `venv.new`, verifies in staging, swaps, and
  hard-fails loudly ("Do NOT swallow the failure with `|| echo unknown` (the
  old bug)"). Downloads verify a `SHA256SUMS` release asset by default; a
  failed pre-upgrade backup *aborts* the install.
- **The symlink is truth.** `current -> versions/<v>` is the source of
  activation state; the manifest mirrors it for history. Activation is a
  tmp-symlink + `os.replace`, plus wrapper/profile regeneration, plus a
  best-effort boot-hook regen — added after the deployed startup script
  "drifted years behind the code."
- **Refuse rather than guess.** The generated `bin/syrvis` wrapper falls back
  to a volume scan when its baked path dies; exactly one candidate runs (with
  warnings), two refuses, zero produces recovery advice that explicitly
  forbids reinstalling. `assert_no_collision_artifacts` blocks `install`
  outright while a `<name>_1` rename artifact exists — "THE ONE COMMAND THAT
  WOULD HAVE SOLVED 2026-08-16 IN SECONDS… prose is not a guard."
- **Backup/restore is digest-verified and staged**: sidecar sha256, per-member
  digest comparison in a staging dir, traversal-guarded destinations, venv
  rebuilt from the version's cached wheel *before* the symlink moves.
  Pre-upgrade backups are declarative-only by design (never copy a running
  datastore).
- **`syrvisctl doctor`** answers from the rootfs with *no resolvable home*:
  boot-hook presence and contract currency, a volume-root census that names
  collision renames, seam-account shells. It is deliberately outside the
  error-handling wrapper — "a home-resolution failure is this command's
  PRIMARY FINDING."

### 4.3 The service core

**Home resolution** (`paths.py`) tries `$SYRVIS_HOME` (content-checked
against a self-identifying manifest — an unguarded env var once produced "a
clean, empty, ACTIVE homebase that had converged nothing"), then
`/volume1..9/syrviscore` with a strict identity check, then a walk up from
`__file__`. The apps-root segment (`SYRVIS_APPS_ROOT_NAME`, default
`syrviscore`) is configurable — the share-rename enabler — and refuses a
trailing `_<digits>` because "that is the suffix DSM hangs on a
collision-renamed volume root."

**Configuration** is `config/.env` (0600, the only secrets/network surface)
plus `config/stack.yaml` (core-tier enablement: traefik/portainer primordial
and force-enabled; cloudflared/dashboard optional). The 0.5.17 write-time
guard rejects hazardous env values (the root boot hook *sources* `.env`) and
hazardous key names (`PATH`, `LD_PRELOAD`, … — reboot-free RCE through
`load_dotenv(override=True)`): "Rejecting is a loud, local failure; the
alternative (a root shell at boot) is not recoverable."

**Compose generation** is the committed pin table `DEFAULT_DOCKER_IMAGES`
(digest-pinned, no `:latest` anywhere) optionally overridden by a
release-attached `config.yaml`. Deliberately never emitted: Traefik labels
(routing is file-provider for *every* tier — "Traefik holds no host-level
authority at all," no docker socket), published metrics ports, L2 services
(each is its own compose project `syrvis-<name>`), and `depends_on` (an
orchestration concept here, never a compose one).

**Networking**: Traefik owns a LAN IP on macvlan; the host gets a
`syrvis-shim` interface so the host can reach it; the shim is
drift-detected and its `ifcfg` stub silences DSM's health poller. DNS-01
certs via Cloudflare token; a documented 20s < 30s graceful-timeout ladder
("equal deadlines lose the drain race").

**Exposure** is a two-value declared intent (`internal`/`tunnel`) consumed
only by the `stack hostnames` report — the versioned contract a deployment
repo reconciles into LAN DNS records or tunnel hostnames + Access policies.
The honest limit is documented: "Exposure is declared intent, not routing
enforcement" — a LAN client that points a tunnel hostname at the Traefik IP
bypasses Access; the LAN is inside the trust boundary by declaration.

**Boot persistence** is two rendered artifacts — the volume-resident
`bin/syrvis-startup.sh` (seam heal, shim, 600s Docker poll, boot reconcile
×3, schedule apply, ntfy on failure) and the rootfs-resident
`S99syrviscore.sh` (inline seam heal, advisory `synocheckshare` gate,
the reclaim guard, a trampoline with a load-bearing `else` that alarms from
the rootfs env cache). Both are rendered by pure functions shared with
content-aware validators, and the S99 carries `# boot-hook-contract: 3` so
the manager can grade currency from the rootfs without importing anything.

### 4.4 The Layer-2 declaration model — configuring deployed apps/services

`config/services.d/` is the substrate: "the file collection is the substrate;
every explicit interaction is sugar that writes those files… One mechanism,
four front doors" (IaC rsync, CLI, dashboard, MCP). Three properties define
it:

**The schema is the trust boundary.** A `syrvis-service.yaml` is treated as
attacker-controlled input that becomes filesystem paths and a compose file
root starts. Hence: an allowlist of 28 top-level keys with unknown keys
rejected outright on every write path (tolerant key-dropping exists only for
read-only display); names charset-pinned with reserved-name protection
(`deployments`/`state` are reserved because `data/<name>` would overlay
platform subtrees); images must be tag- or digest-pinned; volumes admit only
named volumes, service-data-relative paths, and declared-share `fileplane=`
binds — `$`, `..`, absolute paths, and `docker.sock` refused; `tier: infra`
unlocks exactly four read-only host mounts and is granted by a *root-held
name+image-repo list*, not by anything in the manifest (the earlier
provenance-string gate "tested the very string deploy_bundle had just
written — a tautology that could not fail"); `command:`/`tasks:` are
exec-form argv running under the container's own confinement; `hooks:` can
only select *which declared task runs when*, never supply code.

**Per-file isolation.** One bad file invalidates only itself — load,
converge, and health isolation are all structural. This is the property that
makes a 39-service fleet operable by one person: a typo cannot blank the set.

**Orchestration is layered on, not baked in:**

- `depends_on: ["name[:readiness]"]` with `started`/`healthy`/`soft` —
  parsed suffix-first (so `db:healthy` survives name validation), unknown
  suffix an error never a default. Whole-set graph validation (cycles,
  unknown targets, `healthy`-onto-checkless) invalidates the *declaring*
  file only; a hard edge onto a disabled/shed/invalid target is a plan-time
  `blocked` **bucket**, never a validation error — because "a deliberate
  14-service load-shed must not fail every hourly reconcile for its
  dependants," and because validity must not flip under an apply.
- **Durable intent** lives outside the declaration set in
  `data/state/intent.json`: *a workload runs iff the device is `in-service`
  AND the service is `enabled` AND the service is not shed.* Shed pins
  `enabled: false` over any incoming bundle; `unshed` starts nothing ("one
  bring-up path, not two"); imperative `start`/`recreate` refuse on a shed
  service; deploy lands bits but does not start. This exists because "the
  flag that says 'this service is deliberately down' lived in exactly the
  file the next apply overwrites."
- **Placement**: `location: /volumeN` re-homes the whole app;
  `volume_locations:` places individual named volumes; both are
  containment-asserted, mounted-volume-checked on every start path, and
  immutable while populated ("a nested bind SHADOWS what is underneath…
  the app would come up looking freshly initialised").
- **Guards with paid overrides**: the enable-change guard (off→on flips
  refused without `--allow-enable-change`), the bulk-degraded guard
  (mutations refused during array resync without `--force`), each override
  journaled — "the refusal and the bypass cost the same one line of
  evidence."

### 4.5 The deployment plane

Four layers, kept distinct:

**Transport** — two stdin-only JSON bundle schemas. `syrvis-instance/v1`
(`apply`): env whole-file-replace + stack enablement + the *complete*
declaration set as a replace set, with plan-time guards (secret-change and
enable-change refusals, shed pinning) and per-file atomic writes.
`syrvis-bundle/v1` (`deploy`): one service's manifest + non-secret configs +
secret env values, applied atomically, start last. Secrets travel
memory → ssh-stdin → 0600 env_file; never argv, never ps, never logs, never
an LLM context — and the stdin verbs are deliberately not MCP tools.

**Materialization** — `ServiceManager` (3,651 lines, the system's center of
gravity). Every install/replace funnels through one choke point so history
records one deploy with `previous_image → image`, never a remove+add pair.
The **byte-identical-redeploy rule** (design/60 G1) is implemented as a
digest comparison against the newest deployment record, FAIL-CHANGED in
every ambiguous case, with the fix-up differentiated by artifact: changed
secrets → force-recreate (Docker bakes env at container create), changed
configs → restart (bind mounts re-read on process restart), neither →
nothing. `recreate` exists as a verb precisely because `stop`+`start` is an
intent operation, not a repair: "an operational trap hiding inside a repair
procedure."

**Convergence** — one reconcile engine (`build_reconcile_plan` /
`apply_reconcile_plan`), plan pure and side-effect-free, apply
halted-guarded and per-action isolated. Actions sort in one pass:
topological depth primary, reversed shutdown band as intra-wave tie-break —
byte-identical to the pre-graph interim when no edges exist. Four
*non-action buckets* (`disabled`, `shed`, `terminal`, `blocked`) keep
deliberate states out of the failure column. The floor check refuses to
reconcile a populated instance against an empty declaration set.

**Memory** — three state surfaces beside `runstate.json` and `intent.json`:

- `data/deployments/<workload>/NNNN.json` — immutable, atomically-numbered
  revision records (redacted; `secrets_checksum` records go 0640 because "a
  digest of a SHORT low-entropy secret is a confirmation oracle"); rollback
  re-validates the stored manifest through the full trust boundary, pulls
  first, preserves operator intent, and records `rollback_of`.
- `data/state/deploy-journal.json` — the in-flight run record with a
  four-clause contract: absent ≠ unparseable ("different verdicts"),
  `failed` is terminal, 60-minute staleness *annotates, never refuses*, and
  every success records `started` never `healthy` — "0.5.16 verifies no
  health: nothing was verified."
- `data/state/breakers.json` — the one durable breaker store (rows per
  `{plane, context}`, capped jittered curve, cross-plane suppression, "a
  close closes all," `by` as a field where only `cli:`/`seam:`/`mcp:` close).
  **Recording only** at v1 — the consuming engine is design/63 M2 (§6.2).

### 4.6 Lifecycle and host integration

- **Shutdown/resume**: halted runstate written *first* (closing the
  cron-reconcile race), VM ACPI issued fire-and-forget, L2 stopped in
  priority bands under **reserve-first clamping** — stores (band ≥90) always
  keep their declared grace; consumers clamp into what remains after the
  store tail + remaining VM drain + core reserve. Resume is core → VMs → L2
  through the one reconcile engine; a killed resume leaves the instance
  halted. The boot matrix auto-resumes a UPS halt and holds a maintenance
  halt.
- **Transition hooks**: container hooks select a declared task at
  `pre-stop`/`post-start`; host hooks are root-authored drop-ins with a
  strict trust walk, scrubbed env, re-entrancy fuse, and a fail-open
  *authorship* control (an unprivileged operator cannot write such a file).
  Only `pre-start`/`pre-deploy` may abort — "you must always be able to
  stop."
- **Scheduling**: a delimited managed block in `/etc/crontab`; jobs.d
  declares only `{schedule, enabled}` — the command is *derived*
  (`jobs/<name>`), the source is a root-configured, **commit-pinned** git
  clone with a strict sha256 manifest ("a parser that silently ignores rows
  it does not understand is a parser an attacker writes rows for"). The pin
  work (design/66/68) opens with a rare public retraction: the old claim
  that a compromised operator "can at most re-apply the already-synced,
  root-vetted set" **"WAS FALSE, and was false for as long as this module
  has existed."** A read-only census of DSM's own Task Scheduler closes the
  "what ELSE is scheduled on this box" blind spot — census, never
  management, and "'the census could not run' and 'there are no other
  tasks' must never look the same."
- **VMs**: adopt-first Synology VMM guests as declared workloads —
  lifecycle and inventory only, never creation ("SyrvisCore owns the VM's
  lifecycle + resource envelope; it does not own the VM's birth"). VM
  `stop_timeout` is a declared claim on the shutdown budget, surfaced by
  `vm list` as the budget census.
- **Supply-chain readouts**: report-only image update detection with
  flavor-aware tag matching (a `2.5` never jumps to CalVer), provenance
  scored from a curated trust file ("a curated, diffable git assertion, not
  an unanswerable live lookup"), and `syrvis export` — a redacted
  `syrvis-instance/v1` snapshot that hard-errors on an unparseable
  declaration because a silently-partial snapshot would later *delete* that
  service's intent.

### 4.7 The operator seam

The seam is v1's crown jewel and the part most worth studying.

**The model.** All routine management happens as `syrvis-operator`, a
dedicated least-privilege account whose SSH key line is
`restrict,command="<shim>",from="<cidr>" <pubkey>` and whose sudo rights are
an enumerated `NOPASSWD` allowlist. Both artifacts — the 291-line
forced-command shim and the 69-line sudoers policy — are **generated from
one registry** (`syrviscore.seam.registry`, 77 commands, 18 slot kinds) that
the runtime *also* uses to build argv: "the enumerated sudoers boundary and
the shim allowlist are derived from the same source the runtime uses to
build argv, so they cannot drift" (G18). A committed drift test asserts
generated == committed.

**Defense in depth** (G13): the client validates every user value against
per-kind allowlists; every user positional lands after a literal `--`;
tokens are `shlex.quote`d into one remote string; and the NAS-side shim
independently re-validates `$SSH_ORIGINAL_COMMAND` — a character whitelist
(`[^A-Za-z0-9 ._@:/-]` kills every metachar in one grep), an
embedded-newline check, `set -f`, exact arity + literal equality + per-kind
predicate per slot, then `exec "$@"`. Sudo's trailing globs are safe because
sudo `execve`s a single argv, never a shell. The independence is the point:
"OpenSSH re-parses remote args through the remote shell."

**Verb classes** carry the policy: reads (no sudo, all `--json`);
sudo-bearing reads that are side-effect-free by construction; converge verbs
(idempotent lifecycle) — with `shutdown`/`resume` and `shed`/`unshed`
deliberately token-free so an unattended NUT hook or degradation response
needs no human; destructive verbs behind a two-call HMAC confirmation
handshake (per-process-salted tokens, nonce-locked, state-bound — a restart
voids every outstanding token); and stdin writers that are script-only.
Overrides are **distinct registry ids** (`reconcile_force`, `apply_secrets`,
`apply_enable_change`…) so "the override must be asked for, not inherited,
and the audit line names which one ran," with deliberately no combined
shape: "two overrides in one call is two decisions in one."

**Lifecycle**: `syrvisctl activate`/`rollback`/`install` re-render sudoers +
shim from the newly active version — new verbs arrive with the release that
implements them; a rollback narrows the seam back. The trade is recorded
verbatim: "the trust anchor becomes the release channel plus this root-held
policy file — not a human re-provision."

**The deliberate floor**: `setup`, `restore` ("disaster recovery must not
depend on the thing being recovered"), `doctor`/`clean`/`reset`, `--purge`,
and VM creation/deletion stay off the seam. Break-glass SSH remains, by
design.

**Honest residuals** (from the 2026-07 red team and this review): a stolen
operator key can invoke every enumerated command legitimately — the layers
exist to keep that surface from *widening*; sudoers globs are looser than
the shim, so a second, unforced key on the account would collapse the
boundary to the glob layer; and the auto-sync trust trade has a real
fail-open edge (§6.5).

### 4.8 The adapters

**MCP** (operator-side, Python 3.12): 50 tools projecting registry commands
over injection-hardened SSH, with sandbox membership checks, fail-closed
allowlists (an empty git-host allowlist means *disabled*, never *any*), a
0600 append-only audit log, and ground-truth follow-up reads instead of
synthesized state. The red team's verdict: "No critical, no unauthenticated
RCE, no forgeable confirmation token… The layered model held."

**Dashboard** (on-NAS container): imports the library in-process against a
bind-mounted install tree; TTL-cached concurrent probes that degrade instead
of 500ing; SSE over polling as an optimization, not a dependency; write
paths double-gated (`ENABLE_L2_MUTATIONS` runtime flag × `WITH_L2_TOOLS`
build arg) and everything host-root rendered as an SSH hint — "Run this
yourself over SSH — the dashboard never executes host-root commands." The
`/api/summary` fold encodes the alarm-hygiene doctrine: shed is not
unhealthy ("an amber that never clears is an amber nobody reads"), absent
beats invented, a summary poll never touches the network. Its structural
weakness is the **image-lockstep coupling** — the container bundles its own
copy of the library, so a stale image mis-reads newer state; two silent
blindness regressions shipped in three image releases, and the `depends_on`
reader-enumeration gate exists only as prose in CLAUDE.md (§6.6).

### 4.9 Testing, simulation, CI

~1,500 hermetic tests in 25 seconds on 3.8 (plus 423 MCP and 108 dashboard
tests on 3.12), built on dependency injection rather than a DSM simulator:
injected runners for `synoschedtask`/`synowebapi`/mdstat, argv-dispatching
fakes for `ip`, instance-method surgery at the Docker boundary, and — the
strongest technique — **executing the rendered shell**: the boot reclaim
guard and wrapper fallback are proven by running the shipped script bodies
against fake volume trees, "assertions alone would not prove a shell script
works." State-file contracts (journal, breakers) are pinned clause by
clause. The `dev-loop` CI job runs a real venv, real pip, and a real
disaster recovery (backup → `rm -rf` the home → restore → verify).

The posture's honest description: the suite is the institutional memory of
every incident, and it is *hermetic-library* testing — root, DSM, and Docker
are injected, string-asserted, or untested. What that costs is quantified in
§6.9–§6.12.

---

## 5. What v1 got right

These are the parts a rewrite must preserve — as principles, not code.

1. **The seam.** One registry rendering policy, shim, and provision script;
   independent server-side re-validation; distinct ids per override;
   token-free reversibility for unattended paths; a deliberate break-glass
   floor. Red-teamed and held. Transferable to any project.
2. **Deterministic core, thin adapters — enforced.** Typed errors with
   stable codes, no printing in the library, `--json` everywhere, three
   adapters and none re-derives semantics.
3. **Stage-then-swap and digest-verified restore.** The install path cannot
   destroy a working version; restore cannot claim success while leaving a
   non-runnable installation active.
4. **Durable intent outside the declaration set.** The one-sentence
   composition rule; shed pinning; "one bring-up path, not two." The single
   best structural fix of 2026, and it generalizes.
5. **Buckets, not failures — alarm hygiene as a design concern.**
   `disabled`/`shed`/`terminal`/`blocked` are report rows; shed is not
   unhealthy; a permanently-armed check was demoted because "a liveness gate
   only goes red for states someone can fix." Very few systems treat false
   alarms as bugs.
6. **Three-valued honesty.** Absent ≠ unparseable ≠ empty, everywhere: the
   journal's `unknown`, the census's `ok:false`, "absent beats invented,"
   unreadable mdstat as "no evidence" never "assume one."
7. **Per-file isolation with a named strict/tolerant asymmetry.** The write
   path is the trust boundary; the read path may tolerate. One typo never
   blanks the fleet.
8. **Refusals that carry their recovery.** The collision precheck, the
   wrapper's refuse-over-guess, the deleted destructive advice ("Run
   `syrvisctl install`" is the one that makes it worse), the red package
   status as an out-of-band signal, alarms cached outside the failure
   domain.
9. **Supply-chain posture.** Digest pins, no `:latest` anywhere, SHA256SUMS
   by default, offline SPK installs, an owned non-root distroless exporter
   replacing an anonymous root container, a commit-pinned jobs source.
10. **Comments as provenance.** The dated incident behind nearly every
    constant. It is why this retrospective was possible, and a rewrite that
    keeps the code and drops the comments would lose more than it kept.

---

## 6. Gaps in the design

Ranked. Each verified against the tree at 0.5.17 unless marked otherwise.

### 6.1 No concurrency control in the service plane — critical

The service package contains **no `flock` at all** (verified). At least five
writers touch the same tree — hourly cron reconcile, the S99 boot path, seam
`apply`/`deploy`, `schedule apply`, the dashboard in management mode — with
only per-file `os.replace` atomicity between them. `intent.json`,
`breakers.json`, and the journal are unguarded read-modify-write (two
concurrent sheds lose a row); `instance_bundle._write_declarations` can
unlink the set under an in-flight reconcile that already loaded it; revision
numbers are claimed by a lock-free `os.link` retry. The manager *has* a lock
but takes it per primitive — `download_and_install` releases it between
install and activate; `restore_from_backup` takes none. The gap is silent:
every individual write succeeds. No cross-process test exists anywhere.

### 6.2 The safety machinery is written but unwired, and reads as built — critical

`journal_status`, `should_attempt`, and `suppressed_by` have **zero non-test
callers** (verified). The breaker store records; nothing consults it —
half-open, cross-plane suppression, and page-once are specified, tested, and
unenforced (design/63 M2 is the missing engine). `healthy` readiness is
validated at declaration time and not enforced at runtime — the exact
"checks that report what they cannot see" class the same module refuses
elsewhere. The journal also *leaks*: `record_event(STARTING)` fires before
at least six early-return refusal paths in `deploy_bundle` that never write
a terminal event, leaving permanently in-flight runs that go stale and then
annotate every later bring-up. A partially-built safety mechanism that
renders on status surfaces is worse than an absent one.

### 6.3 The shutdown budget does not fit its transport — critical, unproven

Measured reserve: stores 120s + VM drain 90s + core 30s = **240s
irreducible**. The rc.d `stop)` wrapper bounds the whole flush at
`timeout 150s`, and the source admits "DSM's rc.d-stop timeout is UNVERIFIED
here." On a plain DSM reboot a store-heavy instance can be SIGKILLed
mid-walk — the exact WAL-replay outcome the reserve-first doctrine exists to
prevent. Compounding: `elapsed_s` saturates at the budget on overrun (the
one number an operator would tune with), and the DSM-side UPS toggles that
*trigger* the flush live in UI state no verb can read — "until it is,
nothing triggers the flush on a real outage" (design/28).

### 6.4 Backup/DR has drifted behind the storage model — high

`_gather_backup_items` walks only home-relative paths (verified) —
`config/`, L1 data dirs, `services/`, `compose/`, `data/<svc>` — while 15
located services keep their app homes (including `config/` and `secrets/`
slots) on other volumes. A "full disaster-recovery backup" captures none of
them, and the archive's own metadata does not declare the omission.
`preupgrade` also writes `backups/upgrade-<ts>/` directories that
`list_backups` ignores and cleanup never removes.

### 6.5 Auto seam sync fails open on width; drift is unobservable over the seam — high, security

Auto-sync runs the newly-activated version's generator as root, and that
generator rewrites the policy constraining it. Rolling back below 0.4 leaves
the artifacts *unchanged* — the boundary stays **wider** than the running
version. `shim_path` in the policy is never cross-checked against the forced
command in `authorized_keys`. And `syrvisctl seam status` self-elevates, so
policy drift can only be observed from the break-glass login the seam exists
to eliminate. Adjacent: `guard_bulk_degraded` hardcodes `by="cli:force"`, so
a seam-driven `--force` closes breakers as if a human typed it — the exact
inference `opc:F2` forbids; and `privilege.self_elevate` re-execs an argv
shape that is not in the sudoers policy — a second, unenumerated privilege
model coexisting with the enumerated one.

### 6.6 The reader-enumeration gate is prose, and image publishing contradicts itself — high

`depends_on` is *inside* the schema allowlist, so the dashboard's tolerant
reader gives no protection: a stale image rejects the key, the declaration
lands in `invalid`, and the running service renders `unmanaged` — "the one
state it definitively is not." The gate (rebuild + repin before any edge;
no rollback below 0.5.16 while edges exist) lives only in CLAUDE.md.
Meanwhile two workflows publish the dashboard image with *different tag
sources* (verified: `dashboard-image.yml` tags at the **service** version;
`test.yml`/`build-dashboard.sh` at the **dashboard** version), both on merge
to main, both pushing mutable `:latest`. The precedent is real: two silent
blindness regressions in three image releases.

### 6.7 Six state stores that can disagree, with no reconciler over them — high

Symlink vs manifest is resolved (symlink wins). Beyond it: two independent
drift notions (planner vs `drift.py`) that can disagree, with shed filtering
bolted onto only one; intent read/write asymmetry (reads accept what writes
reject, so a hand-edited file poisons the next unrelated write); shed rows
never garbage-collected — a shed row for a deleted declaration blocks
dependants forever; revision retention silently deletes rollback targets at
50; breakers' unknown-schema behavior is asymmetric (read → `[]`,
write → swallowed raise) so counting stops with no signal. "What is true
right now" has no single answer — the structural version of design/61's
thesis.

### 6.8 The stdin plane is outside the enumerated model — high, trust boundary

"Stdin writers are script-only, never MCP tools" is a convention, not an
invariant — `remote.py` supports `_stdin` generically, and a tool exposing
`deploy` is one function away, which would put secrets in an LLM context
with no structural objection. Shim validation never sees stdin; the only
bounds are byte caps plus downstream schema.

### 6.9 `syrvis setup` is essentially untested and partly broken — high

11% coverage on the verb every install runs first. Setup cannot enable the
dashboard (it keys off a config value the prompt never sets); non-interactive
setup never populates the DNS token or Portainer password; the no-stack.yaml
migration default enables cloudflared with no token, yielding a restarting
container. Neighbours: `doctor.py` 9%, `update.py` 11%,
`privileged_ops.py` 43%; no `test_setup` end-to-end, no `test_verify.py`, no
`test_doctor.py`.

### 6.10 Docker is never exercised in any gated test — high

The Docker boundary is stubbed, mocked, or dead-socketed everywhere. The
proven consequence class: 0.5.12's flapping detection read `RestartCount`
from "a location Docker has never populated" — the fake fabricated the
field, 1,100 tests passed, the feature shipped inert. The G1 rule rests on
unverified premises (that force-recreate re-bakes env; that restart re-reads
a bind mount). Nothing verifies a generated compose file is one Docker will
accept.

### 6.11 The SPK — the only artifact DSM installs — has no CI — high

No job builds or validates the SPK; no `sh -n`, no shellcheck. The
263/261/313/276 error taxonomy is guarded by human memory. The shell scripts
each reimplement home discovery and **none** knows about collision renames
or the configurable apps-root; `preupgrade` still probes a legacy path;
`spk/INFO` reads `version="0.1.21"`. The SPK's reliability is a stated DR
requirement and it is the least-gated artifact in the repo — the 2026-08-16
hardening reached the Python layer and stopped at the shell boundary.

### 6.12 The DSM simulator is dead; DSM-interaction paths are production-tested — high

Everything in `tests/dsm-sim/` predates the modern platform; neither shell
test runs in CI; the sim writes a *different* S99 body than production
renders; no mock exists for `synocheckshare`, `synoschedtask`, `synowebapi`,
`crontab`, `ip`, or `docker`. The 2026-07 audit's verdict — "the DSM
simulation simulates success, not DSM"; "Would the suite have caught the
real NAS failures? No" — is only half-retired. The 2026-08-16 pattern (eight
versions in one day) rhymes with 2025-12-26 structurally, not
coincidentally: for boot/rename/DSM paths, the NAS is still the integration
test.

### 6.13 Four writers of the boot artifacts; a contract integer that no longer means currency — medium/high

`update.py` (a second, stale version manager still registered in the CLI),
`activate`'s regen hook, `setup`, and `verify --fix` all write S99.
`BOOT_HOOK_CONTRACT` is hand-duplicated across the package boundary and —
since the `synocheckshare` gate deliberately did not bump it — now marks
capability, not content currency. The 0.5.2 root cause (a deployed hook
drifting years behind its renderer) is structurally still possible from the
writer nobody audits.

### 6.14 Composite operations are not transactional — medium

`apply` writes env → stack → declarations, each atomic, none joint
("recovery is re-run apply"). `sync_from_source` installs declarations
before materializing scripts, so a mid-way failure silently unschedules
jobs. The repo already owns the fix pattern — stage-then-swap — and uses it
three times elsewhere.

### 6.15 Guard and replace-set asymmetries — medium

The enable guard exists on the `apply` path but not on `converge`'s
declare path (`stack apply --from` can resurrect unguarded). Replace-set
removal has no guard: an accidental omission from a bundle deletes a
declaration silently, and the orphaned container reads `unmanaged`.

### 6.16 The MCP adapter has fallen behind its own boundary — medium

50 tools against 77 registry commands — `vm_*`, `shed`/`unshed`/`recreate`,
`schedule dsm-tasks`, `doctor` are permitted on the key and unreachable as
tools. The validator map covers 16 of 18 slot kinds (a bare `KeyError`
escapes the error funnel for the two new ones). `_parse_ssh_user` fails open
on aliased/`Match`/`Include` ssh configs, silently skipping the
forbidden-user refusal. Audit lines have no timestamp, rotation, or bound.

### 6.17 Dashboard read-path and presentation gaps — medium/low

`/api/docs`+`openapi.json` escape auth (constructor-registered); stale OIDC
session outranks a live Access JWT in `both` mode; the read-only data mount
means the update cache can never be written (so `image_updates` stays `null`
forever on default installs); and the frontend renders `shed`/`terminal`/
`blocked` as "Unmanaged" — reproducing in the UI the exact mislabel the
backend comment warns about.

### 6.18 Doctrine gaps the platform inherits — medium

No `share declare` verb exists, so every DSM share is hand-made — the exact
process that armed the collision landmine. `logging:` is deferred to 0.6, so
**every container today has unbounded logs** and all 12 managed jobs discard
output. And the doc corpus has materially rotted: `design-doc.md` claims
"Implemented" while documenting removed verbs; the wiki still says
`depends_on` is rejected; the schema reference omits ~10 of 28 keys; four
docs are untouched since 2025-12-26. Doc rot here is not cosmetic — the
schema's published surface understates the trust boundary it defines.

---

## 7. Cross-cutting tensions

Deliberate trades that were right when made and now bind.

**Python 3.8, forever, for DSM parity.** Correct — the code that runs on the
NAS is the code that is tested — and expensive: Black frozen at 24.8.0, CI
pinned to `ubuntu-22.04`, two interpreters, three test suites that cannot be
collected in one process, 91 orphaned tests, and an entire drift class
(`_cli_regexes.py`, duplicated constants with drift tests) whose root cause
is "two runtimes that cannot import each other." The pin binds until DSM
moves or SyrvisCore ships its own runtime — and shipping an interpreter
breaks the lightweight-bootstrapper constraint Error 276 forced.

**CLI-as-engine vs a daemon.** Every operation is a one-shot process. That
is why flapping is inferred from one sample ("an engine that has to remember
a previous observation to notice a crash loop cannot notice one across a
restart of itself"), why the journal reconstructs "in flight" from a pid and
a staleness rule, why a half-open timer cannot exist, why nothing serializes
writers, and why design/61 is 1,320 lines of unbuilt Go. The benefit is real
and must not be traded casually: every action is reproducible from a shell,
inspectable in files, and cannot be held hostage by a wedged daemon. The
honest reading: v1 needs *one* long-lived thing — a lock holder and a
timer — not a supervisor for its own sake.

**Files as the database.** Atomic per-file writes buy crash-safety, human
legibility, and zero operational dependencies; they cost transactions,
cross-file invariants, and indexes. Six stores now hold overlapping truth
with six schema integers and hand-rolled retention/GC per file. Files were
the right call; the missing piece is the one thing a database gives for
free — a serialization point.

**One-shot reconcile vs continuous convergence.** The plan/apply split is
exemplary and dry-run is safe by construction. But nothing converges between
cron ticks — a service that dies at 12:05 stays dead until 13:00 — and with
no breaker engine, a crash-looping dependency is retried at full rate every
hour. The buckets are the right vocabulary for a system that will eventually
gate; today they describe a system that never retries differently.

**Generic platform vs deployment repo.** The discipline is real, rare, and
what makes the repo public. It also exports the hardest problems: share
creation belongs to no engine; the cross-repo gates (reader enumeration,
rollback prohibition, tag gate) are properties of two repos and live in
prose in one; and SyrvisCore's history is not reconstructible from
SyrvisCore — a reader hitting "design/63 D2 as amended, `opc:F10`" has no
local referent.

**Refuse-don't-guess.** The strongest cultural rule in the codebase, and it
compounds into a growing surface where operations wedge. The best refusals
name their exact recovery command; the rule should be that every refusal
does.

**Incident-driven design.** Extraordinary signal per line — and it optimizes
for the last failure. The untested classes (concurrency, ENOSPC, truncated
writes, kill-mid-deploy, real Docker semantics) are precisely the ones that
have not yet produced an incident. ~10 failure injections across 56 test
modules.

---

## 8. From scratch: what I would do differently

This section answers the direct question. It is deliberately opinionated;
§9 turns it into paths.

### 8.1 Language: Go on the NAS; Python 3.12 + uv on the Mac; not Rust

The 3.8 pin is a *distribution* decision wearing a language costume — the
question is not "is Python good enough" but "what does shipping an
interpreted runtime onto DSM cost," and v1 priced it: the venv-per-version
machinery (staging, shebang relocation, `pyvenv.cfg` rewriting, offline
wheel bundles, tree chmods — and 962 MB of retained versions on the live
box), the toolchain freeze, the two-runtime drift class, and an interpreter
that is DSM's private dependency. The house has already ruled on this once,
for the host agent (design/61 D1): static stdlib-only Go, because sh fails
at daemon scale, rootfs python "can move or vanish at a major rev, which is
precisely the event this agent must survive," and a static binary sidesteps
DSM's glibc jump. That ruling applies verbatim to the platform.

- **Staying Python (3.12 + uv)** fixes the toolchain half and preserves the
  ~2,000 gated tests — the strongest anti-rewrite argument — but fixes
  nothing structural: you still ship a relocatable interpreter tree onto
  DSM, still cannot import the platform from modern-Python adapters without
  moving everything at once, and still have no compile-time protection
  against the drift class that actually bites here. The 0.5.12 case is the
  study: a field Docker never populates shipped inert past 1,100 tests
  because a fake fabricated it. A typed API client makes that a field that
  does not exist; an exhaustive switch makes 16-of-18 validator coverage a
  build error.
- **Rust: no.** The security boundary here is argv construction, enumeration
  and allowlisting — not memory safety. Nothing in the audit or the red team
  was a memory bug. Rust buys the wrong safety, costs an async runtime, and
  lands on a single maintainer whose house language is Python.
- **Go's fit is specific**, not generic: the only Docker SDK import in the
  tree is `docker_manager.py` (everything else already shells out); the
  stdlib talks to `/var/run/docker.sock` with a custom transport and zero
  dependencies; `os/exec` with contexts is what `synowebapi`/`synoschedtask`
  /`ip`/`git` need; gofmt ends formatter debates; a static linux/amd64
  binary cross-compiles from the Mac with no Docker and no CI dependency.
  Precedent on-platform: Tailscale, Entware — and SyrvisCore's own
  `docker-state-exporter` is already a Go program in this repo.
- **Keep Python where it is already modern**: the MCP server runs on the Mac
  at 3.12 and never touches DSM. Move it to uv and make it a *generated*
  client (below) so the 50-of-77 tool gap becomes structurally impossible.

### 8.2 Architecture

**1. A resident agent, not a one-shot CLI.** Every file in `data/state/`
exists to fake continuity across process deaths; design/61 already specifies
the daemon the platform keeps approximating in shell and JSON. Build
`syrvisd`: rootfs-resident, static, the **single writer** for all derived
state, owning the reconcile loop, the flapping window, the breaker engine,
the journal, boot orchestration, the seam heal, and schedule apply.
Serialize every mutation through one in-process queue and the entire §6.1
class is deleted, not mitigated. The CLI remains as a thin socket client —
with one non-negotiable exception: a `--rescue` mode that works with the
daemon dead and no resolvable home, which is `syrvisctl doctor`'s lesson
made permanent.

**2. API-first; the adapters are generated.** Today one operator call is
parsed five times (MCP argv build → ssh string join → shim re-split →
sudoers glob → Click), and the layering deforms the design — `schedule
apply` takes no cron argv because `*` and `,` cannot pass the shim charset;
18 slot kinds exist to describe how to spell values safely in a shell. Make
the wire a typed, versioned request. From one API schema, generate the CLI
command tree, the MCP tool set, the dashboard client, and the authorization
table — G18 applied once instead of three times, with adapters that cannot
lag.

**3. State: SQLite, one file, one writer; declarations stay files.** Six
schema integers, hand-rolled retention, a 1,000-attempt `os.link` numbering
loop, silent rollback-target expiry, unGC'd shed rows, lexical ISO
comparison, non-transactional `apply` — every one is a database feature
re-implemented by hand, once per file. `state.db` (WAL, single writer) with
a real migration ladder replaces them; `apply` becomes a transaction;
"the rollback target vanished" becomes a query that can say so. **The line
to hold:** `services.d/`, `.env`, `stack.yaml`, `shares.d`, `jobs.d` remain
plain files — the substrate, rsync-pushable, git-diffable. Nothing a human
or another repo authors lives in the database; nothing the daemon derives
lives in a file.

**4. Level-triggered convergence; every verb writes intent.** v1 already
discovered this shape under pressure: three mechanisms hold a service down
and the code's own comment reads "Three mechanisms, one field, cancelling
out"; `stop` writes ephemeral intent that an apply resurrects; shed had to
be invented mid-incident. Make shed the *only* shape — every imperative
verb is an intent write (`stop` is a shed with `reason: operator`) and one
level-triggered loop converges. The loop is also what finally gives the
written doctrine a caller: breakers consulted (`should_attempt`), the
journal read, `blocked` re-evaluated every tick, flapping observed rather
than inferred. Keep the plan/apply purity split verbatim — it is the
best-tested thing in the repo.

**5. Orchestration: direct Docker API; compose becomes a projection.**
Reject k3s (the routing fight does not get easier with a CNI on Synology;
a control plane's write volume contradicts the light payload; adding etcd
to the 2026-08-16 blast radius is strictly worse for one maintainer with no
staging). Note podman/quadlet as conceptually closest and reject it on
platform (DSM ships Docker; switching runtimes abandons Portainer, the
socket, and the metrics plane). Then stop treating compose as truth: it is
already only an intermediate serialization the platform writes, shells out
to, and works around — `depends_on` suppressed, G1 hand-built because
`up -d` compares spec not content, `--remove-orphans` too wide, per-service
projects purely for isolation, a v1/v2 binary probe. Drive the Docker API
directly: the container spec is typed, ownership is a label, and
"does the running container match the declaration" becomes a spec-hash
comparison — which collapses the two drift notions, the
recreate-vs-restart distinction, and the double generation. Keep
`syrvis compose export` as a human/Portainer projection — a debug artifact,
never the truth. The cost is honest: you own restart policy and readiness
yourself — which v1 *already does* (design/63 chose `restart: no` on moby
internals; "healthchecks absorb that" was already ruled "a hope, not a
mechanism").

**6. The seam, redesigned: socket + capability grants; ssh as pure
transport.** Three of the seam's layers exist only because the wire is a
shell string. Delete the string: the agent listens on a root:operator 0660
UNIX socket; the operator key keeps `restrict,from=`, but the forced command
becomes a subcommand of the same binary that reads a length-framed JSON
request on stdin and forwards it. **No argv on the wire at all** — which is
already how `apply`/`deploy`/`secret set` work, and they are the only verbs
where secrets are structurally safe; generalize the exception into the
rule. Authorization becomes an in-process role check against caller
identity, shipped and versioned with the binary. That retires, by
construction: sudoers globs looser than the shim; the charset allowlist
deforming schemas; the three duplicated `/etc/passwd` heals (a forced
command that is the agent's own subcommand needs no login shell); the
auto-sync fail-open-on-width hole; and the second unenumerated elevation
path. Keep unchanged: the dedicated account, `restrict`+`from=`, the
two-call confirmation handshake, the break-glass floor, and
secrets-never-on-argv. Fix the audit log (timestamps, rotation, bounds).

**7. Packaging: the SPK survives; the venv does not.** The SPK's reason
still holds — "its primary justification is disaster recovery." It becomes a
file drop: one static binary, an rc.d hook, a boot-env cache. The
manager/service split existed because a venv is expensive and mutable; a
binary is neither, so collapse to one binary with subcommands — keeping the
split's actual value as the rootfs-resident `--rescue` copy (strictly
better than today: on 2026-08-16 the rootfs hook survived while everything
volume-resident died together). Versioning: download, verify sha256,
symlink. Rollback is a symlink swap of a file — which is what "instant
rollback" always claimed to be. Non-negotiable: CI builds and validates the
SPK and the binary from day one.

**8. Adapters.** Fold the dashboard *backend* into the agent — it is
already an in-process library import, and same-binary serving deletes the
image-lockstep failure class outright (two silent regressions in three
releases; the D2 gate enforced in prose). Ship the SPA as static assets
behind Traefik. The honest trade to weigh: v1's dashboard container is an
unprivileged, `no-new-privileges` process, and folding it into a root
daemon trades that isolation for coupling-correctness — mitigate with a
read-only HTTP surface, strict handler/auth separation, and the mutation
API staying socket-only. The MCP stays Python on the Mac, generated from
the API schema.

### 8.3 What must be kept (restated as principles)

1. Declarations are files; the file collection is the substrate.
2. Deterministic core, thin adapters; nothing exists only via an adapter.
3. Per-file validation isolation.
4. Strict on write, tolerant on read — the write path is the trust boundary.
5. Intent is durable and lives outside the declaration set.
6. Refuse rather than guess — and every refusal names its recovery.
7. Name the state; never infer it. Unknown is never a pass. Shed is not
   unhealthy.
8. Guards are overridable, and the override costs evidence.
9. Plans are pure; dry-run is safe by construction.
10. Anything two components must agree on is generated from one source —
    and prose is not a guard: cross-repo gates belong in CI.
11. The alarm must not live inside the thing it alarms about.
12. Reserve-first budgets — and verify the transport window before trusting
    the budget.
13. Secrets transit stdin/socket only — never argv, ps, logs, or an LLM
    context.
14. Stay generic. Exposure is declared intent; the platform never touches
    DNS. Every incident becomes a named test.

---

## 9. Refactor and rewrite paths

### Option A — incremental refactor in place

Move to 3.12 + uv, unify environments, adopt SQLite for `data/state/`, wire
the breaker engine and journal consumers, fix the backup layout bug, adopt
the 91 orphaned tests, add the instance lock. *Effort:* weeks, part-time.
*Risk:* low per step. *Limit:* it deletes no structural cost —
venv-per-version, the argv seam, the shim/sudoers pair, the dashboard
lockstep, and the four boot-artifact writers all survive. And the 3.8→3.12
step alone forces shipping an interpreter: paying a static binary's
distribution cost without getting one.

### Option B — strangler: a Go agent alongside; verbs migrate

Build design/61's `syrvis-hostd` as the seed of `syrvisd` — it is already
specified at 1,320 lines, already ruled Go, already has placement, release,
checksum, and update-survival stories. Sequence: (1) boot orchestration and
self-heal first — highest value, lowest coupling, and the exact thing that
failed on 2026-08-16; (2) the state store, single-writer from the first
commit; (3) the reconcile loop plus the breaker engine. Then verbs migrate
one at a time — each becomes a socket method, the Python CLI its client;
the forced command becomes the JSON proxy when the last argv verb moves;
the dashboard backend folds in; the SPK drops the venv when the last
Python verb is gone. *Effort:* months, but every step ships and is
independently revertible. *Risk:* a dual-writer window on state — held
closed by one invariant from day one: **the Python side never writes the
new state store; it calls the agent.**

### Option C — clean rewrite + state import

The importer is genuinely easy (six JSON schemas + YAML declarations). But
it discards ~2,000 gated tests that encode a dozen named incidents, and the
surfaces it must re-derive are exactly the untested ones (`setup` 11%,
`doctor` 9%, Docker never exercised, no SPK ever built in CI). With no
staging environment, re-deriving those against a production NAS is a return
to the condition the audit named: *"the NAS was the integration test."*
Reject.

### Recommendation

**Option B**, with three hard gates:

1. **No verb migrates without its incident tests ported.** The tests are
   the design record; losing them is the only irreversible cost in this
   plan.
2. **CI builds the SPK and the binary from day one** — the artifact DSM
   actually installs has never been gated.
3. **The agent ships its rescue path (rootfs-resident, zero volume
   dependency) before it owns anything important.** The platform's worst
   outage came from trusting a volume path; the replacement must not repeat
   it on its first boot.

Independent of any path, four items from §6 are worth doing *now* in v1
because they are cheap and the exposure is live: the instance-level lock
(6.1), the journal `try/finally` leak fix (6.2), the backup layout fix
(6.4), and deleting the relic dashboard-image workflow (6.6).

---

# Part II — The consumer, and the v2 that answers it

*Added 2026-08-18. §10 is an evidence inventory from a full audit of
home-tech's executable surfaces; §11 records the owner's mandate; §12–§13
are the v2 design that Part I's §8 sketched, now grounded in what the
consumer actually does.*

---

## 10. The consumer in practice — how home-tech drives v1

The seam registry enumerates 77 commands. The question Part I could not
answer is which of them the deployment repo *actually uses*, through which
channels, and what it has to build around them. The answer reshapes the v2
design: **roughly twenty argv shapes carry all the automated traffic, a
third of the registry is documentation-only, nine rows are dead — and the
consumer's real workload is not invoking verbs but compensating for the
orchestration, state-reading, and filesystem surfaces the verbs don't
provide.**

### 10.1 Five channels

| Channel | Shape | Load-bearing for |
|---|---|---|
| **Seam** | `ssh -F ~/.config/syrviscore-mcp/ssh_config syrvis-nas -- '[sudo -n] /volume4/syrviscore/bin/syrvis <verb> --json'` | every scripted read and write |
| **Local root** (on-NAS) | managed-crontab jobs invoking `$SYRVIS_HOME/bin/syrvis` or reading state files directly | the 30-job cron plane |
| **Reader** | `ssh syrvis-reader -- '<raw shell>'` — an identity that exists *entirely* because the seam has no filesystem verbs | backup freshness, file-plane probe, `/etc/passwd` audit, zero-byte-secrets sweep |
| **Dashboard HTTP** | `curl --resolve dash.konsume.org:443:<traefik_ip> …/api/services` — exists for exactly one missing field: `restart_count` | acceptance sweeps |
| **Break-glass** | `ssh cerebrate@ds` — pages the owner on every login | every `mv`, `rsync`, `docker exec/rm/inspect`, `/etc/passwd` repair, rootfs install |

The MCP server (50 typed tools) is configured, doctrinally endorsed — and
appears in **no executable path in the repo**. Five read-only tools are
pre-approved; everything else prompts. In practice the raw seam is the
channel automation and agents both use, which matters for §12: the typed
surface must be the *only* surface, not a parallel one.

### 10.2 The executed surface

**Automated writes** (the entire set): `apply` (×4 flag shapes, stdin
bundle), `deploy [--force]` (stdin bundle), `config set`, `secret set`,
`stack apply`, `reconcile -y`, `service recreate`, `resume`, `shutdown
--reason ups|maintenance`, `service shed/unshed`, `schedule sync [--to
--manifest]`. **Automated reads**: `status`, `service list`, `export`,
`schedule list`, `history`, `logs`, `updates`, `verify [--smoke]`, `stack
hostnames` (both sudo variants), `dashboard generate`. **On-NAS local**:
exactly two of 30 cron jobs invoke a platform binary at all —
`nas-heartbeat` (`verify --smoke`, exit code only) and `syrviscore-backup`
(`syrvisctl backup create -o`, an argv shape the registry cannot even
express, so it is unreachable over the seam).

The tails: **nine registry rows with zero usage anywhere** (`stop`,
`stack_disable`, `service_task`\*, `service_update`, `service_declare`,
`service_adopt`, `vm_start/stop/restart`; \*`service_task` was invoked once
in its lifetime, by an incident-day human), twelve doc-only single-mention
rows, and thirteen doc-heavy-never-scripted rows — including
`service rollback`, which `deploy-stack` *computes and prints* as a
ready-made command line but has never executed. The most-discussed verb in
the repo, `schedule sync` (228 mentions), has zero scripted callers;
nothing reconciles the jobs pin.

**The argv shape itself is a defect class.** The sudo/no-sudo split is not
derivable from the verb name (`export` is a sudo read; `stack hostnames`
exists in both forms with different answers), and it has produced a live
silent bug: `verify-all` calls `vm list --json` without sudo against a
sudo-only registry row, so the shim can never match it and the check has
been a permanent, mis-narrated UNKNOWN. The two seam commands on the agent
permission allowlist both spell a *read* with `sudo -n` — contradicting the
repo's own documentation — and measurement shows they **never fired once**:
across 4,338 mined shell invocations, "nobody spells it the same way
twice." A wire format that is a shell string cannot be permission-matched,
cannot carry capability class, and deforms the schema it transports (the
cron spec travels as file content because `*` and `,` cannot pass the shim
charset).

### 10.3 The client-side orchestration burden

Nothing asks the platform for a plan; every serious caller re-implements
the same loop — read state, subtract shed/disabled, order by a local
heuristic, iterate serially, stop on first failure, point at history.

- **`deploy-stack`** assembles `syrvis-bundle/v1` entirely on the laptop
  (manifest + config sources + sops-decrypted values), hand-copies the
  platform's 64 KiB config cap so dry-runs fail early (that cap has forced
  one alerting-rules file to split five times and leaves the generated
  127 KB overview board permanently undeployable), streams one bundle per
  service serially in declaration order, and carries `ALERTING_PAIR_LAST` —
  a hardcoded stable-partition forcing the alerting pair to deploy last,
  born from a 32-minute undelivered-alerts window. There is **no change
  detection**: every `--apply` streams every non-staged service in full;
  scoping is a hand-typed `--only`. After the loop, a second round trip to
  `history --json` exists purely to print revisions and synthesize rollback
  hints.
- **`apply-instance`** reads `export --json` *before every write* solely to
  overlay live `enabled: false` back onto git's replace-set — the client
  re-implementing intent preservation because pushing the set twice
  resurrected the 14-service load-shed. Then converge is a two-step
  (`stack apply`, `reconcile -y`) the client sequences itself.
- **`recreate-located`** re-invents a plan: read `service list`, union the
  shed set from *two* sources (a laptop YAML and the NAS's `intent.json`),
  order stores-first **by container-name suffix** (`-db`, `-redis`,
  `-valkey`…), loop serially with a hand-copied timeout budget. A
  dependency graph, written as a string heuristic, in a repair script.
- **`verify-all`** (9,733 lines, 83 checks) hand-codes seam budgeting
  (three memoized reads serving ten checks), and classifies "I could not
  answer" with **two regexes over summary prose**, kept honest by a
  build-breaking meta-check — because the platform has no typed way to say
  *unreachable*. Eleven seam-bound checks go blind together and the tally
  scores INFO as zero.
- **`accept`** fuses three planes (dashboard HTTPS for `restart_count`, a
  VictoriaMetrics PromQL fallback added the evening OIDC broke the first
  workaround, a reader-identity `find` for zero-byte secrets) plus a
  wall-clock hold — written only after four bespoke acceptance lists in one
  week.
- **Runbooks** carry the rest: a five-wave, 14-service recreate table
  executed one SSH round trip at a time; the G0–G6 post-boot ladder; the
  R0–R4 seam-dead ladder; per-name loops unrolled by hand because
  `service stop` takes one name. Ten interlock rows and an autonomy matrix
  are enforced by *a model reading Markdown* — `forbidden_verbs[]` is read
  by three consumers and enforced by none.

Six scripts carry duplicated copies of the ssh-runner/sops/JSON-hardening
plumbing with divergent timeouts (30/45/60/300/600 s, some unbounded);
three independently strip the same OpenSSH banner.

### 10.4 The parallel root plane

The 30-job cron plane is the sharpest verdict on the v1 CLI: **28 of 30
jobs bypass it on principle.** 26 parse `$SYRVIS_HOME` state as text (a
regex over `services.d/*.yaml`, a `grep` over `intent.json`) and 23 shell
straight to `/usr/local/bin/docker` — with the doctrine stated in a job
header: *"Not `syrvis service list --json`: this job runs as root from cron
INSIDE the box and must keep working when the CLI cannot (a half-installed
version, a `current` symlink that does not resolve — the 2026-08-16
shape)."* Thirteen jobs carry a byte-identical copied `vm_push` block that
`docker exec`s into the metrics container because no push verb exists;
`seam-selfheal` rewrites `/etc/passwd` every five minutes to keep the seam
itself alive; `hourly-reconcile` is a declaration with **no script**,
disabled, because an hourly reconcile before the shed overlay existed would
have been an hourly resurrection. The availability reasoning behind all of
this is *correct* — which is exactly the case for a resident agent with a
local, CLI-independent read/emit surface.

### 10.5 Under fire

The 2026-08-16 timeline tallies **3 mutating seam calls, ~13 reads, and 9
break-glass shell commands — and the repair itself was 100% break-glass**
(four `mv`s the seam cannot express). The owner found root cause with a
plain `ls` before any tooling did. The single read that broke the second
wave open was `history --json`'s per-revision `location` field. The seam's
most-cited missing capability is now literally specified in a runbook: R2's
"one reader round trip" (volume-root census + `current` target + seam-account
shells) *is an API call written as prose*. And 2026-08-14 proved the
transport shares fate with its subject twice over: a wedged dockerd hung
**every** verb including reads (the CLI builds a Docker client at startup
regardless), and the client's defensive `timeout` wrapper around a deploy
is what stopped the monitoring plane.

### 10.6 The negative space, consolidated

What the consumer builds *around*, each a missing platform capability:

| Missing | The workaround it forced |
|---|---|
| Filesystem/census reads | the `syrvis-reader` identity; R2/R4 ladders; break-glass `ls`/`find`/`du`; `appenv-metrics` |
| Truthful per-service status (`restart_count`, health, flapping, `blocked_by`) | dashboard HTTP detour → VM PromQL detour; trap T13 ("status is blind to crash loops") |
| Server-side plan/ordering | `ALERTING_PAIR_LAST`, `_STORE_SUFFIXES`, "declaration order = deploy order", five-wave prose tables |
| Change detection | full-fleet redeploys; hand-typed `--only`; "a deploy of unchanged bits is a no-op" as doctrine instead of mechanism |
| Effective-intent read (after all overlays) | `apply-instance`'s `export` pre-read; `recreate-located`'s two-source shed union |
| Batched/typed reads with unreachable semantics | three memo caches; regex UNKNOWN classification; INFO-scored-as-zero blindness |
| Metrics/telemetry push | 13 copied `docker exec` blocks; SKIPPED window metrics; liveness moved off-box |
| Job supervision (`job-wrap`) | five hand-rolled `last-run.log`s; two metrics jobs dead invisibly for 24 h; one dark its entire life |
| Config prune | seven retired Grafana boards serving from disk; an owner hand-delete as the documented remedy |
| Share/user/data-plane verbs | 74 registry files (`shares.d`, `data.d`, `backup-tasks.d`, `volumes.yaml`) the platform never sees; hand-runbook share creation — the process that armed the collision landmine |
| A schedulable, shed-aware reconcile | `hourly-reconcile` declared but scriptless; "the homebase's self-healing story is 'someone notices'" |
| Seam self-assertion | `seam-selfheal` cron; three duplicated `/etc/passwd` heals |
| Cross-channel confirmation | `schedule sync` token-gated via MCP, ungated via the seam spelling the docs teach |

The consumer-side deletion list this implies is the true measure of §12: a
v2 that absorbs these concerns retires two scripts outright, guts four
more, deletes an SSH identity, and converts a 62-runbook prose-orchestration
corpus into agent refusals that name themselves.

---

## 11. The v2 mandate

After reviewing Part I, the owner ruled (2026-08-18):

> *"While it does seem to work, it seems very fragile and assembled by a
> fleet of scripts. It served as an MVP — now we need a very robust,
> modern, well designed Go implementation that companies would consider
> rolling out in production."*

That verdict is fair, and Part I's evidence is its bill of particulars: the
boot chain is a relay of shell hand-offs (S99 → trampoline → startup script
→ cron heals) that died as a unit on 2026-08-16; liveness of the privileged
boundary depends on a five-minute cron rewriting `/etc/passwd`; the
consumer compensates for the platform with ~10k lines of laptop Python and
62 runbooks; and the state of the system is spread across six JSON files,
prose conventions, and three disagreeing channels. v1 validated the
*doctrine* — declarative intent, the trust boundaries, refuse-don't-guess,
durable intent, alarm hygiene — while proving the *assembly* wrong.

The mandate, as requirements on v2:

1. **Production quality bar.** One well-designed Go agent (`syrvisd`) that a
   company would deploy: supervised, observable, transactional state, typed
   API, CI-built artifacts, no script chains in the critical path.
2. **Declarative deployments, stored on the Synology, converged by the
   agent.** The declaration set on the NAS *is* the intent; SyrvisCore's job
   is matching running state to it, continuously (the level-triggered loop),
   not executing imperative verb sequences.
3. **Home Kit owns the configs and ships them through an established
   production path.** "Home Kit" is the deployment-repo role home-tech plays
   today: it maintains the declarative deployment configs in git and
   *deploys the configs* — a push of desired state through a normal
   CI/deploy pipeline (plan → review → apply, exactly §12.3's shape) — after
   which the agent owns everything that happens on the device. The division
   of authority from §3.1 survives intact; what changes is that the
   transport becomes a real deployment path instead of a fleet of
   hand-invoked laptop scripts.
4. **MCP is a client, not the product.** The typed tools on the operator's
   laptop are one convenient interface to the agent — generated from the
   agent's own capability schema (§12.5) alongside the CLI and dashboard,
   never a parallel surface with its own guards.
5. **DSM-native install; volume-decoupled placement** (§13): more of the
   lifecycle through Package Center, an unmistakable answer to "which volume
   am I on," all volumes presented as placement options with sane defaults
   from initial configuration.

---

## 12. syrvisd — device lifecycle manager and deployment orchestrator

This section is the centerpiece of the v2 design: one agent that owns the
device lifecycle (drains, partial drains, boot, heals) and the deployment
plane (server-side orchestration, velocity control), exposing one rich,
truthful status surface. It extends §8's architecture with the lifecycle
and orchestration semantics §10's evidence demands.

### 12.1 The lifecycle model: one suppression mechanism, scoped

v1 holds a service down three ways — declared `enabled: false`, the
ephemeral live-only flag `service stop` writes, and the durable `shed` row —
and its own comment reads "Three mechanisms, one field, cancelling out."
v2 has **one: the drain.** A drain is a durable, scoped, reasoned record in
the agent's state store, never in the declaration set. `enabled:` stays in
the declaration and means only "part of the declared fleet." `stop` is
sugar for a service-scoped drain with `reason: operator`; a maintenance
halt is a device-scoped drain; `shed` is the service-scoped degenerate
case. "Drained" and "partial drain" are the same object at different
scopes:

```json
{"id": "drn-20260816T0507-mdresync",
 "scope": {"kind": "volume", "selector": "/volume5"},
 "class": "hardware",
 "reason": "md resync on volume5 array",
 "by": "seam:kevin", "since": "…", "until": "2026-08-24T00:00:00Z",
 "state": "drained",
 "policy": {"dependants": "refuse", "deploys": "land",
            "floor": "default", "on_expiry": "hold", "on_boot": "hold"},
 "seq": 4417}
```

**Scopes, each grounded in a real event:**

| Scope | Selector | Membership | The event that demands it |
|---|---|---|---|
| `device` | — | everything but the floor | UPS (NUT), maintenance windows, DIMM isolation, migrations |
| `volume` | `/volume5` | every service whose *effective* storage touches it (`location:`, `volume_locations:`, `fileplane:`, bind sources) | the md-resync 14-service shed; the NVMe flips |
| `plane` | `monitoring`, `container`, `routing` | plane-labeled + derived membership | the 2026-08-14 container-plane outage; monitoring redeploys |
| `set` | `stack:onyx`, label selector | stack/label membership | onyx/immich restores, quiesce windows |
| `service` | one name | one | today's shed/stop |

**Membership resolves at evaluation time, never at declaration time** — the
rule every runbook already states as "resolve the list at execution."
Volume-scope resolution is precisely the read that cost a break-glass
session on 2026-08-16 (hand-deriving 11–17 names from `service list` plus a
grep over `location:` fields).

**Composition.** Drains are a set; a member is drained if *any* active
drain covers it; release is per-drain, so lifting the maintenance window
does not lift the hardware drain beneath it. Effective intent is a total
lattice evaluated every tick: rescue mode → device drain → scope drains →
declared `enabled: false` → breaker-open (suppresses starts, never stops) →
`blocked` (hard edge onto a drained/disabled target — a bucket, never a
failure) → should-run. The safety rule is one sentence: **a desired-state
push can change declarations; it can never change drains.** Bundles carry
declarations; drains are written only by intent methods. That single
property deletes `apply-instance`'s export-overlay, `deploy-stack`'s
staged-skip, and the client half of `guard_enable_change` — the resurrection
class becomes unrepresentable rather than guarded.

**Durability.** Drains live in the agent's `state.db` *and* a rootfs mirror
(`/usr/local/etc/syrvisd/intent.json`, monotonic `seq`) — the 2026-08-16
lesson (everything volume-resident died in one rename; only rootfs
artifacts survived) as a first-class artifact. Boot: agent starts from the
rootfs binary, reads the mirror, **refuses to converge until the declared
roots are proven present and genuine** (absent means refuse-and-page,
never create — the rule whose absence produced wave two of the incident),
then converges to intent in both directions: in-service brings the fleet
up; drained converges crash-resurrected strays *down*. Mirror/DB
disagreement resolves by newest `seq` and is an event, never a silent pick.

**Expiry.** `until` is a review deadline, not a timer: `class: hardware`
and `power` default `on_expiry: hold` (an auto-restore at 03:00 onto a
still-degraded array is the resurrection with a clock), page once, and
surface as an open decision. `maintenance`/`operator` drains may opt into
auto-restore. Drains are garbage-collected honestly — v1's shed rows are
never GC'd and a row for a deleted service blocks dependants forever.

### 12.2 Drain semantics

**Declaring a drain** computes and returns a plan (membership, order,
budget, conflicts) and executes it as a server-side job: declared pre-stop
gates run first (`drain_gate:` on the manifest — the `immich-quiesced`
laptop script becomes a journaled agent probe); stop proceeds in
**reverse-topological waves** over `depends_on` with the shutdown band as
intra-wave tie-breaker (derived, not parallel — retiring all three v1
ordering hacks at once); budgets are reserve-first with stores never
clamped, and a device drain refuses to start when the reserve exceeds the
*measured* transport window (v1 ships 240 s of reserve against an
unverified 150 s wrapper; the agent measures its window once at install and
stores the number).

**Dependants outside the scope** are policy, defaulted per edge kind:
`refuse` (hard `healthy` edges — the plan names the outside dependants and
the flag that proceeds), `cascade` (pull them in, reported before
execution), `strand` (`soft` edges — they show as `blocked` with
`blocked_by`, a state, not an alarm).

**The observability floor** is the declared set that must survive every
non-device drain: the agent itself, the socket proxy, the exporters, the
metrics/alerting chain (today's thirteen hard-coded collector names in a
laptop check, moved into the device and marked `floor: true`). A scope
drain never stops a floor member; if one is *in* the resolved scope
(victoria-metrics homed on a resyncing volume is the live case) the agent
refuses and names the conflict — `--accept-blind` proceeds, records a
**declared blind window** on the drain, and pages once. A device drain
stops the floor last, after emitting a terminal event out-of-band (ntfy,
the external dead-man, the rootfs flight recorder): the alarm must not
live inside the thing it alarms about.

**Rehydration.** Release writes intent and starts nothing — v1's `unshed`
got this right. The convergence loop plans the restore: forward-topological,
readiness-gated (`started`/`healthy`/`soft`), breaker-consulted, journaled,
with the plan readable before it runs. The 14-service window close — today
a read, fourteen unrolled unshed calls, an acceptance run, and fourteen
recreates in five hand-authored waves — becomes `Drain.Release(id)` +
`Watch(job)`.

**Drains × deploys.** Generalizing v1's shed+deploy rule: **a deploy into a
drained scope lands bits and does not start** — the revision records
`{applied: true, started: false, held_by: <drain-id>}`, and held revisions
are exactly the set the restore wave brings up. Restart-vs-recreate is
decided by spec-hash comparison (Docker bakes env at create; a changed
env digest means recreate), which deletes `service recreate` as a verb and
`recreate-located` as a script, and makes G1 ("a no-op apply restarts zero
containers") a property of content addressing rather than a bolted-on
digest comparison.

### 12.3 The deployment orchestrator

Today the client owns discovery, assembly, ordering, caps, staging
filters, serial execution, abort semantics, and the follow-up reads — and
has **no change detection at all**. v2 moves orchestration server-side
behind one write pair, `Deploy.Plan` / `Deploy.Apply`, over one
content-addressed bundle (`syrvis-bundle/v2`: declarations + config items
with digests/modes + secrets + env, `target ∈ {instance, stack, service}`,
a prune flag, a rollout policy). Home Kit's deploy pipeline becomes: build
the bundle from git + sops → `Plan` → review the typed diff → `Apply` →
`Watch`. This *is* the "established production path" of §11.3 — plan,
review, apply, observe — with the repo remaining the source of truth and
the device holding the authoritative declaration set it converges to.

- **Change detection is server-side.** The agent computes a spec digest per
  service (declaration ⊕ image ⊕ config digests ⊕ secret digests ⊕ volume
  bindings) and the plan returns `new | unchanged | declaration-changed |
  config-changed | secret-changed | image-changed | removed` — giving
  `--only` a computed answer, making resume trivial, and ending the
  "I redeployed it" non-repair.
- **Plan renders and validates everything**, including secrets (in memory,
  reporting digests only) — v1's dry-run omits secret rendering, so
  failures surface only on the live path. Validated at plan time: size
  against the agent-*published* limit (killing the hand-copied 64 KiB
  constant), `dest` against the service's declared mounts (nothing checks
  this today — "a mismatch deploys green and serves nothing"), file mode
  against the container uid (the 0600-root-vs-`USER nobody` crash-loop
  trap), the prune set (config delivery finally *converges* — seven retired
  dashboards currently serve from disk because a bundle only writes), and
  graph validity with per-file isolation.
- **Execution is a server-side job with an id.** Client disconnect is not
  abort — the direct fix for 2026-08-14, where a `timeout`-wrapped client
  killed mid-`up` left the monitoring plane stopped and unrecoverable by
  restart policy. Waves are topological and bounded (`max_parallel`);
  health gating is per declared readiness, so the journal can finally
  record `healthy` as an observed transition. The breaker **engine** (the
  half v1 wrote but never wired) runs here: the capped jittered curve,
  cross-plane suppression, close-closes-all, `by` as a field, page-once on
  the open transition. Rollout policy per apply: `halt` (instance default),
  `continue-independent` (stack default — the 08-14 casualties were
  unrelated subtrees), per-service or per-stack rollback, and named
  canaries.
- **Rollback** re-applies a stored revision (content-addressed, so no
  client repo required) through the same wave machinery; retention never
  expires a live rollback target; the response names the git-side revert
  because rollback remains GitOps-ephemeral.
- **The round trip collapses**: the terminal job result carries per-service
  outcome, revision, image transition, digests, prune list, wave, elapsed,
  breaker state, and an annotation-ready record — deleting the follow-up
  `history` read and giving the disarmed Grafana-annotation lane a
  credential-free path.

### 12.4 Rich status: answering "what is true right now" in one read

**`Status.Get`** returns one document, one timestamp, covering every rung
of the degraded-ops ladder and every post-boot gate: agent
(version/mode/uptime), device (phase, intent, intent source), host (roots
**with collision siblings**, `current` resolution, array state,
per-volume mount/capacity, boot-hook currency, seam-account shells),
active drains (with member counts, held revisions, expiry state), planes
(floor integrity, declared blind windows, dockerd responsiveness),
services, breakers, jobs, and a `blind[]` list of unanswered facts.

**Every service row carries the full explanation tuple** — declared spec +
digest, effective intent, a closed-enum reason (`running | starting |
drained | disabled | blocked | breaker-open | terminal | crash-loop |
image-missing | secret-empty | volume-unmounted | unmanaged | unknown`)
with the drain id or blocking edge attached, observed state including
`restart_count`, health, `started_at`, running spec digest and drift
class, pending held revisions, and secrets facts (`bytes: 0` reported
directly — retiring the reader-identity `find` and the `appenv-metrics`
job). "Why is X not running" is one read, not a ladder.

**Three protocol rules close v1's structural blind spots:**

1. **Every fact carries `{answered, observed_at, unreachable_reason}`.**
   The typed replacement for regex-over-prose UNKNOWN classification and
   INFO-scored-as-zero blindness; an aggregator *cannot* silently score an
   unanswered fact as healthy.
2. **Reads survive the failure they diagnose.** Status/events are served
   from the agent's in-memory model and rootfs mirror with hard per-fact
   deadlines; the Docker probe is bounded, cached, and partial (one sick
   container must never blind the sensor); docker-unreachable yields
   unanswered container facts and full answers on everything else;
   `denied`, `hung`, and `degraded` are distinguishable returns.
3. **`Events.Watch`** is an append-only, cursor-paged, transition-only
   stream with protocol-level dedup keys (digit-masked — a countdown is not
   a new problem, 3→30 is), a `gap: true` signal when a cursor falls off
   the ring (v1 has no way to know it missed something), severity that
   never renders drained as unhealthy, and **trust labels on untrusted
   payloads** (container logs, image names, alert bodies) so an
   agent-holding LLM session gets its injection fencing from the protocol,
   not from an unbuilt script. The last N events mirror to a rootfs flight
   recorder readable in rescue mode.

### 12.5 The API

Transport: length-framed JSON over a `root:operator` 0660 UNIX socket; the
remote seam is `syrvisd seam-proxy` as the SSH forced command — **no argv
on the wire**, generalizing the one pattern v1 got structurally right (the
stdin bundles). This retires, by construction: the shim charset allowlist
and its 18 slot kinds, the sudoers-glob/shim mismatch, the sudo/no-sudo
argv split and its silent-UNKNOWN bug class, the three duplicated
`/etc/passwd` heals and the `seam-selfheal` cron (a forced command that is
the agent's own subcommand needs no login shell), and the auto-seam-sync
fail-open-on-width hole (there is no generated policy pair to drift).

Every method declares a queryable **capability class** (`read | converge |
destructive | rescue`), so the CLI tree, the MCP tool set, the dashboard
client, the permission allowlist, and the drift check are all *generated*
from `Meta.Methods` — the only version of the permission plane that
survives the next "always allow" click.

| Method | Class | Replaces |
|---|---|---|
| `Status.Get{sections}` | read | `status`, `service list`, `export`, `verify`, `schedule list`, `updates`, `vm list`, `stack hostnames`, the memo caches, the dashboard detour |
| `Events.Watch{from_seq}` | read | `history` polling, `logs`-as-monitoring, client dedup state |
| `Census.Get{}` | read (**rescue-capable**) | R2/R4's break-glass blocks: roots + collision siblings, `current`, shells, mdstat, df — answerable with no resolvable home |
| `Fs.Stat / FindEmptySecrets / Digest` | read | the `syrvis-reader` identity, scoped to declared roots |
| `Meta.Methods / Meta.Limits` | read | the hand-copied 64 KiB constant; the hand-curated permission files |
| `Declarations.Get / Validate` | read | `export` pre-reads; effective-intent recomputation |
| `Deploy.Plan / Apply / Watch / Revisions / Rollback`, `Job.List / Cancel` | read/converge/destructive | `deploy`, `apply`, `stack apply`, `reconcile`, `secret set`+`config set` (as bundle items), `service update/add/run/declare/adopt/set-image`, the client loop |
| `Drain.Declare / Amend / Release / List` | converge/read | `shutdown`, `resume`, `service stop/start`, `shed/unshed`, `restart --graceful`, `maintenance-state.yaml` |
| `Service.Converge{name}` | converge | `service recreate`/`restart` (spec-hash decides) |
| `Config.SetSecret / SetJobConf` | converge | the standalone stdin writers |
| `Schedule.Plan / Sync / Run / Logs`, `Dsm.Tasks` | read/destructive/converge | `schedule *` — with `Sync` destructive on *every* channel, closing the MCP/seam guard asymmetry |
| `Vm.List / Power` | read/converge | the vm family (ends the designed-in standing UNKNOWN) |
| `Attest.Accept{since}` | read | `scripts/accept`'s three-plane fusion |
| `Version.Activate`, `Backup.*` | destructive | the `syrvisctl` family (version pin as intent; download-verify-symlink as convergence) |
| `Emit{samples}` | converge (local) | 13 copied `vm_push` blocks; the missing metrics verb; outward heartbeat |
| `Confirm{method,args}` | — | the MCP-only HMAC handshake, moved into the device so every channel inherits it; tokens bound to a plan digest |
| `Rescue` *(binary mode)* | rescue | `syrvisctl doctor` made permanent — and the end of the two-doctors name collision |

Roughly fourteen method families cover everything §10 found in use.
Refusals are structured and keep naming names —
`{"guard": "bulk_degraded", "blocking": [...], "override": "force",
"recovery": "…"}` — because both appliers already treat the verbatim
refusal list as the whole value of the guard. Reversible power/drain
methods stay deliberately token-free (the NUT hook and unattended
degradation responses must not need a human); destructive methods take the
`Confirm` handshake on every channel. The break-glass floor survives for
*writes* (arbitrary exec, `mv`/`rsync`, restores, provisioning) while its
*read* half — the majority of actual break-glass sessions — migrates into
`Census`/`Attest`/`Fs.*`. And one thing is deliberately **not** absorbed:
`rootfs/boot-integrity` stays a hand-installed, agent-independent rootfs
gate, because a check that shares fate with the thing it checks is not a
check — whatever v2 self-heals, something outside v2 must still assert v2
is present, resolvable, and scheduled.

### 12.6 What Home Kit deletes

The consumer-side ledger, per file: `recreate-located` — deleted (`Drain` +
`Converge`). `apply-immich-secrets` — deleted (already superseded).
`gen-syrvis-dashboard` — deleted with the dashboard image and its lockstep
gate. `deploy-stack` — keeps repo→bundle assembly and sops decryption;
loses ordering hacks, the cap constant, history follow-ups, the timeout
zoo, the staged filter. `apply-instance` — loses the export overlay, the
runstate pre-check, and the two-step converge. `verify-all` — loses the
seam helper, three memo caches, the reader identity and its four probes,
the regex UNKNOWN classifiers and their meta-check, the metric-proxy conf
plane, and the dashboard/VM detours; `nas.vms` becomes a real check.
`accept` — collapses to `Attest` plus exit-code mapping. `monitor-tick` —
keeps the off-box blackout probe and dead-men (they must survive the
agent's death); loses liveness SKIPs and client dedup. `maintenance-mode` —
becomes `Drain.Declare`; the YAML inverts from source to generated mirror.
The jobs plane — loses `_vm-push.sh` and 13 copies, the YAML regex parse,
the intent grep, five `last-run.log` hacks, and `seam-selfheal`;
`hourly-reconcile` finally exists as the agent's own loop. The permission
plane — generated from `Meta.Methods`; the unbuilt `scripts/seam` is never
built.

### 12.7 Transition invariants

1. The Python side never writes the new state store — it calls the agent.
2. No verb migrates without its incident tests ported.
3. The rescue path (rootfs-resident, zero volume dependency, answering
   `Census`/`Status` with no resolvable home) ships before the agent owns
   anything.
4. Intent imports once, then the agent owns it: shed rows lower into
   service-scoped drains at first start; `maintenance-state.yaml` inverts
   to a generated mirror the same day.
5. While any non-service-scoped drain exists, platform rollback below the
   agent is forbidden — the reader-enumeration gate, applied to scopes.
6. Unknown schema ⇒ report unknown and refuse to act — the journal's rule
   becomes the whole protocol's rule.
7. CI builds the SPK and the binary from day one; `rootfs/boot-integrity`
   stays outside the agent forever.

---

## 13. v2 packaging and the volume model

*Grounded in fresh research against Synology's DSM 7 developer guide and
the packaging ecosystem (SynoCommunity spksrc, Tailscale's package),
cross-checked against the repo's own SPK scar tissue. Facts below are
marked where they remain uncertain; every uncertain fact has a defensive
design around it.*

### 13.1 What DSM 7 natively provides — and the one wall that doesn't move

The research reframes v1's install chain: much of it re-implemented things
Package Center already does, while the one thing it *couldn't* route
around is a real platform boundary.

**Native, verified, and unused by v1:**

- **The volume picker.** Package Center natively asks the user which volume
  to install on; `SYNOPKG_PKGDEST_VOL` carries the answer to every script,
  and `support_move="yes"` lets DSM relocate the package later. v1's custom
  `pkgwizard_volume` combobox re-implemented a built-in feature.
- **Dependency declarations.** `install_dep_packages="ContainerManager"` /
  `start_dep_services` replace the `preinst` Docker probe the package user
  was never allowed to make.
- **`usr-local-linker`** symlinks package binaries into `/usr/local/bin` on
  start — the sanctioned replacement for both the hand-made symlink Error
  276 blocked and the profile-sourcing dance.
- **The `data-share` resource worker** creates DSM shared folders for a
  package (skipping existing ones; shares deliberately survive uninstall).
  Caveat: it exposes no volume field, so it can anchor one share on the
  package volume but cannot express multi-volume placement — the daemon
  creates the rest.
- **Dynamic wizards.** `install_uifile.sh` runs at wizard time and emits
  the wizard JSON from live system state — real volume lists with real
  capacity, not hardcoded options. (v1 polished a wizard that was never
  even packaged.)
- **The upgrade contract.** `target` is *replaced* on upgrade;
  `var` → `@appdata`, `etc` → `/usr/syno/etc/packages`, and data shares
  survive. There is no native multi-version or rollback — that stays ours,
  but for one static binary it costs a directory and a rename, not a venv.
- **Real status vocabulary.** `start-stop-status` supports exit **150**
  ("broken — reinstall"), the honest answer when the managed root has
  vanished; v1 only ever used 0/1. An empty `SYNOPKG_PKG_STATUS` is DSM
  saying "this is boot" — a first-class signal v1 inferred from hook
  existence.
- **Self-hosted package feeds.** Package Center's third-party package
  sources speak a simple query protocol (`spkrepo` is the open reference);
  a static CI-generated feed behind Cloudflare buys native update
  notifications and auto-update channels (`stable`/`beta`). Manual `.spk`
  stays the DR/air-gap path.

**The wall:** `conf/privilege` accepts `run-as: root` and per-script
overrides *in syntax*, but **a package declaring root will not install
unless signed by Synology** — Error 276 seen from the other side, and it
has not moved. Tailscale's DSM 7 package is the reality check: it runs
unprivileged and documents a root Task Scheduler task as its own
workaround. The genuinely new finding is the escape hatch that is *not*
root: since DSM 7.0-40656, the privilege file's `tool` block can grant
**Linux file capabilities** to a named package binary
(`cap_dac_override`, `cap_net_admin`, `cap_net_raw`, `cap_chown` — enough
for the Docker socket, app-home ownership, and the macvlan shim without
uid 0). Second candidate: a package may ship **its own systemd units**
(`conf/systemd/pkg-*`), which DSM copies into the system — if a unit
without `User=` runs for an unsigned package, that settles privilege *and*
supervision (Restart=on-failure, watchdog) in one mechanism. Both are
marked uncertain pending one NAS experiment each; the design below works
at every outcome.

### 13.2 The one-package model

v2 ships **one SPK, one payload**: `bin/syrvisd` (static Go, ~25 MB, with
`syrvis` as an argv[0] symlink), the dashboard SPA under `ui/`, and nothing
else executable. `startable="yes"`; INFO is CI-generated from source (the
`version="0.1.21"` drift class dies as a build assertion);
`install_dep_packages` declares Container Manager; `os_min_ver="7.0-40656"`
floors at the capabilities feature.

**Privilege is a declared, probed, reported tier — never an assumption:**

| Tier | Obtained by | Covers |
|---|---|---|
| `tier-root` | shipped systemd unit (if the experiment passes) or one explicit `sudo syrvisd bootstrap` | everything: share creation, group mgmt, rootfs writes |
| `tier-cap` | `conf/privilege` `tool` capabilities | Docker API, app homes, macvlan, rc.d writes |
| `tier-user` | neither | the whole read plane, validation, `Census`, `Status` |

`syrvisd selftest --privileges` runs at every start; every API method
declares its required tier; a method that cannot run refuses with
`{have, need, recovery}` — refusal-with-recovery applied to the thing v1
papered over with self-elevation. Supervision: the shipped systemd unit
with `Restart=on-failure` + watchdog if available; else
`start-stop-status` + the rootfs rescue stub as the one dead-man — never
again a relay of shell heals.

**The delta, per v1 chain step:** SPK-as-bootstrapper → the SPK *is* the
product; `syrvisctl install` + GitHub wheels → Package Center + own feed;
venv-per-version → deleted (rollback = `@appdata/binaries/<ver>` +
`current` symlink: three retained binaries ≈ 75 MB against v1's 962 MB of
retained venvs, with a `schema_floor` refusal so a binary below the state
schema cannot activate); the profile snippet → `usr-local-linker`; the
wrapper with its baked path → a socket client; `syrvis setup` +
self-elevation → wizard seed + idempotent first-start convergence (the
same code path as `doctor --fix`); the S99→startup→cron-heal relay → a
six-line rootfs stub exec'ing the rescue binary; four boot-artifact
writers → one; the manager/service split → one binary, with the split's
real value preserved as the **rootfs rescue copy**
(`/usr/local/lib/syrviscore/syrvisd-rescue`) that answers `Census`/`Status`
with no resolvable home.

**Flows.** Install: add the package source (or drop the `.spk`), pick the
volume in DSM's native picker, answer a near-empty generated wizard
(§13.3), done — `postinst` does no privileged work (it seeds
`bootstrap.json` from the wizard env and tees per-script logs; Package
Center's error surface is a number, so per-script forensics are
non-negotiable), and first start converges. **No secrets ever transit the
wizard** — wizard values land in env vars and DSM logs; v1's tunnel-token
step is a recorded mistake. Upgrade: Package Center native; durable state
lives in `@appdata`/`etc`/shares, all preserved. Integrity: DSM 7 removed
third-party package signing, so CI publishes `SHA256SUMS` + a detached
signature and **the binary verifies itself at start** against an embedded
key, reporting `payload: verified|unverified`. CI gates the SPK from day
one, with the whole error taxonomy as assertions (uncompressed outer tar /
263, `scripts` a directory / 313, `start-stop-status` present / 261,
clean INFO, reproducible tar, and an install→upgrade→uninstall smoke run).

### 13.3 The volume model: placement as a first-class concept

**Identity.** `/volumeN` is Synology *metadata* — reassigned across
migrations and pool rebuilds, with DSM 7 having removed the supported
renumber path, and this box holding the local proof that even the *name*
can change under you. So the daemon builds a **volume census** (from
`/proc/mounts` + `statvfs` + `/dev/disk/by-uuid`; `synowebapi` enriches
health but is never load-bearing): per volume, filesystem **UUID** (the
identity), path (a label), fs type, mounted/ro, capacity, health, and a
declared **role** (`fast | bulk | cold`). Every managed root and app home
carries a self-describing marker (`.syrvis-volume.json` /
`.syrvis-app.json`: instance id, volume UUID, path-at-write), so discovery
is **by marker, not by path memory**.

**The collision-incident answer has four legs:** (1) nothing SyrvisCore
owns is ever again a bare directory at a volume root — managed roots are
**registered DSM shared folders**, so `synocheckshare` has a record and the
reclaim race that decapitated v1 cannot arise; (2) marker-driven discovery
finds even a renamed root and classifies it (`renumbered` → accept +
rewrite + loud event; `root-missing` / `root-collision` → halt, **exit 150
in Package Center**, every converge method refuses; `foreign root` →
never adopt); (3) `Census.Get` reports collision siblings from the rootfs
with no resolvable home; (4) v1's refuse-to-install-over-a-collision guard
survives verbatim, generalized to all converge paths. The acceptance gate
for the migration is literally the incident as a test: a boot with the
share deliberately renamed must produce `root-collision`, a red package
status, and refusals — nothing else.

**Placement policy.** Device defaults are seeded by the wizard and then
live as a plain declaration Home Kit owns:

```yaml
# declarations/placement.yaml
apps_root: syrvis-apps
volumes:
  e3b0c442-…: { role: fast, alias: nvme }
  9f2a1d77-…: { role: bulk, alias: array }
defaults:
  app_volume: vol:uuid:e3b0c442
  bulk_volume: vol:uuid:9f2a1d77
  class_map: { database: fast, media: bulk, logs: bulk }
guards: { min_free_pct: 10, refuse_on_degraded: true }
```

`location:` in a service declaration becomes a parsed union — `vol:fast` /
`vol:bulk` (by role, the preferred form), `vol:uuid:<prefix>` (by
identity), `vol:default`, with bare `/volumeN` accepted during migration,
resolved to a UUID, and reported `deprecated_placement`. Resolution
happens at **plan time**; an unresolvable reference is a `blocked` bucket
with named recovery, never a guess. Precedence is total and *reported with
its source* on every resolution: per-volume override → per-service
`location:` → service class → device default → refuse.

**"It should be very clear what volume we are on":** one census feeds
every surface. `Volumes.List` is the API; the CLI renders it as a table,
the dashboard as a picker with capacity bars and health chips, the install
wizard as generated options labeled `volume4 — btrfs — 12.1 TB free —
healthy` (independently derived from `/proc/mounts` since the daemon
doesn't exist yet at wizard time — so first start re-validates every
wizard answer against the real census and refuses an unknown volume rather
than defaulting silently). `Status.Get{placement}` answers per service and
per named volume: declared form, resolved path, volume identity, and
*which rule* placed it. v1's validations survive intact — containment,
mounted-volume checks on every start path, immutability-while-populated,
purge coverage — plus capacity/health prechecks, and `Placement.Move`
becomes a real agent job (drain → copy → digest-verify → repoint → verify
→ release) instead of a seven-step break-glass runbook.

**Storage layout** follows DSM's own convention (Container Manager:
visible `docker` share + hidden `@docker` state): **derived state in
`@appdata`** (`state.db`, retained binaries, the socket — DSM-preserved,
single-writer, no human authors it); **everything humans or Home Kit
author in a visible DSM share** (`SyrvisCore/declarations/…`) — rsync-able,
git-diffable, File-Station-visible, and selectable by Hyper Backup's share
picker. Because `@appdata` is invisible to backup, the daemon mirrors
`intent.json`, `revisions.json`, `status.json`, and a consistent
`state.db.bak` into the share's `state-export/` on every intent write.
That keeps design/53's backup-visibility lesson while **deleting its
hidden-share construction** — the anchor is an ordinary share and the
backup story is "select the share." The light-payload doctrine survives
rebudgeted: the package volume carries ~500 MB (alarmed at 80%) instead of
v1's ~2 GB target.

### 13.4 Migration from v1, condensed

(1) Pre-flight on 0.5.17: `export --json` into Home Kit, a manager backup,
a collision census. (2) **Register the roots as DSM shares first** — this
single step ends the reclaim race and is worth doing even if v2 slips.
(3) Install the v2 SPK via Package Center; wizard seeded with the existing
volumes and apps-root. (4) `syrvisd import --from-v1` (dry-run, then
apply): manifest, declarations, `.env`, jobs, intent (shed rows lower into
service-scoped drains), journal, breakers, revisions. (5) The daemon
*proposes* the placement rewrite (`/volumeN` literals → `vol:uuid:` +
roles); **Home Kit commits the diff** — declarations stay repo-owned.
(6) Cutover under a maintenance drain; running containers are adopted by
label and compared by spec-hash, so byte-identical services are not
recreated. (7) Retire the old plane: enroll the key against
`syrvisd seam-proxy`, delete sudoers/shim/heals/S99-chain/profile/wrapper;
keep `rootfs/boot-integrity`. (8) Decommission v1 (the 962 MB of
`versions/` last, after the rescue path has answered across a reboot).
(9) The two-reboot acceptance gate: a slow-Docker cold boot (the daemon
must wait and converge, not fail) and the renamed-share boot (the incident,
as a test).

---

## Appendix A — Numbers

| Metric | Value |
|---|---|
| Commits / tags | 286 / 68 (last tag `v0.5.7`; 0.5.8–0.5.17 untagged behind the release gate) |
| Service package | 45 modules, ~27,600 lines; `service_manager.py` 3,651 |
| Seam registry | 77 commands, 18 slot kinds, 11 destructive; 69-line sudoers, 291-line shim |
| MCP | 50 tools; red team 23 candidates → 10 confirmed → all fixed |
| Tests | 1,507 gated (3.8, ~25s, 68% cov) + 423 MCP + 108 dashboard + **91 orphaned** = 2,129 written / 2,038 gated |
| Coverage lows | `doctor` 9%, `setup` 11%, `update` 11%, `privileged_ops` 43%, `validators` 46% |
| Live fleet | 39 services; 15 without healthchecks; graph depth 4, ~4 waves; 6 of 39 edge-free |
| Shutdown math | reserve 120+90+30 = 240s vs `timeout 150s` rc.d wrapper; defaults 180/90/30, floor 5, store band 90 |
| State stores | 6 (manifest v3, backup v1, runstate v1, intent v1, journal v1, breakers v1) |
| Data plane | 224 GB of 227 GB under the pre-split home was one Immich upload tree; `versions/` held 40 versions (962 MB) against keep-2 |
| Breaker doctrine | threshold 3, base 30s, cap 600s, ±20% jitter — "doctrine, not a tuning knob" |
| SPK error taxonomy | 263 / 261 / 313 / 276 |
| **Consumer surface (§10)** | 77 registry rows → ~20 executed argv shapes; 9 rows zero-use; 12 doc-only; `schedule sync` 228 mentions / 0 scripted callers |
| Consumer plumbing | 6 duplicated ssh/sops runners; timeouts 30/45/60/300/600s; `verify-all` 9,733 lines / 83 checks (20 seam-bound); 62 runbooks |
| The cron plane | 30 jobs, **2** invoke a platform binary; 26 parse state as text; 23 shell to docker; 13 copied `vm_push` blocks |
| Permission plane | 4,338 mined Bash calls, 4.7% auto-approved; 134 walks around the deny list; the 2 allowlisted seam rules fired **0** times |
| 2026-08-16 under fire | 3 mutating seam calls, ~13 reads, 9 break-glass commands; repair 100% break-glass (4 `mv`s) |

## Appendix B — The quote wall

- "The NAS **was** the integration test." — code audit, on 2025-12-26.
- "The DSM simulation simulates success, not DSM." — code audit.
- "Anything Claude can do, `ssh nas && syrvis …` can do." — v2 design.
- "The enumerated list **is** the security boundary, and it's auditable."
- "This makes the SPK's reliability a DR requirement, not a convenience."
- "Nothing alarmed, because the alarm was stored inside the thing it was
  supposed to alarm about." — the 2026-08-16 incident, in code.
- "Prose is not a guard." — the collision precheck.
- "A workload runs iff the device is `in-service` AND the service is
  `enabled` AND the service is not shed." — `intent.py`.
- "fourteen load-shed services could be resurrected, mid-array-resync, by a
  runbook whose own text said they would stay down."
- "an amber that never clears is an amber nobody reads." — the summary fold.
- "a consumer force-killed at 60s is an inconvenience, a postgres
  force-killed at 5s is a WAL replay or worse." — reserve-first clamping.
- "Absent means 'no run'; unparseable means 'I cannot tell'. Different
  verdicts." — the deploy journal.
- "a digest of a SHORT low-entropy secret is a confirmation oracle."
- "A parser that silently ignores rows it does not understand is a parser
  an attacker writes rows for." — the jobs manifest.
- "15 of 39 services have no healthcheck, so 'healthchecks absorb that' was
  a hope, not a mechanism." — the plan ordering key.
- "a location Docker has never populated" — 0.5.12, the fixture-shaped bug.
- "A liveness gate only goes red for states someone can fix." — 0.5.13.
- "the refusal and the bypass cost the same one line of evidence." — guards.
- "nothing on the box could answer 'what phase is this machine in, and what
  is actually true right now?' from outside the failure domain." — design/61.
- "AI is an accelerant, never a dependency." — the provisioning contract.

## Appendix C — home-tech design cross-reference

| Design | Subject | SyrvisCore artifact |
|---|---|---|
| 00 / 01 | Target architecture, desired state | the generic-platform boundary; `deployment.yaml` `tuning:` → `SYRVIS_APPS_ROOT_NAME` |
| 11 / 14 / 15 | Operator seam scope | the seam, MCP boundary, read checks |
| 13 | Scheduled jobs | `jobs.d` / `schedule` (job-wrapper `syrvis-run-job` still 0.6) |
| 18 / 45 | Data ownership, the file plane | `data.d`/`shares.d` consumption, `fileplane=` binds |
| 20 | Config management | GitOps apply; the config-render invariant; DSM task census |
| 21 / 22 | L2 deployment, privileged tier | `syrvis deploy` bundle; `tier: infra` + root-held grant |
| 25 / 28 | Lifecycle adoption, UPS | revisions/rollback/runstate; `shutdown --reason ups` |
| 26 / 37 | App location, volume placement | `location:`; `volume_locations:` |
| 51 | Two reconcilers | declare (home-tech) vs execute (platform) |
| 53 | Data-plane split | the light payload; `SYRVIS_APPS_ROOT_NAME`; collision guards |
| 55 / 57 | LAN posture, ops console | exposure-not-security; `syrvis-summary/v1` |
| 60 | Deploy orchestration | G1 digest compare; journal; breaker store; backoff doctrine |
| 61 | Host agent | unbuilt — the seed of §8's `syrvisd` |
| 63 | Dependencies & bring-up | `depends_on`; graph; wave ordering; reserve-first budgets |
| 64 | Placement & degraded mode | fast/bulk doctrine; `volume_locations:` as D7's enabler |
| 65 | Device lifecycle intent | `intent.json` `device` + `shed[]` (drain/restore pending) |
| 66 / 70 | Agent permissions, env trust | jobs pin + manifest; `.env` hazard guards (0.5.17) |
