# SyrvisCore

> # ⚠ DEPRECATED — THIS IS THE RETIRED v1 SYSTEM (2026-09-01)
>
> **SyrvisCore is SyrvisCore v1, and v1 is retired.** Do not install it, do not
> run it, and do not extend it. The successor is **[syrvis
> v2](https://github.com/kevinteg/syrvis)** (`~/code/syrvis`) — a rewrite in Go
> as a static agent (`syrvisd`) plus a `syrvis` CLI — and it has been **live on
> the RS1221+ since 2026-09-01**, reached over the mTLS **T-api** transport:
>
> ```sh
> syrvis --context rs1221-ops status     # what the device says it is doing
> syrvis --context rs1221-ops events     # what has happened
> syrvis --context rs1221-ops health     # conformance
> ```
>
> **What was retired, and when.** Ruling R11-1 (2026-08-26) retired v1's operator
> seam and its `syrviscore-mcp` MCP server. `~/.config/syrviscore-mcp/` was
> renamed `.v1-retired` on 2026-08-29, and `/volume4/syrviscore/bin/syrvis` — the
> wrapper the seam's forced command allowlisted — was removed from the NAS by the
> v1→v2 pivot. On 2026-09-01 the owner closed the rollback question outright: *we
> are past rolling back to v1.* Anything in this README that reads as a live
> instruction — installing the SPK, `syrvisctl install/activate`, the MCP's 42
> tools, the seam call shapes — is history.
>
> **Do not rebuild anything shaped like the v1 seam.** That is the explicit content
> of R11-1, and it applies to repointing the MCP as much as to re-provisioning the
> shim. If v2 cannot yet answer a question, the answer is *we cannot see that yet* —
> not a v1 path resurrected for one read.
>
> **Why the repo is kept, in full.** Two reasons, both load-bearing:
>
> 1. **History.** This is the most complete record of how the platform actually
>    behaved on a real DSM box — the macvlan shim, the passwd-revert trap, the
>    forced-command seam's argv discipline, the compose generator's create-on-absent
>    semantics. v2's design reads it as input.
> 2. **DSM-side artifacts still on devices.** v1 left real things on real NASes:
>    an SPK slot, `/volume*/syrviscore*` roots, DSM Task Scheduler entries, a
>    `syrvis-operator` account, `/etc/sudoers.d/` policy. Retiring those is owner
>    work done from the DSM console, and this repo is what tells you what each one
>    was for. The sweep is tracked in
>    `~/code/home-tech/syrvis-v2/docket/12-v1-ledger.md`.
>
> Everything below is preserved as that record. Read it in the past tense.

---

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Python 3.8+](https://img.shields.io/badge/python-3.8+-blue.svg)](https://www.python.org/downloads/)
[![Code style: black](https://img.shields.io/badge/code%20style-black-000000.svg)](https://github.com/psf/black)

A self-hosted infrastructure platform for Synology NAS. SyrvisCore turns a DSM
box into a coherent, declaratively-managed application host: Traefik reverse
proxying with real certificates on your LAN, optional remote access through a
Cloudflare Tunnel, a curated Layer 2 service tier for the apps you actually
run, and a management seam that makes routine operations API calls instead of
SSH sessions.

**Why it exists.** DSM owns ports 80/443 and makes clean HTTPS for
self-hosted services painful. SyrvisCore gives Traefik its **own LAN IP** via
macvlan (plus a shim so the host stays reachable), issues **DNS-01 wildcard
certificates** so nothing needs to be exposed to the internet, routes DSM's
native apps (DSM UI, Photos, Drive, WebDAV) through the same clean hostnames,
and survives reboots without manual steps.

## Architecture (v3 — split packages, thin adapters)

One deterministic library does the work; every interface is a thin adapter
over it — anything an adapter can do, `ssh nas && syrvis …` can do.

| Package | What it is | Runs |
|---------|-----------|------|
| `syrviscore` (`syrvis`) | The platform: compose/Traefik generation, the Layer 2 service tier, the declarative stack, the seam verb registry | On the NAS, per-version venv |
| `syrviscore-manager` (`syrvisctl`) | Version manager: install/activate/rollback service versions from GitHub releases; seam lifecycle | On the NAS, SPK-installed (rarely updated) |
| `syrviscore-mcp` | MCP server exposing 42 typed management tools over a hardened SSH seam | On your workstation |
| `syrviscore-dashboard` | Web UI (FastAPI + React): health, drift, routes, logs | On the NAS, optional core container |

### The tiers

- **Core (declared in `config/stack.yaml`)** — the platform substrate.
  *Primordial* (always on): Traefik, Portainer. *Optional*: cloudflared,
  the dashboard, Cloudflare DDNS. Toggled with `syrvis stack enable/disable`,
  converged with `syrvis stack apply` + `syrvis start`.
- **Layer 2 (declared in `config/services.d/`)** — your apps. Each is a
  strictly-validated `syrvis-service.yaml` (pinned image, contained volumes,
  audited keys — unknown keys rejected) run as its own compose project and
  routed by a generated Traefik file. `syrvis reconcile` converges the
  declarations; `syrvis service run/add` covers the imperative path.
- **Profiles** — platform-curated sets: `syrvis profile enable monitoring`
  declares a complete observability substrate (VictoriaMetrics, vmagent,
  vmalert, Alertmanager, node/docker exporters) with platform-pinned images.

### Exposure: internal vs tunnel

Every routed service declares `exposure: internal` (LAN-only; a LAN DNS record
pointing at Traefik) or `tunnel` (remote via Cloudflare Tunnel + Access).
SyrvisCore never touches DNS or the Cloudflare API — `syrvis stack hostnames
--json` reports the exact external records each hostname needs, and a
deployment repo reconciles them. That report, the bundle formats, and the
operator seam are the documented cross-repo surface: see
[`docs/seam-contract.md`](docs/seam-contract.md).

### The management seam

Routine operations run as a dedicated least-privilege operator account whose
SSH key is locked to a **generated forced-command shim** and an **enumerated
sudoers allowlist** — both derived from the platform's verb registry
(`syrviscore.seam`), regenerated automatically when a version is activated.
The MCP server brokers those verbs as typed tools (with a two-call
confirmation handshake for destructive ones); configuration flows as validated
JSON bundles over stdin (`syrvis apply` for the instance, `syrvis deploy` per
service) so secrets never touch argv, logs, or an LLM context. Break-glass SSH
is reserved for what genuinely needs it: first-boot setup, disaster restore,
troubleshooting.

## Quick start

```bash
# 1. Install the SPK (manager) on DSM, then on the NAS:
source /var/packages/syrviscore/target/syrviscore.profile
syrvisctl install                 # downloads + installs the service package
sudo syrvis setup                 # interactive: network, domain, tokens, boot hooks
syrvis start                      # bring up the core stack

# 2. Run something:
sudo syrvis service run uptime-kuma          # from the bundled catalog
sudo syrvis profile enable monitoring        # the observability substrate
sudo syrvis reconcile

# 3. See what DNS records the instance needs:
syrvis stack hostnames --json
```

Requirements: DSM 7.0+, Container Manager (Docker), a domain (Cloudflare for
DNS-01 certs + optional tunnel), a reserved LAN IP for Traefik.

## Documentation

The engineering handbook lives in **[`docs/wiki/`](docs/wiki/README.md)** —
architecture, networking/macvlan, split-horizon DNS, the Layer 2 how-to, the
service schema reference, disaster recovery. Key references:

- [`docs/seam-contract.md`](docs/seam-contract.md) — the deployment-facing
  contract (hostnames report, bundle schemas, seam verbs)
- [`docs/design-doc.md`](docs/design-doc.md) — the v3 design
- [`docs/dashboard.md`](docs/dashboard.md) / [`docs/mcp-design.md`](docs/mcp-design.md) — the adapters
- `packages/syrviscore-mcp/README.md` — MCP setup + operator provisioning

Naming, for orientation: “core” is the stack tier in `stack.yaml`
(“primordial” = its always-on subset); “Layer 2” (L2) is the app tier in
`services.d/`; a “bundle” is a validated JSON document streamed over the seam.

## Development

```bash
pyenv install 3.8.12 && pyenv virtualenv 3.8.12 syrviscore && pyenv activate syrviscore
pip install -e "packages/syrviscore-manager[dev]" -e "packages/syrviscore[dev]"
pytest tests/ packages/syrviscore/tests/          # service + manager (3.8, DSM parity)

# MCP + dashboard test on modern Python (3.12), each with the platform lib:
pip install -e packages/syrviscore -e "packages/syrviscore-mcp[dev]"
pip install -e packages/syrviscore -e "packages/syrviscore-dashboard[dev]"
```

Docker image pins are committed in code (`DEFAULT_DOCKER_IMAGES` in
`packages/syrviscore/src/syrviscore/compose.py`); a release may attach a
generated `config.yaml` asset that overrides them per-version. Build scripts
live in `build-tools/` (SPK, wheels, dev tarball, releases). CI runs the 3.8
matrix (black + ruff + pytest), the MCP/dashboard suites, a seam-artifact
drift check, and a full install → backup → wipe → restore cycle.

## License

MIT — see [LICENSE](LICENSE).
