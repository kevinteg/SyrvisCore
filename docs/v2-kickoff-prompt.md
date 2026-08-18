# SyrvisCore v2 (`syrvisd`) — implementation kickoff prompt

> Paste this into a fresh Claude Code session in `~/code/SyrvisCore` (or just
> say: *"read docs/v2-kickoff-prompt.md and execute it"*). Authored
> 2026-08-18 at the close of the v1 retrospective; v1 is at service 0.5.17,
> live in production on the NAS. The retrospective and all referenced
> research are committed in this repo.

---

Start the SyrvisCore v2 implementation project: replace the Python v1
platform with **one production-grade, static Go agent (`syrvisd`)** — a
device lifecycle manager and deployment orchestrator for the Synology NAS —
built via the strangler path so v1 keeps running production throughout.

## 1. Read first, in this order

1. **`docs/v1-retrospective.md`** — the whole document. Part I is the v1
   system and its judged gaps; Part II is the spec-shaping half:
   §10 (what the consumer actually uses), §11 (the owner's mandate),
   §12 (the syrvisd design: drains, orchestrator, status, the ~14-method
   API, transition invariants), §13 (DSM-native packaging + the volume
   model), §8.3 (the fourteen principles that bind any implementation),
   §9 (the strangler recommendation and its three hard gates).
2. **`docs/v2-research/README.md`**, then as needed:
   - `docs/v2-research/hometech-usage/lifecycle.md` — the fullest drains/
     orchestrator design draft (richer than the retro's condensation).
   - `docs/v2-research/hometech-usage/consolidation.md` — the verb→v2
     disposition map and residual imperative core.
   - `docs/v2-research/dsm-packaging/research.md` — DSM 7 packaging facts
     with VERIFIED/CORROBORATED/**UNCERTAIN** verdicts and citations.
   - `docs/v2-research/dsm-packaging/design.md` — the one-package + volume
     model draft (INFO/privilege/resource stanzas, wizard fields, layout,
     migration steps).
   - `docs/v2-research/v1-subsystem-maps/` — per-subsystem deep maps of v1
     when migrating a specific verb's behavior.
3. **home-tech doctrine designs** (in `~/code/home-tech/design/`): 61
   (host agent — placement, Go ruling, supervision stack, check registry),
   63 (dependency graph, `syrvis up`, waves, budgets), 65 (device intent,
   boot-converges-to-intent, shed), 60 (velocity: journal, breakers,
   backoff doctrine). v2 absorbs these; where the retro and a design
   disagree, the retro's Part II is newer — but flag the conflict rather
   than silently picking.
4. **`docs/seam-contract.md`** — the v1 contract that must keep working for
   home-tech during the entire strangler window.
5. Memory (auto-loaded): `syrviscore-v2-direction` carries the owner's
   mandate; `syrviscore-dev-test-topology` the v1 test envs.

## 2. The mandate (owner-ratified 2026-08-18 — binding)

- **Production quality bar**: "a very robust, modern, well designed Go
  implementation that companies would consider rolling out in production."
  v1 is the fragile MVP "assembled by a fleet of scripts"; v2 must have no
  script chains in any critical path.
- **Declarative deployments, stored ON the Synology, converged by the
  agent** — the declaration set on the NAS is the intent; the agent's
  level-triggered loop matches running state to it.
- **Home Kit** (the deployment-repo role home-tech plays today) maintains
  the deployment configs in git and ships them through an established
  production path: build bundle → `Deploy.Plan` → review → `Deploy.Apply`
  → `Watch`.
- **MCP is just one client** — generated from the agent's `Meta.Methods`
  capability schema, alongside the CLI and dashboard. Never a parallel
  surface with its own guards.
- **DSM-native install; volume-decoupled placement** — one SPK through
  Package Center; volume identity by UUID + role, never bare `/volumeN`;
  all volumes presented as options with sane defaults from initial config;
  "which volume is every byte on" answerable in one read.

## 3. The plan of record

**Strangler (retro §9 Option B)** with three hard gates:
(1) no verb migrates without its incident tests ported — the ~2,000 v1
tests are the design record; (2) CI builds the SPK and the binary from day
one; (3) the rescue path (rootfs-resident, zero volume dependency,
answering `Census.Get`/`Status.Get` with no resolvable home) ships before
the agent owns anything.

**Transition invariants (retro §12.7)**: Python never writes v2 state — it
calls the agent; intent imports once then the agent owns it
(`maintenance-state.yaml` inverts to a generated mirror the same day);
platform rollback below the agent is forbidden while any non-service-scoped
drain exists; unknown schema ⇒ report unknown and refuse to act;
`rootfs/boot-integrity` is never absorbed.

**Do not destabilize v1.** It is production. v2 work happens in new
directories and new CI jobs. (The four cheap v1 fixes — instance lock,
journal leak, backup layout, relic image workflow — are a separate,
optional v1 workstream; see retro §9.)

## 4. Phase 0 — settle the UNCERTAIN facts before the design freezes

Every fact below is marked UNCERTAIN in `dsm-packaging/research.md` or
named as unmeasured in the designs. **Do not build on any of them until
settled.** Produce two artifacts: an on-NAS experiment runbook (batched —
NAS access is production and break-glass pages the owner) and
`docs/v2-research/verdicts.md` recording every outcome.

**On-NAS experiments (batch into one or two owner-attended windows):**

1. **The systemd-unit experiment** — does a `conf/systemd/pkg-*.service`
   unit without `User=` run as root for an *unsigned* SPK? Does
   `Restart=on-failure` supervise? This single experiment settles both
   privilege and supervision (retro §13.1). Build a throwaway hello-world
   SPK for it.
2. **The capabilities experiment** — does the `conf/privilege` `tool`
   block actually apply file capabilities for an unsigned package on this
   DSM build, and which caps are accepted (`cap_dac_override`,
   `cap_net_admin`, `cap_net_raw`, `cap_chown`)? Can a cap_dac_override
   binary open `/var/run/docker.sock` and write `/usr/local/etc/rc.d/`?
3. **Wizard handoff on DSM 7.2.x** — do `pkgwizard_*` values reach
   `postinst` env with the v2 (Vue) wizard, and does `install_uifile.sh`
   dynamic generation work?
4. **`data-share` worker placement** — which volume does a declared share
   land on; `once: true` semantics; interaction with an existing share.
5. **Exit-150 behavior** — what Package Center renders for
   `start-stop-status` exit 150; whether it offers repair/reinstall.
6. **The rc.d stop window** (design/61 P18) — a timed test reboot
   measuring SIGTERM→SIGKILL spacing and whether the full stop case fits;
   this bounds every drain/shutdown budget claim (v1 ships 240s of reserve
   against an unverified 150s wrapper).
7. **DSM's Docker version** — what Engine API version Container Manager
   ships (pins the API version the agent negotiates), and whether
   `install_dep_packages` should say `Docker` or `ContainerManager` on
   this DSM line.
8. **Arch acceptance** — does one `x86_64`-family SPK install across the
   named platform families, or does the feed need per-family INFO?
9. **P14 canary harvest** — at the next DSM minor update, harvest the
   `/usr/local` survival canaries design/61 planted (verify they exist
   first); the rootfs mirror and rescue binary depend on this contract.

**Desk research (no NAS; fan out in parallel):**

10. **SQLite without CGO** — `CGO_ENABLED=0` is non-negotiable for the
    static binary; evaluate `modernc.org/sqlite` (pure Go) vs
    alternatives vs a log+snapshot file store. This is a deliberate
    deviation from design/61's stdlib-only rule — size the dependency,
    decide, and write the dependency policy (what may be vendored, what
    triggers rebuilds).
11. **Docker Engine API over the socket from stdlib** — the exact
    endpoint set needed (create/start/stop/inspect/events/pull with
    registry auth, exec for tasks), minimum API version pinning, digest
    handling, and the adoption-by-label + spec-hash scheme (retro §12.2).
12. **Wire protocol + codegen** — length-framed JSON vs JSON-RPC 2.0 vs
    gRPC/ConnectRPC, judged on: generated clients in Python (MCP), TS
    (dashboard), Go (CLI); streaming (`Events.Watch`, `Deploy.Watch`);
    proxying over an ssh forced-command stdin pipe; zero external runtime
    on the NAS side. Recommend one and prototype the schema→client
    generation for `Meta.Methods`.
13. **sd_notify/watchdog from stdlib** (`$NOTIFY_SOCKET` datagrams) — for
    the systemd path.
14. **Release signing** — minisign vs cosign for the detached signature +
    embedded-pubkey self-verification at start; toolchain pinning,
    `-trimpath`, reproducible builds, `govulncheck` in CI, and the
    named-rebuild-trigger rule from design/61.
15. **The package feed** — the exact Package Center source query
    (`arch/build/major/minor/micro/language/package_update_channel`) and
    response schema; a static CI-generated feed behind Cloudflare
    (spkrepo as the reference; a Worker if static won't do).
16. **A DSM integration lane** — v1's deepest testing gap was "the NAS
    was the integration test." Research: Virtual DSM inside VMM as a
    disposable integration target for SPK install/upgrade/boot tests;
    plus a plain docker-in-docker CI lane for engine semantics
    (recreate-rebakes-env, restart-rereads-binds — the G1 premises v1
    never verified, retro §6.10).
17. **Netlink for the macvlan shim** — `golang.org/x/sys` rtnetlink vs
    shelling to `ip` under `cap_net_admin`; decide against the dependency
    policy from item 10.
18. **Incident-test port inventory** — walk `tests/*.py`, tag every test
    by the incident/design clause it pins (the docstrings name them), and
    produce the Go table/golden-test port plan per verb — the gate-1
    artifact for every future migration.
19. **Prior art pass** — how k3s/Tailscale/Nomad-class single-binary
    agents structure: config+state layout, socket API auth
    (SO_PEERCRED), long-running job supervision, self-update. Steal
    shapes, not dependencies.

## 5. Repo mechanics (decide in the first session, flag disagreements)

- **Code home**: `packages/syrvisd/` in this repo (design/61 D1 ruled the
  agent lives in SyrvisCore; the retro's §12 agent *absorbs* the hostd
  role, so there is one binary — reconcile the `syrvis-hostd` vs `syrvisd`
  naming in favor of `syrvisd` and note the supersession).
- **Module/versioning**: own Go module; version `2.0.0-alpha.N` line,
  independent of v1's 0.5.x (which continues for v1 patches).
  Last-segment-only discipline once stable.
- **CI from day one** (gate 2): `go build` (linux/amd64 static,
  `-trimpath`), `go test` + golden files, `govulncheck`, SPK build +
  `validate-spk` with the whole 263/261/313/276 error taxonomy as
  assertions, and the INFO-version-equals-source check.
- **Branching**: `v2/*` branches; v1 release process untouched.

## 6. First-session deliverables (no NAS access required)

1. Repo mechanics decided and committed (directory, module, CI skeleton
   green on an empty-but-real package).
2. **`docs/v2/design.md`** — distill retro §12–§13 + the two research
   drafts into the single buildable spec, with the **API schema as a
   committed artifact** (the source `Meta.Methods` generates from). Where
   the drafts disagree with the retro, resolve explicitly.
3. **`docs/v2/phase0-experiments.md`** — the on-NAS runbook (items 1–9),
   batched for one owner window, each with its throwaway artifact,
   expected outcomes, and the design decision each outcome selects.
4. Desk research (items 10–19) launched as a fan-out; verdicts recorded in
   `docs/v2-research/verdicts.md`.
5. **Skeleton that earns its CI**: `syrvisd` with `version`, `rescue`, and
   `Census.Get` implemented against a fake volume tree (collision
   siblings, dangling `current`, marker files), with golden tests ported
   from `tests/test_home_collision.py` / `test_wrapper_fallback.py` — the
   rescue path first, per gate 3.

**Hard rules for the session**: do not touch the NAS without the owner
(experiments are batched, attended, and reversible); do not modify v1
behavior; keep every kept-principle from retro §8.3 (refuse-don't-guess,
strict-on-write, per-file isolation, secrets never on argv, alarms outside
the failure domain, prose is not a guard — gates go in CI).
