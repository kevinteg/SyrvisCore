# Design: Next-Iteration Service Declaration & Flow

**Status:** Proposed · **Audience:** SyrvisCore core · **Date:** 2026-07-11
**Companion:** [wiki/05 Layer 2 Services](wiki/05-layer2-services.md), [wiki/07 Schema Reference](wiki/07-service-schema-reference.md), [home-tech-provisioning-requirement.md](home-tech-provisioning-requirement.md)

We just shipped the exposure model and ran our first Layer 2 service end to end (cyberquill). This
doc proposes the next iteration of the service declaration + flow, with one north star:

> **Adding a new service should be trivial, honest about what it can't do, and safe by default.**

It builds on the concrete friction the deep review surfaced. Where a fix already landed it is marked
✅; the rest is the forward design.

---

## 1. Where we are — the current friction

The two declaration paths (`service add <git-url>` and `service run --image`) both produce the same
on-disk artifact and run each service as its own isolated compose project. That model is sound. The
friction is in four places:

1. **The schema can't express what real services need.** No `healthcheck` (so the dashboard only
   knows "running", not "ready"), no `env_file`/secret injection (secrets live as plaintext in the
   manifest), no resource limits, no second port, and no non-HTTP exposure. Anything absent from the
   allowlist is simply impossible to declare.
2. **The two declaration models overlap with no guidance.** For the common single-container case,
   image-first is strictly simpler and safer (no arbitrary git clone as root); the git model's real
   extra value (config templates, middlewares) is niche, yet nothing tells a user which to reach for.
3. **Failures are opaque.** A bad image tag or unreachable registry fails at `docker up` time and the
   whole install rolls back with a single raw stderr line — the most common real failure (a typo'd
   GHCR tag) is the worst-served.
4. **Silent footguns, now fixed.** ✅ Subdomain collisions are rejected at add time; ✅ an unrouted
   service now says so; ✅ the success message points at `syrvis stack hostnames`; ✅ `depends_on` is
   rejected with a clear "unsupported" message rather than silently no-op'ing.

---

## 2. Design goals

- **Additive and still audited.** Every new field passes through the same strict trust boundary;
  unknown keys stay rejected. New expressiveness must not widen the attack surface.
- **Image-first is the blessed path.** Optimize the 90% case (one published image) to a single
  command; keep the git-repo path for services that genuinely ship templates.
- **Fail early with actionable errors**, not at container-run time.
- **Converge sets, not just items** — support declaring the *whole* L2 set and reconciling to it
  (the seam home-tech needs; see the [provisioning requirement](home-tech-provisioning-requirement.md)).

---

## 3. The v2 schema (additive)

Extend `ALLOWED_TOP_LEVEL_KEYS` with strictly sub-validated fields:

```yaml
name: my-service
version: "1.2.3"
image: ghcr.io/you/my-service:1.2.3

traefik:
  enabled: true
  subdomain: mysvc
  port: 8080
  exposure: internal
  # NEW — additional routed ports (each its own subdomain), for services with
  # more than one HTTP interface (e.g. an app + its admin UI):
  extra_routes:
    - { subdomain: mysvc-admin, port: 9090, exposure: internal }

# NEW — a real container healthcheck (test/interval/retries only; no arbitrary shell escape hatch)
healthcheck:
  test: ["CMD", "curl", "-f", "http://localhost:8080/healthz"]
  interval: 30s
  timeout: 5s
  retries: 3

# NEW — secrets stay OUT of the manifest: point at a data-dir-relative env file
#        (installed 0600, never committed to a repo)
env_file: secrets.env

# NEW — resource guardrails (optional)
resources:
  cpus: "1.5"
  memory: 512m

environment:
  - "LOG_LEVEL=info"
volumes:
  - "data:/app/data:rw"
restart: unless-stopped
```

Validation rules for the new fields:

| Field | Rule |
|-------|------|
| `healthcheck.test` | list; first element `CMD` or `CMD-SHELL`; no metachars beyond the audited set |
| `healthcheck.interval/timeout` | duration `^\d+(s\|m\|h)$` |
| `healthcheck.retries` | int 1–10 |
| `env_file` | a **relative** path under the service data dir (same rule as volumes/templates); installed `0600` |
| `resources.cpus` | decimal string; `resources.memory` | `^\d+(b\|k\|m\|g)$` (compose units) |
| `traefik.extra_routes[]` | each a full mini-traefik block; subdomains globally unique (collision check extended) |

**Secrets handling.** Today `environment:` values are persisted verbatim into a world-readable
manifest. v2 adds `env_file` (a data-dir-relative file installed `0600`) as the recommended home for
secrets, and writes any manifest that still carries inline `environment` secrets as `0600`. This
closes the "secrets in a world-readable yaml" smell without breaking existing manifests.

**Non-HTTP services.** Decision point, not yet committed: either (a) explicitly reject any attempt to
declare a host/TCP port with a clear "HTTP-through-Traefik only" error (honest, minimal), or (b) add
an optional `traefik.protocol: tcp|udp` that emits a Traefik TCP/UDP entrypoint + router. Recommend
starting with (a) — an explicit, documented boundary — and only building (b) when a concrete need
appears. Whichever we pick, it must be *documented in the schema*, not discovered by a service that
mysteriously can't be reached.

---

## 4. Blessed image-first flow

Make `service run` cover everything the common case needs so a git repo is rarely necessary:

```bash
syrvis service run mysvc \
  --image ghcr.io/you/mysvc:1.2.3 \
  --subdomain mysvc --exposure internal --port 8080 \
  --volume data:/app/data:rw \        # NEW: repeatable, same relative-path policy
  --env-file ./secrets.env \          # NEW: 0600-installed, keeps secrets off the CLI
  --healthcheck 'curl -f http://localhost:8080/healthz'   # NEW: sugar for the block above
```

- Add `--volume` (repeatable), `--env-file`, and `--healthcheck` to `service run`, mapping onto the
  new schema fields.
- **Document the split** in help text and the wiki: *use `service run` for a published image (the
  default); use `service add <git-url>` only when the service ships `config_templates` or a
  multi-field manifest you want version-controlled.*
- The git path keeps its extra power (`config_templates`, `middlewares`) but stops being the implicit
  "real" way to add a service.

---

## 5. A lightweight service catalog (optional, high-leverage)

Today `service run` requires the full image reference every time, and `service add <name>` (non-URL)
returns "registry lookup not yet implemented". A small, **file-based catalog** would make the common
services one word:

```bash
syrvis service run gollum          # resolves from a bundled/known catalog entry
```

- A catalog is just a directory of vetted `syrvis-service.yaml` templates (bundled with SyrvisCore
  and/or pointed at a git catalog repo), keyed by name.
- `service run <name>` with no `--image` looks up `<name>` in the catalog, applies any `--subdomain`
  / `--exposure` / `--env` overrides, and installs — the fastest possible "add a wiki" experience.
- This stays generic: the catalog ships *templates*, not site config; the operator still chooses
  exposure and subdomain.

This is the single biggest "trivial to add a service" win and composes cleanly with the existing
override machinery (`_apply_overrides`).

---

## 6. Validation & error UX

- **Pull before run.** Run `docker compose pull` first with a targeted message ("image not
  pullable: check the tag/registry/auth") so the most common failure (a typo'd tag) is diagnosed
  precisely instead of surfacing as an opaque rollback line.
- **Keep the manifest on start-failure.** On a failed *start* (as opposed to a failed *validate*),
  keep the definition + compose on disk (not started) so the user can fix env/tag and
  `syrvis service start <name>` — rather than re-running the whole `add`.
- **Typed, machine-diffable errors.** Give the service layer a shared `SyrvisError` base with stable
  codes (mirroring the manager package) so the MCP/dashboard can serialize and act on service
  failures uniformly.

---

## 7. Whole-set convergence (the home-tech seam)

The highest-leverage flow addition, shared with the
[provisioning requirement](home-tech-provisioning-requirement.md):

```bash
syrvis stack apply --from desired.yaml   # converge core stack + L2 set to match; --json plan; dry-run
```

- Read the current core stack + installed L2 set, diff against `desired.yaml`, and **add / recreate /
  remove** to match — honoring a per-service `on_absent: stop|remove|purge` deletion policy.
- Emit a structured `--json` plan; gate the destructive subset (removals) behind the existing
  confirmation handshake; support `--dry-run`.
- This belongs in the **library** (it's deterministic domain logic about SyrvisCore's own resources),
  so the CLI, a new MCP `stack_apply_from` tool, and the dashboard share it identically — and it is
  exactly what lets home-tech declare "these are the services" and reconcile.

---

## 8. Phasing

| Phase | Ships | Status |
|-------|-------|--------|
| **1 — honesty & UX** | subdomain-collision reject ✅, unrouted warning ✅, reachability message ✅, `depends_on` reject ✅; pull-before-run and keep-manifest-on-start-failure remain | ✅ mostly shipped |
| **2 — richer schema** | `healthcheck`, `env_file` (0600), `resources` ✅; `service run --volume/--env-file` ✅ | ✅ shipped |
| **3 — catalog** | file-based service catalog (bundled + `$SYRVIS_HOME/catalog/`); `service run <name>` resolution; `service catalog` list ✅ | ✅ shipped |
| **4 — convergence** | `stack apply --from` with plan/dry-run/`on_undeclared` policy ✅; `verify` extended to the L2 set ✅; the MCP `stack_apply_from` tool remains (needs a base64url transport slot through the forced-command shim's charset whitelist) | ✅ CLI/library shipped; MCP tool pending |
| **5 — non-HTTP (if needed)** | explicit reject *or* `traefik.protocol: tcp/udp` | deferred until a concrete need |

Phases 1–4 shipped 2026-07-11 (additive, backward-compatible, audited). Remaining follow-ups:
pull-before-run diagnostics, keep-manifest-on-start-failure, and the MCP convergence tool.
