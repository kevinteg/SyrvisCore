# Layer 2 Services

**Layer 2** is where SyrvisCore earns its keep: running *your* containers — a wiki, an uptime
monitor, a home-automation hub — behind the same Traefik routing, TLS, and (optionally) Cloudflare
Access that the core stack uses. The goal is that adding a service is close to trivial.

This page is the practical guide. For the exhaustive field list see the
[`syrvis-service.yaml` Reference](07-service-schema-reference.md).

---

## Two ways to add a service

```mermaid
flowchart TB
    subgraph imagefirst["Image-first (recommended for most)"]
        a1["syrvis service run NAME<br/>--image ... --subdomain ... --exposure ...<br/>--port ... --env K=V"]
    end
    subgraph gitrepo["Git-repo (for services shipping config templates)"]
        b1["syrvis service add GIT_URL<br/>--subdomain ... --exposure ..."]
        b2["repo contains syrvis-service.yaml"]
        b1 --> b2
    end
    a1 --> synth["synthesize a syrvis-service.yaml"]
    b2 --> load["load + validate syrvis-service.yaml"]
    synth --> install
    load --> install["materialize compose + Traefik route, start"]
```

Both paths converge on the **same** on-disk artifact — a validated `syrvis-service.yaml`, a generated
`compose/<name>.yaml`, and a Traefik dynamic-config file — and run each service as its **own isolated
compose project** (`-p syrvis-<name>`).

### Image-first — `syrvis service run`

The simplest path. You hand SyrvisCore an image and how to route it; it synthesizes the manifest:

```bash
syrvis service run uptime-kuma \
  --image louislam/uptime-kuma:1.23.16 \
  --subdomain status \
  --exposure tunnel \
  --port 3001 \
  --env UPTIME_KUMA_DISABLE_FRAME_SAMEORIGIN=1
```

Best for the common case: a single published image you just want routed. No git repo to maintain, and
no arbitrary code cloned as root.

### Git-repo — `syrvis service add`

For services that ship **config templates** or a richer, version-controlled manifest, put a
`syrvis-service.yaml` in a git repo and:

```bash
syrvis service add https://github.com/you/my-service.git \
  --subdomain wiki --exposure internal
```

The repo is shallow-cloned (over an allowlisted transport), its manifest validated through the same
trust boundary as the image-first path, and installed. `--subdomain`/`--exposure` override the
manifest at enable time. Reach for this only when the service genuinely needs the extra expressiveness
(templates, custom middlewares); otherwise prefer `service run`.

---

## What "adding a service" actually does

```mermaid
sequenceDiagram
    autonumber
    participant U as You
    participant SM as ServiceManager
    participant FS as Disk
    participant D as Docker
    participant T as Traefik
    U->>SM: service run/add ...
    SM->>SM: validate the definition (trust boundary)
    SM->>SM: reject if the subdomain is already claimed
    SM->>FS: write services/<name>/syrvis-service.yaml
    SM->>FS: write compose/<name>.yaml (security_opt: no-new-privileges)
    SM->>FS: write data/traefik/config/dynamic/<name>.yaml (router + cert)
    SM->>D: docker compose -p syrvis-<name> up -d
    SM->>T: restart Traefik so it loads the new route
    SM-->>U: "added and started — reachable once its DNS/tunnel record exists"
```

Every step rolls back on failure, so a failed install never leaves partial state that blocks a retry.

---

## Exposure and reachability — the part people miss

Adding a service makes it **routable**, but not necessarily **reachable** yet: the outside world
still needs a DNS/tunnel record, which SyrvisCore reports but does not create (see
[Split DNS](04-split-dns.md)). The success message tells you which case you're in:

- **routed** (`traefik.enabled`, a subdomain set): *"reachable once its DNS/tunnel record exists; run
  `syrvis stack hostnames` for the exact record."*
- **not routed** (a git manifest with no `traefik:` block): *"installed but NOT routed — unreachable
  via Traefik."* — a deliberate, honest warning so a service that will 404 doesn't look "successful".

Then:

```bash
syrvis stack hostnames          # shows the exact A / CNAME record each host needs
```

…and home-tech reconciles that record. For `internal` that's a LAN A record → `TRAEFIK_IP`; for
`tunnel` it's a proxied CNAME + a Cloudflare Access policy.

---

## Guardrails (the trust boundary)

A `syrvis-service.yaml` from a third-party repo is **attacker-controlled input** that becomes
filesystem paths and a compose file that root starts. The schema is therefore strict — see the
[reference](07-service-schema-reference.md) for the full list, but the load-bearing rules are:

- **Names** are a narrow charset (`[a-z0-9][a-z0-9_-]{0,63}`) and may not impersonate a core service
  (`traefik`, `portainer`, `cloudflared`, `proxy`, `syrvis-macvlan`).
- **Images must be pinned** — a specific tag or `@sha256` digest, never `:latest`.
- **Volumes** may only be named volumes or paths **relative to the service's own data dir**; absolute
  host paths, `..`, `$`-expansions, and the Docker socket are all rejected.
- **Unknown keys are rejected** outright, so a manifest can't smuggle `privileged`, `cap_add`,
  `network_mode`, `devices`, etc.
- Every container gets `security_opt: no-new-privileges:true`.
- **Subdomain collisions are rejected** at add time (two services can't claim the same host).

---

## Lifecycle commands

```bash
syrvis service list              # installed services (name/version/status/url/exposure)
syrvis service start <name>
syrvis service stop <name>
syrvis service update <name>     # git services: pull + reconcile + restart if the image changed
syrvis service remove <name>     # add --purge to also delete its data
```

Each service is isolated in its own compose project, so `remove` of one never disturbs another. Note
that **removing a service deletes its route immediately** but preserves its data unless you `--purge`.

---

## The service catalog

The fastest path of all — vetted, version-pinned templates make common services one word:

```bash
syrvis service catalog            # list templates (bundled + $SYRVIS_HOME/catalog/)
syrvis service run gollum         # resolve from the catalog, install, route
syrvis service run gollum --subdomain notes --exposure tunnel   # with overrides
```

Bundled templates ship inside the wheel; drop your own `<name>.yaml` (an ordinary
`syrvis-service.yaml`) into `$SYRVIS_HOME/catalog/` to add or override one. Every
template is validated through the same trust boundary at resolve time.

## Whole-set convergence (`stack apply --from`)

For declarative management (the home-tech seam), one document can declare the
*entire* intended state — core stack enablement plus the complete L2 set — and
SyrvisCore converges to it:

```bash
syrvis stack apply --from desired.yaml --dry-run   # side-effect-free plan
syrvis stack apply --from desired.yaml -y --json   # apply (destructive gated)
```

Services absent from the document follow its `on_undeclared: stop|remove|purge`
policy (default `stop` — never destructive by default). `syrvis verify` also
reports Layer 2 drift (a declared service that is stopped or running the wrong
image), and `verify --fix` restarts it.

## Current limitations (be aware)

The single-container, HTTP-through-Traefik model is deliberately simple. Today it does **not** support:

- **Multi-container services / sidecars** — `depends_on` is rejected, because each service is its own
  compose project (a `depends_on` could only ever reference a service in the same project).
- **Non-HTTP services** — everything is routed through Traefik's HTTP entrypoints; there is no host
  port publishing or TCP/UDP entrypoint, so a service whose only interface isn't HTTP can't be
  exposed yet.

Richer declarations landed with schema v2: `healthcheck`, `env_file` (0600 secrets
out of the manifest), and `resources` — see the [schema reference](07-service-schema-reference.md).
The remaining roadmap lives in the [next-iteration design](service-declaration-v2.md).

---

## A worked example

```bash
# A LAN-only wiki:
syrvis service run gollum \
  --image gollum/gollum:v5.3.2 \
  --subdomain wiki --exposure internal --port 4567

syrvis stack hostnames
#  wiki.example.com   internal   A → 192.168.8.4   (create this on your LAN resolver)

# ...home-tech creates the A record, then:
open https://wiki.example.com     # from the LAN
```

See the shipped `examples/` directory for `internal` and `tunnel` sample definitions.
