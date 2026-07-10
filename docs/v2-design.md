# SyrvisCore v2 Design

Direction set 2026-07-09. Companion: `docs/code-audit-2026-07.md` (why the rewrite/refactor split below). This document is the pickup point for implementation sessions in this repo, and the reference point for sessions in home-tech and personal-assistant.

## 1. Scope

SyrvisCore v2 is **the Synology control plane, and only that**. It is four deliverables in one repo:

| # | Deliverable | Consumer |
|---|---|---|
| 1 | **Configuration specification** — the schema a repo uses to declare what it needs from the Synology (a service container, Traefik routing, port mappings, volumes, data directories) | Any pursuit/family repo (e.g. a repo ships `syrvis-service.yaml`) |
| 2 | **NAS-installed software** — `syrvisctl` (version manager) + `syrvis` (service manager): a small, deterministic CLI ecosystem that keeps everything under control and is fully usable over SSH **with no LLM anywhere** | Kevin, logged into the NAS |
| 3 | **MCP server** — exposes the same operations as typed tools, including install/health verification | Claude sessions, primarily in home-tech |
| 4 | **Drivers/skills** — Claude Code skills for operating a SyrvisCore instance (deploy a service, verify health, upgrade) | home-tech (the operator repo) |

Out of scope (live elsewhere): the registry of instances and operational runbooks (home-tech), cross-repo discovery/advertisement (Cadence — kevinteg/Cadence#15–#17), data catalogs and site publishing (pursuit repos).

## 2. Operating principles

1. **The NAS is a critical production system.** Weak DR story (it holds the Time Machine backups; AWS restore is a last resort). Implementation-phase sandbox is limited to: Portainer, Traefik, Cloudflare tunnel, and SyrvisCore itself. Everything else on the NAS is production and untouchable.
2. **Declarative intent only.** Config expresses desired state; sanctioned tools nudge reality toward it. No hand-editing Synology config files, ever — including by the MCP. If a needed change has no sanctioned tool, the tool gets built first.
3. **Deterministic core, thin adapters.** One tested library layer does the work; the CLI and the MCP server are both thin adapters over it. No operation exists only via MCP; anything Claude can do, `ssh nas && syrvis …` can do.
4. **Machine-readable everything.** Every command supports `--json`; every operation returns typed results and typed errors. Prompts exist only in the CLI adapter and every one is bypassable (`-y`, `--non-interactive`).
5. **Integrity chain.** Releases ship sha256 sums; nothing installs unverified artifacts; installs are staged-then-swap (never destroy-then-download).
6. **Verify is first-class.** `syrvis verify` compares desired state (config + manifests) against actual state (files, permissions, containers, network) and reports drift; `--fix` applies only sanctioned remediations. The MCP exposes it so any session can start with a health check.

## 3. What we keep / refactor / rewrite (from the audit)

| Component | Disposition |
|---|---|
| Two-package split (manager/service), versioned dirs + `current` symlink, per-version venvs | **Keep** — the architecture is sound |
| CLI command surface (`syrvisctl install/activate/rollback/…`, `syrvis setup/start/verify/service …`) | **Keep**, add `--json` everywhere |
| `privileged_ops.py` provider pattern, `validators.py`, compose/traefik generators, versioned `paths.py` | **Keep/refactor** — validators become the `verify` engine |
| Manager internals (downloader, version_manager, backup, manifest) | **Rewrite** as a library core: `SyrvisManager(home: Path)`, typed exceptions, dataclass results, flock-guarded mutations, atomic symlink (`os.replace` over tmp) and manifest writes, checksum-verified downloads, `--no-deps` installs against hash-pinned requirements |
| Service `cli.py`/`setup.py` | **Refactor** into thin shells over structured-return managers |
| Layer 2 service intake | **Rewrite the validation layer**: `name` ∈ `^[a-z0-9][a-z0-9_-]{0,63}$`, resolve-and-contain every derived path, volume policy (no absolute host paths outside an allowlist, no `..`, no docker.sock, no privileged/cap_add), explicit compose project per service (`-p syrvis-<name>`), rollback on failed add |
| SPK lifecycle scripts | **Rewrite** around the run-as-package-user reality (see §6) |
| Wizard, `spk/package/setup-privileges.py`, root `pyproject.toml`, Makefile wheel path, dead test infra | **Delete** |
| Elevation (`os.execv(sudo …)`) | **Redesign** (see §5) |

## 4. Library core shape

```
syrviscore_core (importable by CLI and MCP alike)
├── manager/    # versions: install(wheel|release), activate, rollback, list, cleanup
├── services/   # core stack + layer2: add/remove/start/stop/update, compose gen
├── verify/     # drift engine: desired vs actual (files, perms, containers, network)
├── privileged/ # SystemOperations providers (dsm / sim / transcript-fake)
└── errors.py   # typed taxonomy: NotFound, Network, Integrity, Privilege, Drift, …
```

Rules: no `click` imports, no prompts, no `print`, no `os.environ` mutation (home path is an explicit parameter), every mutation holds a lockfile, every function returns data.

## 5. Privilege model

Self-elevation via `os.execv` is incompatible with MCP (replaces the process) and loses env/state (audit: manager M3, service H2). v2:

- **CLI (interactive):** keep self-elevation as UX sugar, but resolve *all* inputs to argv flags before re-exec, preserve `SYRVIS_HOME` explicitly through the boundary, and prefer erroring with the exact `sudo …` command when no TTY.
- **MCP server:** never elevates itself. It invokes the CLI as a subprocess with `--json`; privileged operations require the server to have been granted narrow sudoers entries (`NOPASSWD` for an enumerated list of `syrvis`/`syrvisctl` subcommands — not blanket root). The enumerated list *is* the security boundary, and it's auditable.
- All privileged effects remain inventoried in `privileged_ops` — one place to review what root is used for (docker group, socket 0660, rc.d boot hook, macvlan shim).

## 6. Install strategy

### Production: the SPK stays — it is the DR path

The SPK remains the sanctioned production install (bootstrapper role, as today: install manager → `syrvisctl install` → `syrvis setup`), and its primary justification is **disaster recovery**: when the NAS is rebuilt, the recovery flow is *install the SPK → run the installer chain → `syrvis verify`* — fast, deterministic, and requiring no LLM or workstation tooling. Once the instance is healthy, the MCP layer validates it and resumes remote management. This makes the SPK's reliability a DR requirement, not a convenience. Script fixes required (audit §3): rewrite preuninst/postuninst for the package-user privilege model with correct boot-hook names and **no docker.sock chmod**; fix preinst's directory check; support `ContainerManager` (DSM 7.2+) in `INFO` dependencies; POSIX-sh-only constructs; postupgrade stages the new venv before removing the old.

### Development: the tarball loop

Fast iteration never goes through Package Center:

```
make tarball          # manager wheel + pinned dep wheels + service wheel
                      #   + config.yaml + bootstrap.sh, deterministic tar
scp … nas:            # or rsync
ssh nas 'tar xf … && ./bootstrap.sh [--home /volume1/syrviscore-dev]'
ssh nas '…/syrvis verify --json'   # gate: green before it counts
```

- **Enabler (build first):** `syrvisctl install --wheel <file>` — the only missing primitive; everything else (`venv` + offline pip + `syrvis setup`) already exists as automation.
- `bootstrap.sh` replicates only postinst (venv, offline pip from bundled wheels, profile snippet), then delegates to `syrvisctl install --wheel` + `sudo syrvis setup`. Never reimplement setup in shell.
- **Dev installs use a distinct `SYRVIS_HOME`** (e.g. `…/syrviscore-dev`) so the SPK's manifest-scan discovery never confuses a dev tree with production.
- `bootstrap.sh --clean` tears down the dev instance completely (containers, rc.d hook, dev home).
- Deterministic archives: `tar --sort=name --owner=0 --group=0 --mtime=@$SOURCE_DATE_EPOCH`, `COPYFILE_DISABLE=1` on macOS; sha256sums generated for every artifact and verified by bootstrap.

## 7. Verify / health engine

Built on the existing `validators.py` dataclasses, extended with what the audit found missing:

1. **Drift check**: containers defined in generated compose vs actually running (image tags vs manifest version) — today's validators never compare desired vs actual.
2. Fix the fixer gaps: `boot_script` and `manifest_perms` branches in `apply_fixes`; resolve the invoking user via `SUDO_USER` when euid=0.
3. Tiers: `verify --smoke` (non-destructive, seconds: ports, shim route, container health, cert perms — the post-install gate for the tarball loop), `verify` (full report), `verify --fix` (sanctioned remediations only, each logged).
4. JSON report is the MCP's native return type.

## 8. MCP server

- **Transport/placement:** stdio MCP server that runs where the operator session runs (Mac) and executes `syrvis`/`syrvisctl` on the NAS over SSH with `--json` — no new daemon or attack surface on the NAS, and the CLI contract tests are the MCP contract tests. (A NAS-resident server behind Traefik can come later if latency or multi-client needs demand it; nothing in the design precludes it.)
- **Tools (initial):** `status`, `verify` (+smoke/fix), `service_list/add/remove/start/stop/update`, `versions_list`, `install`, `activate`, `rollback`, `logs`. Destructive tools (`rollback`, `service_remove`, `activate`) declare themselves destructive and require explicit confirmation parameters.
- **Guardrails:** the MCP can only reach the enumerated sudoers commands (§5); it cannot run arbitrary shell on the NAS. Sandbox/production awareness comes from config: tools refuse to touch services not marked managed-by-syrviscore.

## 9. Configuration specification (deliverable 1)

`syrvis-service.yaml` (existing, see `examples/`) grows into the spec a repo ships to declare its Synology needs:

- Today: name, image, traefik subdomain/port, volumes, env, networks.
- v2 additions: schema version + strict validation (§3 security rules), declared port mappings, resource hints, `internal: true|false` (Traefik-only vs Cloudflare-tunnel exposure), data directory declarations (feeds future backup/data tooling), and a `checksum`/pin policy for images (specific tags only — already the house rule).
- The spec is versioned and documented here; consuming repos advertise the file's presence via the Cadence content manifest (kevinteg/Cadence#17) so home-tech can discover every deployable service across repos.

## 10. Testing strategy

Adopt the audit's priority list verbatim (audit §4): gating CI on Python 3.8.12 against the monorepo layout → `CliRunner` contract tests with `--json` for every command → command-transcript fake for `DsmOperations` → SPK lifecycle test in a Linux container → hermetic manager tests with a local release source → golden-file compose/traefik tests → `verify --smoke` as the NAS-side gate. Sim mode that returns unconditional success is deleted in favor of the transcript fake.

## 11. Phases

| Phase | Contents | Exit criteria | Status |
|---|---|---|---|
| **0. Hygiene** | Delete dead artifacts (wizard, `spk/package/`, root pyproject, orphaned tests); fix Makefile/tox/CI to monorepo; pin manager deps | CI green and gating on 3.8 | ✅ done (2026-07-09) |
| **1. Manager core** | Rewrite internals per §3/§4: typed core, atomic ops, checksums, `--json`, `install --wheel` | Hermetic install→activate→rollback→restore tests pass | ✅ done (2026-07-09) |
| **2. Dev loop** | Tarball build + `bootstrap.sh` (+`--clean`); SPK script rewrite | Full loop green on the real NAS against a dev `SYRVIS_HOME` | ✅ done (2026-07-09). Validated on the NAS (Synology avoton, DSM Python 3.8.12) as user `cerebrate` into `~/syrviscore-dev`: checksums verified, offline manager venv, `install --wheel`, `--json` output, idempotent re-run, `--clean` teardown — all green. `setup` skipped (no passwordless sudo / docker for that user; privileged setup is Phase 4/5). `verify --smoke` moved to Phase 4 |
| **3. Service refactor** | Security fixes (L2 C1/C2), elevation redesign (H2), thin CLI over structured managers, compose v2 unification | `sudo syrvis service …` works; malicious-manifest tests pass | ✅ security fixes done (2026-07-09); non-security cleanups tracked in `PHASE3-NONSECURITY-NOTES.md` |
| **4. Verify engine** | Drift check, fixer gaps, JSON reports | `verify` catches an injected drift on the NAS | 🟡 **read-only core done (2026-07-10)**: `syrvis verify [--smoke] [--json]` + `drift.py` (desired-vs-actual container drift), pure/docker-free, 22 tests. Remaining ⚠️ **route to a non-Fable model**: the privileged `verify --fix` remediation (docker socket, macvlan, boot hooks, sudo) — see `memory/fable-avoid-privileged-layer.md`. NAS injected-drift check pending (needs `setup` on the NAS, which is privileged) |
| **5. MCP + skills** ⚠️ | MCP server over SSH+`--json`; sudoers enumeration; home-tech operator skills | home-tech session deploys + verifies a Layer 2 service end-to-end via MCP | ⚠️ **route to a non-Fable model** — privileged/sudoers surface |
| **6. Spec v2** | `syrvis-service.yaml` schema versioning + validation + docs; tie into Cadence #17 | pursuit repo deploys an internal site by advertising a manifest | not started (non-privileged; Fable-safe) |

Production cutover (replacing the currently-installed instance) happens only after Phase 4, via the SPK, with a pre-cutover `syrvisctl backup` and a rehearsed rollback.

## 12. Ecosystem relationships

- **home-tech** — operator: instance registry, runbooks, drives operations via MCP (capture filed in `home-tech/thoughts/unprocessed/2026-07-09T19-55.md`).
- **personal-assistant** — hub: tracks this revival as a delegated pursuit (captures filed 2026-07-09).
- **Cadence** — kevinteg/Cadence#15 (delegation), #16 (guest mode), #17 (content manifest; §9 depends on it for discovery).
- **pursuit repos** (pursuit-gaming, city-services, future family repos) — declare services via the §9 spec; internal sites deploy as Layer 2 services.
- Housekeeping: stale duplicate working copy at `~/code/synology/SyrvisCore` is marked for deletion once Phase 1 lands (confirm first).
