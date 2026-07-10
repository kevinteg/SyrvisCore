# SyrvisCore MCP Server — Implementation Spec (Phase 5)

Synthesized 2026-07-10 by a design workflow (4 understand readers → 3-way design
judge panel). This is the buildable spec; it takes the thin-adapter discipline as
the spine, the security-first privilege model (forced-command shim, purge
un-automatable, mandatory `--`), and the operability layer (ControlMaster reuse,
health cache, actionable errors).

## 0. Position in the system

The MCP server runs **on the operator's Mac**, not the NAS. It exposes SyrvisCore
operations as MCP tools to a Claude session (primarily home-tech). Each tool is a
near-mechanical projection of a `syrvis`/`syrvisctl` `--json` command executed on
the NAS over SSH. The CLI remains the single source of truth — the MCP never
reimplements logic, and it can never run arbitrary shell on the NAS.

The MCP **never elevates itself**. Privileged subcommands run under `sudo -n` on
the NAS, gated by an enumerated NOPASSWD sudoers list for a dedicated operator
user — never blanket root.

## 1. Project layout & dependencies

```
packages/syrviscore-mcp/                     # 3.10+ Mac-only; NEVER shipped to NAS/SPK
├── pyproject.toml                           # requires-python>=3.10; deps: fastmcp; dev: pytest
├── README.md                                # registration + NAS provisioning steps
├── src/syrviscore_mcp/
│   ├── __init__.py
│   ├── __main__.py                          # python -m syrviscore_mcp -> server.mcp.run() (stdio)
│   ├── server.py                            # FastMCP("syrviscore"); one @mcp.tool per CLI --json command
│   ├── tools.py                             # tool LOGIC (fastmcp-free, unit-testable)
│   ├── config.py                            # load/validate NASConfig from TOML + env
│   ├── remote.py                            # SSH runner: argv build, ControlMaster, health, run/classify
│   ├── validate.py                          # arg validators mirroring CLI regexes
│   ├── sandbox.py                           # managed-by-syrviscore membership checks
│   ├── tokens.py                            # HMAC confirmation-token mint/verify
│   ├── errors.py                            # typed error taxonomy -> actionable tool errors
│   └── _cli_regexes.py                      # VERSION_RE / NAME_RE / RESERVED_NAMES copies (drift-tested)
├── deploy/
│   ├── enumerated-commands.yaml             # SINGLE SOURCE for sudoers + shim allowlist
│   ├── gen.py                               # renders sudoers + shim from the yaml
│   ├── sudoers.d/syrviscore-mcp             # generated
│   └── ssh/syrvis-mcp-shim                  # generated forced-command shim
└── tests/                                   # offline (3.12) bulk + @pytest.mark.nas opt-in
```

Framework: `fastmcp`, stdio transport, Python 3.12 venv on the Mac. No runtime
import of `syrviscore`/`syrviscore-manager` (those target 3.8 / the NAS) — the
CLI regexes are **copied** into `_cli_regexes.py` and pinned identical by a drift
test. Separate 3.12 CI job; excluded from the 3.8 SPK matrix.

## 2. Tool list (23 tools)

Return type is always `dict`. Booleans like `--purge`/`--force`/`--no-verify` are
NEVER model-supplied — set by tool logic only. `syrvis` always via the wrapper;
`syrvisctl` always with `--path <home>`.

### Read-only (readOnlyHint; no sudo, no token)
| Tool | Signature | CLI |
|---|---|---|
| `status` | `status()` | `syrvis status --json` |
| `verify` | `verify(smoke=False)` | `syrvis verify [--smoke] --json` (rc==1-with-JSON = healthy return) |
| `service_list` | `service_list()` | `syrvis service list --json` |
| `logs` | `logs(service=None, tail=100)` | `syrvis logs [-- <service>] -n <tail>` → `{lines:[...]}`; `--follow` never exposed |
| `versions_list` | `versions_list()` | `syrvisctl list --json --path <home>` |
| `check_updates` | `check_updates()` | `syrvisctl check --json` |
| `info` | `info()` | `syrvisctl info --json --path <home>` |
| `backup_list` | `backup_list()` | `syrvisctl backup list --json --path <home>` |
| `cleanup_preview` | `cleanup_preview(keep=2)` | `syrvisctl cleanup --keep <keep> --dry-run --path <home>` |

### Privileged, non-destructive (sudo -n; no token)
| Tool | Signature | CLI |
|---|---|---|
| `start` | `start()` | `sudo -n syrvis start` |
| `stop` | `stop()` | `sudo -n syrvis stop` |
| `restart` | `restart()` | `sudo -n syrvis restart` |
| `verify_fix` | `verify_fix(smoke=False)` | `sudo -n syrvis verify [--smoke] --fix --json` |
| `service_start` | `service_start(name)` | `sudo -n syrvis service start -- <name>` (membership-checked) |
| `service_stop` | `service_stop(name)` | `sudo -n syrvis service stop -- <name>` (membership-checked) |
| `service_update` | `service_update(name)` | `sudo -n syrvis service update -- <name>` (membership-checked) |
| `install` | `install(version=None)` | `sudo -n syrvisctl install [<version>] -y --path <home>` (additive only; no --force/--clean/--wheel/--no-verify) |
| `service_add` | `service_add(git_url)` | `sudo -n syrvis service add -- <git_url>` (strict git-URL allowlist) |

### Privileged + destructive (destructiveHint; sudo -n; require confirmation token)
| Tool | Signature | CLI |
|---|---|---|
| `activate` | `activate(version, confirm="")` | `sudo -n syrvisctl activate -- <version> --path <home>` |
| `rollback` | `rollback(version=None, confirm="")` | `sudo -n syrvisctl rollback [<version>] -y --path <home>` |
| `uninstall` | `uninstall(version, confirm="")` | `sudo -n syrvisctl uninstall -- <version> -y --path <home>` |
| `cleanup` | `cleanup(keep=2, confirm="")` | `sudo -n syrvisctl cleanup --keep <keep> -y --path <home>` |
| `service_remove` | `service_remove(name, confirm="")` | `sudo -n syrvis service remove -- <name> -y` (membership-checked, no --purge) |

**`--purge` is NOT exposed** — data deletion against the weak-DR NAS is intentionally
un-automatable in v1 (human-over-SSH only). **`restore`, `reset`, `clean`, `setup`,
`doctor`, `config show`, `compose generate`, and the legacy `syrvis update *` group
are NOT exposed.**

## 3. Injection-safe SSH + sudo execution (remote.py)

Core rule: **no shell string is ever built.** `subprocess.run(argv, shell=False)`.

```
BASE_SSH = ssh -F <ssh_config> -T -o BatchMode=yes -o ConnectTimeout=10
           -o StrictHostKeyChecking=yes -o ControlMaster=auto
           -o ControlPath=<cp> -o ControlPersist=60 <ssh_target>
```
- `syrvis` binary = the wrapper (sets SYRVIS_HOME); `syrvisctl` = venv path + `--path <home>` (argv flag survives sudo env_reset).
- `sudo -n` prepended for privileged tools.
- Two mandatory mitigations because OpenSSH re-parses remote args through the remote shell:
  1. **Allowlist-first validation** (§6) guarantees every user token matches a benign charset; `remote.py` then `shlex.quote`s each token — quoting only ever wraps already-benign strings.
  2. **Forced-command shim on the NAS** (§7) independently re-validates `$SSH_ORIGINAL_COMMAND`.
- `--` inserted before every user positional.
- ControlMaster multiplexing; lazy `health()` probe (cached 30s); per-tool timeout (120s default, install/service_add 600s); timeout on a non-idempotent op returns `indeterminate=True`.

### Result classification
```
rc == 255            -> NetworkError / AuthError / HostKeyError (parse stderr)
rc == 127            -> ConfigError(binary_missing)
"password is required"/"terminal is required" -> PrivilegeError(nopasswd_misconfigured)
"not allowed"/"sudoers"                        -> PrivilegeError(not_enumerated)
expect_json: json.loads(out) succeeds even at rc==1 for verify -> return dict (honor {healthy:false})
             else ProtocolError(non_json) if rc==0 else CliError(err, rc)
rc != 0              -> CliError(err, rc)
```
Every error carries the redacted argv sent + actionable operator text.

## 4. Config (~/.config/syrviscore-mcp/config.toml)

```toml
[nas]
host = "192.168.8.3"                      # env SYRVISCORE_NAS_HOST wins
ssh_target = "syrvis-nas"                 # Host alias in ssh_config_file
ssh_config_file = "~/.config/syrviscore-mcp/ssh_config"
control_path = "~/.config/syrviscore-mcp/cm-%r@%h:%p"
command_timeout_s = 120

[layout]                                  # dev/prod switch
profile = "prod"                          # "dev" | "prod"
syrvisctl_path = "/var/packages/syrviscore/target/venv/bin/syrvisctl"
syrvis_wrapper = "/volume1/syrviscore/bin/syrvis"     # ALWAYS the wrapper
syrvis_home = "/volume1/syrviscore"                   # passed as --path

[privilege]
use_sudo = true
sudo_binary = "sudo"

[safety]
managed_marker = "syrviscore"
environment = "production"
git_url_allowed_hosts = ["github.com"]

[tokens]
secret_env = "SYRVISCORE_MCP_TOKEN_SECRET"
ttl_s = 300
```
Validation on load: profile ∈ {dev,prod}; layout paths absolute; ssh_user ≠
root/cerebrate; key file mode 0600. Dev profile substitutes operator-home paths and
is **absent from sudoers** (privileged tools physically cannot reach the dev tree).

## 5. Sudoers (generated from deploy/enumerated-commands.yaml)

Installed `/etc/sudoers.d/syrviscore-mcp`, `0440 root:root`, `visudo -cf`-validated.
Home carried by wrapper/`--path` (zero `SYRVIS_HOME=`), strict `env_reset`, no purge,
mandatory `--` before value args. Trailing `*` is safe because sudo runs a single
`execve` (never a shell): `activate 0.1.0; reboot` becomes argv
`["activate","0.1.0;","reboot"]`, rejected by `validate_version`. **Never add:**
`restore`, `install --wheel`, `--no-verify`/`--force`/`--clean`, `--purge`, `clean`,
`reset`, `setup`, bare `syrvis */syrvisctl *`, `/bin/sh`, `docker`, `git`.

## 6. Guardrails (G1–G18)

| # | Guardrail | Enforcement |
|---|---|---|
| G1 | No shell string built anywhere | remote.py argv-only; grep test |
| G2 | version = `^v?\d+\.\d+\.\d+$` | validate.py |
| G3 | name = `^[a-z0-9][a-z0-9_-]{0,63}$` and ∉ RESERVED_NAMES | validate.py |
| G4 | git_url: `^https://` / `^git@host:` / `^ssh://` only; reject file://,http://,ext::,fd::,leading-`-`,bare `.git`; host allowlist | validate.py |
| G5 | int bounds: 1≤tail≤10000, 0≤keep≤50 | validate.py |
| G6 | reject NUL/CR/LF/`;`/backtick/`$`/`\|`/`&`/`<`/`>`/`(`/`)`/ws, len>256 | validate.py + remote.py assert |
| G7 | `--` before every user positional | build_remote |
| G8 | Fixed flags never model-derived | tool bodies (constants) |
| G9 | Membership pre-check before target-taking mutators + `logs <svc>` | sandbox.py |
| G10 | Managed-by = compose project `syrviscore`/`syrvis-<name>` | sandbox.py |
| G11 | Destructive tools require valid HMAC token | tokens.py + tool body |
| G12 | NOPASSWD enumerated; mandatory `--`; no wildcard verb; no purge/restore/wheel | sudoers + visudo CI |
| G13 | Forced-command shim re-validates $SSH_ORIGINAL_COMMAND | shim |
| G14 | SSH key: restrict + command= + from=, no PTY/forward/SFTP | authorized_keys |
| G15 | Host key pinned; mismatch aborts | StrictHostKeyChecking=yes |
| G16 | Audit log per call | remote.py → audit.jsonl |
| G17 | Validator drift guard: MCP regexes == CLI source regexes | test_drift.py |
| G18 | Single-source enumeration: sudoers + shim generated from one yaml | gen.py + CI diff |

## 7. Forced-command shim + SSH key

`authorized_keys` (operator user, 0600):
```
restrict,command="/usr/local/bin/syrvis-mcp-shim",from="192.168.8.0/24" ssh-ed25519 AAAA… syrvis-mcp
```
`/usr/local/bin/syrvis-mcp-shim` (POSIX sh): reject any `$SSH_ORIGINAL_COMMAND`
with `; & | \` $ ( ) < > newline`; word-split; match the whole invocation against
the enumerated `(binary, subcommand, flag-shape)` set; re-validate the terminal
value; `exec "$@"` or `exit 1`.

## 8. Confirmation token (tokens.py)

Two-call handshake for `activate`, `rollback`, `uninstall`, `cleanup`,
`service_remove`:
1. Empty/invalid `confirm` → run a **read-only plan** (resolve target + current
   state via RO tools), no mutation, return `{plan, token, expires_at}` where
   `token = HMAC(secret, tool ‖ normalized_args ‖ target_state_hash ‖ nonce ‖ expiry)`.
   `target_state_hash` covers only the **affected subtree**.
2. Second call echoes the token → recompute, constant-time compare, check TTL
   (300s), single-use nonce, re-verify affected-subtree state (TOCTOU void) →
   only then mutate.

Token binds args+target (an `activate 0.2.0` token can't authorize
`activate 0.1.5`). Server-minted; secret is per-process random (restart voids
outstanding tokens).

## 9. `--json` gaps

- **Already closed:** `syrvis status`/`service list`/`verify --json`, manager
  `list`/`check`/`info`/`backup list`.
- **Remaining — manager mutators** (`install`/`activate`/`rollback`/`uninstall`/
  `cleanup` print human text): either (a) ship-now = `expect_json=False`, treat
  rc==0 as success, return a synthesized `{ok, action, args, detail}` then
  follow up with a `versions_list` read; or (b) add `--json` to those five
  `syrvisctl` commands (typed results from the `(success, message)` tuples the
  managers already return). This spec ships (b) — it keeps the thin-adapter
  purity.
- **Companion CLI hardening:** `service_manager._clone_service` →
  `git clone --depth 1 -- <url>` with `GIT_ALLOW_PROTOCOL=https:git:ssh`;
  tighten `_is_git_url` to drop `file://`/`http://`/bare `.git`.

## 10. Test plan

Unit/offline (3.12 CI, the bulk): `test_validate.py` (injection corpus →
validation error, zero SSH), `test_remote_argv.py` (fake Runner records exact
argv; byte-for-byte per tool: `sudo -n`, `--`, `--path`, wrapper-vs-venv,
fixed flags), `test_classify.py` (canned rc/out/err incl. verify rc==1+JSON),
`test_tokens.py` (no-token→plan-only; wrong/expired/replayed rejected; drift
voids), `test_sandbox.py` (unmanaged/reserved refused pre-SSH), `test_sudoers.py`
(`visudo -cf`; no wildcard verb; `--` present; no purge/restore/wheel),
`test_shim.py` (run shim under sh with crafted `$SSH_ORIGINAL_COMMAND`),
`test_drift.py` (MCP regexes == source regexes; sudoers+shim == yaml).

NAS-integration (`@pytest.mark.nas`, opt-in): RO tools over real SSH; sudoers
boundary denials; key least-privilege; end-to-end service_add → verify →
token-gated service_remove.

## 11. Build order

1. Scaffold package (pyproject, 3.12 CI, exclude from SPK).
2. `enumerated-commands.yaml` + `gen.py` → sudoers + shim.
3. `config.py` + TOML/ssh_config schema + startup validation.
4. `_cli_regexes.py` + `validate.py` + drift/validate tests.
5. `remote.py` (argv, ControlMaster, classify, health) + argv/classify tests.
6. `server.py`/`tools.py` read-only tools (9) + `sandbox.py` + tests.
7. `tokens.py` + destructive tools + tests.
8. Privileged non-destructive tools; `--json` on the five manager mutators.
9. `test_sudoers.py`/`test_shim.py`; audit log.
10. `service_manager` git-clone hardening.
11. home-tech `.mcp.json` registration + operator skills.
12. NAS integration suite (dev then prod).

## 12. MUST verify on the real NAS before Phase 5 is "done"

1. Dedicated `syrvis-operator` user with docker access; `sudo -l` matches the
   enumerated allowlist **exactly**.
2. `/etc/sudoers.d/syrviscore-mcp` 0440 root:root, passes `visudo -c`;
   `sudo -n /bin/sh`, `docker ps`, `git`, `restore`, `install --wheel`, `reset`,
   `service remove … --purge` all **denied**.
3. `sudo -n syrvis service stop -- <valid>` succeeds; `… stop <name> --purge` denied.
4. `authorized_keys` forced-command shim installed; `ssh syrvis-nas 'id'` and any
   off-list command rejected; PTY/port-forward/SFTP fail.
5. Host key pinned; mismatch aborts (DSM-reboot recovery = documented manual
   known_hosts update).
6. SYRVIS_HOME survives sudo via wrapper (service) + `--path` (manager); no
   privileged command needs `SYRVIS_HOME=`.
7. `syrvis verify --json` returns valid JSON at **rc==1** when unhealthy.
8. Confirm the real production volume (`/volume1` vs other); regenerate
   sudoers/shim/config from `syrvis_home`/`syrvis_wrapper` if not `/volume1`.
9. Dev `SYRVIS_HOME` tree is **unreachable** by privileged tools.
10. End-to-end acceptance through the MCP: `status` → `service_add` → `verify`
    green → token-gated `service_remove`.
11. ControlMaster socket reuse works over DSM sshd (no SFTP dependency); confirm
    `scp` is never invoked by the MCP.
12. Manager mutators return correct `{versions, active}` after a real
    `install`/`activate`/`rollback` (either via the `--json` change or the
    ship-now synthesized path).
