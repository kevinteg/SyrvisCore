# The Operator Seam

## Purpose & role in the system

The operator seam is SyrvisCore's *only* sanctioned privileged path from an operator machine (Kevin's Mac, home‑tech tooling, the MCP server) onto the NAS. It replaces "ssh in as an admin and type things" with an enumerated, generated, doubly‑enforced verb boundary: a dedicated least‑privilege NAS account whose SSH key is pinned to a forced‑command shim, whose sudo rights are an enumerated `NOPASSWD` allowlist, and whose entire vocabulary is derived from one Python data structure that the runtime *also* uses to build argv. The design goal, stated in `packages/syrviscore/src/syrviscore/seam/__init__.py:1-17`, is that "verbs are added HERE, never forked in a client," and in `gen.py:2-8` that "the enumerated sudoers boundary and the shim allowlist are derived from the same source the runtime uses to build argv, so they cannot drift (G18)."

It is the seam that makes the thin‑adapter architecture real: the MCP server, home‑tech's reconcile engine, and hand scripts all speak the same argv, and everything they can do, a human at `ssh nas && syrvis …` can do.

## Key modules and files (path — role — approx size)

| Path | Role | Size |
|---|---|---|
| `packages/syrviscore/src/syrviscore/seam/registry.py` | THE registry: `Command`/`Slot`/`FlagValue` dataclasses + ~80 enumerated commands with privilege/destructiveness/timeout/expect_json classification | 805 L |
| `packages/syrviscore/src/syrviscore/seam/gen.py` | Renders sudoers, forced‑command shim, and a self‑contained NAS provisioning script from the registry; `check` subcommand for drift | 716 L |
| `packages/syrviscore/src/syrviscore/seam/__init__.py` | Package doc stating the single‑source rule | 17 L |
| `packages/syrviscore-manager/src/syrviscore_manager/seam_sync.py` | `syrvisctl seam sync/status` + the `activate`/`rollback`/`install` auto‑hook; renders via the ACTIVE version's venv python | 184 L |
| `packages/syrviscore-manager/src/syrviscore_manager/cli.py:1131-1200`, `:488-506` | `seam` command group; `_sync_seam_after_switch` | — |
| `packages/syrviscore-manager/src/syrviscore_manager/doctor.py` | Rootfs‑only diagnosis incl. `SEAM_ACCOUNTS` shell census (`:41`, `:178-210`) | ~270 L |
| `packages/syrviscore/src/syrviscore/privileged_ops.py` | The privileged operation inventory + boot‑hook/startup‑script/ifcfg renderers | 1591 L |
| `packages/syrviscore/src/syrviscore/privilege.py` | CLI self‑elevation (`is_root`, `self_elevate`, `ensure_elevated`) | 71 L |
| `packages/syrviscore-mcp/deploy/{sudoers.d/syrviscore-mcp, ssh/syrvis-mcp-shim}` | Committed *reference* artifacts, drift‑tested against the generator | 69 / 291 L |
| `packages/syrviscore-mcp/src/syrviscore_mcp/deploy/gen.py` | Back‑compat launcher re‑exporting `syrviscore.seam.gen` | 40 L |
| `docs/seam-contract.md` | The public contract with a deployment repo | 210 L |
| `docs/home-tech-provisioning-requirement.md`, `docs/mcp-design.md` | Division of labor; G1–G18 guardrails | 153 / 290 L |

## How it actually works

**Account model.** Two seam identities exist: `syrvis-operator` (the default, overridable with `--operator`) and `syrvis-reader`. Only the operator has a provisioning path in this repo; the reader appears solely as something to *heal* and *diagnose* (`privileged_ops.py:223`, `:450`; `doctor.py:41`). Both are created with a random password never used (`gen.py:422-431`) and must have `/bin/sh`: DSM's `synouser` creates accounts as `/sbin/nologin`, and sshd executes the forced command *through* the login shell, so nologin yields a misleading password prompt (`gen.py:434-467`). Worse, DSM regenerates `/etc/passwd` on **every boot**, reverting the shells — which is why the same three‑line `sed` heal is duplicated in three places (the rootfs S99 hook at `privileged_ops.py:450-454`, the volume‑resident `syrvis-startup.sh` at `:223-227`, and a `*/5` cron job `seam-selfheal`). The operator is added to `docker` (`synogroup --memberadd`) so the *read* verbs, which run without sudo and touch the Docker socket, work; the boot hook re‑adds it because DSM also regenerates `/etc/group` (`privileged_ops.py:288-296`).

**The key.** `authorized_keys` gets exactly one additive line: `restrict,command="<shim>",from="<cidr>" <pubkey>` (`gen.py:300`), installed by filtering out any prior line for the same shim and appending — "keeping any other keys," i.e. a break‑glass admin key survives (`gen.py:590-601`). `restrict` kills PTY/forwarding/SFTP (G14).

**The registry.** Each `Command` carries `id`, `cli` (`syrvis` via the wrapper, `syrvisctl` via the SPK venv path), literal `subcommand` tokens, `flags` (literals interleaved with `FlagValue(Slot)`), an optional `positional` Slot placed *after a literal `--`*, and booleans `sudo`/`read_only`/`destructive`/`expect_json`/`install_path` plus `timeout_s`. Eighteen slot kinds exist (`version`, `name`, `git_url`, `keep`, `tail`, `image`, `subdomain`, `exposure`, `port`, `prune_policy`, `boolean`, `stack_service`, `revision`, `halt_reason`, `shed_reason`, `timestamp`, `git_rev`, `sha256_digest`). Construction rules are documented at the top: `syrvis` always through the wrapper (which exports `SYRVIS_HOME`), `syrvisctl` with `--path` **only** on `install`, user positionals always after `--`, and optional positionals expanding to **two** accepted argv shapes.

**Generation.** `render_sudoers` emits only `sudo=True` commands — "read-only commands run without sudo — not in the policy" (`gen.py:94`) — as one `NOPASSWD:` directive with `Defaults:<operator> !requiretty, env_reset, secure_path=…`. Slot values become bare `*` globs; sudo is safe here because it `execve`s a single argv, never a shell (`docs/mcp-design.md:164-168`). `render_shim` emits the whole registry (read verbs included, since they also transit the key) as a POSIX‑sh cascade: (1) a **character whitelist** `[^A-Za-z0-9 ._@:/-]` that kills every metachar, glob, quote and control char in one grep; (2) an embedded‑newline check via `wc -l`; (3) `set -f; set -- $cmd`; (4) an exact match — `[ $# -eq N ]` plus literal equality per token and a per‑kind predicate per slot — then `set -f; exec "$@"`. Numeric predicates re‑implement `validate.py`'s bounds (`keep 0–50`, `tail 1–10000`, `port 1–65535`, `revision 1–999999`) because "the shim is the independent G13 layer, so its bounds must not be looser" (`gen.py:191-194`).

**Provisioning.** `python -m syrviscore.seam.gen provision --home … --pubkey … [--from CIDR] [--no-auto-seam-update]` renders a single POSIX script with the sudoers body, shim body, key line and paths baked in. It validates the pubkey against `^[A-Za-z0-9 @._:/=+-]+$` and `^(ssh-|ecdsa-|sk-)`, and the CIDR shape (`gen.py:289-299`). It: creates the user, surgically rewrites only field 7 of the operator's `/etc/passwd` row (with a record‑count + regex safety check before the atomic `mv`), ensures docker group, installs sudoers via a **dotted temp in `/etc/sudoers.d/` that sudo's `#includedir` ignores, then `mv -f`** (DSM has no `visudo`), installs the shim `0755 root:root`, installs the key, and writes the seam policy. It captures true pre‑install state once (`$ORIG_DIR/.captured<path>` markers) and regenerates `/var/log/syrviscore-mcp-provision/rollback.sh` after every capture so a mid‑run abort still leaves a correct revert.

**The seam policy** — `/var/log/syrviscore-mcp-provision/seam-policy.json`, `0600`, root‑held:

```json
{"auto_seam_update": true, "operator": "syrvis-operator",
 "syrvis_home": "/volume4/syrviscore",
 "syrvisctl_path": "/var/packages/syrviscore/target/venv/bin/syrvisctl",
 "shim_path": "/usr/local/bin/syrvis-mcp-shim"}
```

**Lifecycle.** `seam_sync._render` execs `<home>/current/cli/venv/bin/python -m syrviscore.seam.gen {sudoers|shim} --home … --operator … --syrvisctl … --shim-path …` — the manager deliberately never imports `syrviscore` (separate venvs, `seam_sync.py:16-19`). `_install` compares content first, writes a dotted temp in the target dir, chmods (`0440` sudoers / `0755` shim), runs `visudo -cf` when available, and `os.replace`s. `sync_seam` returns `{"dry_run": bool, "sudoers": "updated"|"unchanged"|"would update", "shim": …}`. `auto_sync_after_activate` returns `None` when unprovisioned or `auto_seam_update` is false. `syrvisctl install` (`cli.py:357`), `activate` (`:482`) and `rollback` (`:586`) all call `_sync_seam_after_switch`, which swallows every exception into a warning: "Never fails the version switch — the seam is recoverable with `syrvisctl seam sync`, a failed activate is not."

**Verb classes** (`docs/seam-contract.md:49-55`): read/no‑sudo (`status`, `verify`, `service list`, `logs`, `history`, `images`, `dashboard generate`, `syrvisctl list/check/info/doctor/backup list`); read/sudo‑to‑read‑0600 (`reconcile --dry-run`, `apply --dry-run`, `export`, `schedule list`, `schedule dsm-tasks`, `vm list/status`, `stack hostnames_full`, `updates`); converge (start/stop/restart/shutdown/resume/reconcile/stack/service lifecycle/shed/unshed/profile enable/install/backup create); destructive, MCP‑token‑gated (`reconcile --prune`, `service remove`, `set-image`, `service rollback --to`, `activate`, `rollback`, `uninstall`, `cleanup`, `backup cleanup`, `schedule apply/sync`); and **stdin writers** `apply`, `deploy`, `secret set`, `config set` — payload on stdin only, and deliberately *not* MCP tools (grep confirms no `secret_set`/`config_set`/`deploy` tool exists in `syrviscore_mcp`), so "secrets never touch argv/ps/logs, and never transit an LLM context."

## Design decisions & their rationale

- **Single source of truth (G18).** Registry → sudoers + shim + provision script; a drift test asserts committed == generated (`packages/syrviscore-mcp/tests/test_drift.py:56-66`).
- **Defense in depth (G13).** The shim re‑validates independently of the client because "OpenSSH re‑parses remote args through the remote shell" (`mcp-design.md:109-111`).
- **Whitelist, not denylist:** "far stronger than a denylist" (`gen.py:234-236`). It also *constrains the design* — `schedule apply` takes no cron argv because `*`/`,` "would fail the shim char-allowlist" (`registry.py:645-647`).
- **Distinct id per override shape.** `reconcile_force`, `deploy_force`, `apply_secrets`, `apply_enable_change` exist "so an MCP client has to ASK for the override rather than inherit it, and so the audit line names which one ran" (`registry.py:242-244`); there is deliberately **no** combined secret+enable shape — "two overrides in one call is two decisions in one" (`:786-788`).
- **Token‑free reversibility.** `shutdown`/`resume` and `service shed`/`unshed` skip the confirmation token "so an unattended NUT low-battery hook can fire shutdown with no human in the loop" and so a degradation response can shed unattended (`registry.py:256-261`, `:382-390`).
- **Explicit targets over the seam.** `service rollback` requires `--to N`: "the default-to-previous convenience is CLI-interactive only, so automation must name its target" (`registry.py:567-570`).
- **Auto seam update trust tradeoff**, recorded verbatim in `seam_sync.py:10-14`: with auto ON "the trust anchor becomes the release channel plus this root-held policy file — not a human re-provision."
- **Break‑glass floor is deliberate.** `setup`, `syrvisctl restore` ("disaster recovery must not depend on the thing being recovered"), `doctor`/`clean`/`reset`, `--purge`, and VM adopt/create/delete stay off the seam (`seam-contract.md:57-63`, `vms-workload-design.md:102`).
- **home‑tech division of labor** (`home-tech-provisioning-requirement.md`): actuate *exclusively* via the seam/MCP — "One privileged seam" (§5 rule 1); dry‑run is side‑effect‑free *by construction* because the first, token‑less call of the handshake only plans (§5 rule 3); Terraform is confined to the Cloudflare plane; SyrvisCore "still learns nothing about domains, IPs, tokens, or the service catalog" (§6).

## Invariants & contracts

1. **Registry is authoritative.** Any argv shape not in `COMMANDS` is denied twice (sudo policy + shim). Clients are *generated consumers*.
2. **`--` precedes every user positional; flag values are server‑gated** (G7/G8).
3. **`syrvis` = wrapper (exports `SYRVIS_HOME` and self‑relocates with a loud warning, `manager/paths.py:375-395`); `syrvisctl` = venv path; only `install` gets `--path`.** No privileged command needs `SYRVIS_HOME=` on the wire.
4. **Read verbs never appear in sudoers**; sudo‑bearing read verbs are side‑effect‑free by construction.
5. **`--json` stability** is the contract with remote automation: `stack hostnames` is versioned (`version: 1`, additive fields don't bump), degrades to `{version:1, domain:null, traefik_ip:null, entries:[], error:…}`; `apply`/`deploy` bundles are `syrvis-instance/v1` / `syrvis-bundle/v1`; the argv service name is authoritative over the bundle's `service.name`.
6. **Secrets never on argv.** `read_json_stdin` caps bundles at 1 MiB, `secret set` at 64 KiB (`cli.py:81-98`, `:1135-1138`).
7. **`syrvisctl doctor` is the one read that answers with no resolvable home** — it runs from the SPK rootfs venv, imports nothing from the service package, exits 1 on findings, and callers "read the JSON, not the exit code" (`registry.py:206-215`).
8. **Boot‑hook contract integer** (`BOOT_HOOK_CONTRACT = 3`) is a cross‑package marker read out of the deployed text by `doctor.MIN_BOOT_HOOK_CONTRACT`.

## Gaps, debt & sharp edges

- **Committed artifacts are pinned to the `/volume1` default** (`gen.check` compares against `render_sudoers()`/`render_shim()` with no config). The real NAS runs on `/volume4`, so the drift test proves the *generator* is stable, not that the deployed policy matches. `DeployConfig`'s docstring admits the failure mode: wrong `--home` ⇒ "sudo denies every command."
- **`syrvisctl seam status` is not itself a seam verb.** It calls `ensure_privileges(/etc/sudoers.d/…)`, so seam drift can only be observed from a break‑glass login — the one thing the seam exists to avoid. `syrvisctl doctor` is on the seam but reports shells/boot hook, not policy drift.
- **Auto‑sync executes the newly activated version's generator as root, and that generator rewrites the policy constraining it.** A release that widens the seam widens it silently at `install` time. Rolling back *below* 0.4 raises `SeamSyncError("…artifacts left unchanged")` — the boundary then stays **wider** than the running version, i.e. it fails open on width.
- **`shim_path` is not cross‑checked against `authorized_keys`.** Editing it in the policy installs a shim at a new path while the key still forces the old one; nothing detects that.
- **Two coexisting privilege models.** `privilege.self_elevate` re‑execs `sudo SYRVIS_HOME=… <python> <argv>` (`privilege.py:57-61`) — an argv shape that is *not* in the sudoers policy. Harmless over the seam (which always prefixes `sudo -n`), but it means the CLI's own elevation path bypasses the enumerated model entirely for a local admin, and a partially‑consumed stdin before an `execv` would be a real hazard if the ordering ever changed.
- **Stdin bypasses every shim check** by design (`remote.py:275-279`) — validation of secret *content* is only the byte caps plus downstream schema. `remote.py` supports `_stdin` generically, so the "script-only, never MCP tools" rule is a convention, not an enforced invariant; adding a tool is one function away.
- **Duplicated constants:** `STACK_SERVICES`/`STACK_PRIMORDIAL` in the registry vs `syrviscore.stack` (bound only by `tests/test_seam_registry_drift.py`), MCP `_cli_regexes.py`, `MIN_BOOT_HOOK_CONTRACT` vs `BOOT_HOOK_CONTRACT`, and three copies of the seam‑shell heal.
- **`/etc/passwd` rewrite races.** Both the provision script and every boot heal edit `/etc/passwd` with awk/sed and no lock, against DSM's own writers.
- **`syrvis-reader` is half a citizen:** healed and doctored, never provisioned by `gen.py` — so in practice there is one privilege tier, and the "reader" tier lives only in a `parked/` patch.
- **Sudoers globs are much looser than the shim.** `service task --task * -- *` under sudo alone accepts any string; the real gate is the shim's `is_name`. Any second key on the operator account without a forced command collapses the boundary to the glob layer.
- **`ensure_config_tree_readable`** chgrps config/declarations to `docker` and adds group‑read — i.e. every docker‑group member can read L2 declarations; only `config/.env` is explicitly exempted (`privileged_ops.py:1436-1437`).

## Raw material worth citing

- G18: "the enumerated sudoers boundary, the shim allowlist, and the actual argv can never drift apart" (`registry.py:6-9`).
- "Character whitelist: only this safe alphabet may appear anywhere… far stronger than a denylist" — `[^A-Za-z0-9 ._@:/-]`.
- Provision verification triad: `ssh … 'id'` rejected by the shim; `sudo -n /bin/sh` denied by sudoers; `sudo -l` lists only the enumerated commands (`gen.py:614-619`).
- Trust anchor sentence: "with auto sync ON… the trust anchor becomes the release channel plus this root-held policy file — not a human re-provision."
- "Never fails the version switch — the seam is recoverable with `syrvisctl seam sync`, a failed activate is not."
- Incident 2026‑08‑16: a DSM share‑name collision renamed `/volumeN/syrviscore` → `syrviscore_1`, decapitating the wrapper, the seam, the cron heal and the startup script at once — "Nothing alarmed, because the alarm was stored inside the thing it was supposed to alarm about" (`privileged_ops.py:390-393`); ~50 minutes of silence.
- `SYNOCHECKSHARE_WAIT_S = 60`, "ADVISORY, never blocking: a boot hook that can stall a boot is a worse failure than the race it prevents."
- Numbers: 18 slot kinds; ~80 registry commands; 69‑line sudoers, 291‑line shim; timeouts 120 s default → 600 s for install/reconcile/deploy/task, 240 s shutdown, 300 s resume/recreate; caps 1 MiB bundle / 64 KiB secret; token TTL 300 s; default `--from 192.168.0.0/16`.
- Panel verdict recorded in `home-tech-provisioning-requirement.md:26`: declarative‑over‑MCP scored "10/10 on architectural fit precisely because it leans into the seam that already exists"; and §7's rule — "AI is an accelerant, never a dependency."