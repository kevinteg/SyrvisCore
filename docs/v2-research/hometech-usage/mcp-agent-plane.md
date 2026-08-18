I have what I need. Writing the inventory.

# MCP + Agent Permission Plane (LLM-session usage)

## What this slice is

The channel by which a Claude Code session working in `~/code/home-tech` touches the RS1221+. It has four sub-channels, only two of which are load-bearing in practice:

1. **The `syrviscore` MCP server** — declared in `/Users/kevinteg/code/home-tech/.mcp.json`, stdio, launched as `/Users/kevinteg/.venvs-syrviscore-mcp/bin/python -m syrviscore_mcp` with `SYRVISCORE_MCP_CONFIG=/Users/kevinteg/.config/syrviscore-mcp/config.toml`. Enabled via `enabledMcpjsonServers: ["syrviscore"]` in `.claude/settings.local.json`. It wraps the same forced-command SSH seam the Bash channel uses; it is not a second transport to the box, it is a typed façade over the same shim.
2. **Raw Bash through the operator seam** — `ssh -F ~/.config/syrviscore-mcp/ssh_config syrvis-nas -- '[sudo -n] /volume4/syrviscore/bin/syrvis <verb> --json'`. `CLAUDE.md` names this "the only sanctioned NAS path", and `.claude/skills/deploy/SKILL.md` §"Seam call shape" teaches the exact argv. This is the channel the doctrine actually pushes agents toward.
3. **Dashboard HTTP** — `curl -sk -H 'Host: dash.konsume.org' https://192.168.8.4/api/services`, allowlisted verbatim, and consumed by `scripts/accept` (`scripts/accept:107`) because `/api/services` is the only surface carrying `RestartCount`.
4. **Break-glass `ssh cerebrate@ds`** — deliberately unreachable by the agent; eleven deny globs, owner-only, pages on every login.

The enforcement substrate is `.claude/settings.local.json` (gitignored, per-machine), whose tracked contract is `config/agent-permissions.yaml`, whose ruling is `design/66-agent-permission-plane.md` (landed 2026-08-17, commit `0ecd494` "P3: harden the agent permission plane"). The doctrine layer is `CLAUDE.md` + three skills (`/status`, `/deploy`, `/incident`) + `wiki/runbooks/index.md` §Interlocks and §Autonomy matrix.

## Verb-usage inventory (exhaustive)

### A. MCP tools the server exposes (50 `@mcp.tool` decorators, `SyrvisCore/packages/syrviscore-mcp/src/syrviscore_mcp/server.py`)

Read-only (`annotations=RO`): `status`, `verify(smoke)`, `service_list`, `stack_hostnames`, `service_catalog`, `profile_list`, `image_updates`, `export`, `deployment_history(workload)`, `logs(service,tail)`, `reconcile_plan`, `schedule_list`, `versions_list`, `check_updates`, `info`, `backup_list`, `cleanup_preview(keep)`.

Mutating, no token: `start`, `stop`, `restart`, `shutdown(reason)`, `resume`, `restart_graceful`, `verify_fix`, `stack_apply`, `stack_enable`, `stack_disable`, `backup_create`, `profile_enable`, `reconcile`, `service_declare`, `service_adopt`, `service_start`, `service_stop`, `service_update`, `service_task`.

Two-call HMAC-confirm (`confirm: str = ""`, `tokens.py`: HMAC over tool+normalized args+fresh state hash+nonce+TTL, per-process secret): `service_set_image`, `service_rollback`, `service_add`, `service_run`, `activate`, `rollback`, `uninstall`, `cleanup`, `backup_cleanup`, `reconcile_prune`, `service_remove`, `schedule_apply`, `schedule_sync`. Plus `install` (openWorld, no token).

| verb / tool | channel | caller (file:line or doc) | cadence | data flow | what the caller does with it |
|---|---|---|---|---|---|
| `mcp__syrviscore__status` | MCP | `.claude/settings.local.json` allow[]; `/status` SKILL step 2; `/deploy` §Halted gate | per status sweep, before any mutation | JSON: core containers, active version, **runstate** | leads the report with `halted`; suppresses "outage" framing |
| `mcp__syrviscore__info` | MCP | allow[]; `/status` | ad hoc | JSON | version/home facts |
| `mcp__syrviscore__service_list` | MCP | allow[]; Interlocks row for `apply-instance --apply` ("`service list --json` enabled flags vs git") | per deploy/incident | JSON enabled flags + `intent`/`shed_reason` | joins against git intent to detect the resurrect inversion |
| `mcp__syrviscore__stack_hostnames` | MCP | allow[]; `mcp/router-dns/README.md` step 1; `design/08:172` | per DNS reconcile | JSON hostname→exposure | feeds `desired_a_records()` → `dns.plan` |
| `mcp__syrviscore__versions_list` | MCP | allow[] | rare | JSON | version census |
| `deployment_history [workload]` | MCP | `/status` step 2; `/deploy` §History; `design/25:55,233` | on unhealthy service / "what changed?" | JSON revisions, env **names** only, `@core` pin history, newest-50 retention | correlate breakage → revision → propose `service rollback --to REV` |
| `reconcile_plan` | MCP | `/status` step 2 | per status sweep | JSON plan | "would converging change anything" |
| `service_rollback(name, revision, confirm)` | MCP | `/deploy` §Rollback | incident-only | two-call: plan+token, then echo token | first-line recovery; caller must also revert the repo pin |
| `shutdown(reason)` / `resume` / `restart_graceful` | MCP | `/deploy` §Instance lifecycle table | maintenance window / UPS / manual | JSON | gated on `config/maintenance-state.yaml` + post-boot G0–G4 |
| `service_start` / `service_stop` | MCP | `/deploy` per-service table | rare, attended | JSON | discouraged — `stop` writes live-only `enabled:false`, `start` uses stored compose |
| `schedule_sync(confirm)` | MCP | design/68, `jobs-pin-cutover.md` | pin cutover | two-call token; MCP **fails closed** on host allowlist | the P3 brief records the asymmetry: the MCP guards this verb, the seam spelling of the same verb does not |
| `service_add` / `service_run` | MCP | — | rare | token + `safety.git_url_allowed_hosts` / `image_allowed_registries`, **empty ⇒ disabled, never "any"** (`config.py:50-53`, `validate.py:111`) | fail-closed by construction |
| **the other ~40 tools** | MCP | *not on the allowlist* | — | — | each call raises a permission prompt; only 5 are pre-approved (`design/47:476` — "5 read-only tools pre-allowed") |

### B. Seam verb shapes pre-approved as Bash (the whole allowlisted NAS surface)

Exactly two entries, both exact strings, both the same verb:

- `Bash(ssh -F ~/.config/syrviscore-mcp/ssh_config -o ConnectTimeout=6 -o BatchMode=yes syrvis-nas -- 'sudo -n /volume4/syrviscore/bin/syrvis status --json')`
- `Bash(ssh -F ~/.config/syrviscore-mcp/ssh_config syrvis-nas -- 'sudo -n /volume4/syrviscore/bin/syrvis status --json')`

Both are spelled with `sudo -n` on a **read** verb, contradicting `CLAUDE.md`'s own FACTS row ("read verbs take NO `sudo`") and trap T7. The `whitelist-rewrite` design measured that these two exact rules **never fired once** — "nobody spells it the same way twice."

### C. Other allowlisted NAS-adjacent Bash (all of it)

| argv | channel | cadence | flows |
|---|---|---|---|
| `curl -sk -H 'Host: dash.konsume.org' https://192.168.8.4/api/services` | dashboard HTTP | per acceptance sweep | JSON list; `RestartCount` deltas (`scripts/accept:94-130`) |
| `nc -z -w3 192.168.8.1 22`, `ping -c2 192.168.8.20`, `dig *` | LAN probes | ad hoc | exit-code gates; `echo "nc exit: $?"` / `echo "ping exit: $?"` are separately allowlisted because loop bodies fail in the sandbox (trap T14) |
| `./scripts/lan-dns-plan --no-color`, `--refresh --json`, `./scripts/validate-intent`, `./scripts/snapshot-lan-dns.sh` | local, no NAS | per DNS change | plan JSON |
| `git add *`, `git commit -m ' *`, `git -C … log/status` (fixed forms) | local | per commit | — |

### D. Doctrine-taught seam/local verb shapes that are **not** allowlisted (prompt every time)

Taught in `CLAUDE.md`, the skills and `wiki/runbooks/index.md` §Interlocks, and reachable only via a prompt: `./.venv/bin/python scripts/verify-all [slice]`, `./scripts/accept`, `./scripts/deploy-stack <stack> --only <name> [--apply]`, `./scripts/apply-instance --converge`, `scripts/maintenance-mode start`, `sudo -n … syrvis service rollback --to <rev> -y -- <name>`, `sudo -n … syrvis shutdown --reason ups|maintenance --json`, `sudo -n … syrvis resume --json`, `sudo -n … syrvis restart --graceful --json`, `syrvis service shed --reason R --until ISO` / `unshed`, `syrvis service recreate`, `syrvis schedule sync [--to <rev> --manifest <sha256>]`, `syrvis apply --dry-run`, `ls -d /volume*/syrviscore*`, `find /volume*/syrviscore* -name secrets.env -size 0`, `syrvis history --json -- <name>`, `syrvis logs -n N -- <name>`, `syrvisctl doctor --json`.

Note the shape of that list: **every state-changing verb in the Interlocks table is off the allowlist by construction** — that is the design's intended enforcement (absence, not denial), and it is also why the daily read workload now prompts.

### E. Deny floor (11 ssh globs + 3 credential reads + 3 exfil verbs; `config/agent-permissions.yaml` `required_deny`)

`ssh cerebrate@*`, `ssh *cerebrate@*`, `ssh admin@*`, `ssh *admin@192.168.8.*`, `ssh nas *`, `ssh ds *`, `ssh *192.168.8.3*`, `ssh ds`, `ssh nas`, `ssh -l cerebrate*`, `ssh -l admin*`; `Read(~/.config/syrviscore-mcp/**)`, `Read(~/.config/sops/**)`, `Read(~/.ssh/**)`; `sops -d*`, `sops exec-env*`, `gh auth token*`. SyrvisCore's twin file adds `scp * cerebrate@192.168.8.3:*`.

### F. The recorded counts (design/66's "71", with the breakdown design/66 omits)

From `design/handoffs/security-review-2026-08-17/solutions/batch-A-raw/P3-agent-plane-designs.json` (the winning `whitelist-rewrite` design; reproduced independently by the correctness judge, `P3-agent-plane-SPEC.md`):

- **4,338 Bash calls** mined from home-tech transcripts; **796** in SyrvisCore.
- Old allow[] auto-approved **204 / 4,338 = 4.7%**; **183 of those 204 (90%)** rode `python3 *`, `/usr/bin/python3 *`, `git push *` or `osxphotos *`. SyrvisCore: 100%. **30 of 47 Bash rules had never fired again** after the click that created them.
- **The deny list was walked around 134 times**, and the "71" in design/66 §1 is *one spelling of that total*: `ssh -o ControlMaster=… ds` (**71**), `ssh -o BatchMode=yes -o ConnectTimeout=8 ds` (51), `ssh -o ConnectTimeout=8 … ds` (11), `timeout 25 ssh ds` (1). ⚠ design/66 renders this as "71 recorded **seam** calls"; the evidence says 71 **cerebrate-bound** `ssh … ds` commands matching none of the seven globs. The design doc's own summary is a misquote of its register.
- **87 recorded `timeout N ssh …` seam calls** (200 `timeout N <cmd>` total) — the seam is routinely wrapped in `timeout` because a wedged dockerd hangs every verb, while `/deploy` and trap T3 forbid wrapping *deploy* verbs in `timeout`.
- **85 recorded commands containing `cerebrate`**, of which **32 are not logins** (commit bodies, `gh issue` bodies about design/11) — which is why the blanket `Bash(*cerebrate*)` deny was rejected.
- WebFetch: the 14 allowlisted domains fired **~11 times ever**; real traffic was `r.jina.ai` (**28**, an arbitrary-URL text proxy), `techspecs.ui.com`, `docs.onyx.app`, `support.apple.com`.
- Projected effect of the whitelist: home-tech 4.7% → 27.9%, SyrvisCore 10.1% → 42.3%; **1,513 newly auto-approved, 251 newly prompting, net −1,262 prompts**. Calibration: 32 violations on the old files, 0 on the proposed.

## Interaction patterns

**Nothing in this slice builds a stdin bundle.** The four stdin-bundle writers (`apply`, `deploy`, `secret set`, `config set`) are reached from `scripts/apply-instance` and `scripts/deploy-stack`, which the agent invokes as opaque repo scripts — and neither script is allowlisted, so every bundle write passes through a human prompt. The MCP has no bundle transport at all: `FABLE-HANDOFF.md:131` (P4) records that `service_declare` carries only `image/subdomain/exposure/port/enabled/critical`, and anything with `env_file`/`volumes`/`healthcheck`/`resources` must be file-pushed by tar-over-ssh, with `stack apply --from` shipped as CLI/library and **no matching MCP tool**.

**Orchestration is entirely client-side, in prose.** The ordering, preconditions and post-verification of every multi-step NAS operation live in Markdown that the model must read and obey: `wiki/runbooks/index.md` §Interlocks (ten verbs × precondition × amplifier × cheapest proof), §Autonomy matrix (failure class × who may act unattended), `config/maintenance-state.yaml` (device_intent / windows[] / forbidden_verbs[] / shed[] / accepted_alerts[] / hardware_watch[]), and the three skills. The platform enforces almost none of it: `halted` is enforced (`instance_halted`), and `guard_enable_change` refuses re-enabling a shed service — but `forbidden_verbs[]` is read and reported by three consumers and **enforced by none** (P3 judge graft #5).

**Result consumption is JSON → prose judgement, with an explicit blindness ledger.** Three named blind spots the agent is required to correct for: a seam-unreachable check degrades to `INFO`, which the tally scores as **zero** and prints clean (trap T10 — 11 checks carry `needs: seam` and go blind together); `syrvis status` reads the raw Docker string, so a crash-looping `restart: unless-stopped` container reads `running` (trap T13) — hence the dashboard `/api/services` `RestartCount` detour; and `nas.drift` compares declaration *names* only, so 14 shed services graded PASS on 2026-08-16.

**Confirmation is two-layer and asymmetric.** MCP destructive tools require a server-minted HMAC token bound to tool+args+fresh state hash+nonce+TTL (`tokens.py`) — the model can relay but not forge, and a TOCTOU state change voids the token. The seam spelling of the *same* verb has no token: the P3 brief's V15-6 records that `sudo -n … syrvis schedule sync --json` is a 6-arg registered shim form with a NOPASSWD sudoers line and no host allowlist and no confirmation — "the strongest guard in the MCP package being absent from the transport the documentation pushes agents toward."

**Untrusted-input handling is prose-only.** `CLAUDE.md` §UNTRUSTED INPUT declares WebFetch/WebSearch results, NAS-surfaced output (`syrvis logs`, container/service/image names, alert bodies, DSM/SNMP strings, DB rows) and anon-persona repo contents as data-never-instructions. The one *enforced* control is the permission file; the fencing of log output (`--- BEGIN UNTRUSTED CONTAINER LOG ---`) exists only in the unbuilt `scripts/seam`.

## Workarounds & missing verbs (the negative space)

- **`scripts/seam` does not exist.** Confirmed absent (`ls scripts/seam` → No such file). design/66 §5 defers it; `config/agent-permissions.yaml` records "until it exists, the two literal read-only `syrvis status --json` seam reads stay individually allowlisted." Its drafted verb table is the single best statement of really-used read shapes: `status · verify · verify-smoke · service-list · hostnames · hostnames-sudo · catalog · images · profiles · history [svc] · logs <svc> [n] · updates · export · schedule-list · dsm-tasks · reconcile-plan · apply-plan · vm-list · vm-status <vm> · ctl-list · ctl-check · ctl-info · ctl-doctor · backup-list`. Note it needs a `hostnames` **and** a `hostnames-sudo` entry — the read/sudo split is not derivable from the verb name.
- **`docs.agent-permissions` verify-all check does not exist** (`grep -c agent-permissions scripts/verify-all` → 0). So the tracked contract ↔ live file drift is unchecked; design/66 §4's "one invariant" (a rewrite may only ADD deny protection) is enforced by review only. Wiring it needs the repo's 5-edit registry dance (CHECKS + CHECK_NEEDS + `render/console_static.py` ANNOTATIONS + the `/status` SKILL.md row + a feed regen).
- **`config/agent-intake.yaml` does not exist** — the judge graft that would put `trust:`/`expires:` dates on the 13 retained WebFetch domains never landed; the list is still append-only.
- **The daily read workload now prompts.** `./.venv/bin/python scripts/verify-all` is the command `CLAUDE.md`, `/status` and `/incident` all instruct, and it is on no allowlist (`python *` / `python3 *` are `never_allow`, and the venv path was never added). Same for `./scripts/accept`, which `/deploy` and `/incident` make the mandatory close of every window. The whitelist landed without its usability half.
- **The seam has no filesystem verbs** — no `ls`, no `mv`, no `find`. This is the single largest hole and it is what makes break-glass structurally necessary: trap T6 and the post-boot G2 gate both begin with `ls -d /volume*/syrviscore*`, the collision repair is `mv`, and the empty-secret fingerprint is a `find`. §Interlocks' last row and trap T15 both resolve to "ask the owner for ONE cerebrate session with the pre-canned block (`seam-dead-after-boot.md` R4)". Break-glass is *batched, not rationed* — a policy that exists only because a verb is missing.
- **`nas.vms` is UNKNOWN by design** "until SyrvisCore ships `vm list`" (`/status` SKILL check table) — a check written against a verb that did not exist at authoring time.
- **`mcp/router-dns/` is a dead MCP that is load-bearing as a library.** No server, no provider adapter ("TODO": `list_records` / `upsert_record` / `delete_record`); what survives is `desired_records.py`, imported at module level by `render/lan_dns.py:32-33` and therefore by `scripts/verify-all:47`. Four places in the corpus (design/49 ×3, design/47) instructed an agent to retire it; all four were struck 2026-08-16 and replaced with a DO-NOT-DELETE banner. The planned `dns_plan` / `dns_apply` tools were never built.
- **`design/15` §3's three planned homebase MCPs all resolved to something else**: cloudflare → tofu; nas/storage → the `syrvis-reader` identity + direct SSH; router-dns → a library. As-built note: "Every row found an owner, and **not one of them is an MCP**."
- **Doctrine self-contradiction as a workaround surface**: the two allowlisted seam reads spell a read verb with `sudo -n`, against T7 and the FACTS table. The permission file and the doctrine disagree about the one verb both bother to name.
- **Residual risk stated in design/66 and the winning design**: CLAUDE.md guidance is advisory (the harness enforces allow/deny, not prose); every allowlisted repo script is an interpreter if the agent can edit it — the whitelist converts invisible arbitrary execution into *auditable* arbitrary execution and does not abolish it; the MCP confirm token remains model-satisfiable within a session.

## Observations for a v2 agent design

1. **Ship the read façade as a device-agent verb, not as laptop shell script.** The whole `scripts/seam` design exists because permission matching is done on a command *string*, and the seam's real spellings are unmatchable (eleven variants, 134 escapes, two exact rules that never fired). A v2 agent with a single stable client entry point (`syrvisd <verb>` / one socket, one argv shape) dissolves the string-matching problem for every consumer at once, and makes `Bash(<one prefix> *)` the narrowest possible rule that covers 100% of routine traffic.
2. **Make the read/write split machine-legible.** Today the split is `sudo -n` or not, is not derivable from the verb name (`export` is a sudo read; `stack hostnames` exists in both forms), and is documented as a trap in three places. A v2 agent should expose capability class (read / converge / destructive) as a queryable property of each verb, so a wrapper, an allowlist generator, and a doc check can all be *generated* rather than hand-mirrored.
3. **Move the Interlocks table into the agent.** Ten preconditions, `forbidden_verbs[]`, `device_intent: drained`, the shed set, "a convergence verb is a WRITE and an absent root means create" — all of it is enforced today by a model reading Markdown. A v2 agent should refuse a verb whose declared precondition is unmet and say which one, the way `instance_halted` and `guard_enable_change` already do. The measured cost of prose-only enforcement is on the record: 2026-08-16, four containers recreated against a scaffold, four empty `secrets.env`, six crash loops, three pages.
4. **Close the transport asymmetry.** Confirmation, host allowlists and registry allowlists must live at the *device*, not in one client. Today `schedule_sync` is token-gated and host-gated via MCP and completely ungated via the shim spelling of the same verb — and the shim spelling is the one the documentation teaches. A consolidated agent should own the token handshake so every channel inherits it.
5. **Give it filesystem-shaped read verbs.** `roots census`, `stat <path>`, `find-empty-secrets`, `rename <a> <b>` (guarded) would eliminate most break-glass and therefore most owner pages. The cheapest proof for three of the ten Interlocks is a shell command the seam cannot express.
6. **Emit structured, trust-labelled output.** Container logs and alert bodies are the cheapest injection path into a seam-holding session. The agent should mark log/alert payloads as untrusted at the protocol level (the fencing that only exists in an unbuilt shell script), and should never let a report of a verb's own success stand in for verification — status is structurally blind to crash loops; `RestartCount` should be a first-class field, not a dashboard-only side channel.
7. **Serve bundles, don't tar-push them.** P4's whole-declaration transport gap is why `stack apply --from` has no MCP tool and why rich declarations bypass the typed surface. A v2 agent with one bundle-write verb over a length-safe transport retires both the tar-ssh workaround and the four separate stdin writers.
8. **Distinguish UNKNOWN from PASS at the protocol level.** Eleven checks degrade to `INFO` when the seam is down, and the tally scores INFO as zero and prints clean. A v2 agent should return an explicit unreachable/unknown status that no aggregator can silently score as healthy.
9. **Make the permission contract generated, not curated.** `config/agent-permissions.yaml` is hand-written, its checker is unbuilt, its `known_bad`/`known_good` calibration corpus was proposed and dropped, and its live twin is gitignored. If the agent publishes its verb table with capability classes, the allowlist, the deny floor and the drift check all become derived artifacts — which is the only version of this that survives the next "always allow" click.