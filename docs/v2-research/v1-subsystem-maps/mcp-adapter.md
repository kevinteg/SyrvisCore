# The MCP Server Adapter — remote operator tooling over the seam

## Purpose & role in the system

`packages/syrviscore-mcp` is the **third adapter** over SyrvisCore's deterministic library layer (CLI and dashboard being the others). It runs **on the operator's Mac, never on the NAS** (`pyproject.toml`: `requires-python = ">=3.10"`, comment "Runs on the operator's Mac, NOT the NAS — free of the DSM 3.8 constraint"), and exposes SyrvisCore verbs to a Claude session as typed MCP tools. Every tool is a near-mechanical projection of one `syrvis`/`syrvisctl --json` invocation executed over SSH as a dedicated least-privilege operator account.

The framing sentence is in `docs/mcp-design.md` §0: *"The CLI remains the single source of truth — the MCP never reimplements logic, and it can never run arbitrary shell on the NAS."* And: *"The MCP **never elevates itself**."* Everything else in the subsystem is machinery for keeping those two claims true against an adversary who controls the model's input.

## Key modules and files (path — role — approx size)

- `packages/syrviscore-mcp/src/syrviscore_mcp/server.py` — FastMCP surface: 50 `@mcp.tool` functions, annotation hints, `_call` error funnel — 462 lines
- `.../tools.py` — the fastmcp-free tool *logic*: validation, sandbox checks, confirmation handshake, follow-up reads — 574 lines
- `.../remote.py` — argv construction, ssh transport, stdin plumbing, result classification, audit log — 303 lines
- `.../validate.py` — the injection boundary: per-kind allowlist validators (G2–G6) — 325 lines
- `.../config.py` — `NASConfig` TOML loader + fail-closed startup validation — 226 lines
- `.../tokens.py` — HMAC two-call confirmation tokens (G11) — 99 lines
- `.../sandbox.py` — managed-service membership checks (G9/G10) — 40 lines
- `.../errors.py` — typed `McpError` taxonomy → structured tool errors — 104 lines
- `.../_cli_regexes.py` — verbatim copies of the CLI regexes, drift-pinned (G17) — 50 lines
- `.../commands.py` — 31-line **back-compat shim**: re-exports `syrviscore.seam.registry`
- `.../deploy/gen.py` — 42-line back-compat launcher for `syrviscore.seam.gen`
- `packages/syrviscore/src/syrviscore/seam/registry.py` — **the real command registry**, 77 `Command`s (59 sudo, 27 read-only, 11 destructive) — 32 KB
- `packages/syrviscore/src/syrviscore/seam/gen.py` — renders sudoers + shim + provision script from the registry — 32 KB
- `packages/syrviscore-mcp/deploy/{sudoers.d/syrviscore-mcp, ssh/syrvis-mcp-shim}` — committed generated artifacts (69 / 291 lines), drift-tested
- `tests/` — 12 files, ~2.5k lines, all offline
- `docs/mcp-design.md` (290 lines), `docs/seam-contract.md`, `docs/archives/mcp-security-review-2026-07.md` (58 lines)

## How it actually works

**Tool → verb mapping.** `server.py` is a projection table: `status()` → `tools.status(ctx)` → `_run(ctx, "status")` → `get_command("status")` → `Command("status","syrvis",["status"],read_only=True,flags=["--json"])`. Tools carry MCP annotation hints — `readOnlyHint` (17 tools), `destructiveHint` (11), `idempotentHint`, `openWorldHint` for anything reaching GitHub/a registry/a git host. `test_server.py` pins the exact set: `assert names == EXPECTED_TOOLS; assert len(EXPECTED_TOOLS) == 50` and asserts the hints per class.

**Argv construction (`remote.build_remote_tokens`).** Order is fixed and mirrored by the shim generator:

```
[sudo, -n]? , <binary>, *subcommand, *flags(literals | FlagValue→validated int), [--path <home>]?, [--, <positional>]?
```

`binary` is `cfg.syrvis_wrapper` for `syrvis` (the wrapper exports `SYRVIS_HOME`, surviving sudo's `env_reset`) and `cfg.syrvisctl_path` for the manager; only `install` gets `--path <home>` (registry docstring: *"Only `install` accepts --path; the others resolve SYRVIS_HOME via the single-install volume scan"*). Every user positional lands after a literal `--`. Comment at `remote.py:99`: *"Every user value was metachar-checked by its kind validator (resolve_slot); the remaining tokens are our own trusted literals."*

**Transport.** `base_ssh()` is fixed:

```
ssh -F <ssh_config> -T -o BatchMode=yes -o ConnectTimeout=10 -o StrictHostKeyChecking=yes
    -o ControlMaster=auto -o ControlPath=<cp> -o ControlPersist=60 <ssh_target>
```

`build_ssh_argv` joins the validated tokens with `shlex.quote` into ONE remote-command string; `subprocess.run(argv, shell=False, capture_output=True, text=True, timeout=command.timeout_s, input=stdin_data)`. Because ControlPersist=60 leaves a 60-second authenticated socket, `RemoteRunner.__init__` `chmod 0700`s the ControlMaster directory (*"A ControlMaster socket is 60s of authenticated NAS access; keep its directory owner-only so a co-resident local process can't reuse it."*).

**Injection defenses, four layers.**
1. `validate.py` allowlist-first per kind, plus `_reject_metachars`: `_FORBIDDEN_CHARS = set("\x00\r\n;\`$|&<>()!*?{}[]\\'\" \t")`, `_MAX_LEN = 256`, and a leading-`-` refusal.
2. `--` before every user positional.
3. `shlex.quote` on already-benign tokens.
4. The NAS-side forced-command shim re-validates `$SSH_ORIGINAL_COMMAND` independently: a **character whitelist** (`grep -q '[^A-Za-z0-9 ._@:/-]'` → reject), an embedded-newline check via `wc -l`, `set -f` (no globbing), `set -- $cmd`, then an exact-arity/literal/per-kind-regex match per enumerated shape, ending in `exec "$@"`. Shim predicates duplicate `validate.py` bounds exactly (`is_keep` 0–50, `is_tail` 1–10000, `is_port` 1–65535, `is_revision` 1–999999).

`_cli_regexes.py` copies the CLI regexes rather than importing them (*"The MCP server must NOT import the syrviscore/syrviscore-manager packages (they target Python 3.8 / the NAS and pull in docker)"*) and compiles them with `re.ASCII` so `\d` cannot match Unicode digits — the pattern *string* stays identical so `test_drift.py` (which regex-scrapes the source files) still passes.

**Fail-closed allowlists.** `validate_git_url` and `validate_image` treat an empty allowlist as *disabled*, never *any*: *"an empty/unset list means 'disabled', never 'allow any host'"*. Images must be registry-qualified AND pinned (no `:latest`, or a `@sha256:` digest). `load_config` additionally refuses to start in production with an empty `git_url_allowed_hosts`.

**Sandbox.** `sandbox.assert_service_managed(runner, name)` refuses `RESERVED_NAMES` (`traefik, portainer, cloudflared, proxy, syrvis-macvlan, deployments, state`) outright, then runs the unprivileged `service list --json` and requires membership. Applied to `logs(service)`, `service_start/stop/update/remove/adopt/task/set_image/rollback`.

**Confirmation handshake (`tokens.py`).** Token = `<hmac_sha256>.<nonce>.<exp>` over `tool | json.dumps(args,sort_keys) | state_hash | nonce | exp`. Call 1 (no `confirm`) gathers a read-only plan + the affected-subtree state, mints, returns `{needs_confirmation, plan, confirm_token, expires_at, note}` and mutates nothing. Call 2 re-reads state, recomputes, `hmac.compare_digest`, TTL (default 300s), then consumes the nonce **under `ctx.nonce_lock`**. `ToolContext.__post_init__` mixes a per-process salt: `self.secret = blake2b(self.secret, salt=os.urandom(16), digest_size=32).digest()` — so a restart voids every outstanding token. Bound state differs per tool: `activate` binds the version list; `reconcile_prune` binds the CLI's own dry-run plan; `backup_cleanup` binds the backup list.

**Result classification (`remote.classify`).** `rc==255` → stderr substring match → `HostKeyError` / `AuthError` / `NetworkError`; `rc==127` → `ConfigError("remote binary not found")`; `"password is required"/"terminal is required"/"askpass"` → `PrivilegeError(NOPASSWD misconfigured)`; `"not allowed"` or `sudo`+`sudoers` → `PrivilegeError("this operation is intentionally outside the MCP boundary")`. With `expect_json`, `json.loads(stdout)` is honored **even at rc==1** (*"verify emits valid JSON even at rc==1 (unhealthy) — honor the dict"*); otherwise `ProtocolError` at rc==0 or `CliError(msg, rc)`. Non-JSON success returns `{"ok": True, "detail": out.strip()[-2000:]}`. A timeout on a non-read-only command raises `NetworkError` with `detail="indeterminate"` and the hint *"run 'verify' or 'status' before retrying — the operation state is unknown"*.

**Audit log.** `~/.config/syrviscore-mcp/audit.jsonl`, appended per call as `{"command", "sudo", "remote":[tokens], "rc", "outcome"}`; rejected calls (validation/sandbox/token) get `{"command", "args", "rc":null, "outcome":<ErrorClass>, "rejected":true}` via `RemoteRunner.audit_event`. The file is created with `os.open(..., 0o600)` — the comment names the reason: *"chmod-after-create leaves a umask window where the file is briefly world-readable"* — and auditing is wrapped so it "must never block an operation".

**Stdin plane.** `RemoteRunner.run` pops `_stdin` **before** `build_remote_tokens` so a secret can never become an argv token, land in `remote_tokens`, or hit the audit log; it is passed as `input=` to subprocess. This carries `secret set`, `config set`, `deploy` (`syrvis-bundle/v1`) and `apply` (`syrvis-instance/v1`). Crucially, **no MCP tool exposes these** — `docs/seam-contract.md` classes them *"Stdin writers (sudo; **script-only, never MCP tools**) … secrets never touch argv/ps/logs, and never transit an LLM context"*. The `deploy/` subpackage in the MCP is now only a 42-line launcher: bundle *building* lives in the deployment repo, and the MCP contributes only the transport primitive plus the enumerated shapes.

**Config.** `~/.config/syrviscore-mcp/config.toml` (`SYRVISCORE_MCP_CONFIG` override) with `[nas] [layout] [privilege] [safety] [tokens]`. Startup validation: `profile ∈ {dev,prod}`; all layout paths absolute; `environment` from a known set; ssh User for the target parsed out of the ssh_config and refused if in `{root, admin} ∪ safety.forbidden_ssh_users`. `token_secret()` resolves env var → 0600 secret file (refused if `st_mode & 0o077` or not owned by the caller) → ephemeral `os.urandom(32)` in non-production → `ConfigError` in production.

## Design decisions & their rationale

- **G18, single-source enumeration.** `commands.py`: *"The single source of truth for every remote command a seam client may run is `syrviscore.seam.registry` … The MCP is a generated consumer of that registry."* The sudoers policy, the shim allowlist and the runtime argv all derive from one list, and `test_drift.py` asserts the committed artifacts equal the generator's output.
- **Registry lives in the *platform*, not the MCP.** That relocation is what lets `syrvisctl activate/rollback` re-render the boundary from the newly-active version (`seam_sync.py`: *"new verbs arrive with the release that implements them, and a rollback narrows the seam back with it"*), with the trust tradeoff written down: *"the trust anchor becomes the release channel plus this root-held policy file — not a human re-provision."*
- **Distinct ids for each override shape.** `reconcile_force`, `deploy_force`, `apply_secrets`, `apply_enable_change` exist as separate registry entries because *"the override must be asked for, not inherited, and the audit line must say which one ran"*, and *"there is deliberately NO combined secret+enable shape: two overrides in one call is two decisions in one."*
- **Token-free reversible verbs.** `shutdown`/`resume` and `service shed/unshed` skip the handshake: *"an unattended NUT low-battery hook must be able to fire `shutdown --reason ups`."*
- **Deliberate non-exposure.** `--purge`, `restore`, `reset`, `clean`, `setup`, `doctor`, `install --wheel` are off the seam — *"data deletion against the weak-DR NAS is intentionally un-automatable in v1"* and *"disaster recovery must not depend on the thing being recovered."* `test_deploy.py::test_sudoers_has_no_dangerous_entries` enforces it, and `test_force_is_confined_to_the_two_degraded_overrides` pins `--force` to exactly two lines.
- **Thin adapter, ground-truth follow-ups.** Manager mutators lack `--json`, so `_with_version_state` / `_with_service_state` / `_with_backup_state` re-read after the mutation rather than synthesizing state MCP-side — the adapter reports, never computes.

## Invariants & contracts

1. **No shell string is ever built by the MCP** (G1); `test_remote_argv.py::test_source_has_no_shell_true_or_string_ssh` greps for it.
2. **Every user value is validated before it can reach ssh**; `resolve_slot` is the only path from `args` into tokens.
3. **Argv order in `build_remote_tokens` == `_shim_token_specs` order** — the generator comment says *"The order mirrors remote.build_remote_tokens exactly so the shim matches the real argv."* Break one and the NAS denies everything.
4. **`_stdin` never appears in argv, audit, or tokens.**
5. **Regex parity with the CLI** (G17) and **artifact parity with the registry** (G18).
6. **`RESERVED_NAMES` and `STACK_SERVICES/STACK_PRIMORDIAL` are duplicated with drift tests**, not imported, so the generator stays stdlib-only.
7. **Redaction at the source**: `export` and `history` are always redacted server-side — the MCP relies on that rather than filtering.
8. Downstream, `home-tech` consumes the seam by the same registry shapes; the dashboard and CLI must keep `--json` payload shapes stable because `classify` passes them through untouched.

## Gaps, debt & sharp edges

- **`_KIND_VALIDATORS` is missing two registry kinds.** The registry defines 18 slot kinds; `remote.py:36` maps 16. `git_rev` and `sha256_digest` (design/66, used by `schedule_sync_pin`) have no entry, so `resolve_slot` would raise a bare `KeyError` — not a `ValidationError` — escaping `_call`'s `except McpError` as a raw traceback. No test asserts kind coverage. This is exactly the drift class G17/G18 were built to prevent, in the one table nobody generated.
- **The adapter has fallen behind the registry.** 77 registry commands vs 50 MCP tools. `vm_list/status/start/stop/restart`, `service_shed/unshed/recreate`, `schedule_dsm_tasks`, `syrvisctl doctor`, `images`, `dashboard_generate`, `stack_hostnames_full`, `reconcile_force`, `deploy_force` are all enumerated on the sudoers/shim boundary — i.e. *already permitted for the operator key* — but unreachable as tools. The enforcement boundary is now strictly wider than the tool surface.
- **`_call` calls `get_context()` outside its `try`.** A `ConfigError` from `load_config()`/`token_secret()` escapes unstructured, breaking the module docstring's promise that *"McpErrors are surfaced as structured error dicts (never a raw traceback)"* — and it is the *most likely* first-run failure.
- **The health probe is vestigial.** `self._health_ok` / `self._health_at` are initialized and never read; `docs/mcp-design.md` §3 promises a *"lazy `health()` probe (cached 30s)"* that does not exist.
- **`cfg.command_timeout_s` is dead.** It is parsed and stored but `_exec` always uses `command.timeout_s` from the registry. An operator tuning the config knob changes nothing.
- **`_parse_ssh_user` fails open.** It matches `Host` lines by exact string equality — a multi-alias `Host syrvis-nas nas`, a pattern `Host syrvis-*`, an `Include`, or a `Match` block yields `ssh_user=None`, and the `root`/`admin`/`forbidden_ssh_users` refusal is silently skipped. The one check that keeps the MCP off a human account is a naive line scanner.
- **Version drift inside the package**: `__init__.__version__ = "0.2.0"` vs `pyproject` `version = "0.3.0"`.
- **Audit lines have no timestamp**, no rotation, and no size bound. For a file explicitly described as *"a security record"*, ordering-only forensics is thin, and rejected-call entries embed raw `kwargs`.
- **`used_nonces` grows without bound** for the process lifetime; the global `_ctx` creation in `get_context()` is unlocked, so two concurrent first calls can race and build two contexts with different salts (tokens minted by one are then unverifiable by the other).
- **Error classification is stderr substring matching.** `"not allowed"`, `"permission denied"`, `"host key"` — locale-, sshd- and sudo-version-dependent; a phrasing change silently reclassifies a privilege denial as a generic `CliError`.
- **`logs` contradicts its own spec.** `docs/mcp-design.md` §2 promises `{lines:[...]}`; the implementation returns `{"ok":True,"detail": out[-2000:]}`, so `tail=10000` is validated, sent, and then truncated to the last 2000 characters client-side with no indication.
- **Two SSH round trips per sandboxed call minimum** (`service list` for membership, then the verb, then often a follow-up read). ControlMaster hides the cost; the membership snapshot is also a TOCTOU window against the actual mutation.
- **`validate_cron_spec` is dead code** by design (documented as "IF a cron value is ever handled MCP-side"), and `validate_stack_service`'s `disable=` guard is only reachable from `tools.py`, not from the slot table — so a hypothetical direct registry call could pass a primordial name to `stack_disable`'s slot.
- **Test blind spots**: no test asserts every registry kind has a validator; no test asserts the MCP tool set is a subset of the registry, nor that registry growth is deliberate; `test_deploy.py`'s shim harness neuters `exec "$@"`, so the real exec path is never exercised; and the NAS-integration suite (`@pytest.mark.nas`, `docs/mcp-design.md` §12's 12-point checklist) is opt-in and not part of any gate.

## Raw material worth citing in the retrospective

- Spec provenance: *"Synthesized 2026-07-10 by a design workflow (4 understand readers → 3-way design judge panel)"* — 23 tools planned, 50 shipped, against a 77-verb registry.
- Security review (July 2026): *"23 candidate findings, 17 verified, **10 confirmed**. All 10 are now fixed."* Verdict: *"No critical, no unauthenticated RCE, no forgeable confirmation token, no sandbox/managed-by escape. The layered model … held."* Suite grew 159 → 180 tests.
- F1/F2 — fail-open git allowlist → fail-closed validator + production startup refusal + `destructiveHint` + token. F3 — *"Shim `exec $cmd` unquoted → word-split + glob re-injection"*, fixed by the *"precise argv matcher: charset whitelist + `set -f` + `set -- $cmd` + exact-argc/per-token validation + `exec \"$@\"`"*. F7 — nonce check-then-add race → `nonce_lock`. F8 — replay across restart → per-process blake2b salt. F9 — *"G16 was false"* until rejected calls were logged.
- Accepted residual risk: *"A **stolen operator SSH key** is inherently powerful (it can invoke every enumerated command legitimately)… F3–F6 mattered because they *widened* that surface."* Also: *"Reserved-core-name protection is a hand-maintained frozen list … acceptable for MVP, tech debt to derive from the live stack later."*
- Sudoers wildcard reasoning (`docs/mcp-design.md` §5): *"Trailing `*` is safe because sudo runs a single `execve` (never a shell): `activate 0.1.0; reboot` becomes argv `[\"activate\",\"0.1.0;\",\"reboot\"]`, rejected by `validate_version`."*
- `authorized_keys` line: `restrict,command="/usr/local/bin/syrvis-mcp-shim",from="192.168.1.0/24" ssh-ed25519 …`.
- Seam contract: *"Both artifacts are **generated** from the platform's verb registry … so the enforcement boundary and the runtime argv can never drift."*
- Numbers: 50 MCP tools / 77 registry commands (59 sudo, 27 read-only, 11 destructive); 69-line sudoers, 291-line shim; token TTL 300s; timeouts 120s default, 600s for install/service_add/deploy; `_MAX_LEN` 256; tail 1–10000, keep 0–50, revision 1–999999; audit + ControlMaster dirs 0700, audit file 0600.