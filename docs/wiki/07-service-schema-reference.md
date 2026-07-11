# `syrvis-service.yaml` Reference

The complete reference for a Layer 2 service definition. A `syrvis-service.yaml` describes one
service; it is written by hand (for a git-repo service) or synthesized by `syrvis service run` (for
the image-first path). For the how-to, see [Layer 2 Services](05-layer2-services.md).

> **Security note.** This schema is the **trust boundary** for third-party repositories. A
> `syrvis-service.yaml` is attacker-controlled input that becomes filesystem paths and a compose file
> that root starts, so every field is strictly validated and **unknown keys are rejected outright.**

---

## Minimal example

```yaml
name: uptime-kuma
version: "1.23.16"
image: louislam/uptime-kuma:1.23.16
traefik:
  enabled: true
  subdomain: status
  port: 3001
  exposure: internal
```

## Full example

```yaml
name: gollum                    # required
version: "1.0.0"                # required
image: gollum/gollum:v5.3.2     # required — pinned tag or @sha256 digest, never :latest
description: "Personal wiki powered by Git"
author: "You"
homepage: "https://github.com/gollum/gollum"
container_name: gollum          # defaults to name

traefik:
  enabled: true
  subdomain: wiki               # a single DNS label
  port: 4567                    # container port Traefik forwards to
  exposure: internal            # internal | tunnel
  middlewares: []               # optional Traefik middleware names

environment:                    # KEY=VALUE strings
  - "GOLLUM_AUTHOR_NAME=Wiki User"

volumes:                        # named volume OR path relative to the service's data dir
  - "wiki:/wiki:rw"

networks:                       # 'proxy' is always added automatically
  - proxy

config_templates:              # files copied from the repo into the service data dir at install
  - { source: "config.example.yml", dest: "config.yml" }

restart: unless-stopped         # no | always | on-failure | unless-stopped
```

---

## Top-level fields

| Field | Required | Type | Rules |
|-------|----------|------|-------|
| `name` | ✅ | string | `[a-z0-9][a-z0-9_-]{0,63}`; not a reserved core name (`traefik`, `portainer`, `cloudflared`, `proxy`, `syrvis-macvlan`) |
| `version` | ✅ | string | free-form (display only) |
| `image` | ✅ | string | pinned: a specific tag or `@sha256:<64 hex>`; **`:latest` and untagged are rejected** |
| `description` | | string | |
| `author` | | string | |
| `homepage` | | string | |
| `container_name` | | string | same charset as `name`; defaults to `name` |
| `traefik` | | map | routing block, see below |
| `environment` | | list | `KEY=VALUE` strings; key must match `[A-Za-z_][A-Za-z0-9_]*` |
| `volumes` | | list | mount policy below |
| `networks` | | list | each a valid name; `proxy` is always included |
| `config_templates` | | list | `{source, dest}` — both relative subpaths (no absolute, no `..`) |
| `restart` | | string | `no` \| `always` \| `on-failure` \| `unless-stopped` |
| `enabled` | | bool | orchestration key (default `true`); `false` → declared but not run. Only meaningful in a `services.d/` declaration — see [Declarative loading](05-layer2-services.md#declarative-loading--servicesd--reconcile). |
| `critical` | | bool | orchestration key (default `false`); `true` → this service's failure makes a `reconcile` run report the stack unhealthy instead of merely degraded. |

Any key **not** in this list is rejected — this is what stops a manifest smuggling `privileged`,
`cap_add`, `devices`, `network_mode`, etc.

## The `traefik` block

| Field | Default | Rules |
|-------|---------|-------|
| `enabled` | `true` | if `false` (or the block is omitted), the service installs **unrouted** and is unreachable via Traefik — the CLI says so explicitly |
| `subdomain` | `""` | a single DNS label (`[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?`); routed at `<subdomain>.<DOMAIN>` |
| `port` | `80` | integer 1–65535; the container port Traefik forwards to |
| `exposure` | `internal` | `internal` (LAN-only) or `tunnel` (remote via Cloudflare) — see [Split DNS](04-split-dns.md) |
| `middlewares` | `[]` | Traefik middleware names to attach |

**Exposure is declared intent, not routing.** SyrvisCore routes `internal` and `tunnel` identically
at the Traefik layer (same router, same Let's Encrypt resolver); exposure only changes which external
record `syrvis stack hostnames` reports for the host.

**Subdomains must be unique** across installed services — a collision is rejected at add time.

## Volume mount policy

Volumes are the sharpest edge of the trust boundary. Allowed:

- **Named volumes** — `myvolume:/container/path[:mode]`
- **Relative host paths** — resolved under `$SYRVIS_HOME/data/<service>/`: `subdir:/container/path[:mode]`

Refused:

- Absolute host paths (`/etc/...`, `/`, and especially `/var/run/docker.sock` in any form)
- `..` traversal and `$`-expansions
- modes other than `ro` / `rw` (default `rw`)

Containment is re-checked when the compose file is generated, so a value that somehow reached that
layer unvalidated still can't escape the service's own data directory.

**On-disk behavior of a bind mount** (handled for you, but worth knowing):

- The host source dir is **pre-created** — DSM's Docker refuses to auto-create it, so `up` would
  otherwise fail with *"Bind mount failed: … does not exist"*.
- An `rw` source dir is made **`0777`** so a non-root container UID can write to it (a root-owned dir
  would shadow the image's volume and crash the app). `ro` dirs get no write bit. A narrower,
  per-service ownership control (a `user:`/PUID-GID field) is on the [roadmap](service-declaration-v2.md).
- `syrvis service start <name>` **regenerates the compose file first**, so a volume dir pruned or
  re-permissioned out from under a service is re-created before the container starts (drift self-heal).

## The v2 fields (healthcheck, env_file, resources)

| Field | Rules |
|-------|-------|
| `healthcheck` | Audited subset of compose's: `test` (a list starting `CMD` or `CMD-SHELL`), `interval`/`timeout`/`start_period` (`<n>(s\|m\|h)`), `retries` (1–10). Unknown keys rejected. |
| `env_file` | A **data-dir-relative** file for secrets (same containment rules as volumes). Materialized empty and clamped to `0600` at install if absent — the recommended home for secrets, keeping them out of the manifest. |
| `resources` | `cpus` (decimal string) and/or `memory` (`<n>(b\|k\|m\|g)`), emitted as compose `cpus`/`mem_limit`. |

A manifest that carries inline `environment:` entries is written `0600` (they may
hold secrets); prefer `env_file`.

## What you cannot (yet) declare

The single-container, HTTP-through-Traefik model intentionally omits:

- **`depends_on`** — *rejected*: each service is its own compose project, so it could never reference
  another service. (Multi-container manifests are not yet supported.)
- **Multiple published/routed ports** and **non-HTTP (TCP/UDP) exposure** — everything goes through
  Traefik's HTTP entrypoints; no host ports are published.

See the [next-iteration design](service-declaration-v2.md) for the roadmap.

---

## Every container gets, for free

- `security_opt: no-new-privileges:true`
- membership in the external `proxy` network (so Traefik can reach it)
- a generated Traefik dynamic-config file (HTTP→HTTPS redirect + HTTPS router with a Let's Encrypt
  cert) when `traefik.enabled`
- rollback of all artifacts if any install step fails
