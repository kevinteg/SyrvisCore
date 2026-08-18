# The verify / monitoring plane (the read path)

## What this slice is

home-tech's read path is a single-writer check registry (`scripts/verify-all`, 9,733 lines, 83 checks) plus three consumers: `scripts/monitor-tick` (the unattended pager), `.github/workflows/verify.yml` (the pure pre-merge slice) and `scripts/accept` (the post-incident acceptance sweep). Around it sit four smaller read tools: `scripts/lan-dns-plan` (the only *writer* of the seam-derived hostnames cache), `scripts/snapshot-lan-dns.sh` (the router zone's only backup), `scripts/maintenance-mode` (declares intended-down so checks don't page), and `scripts/backup-drill` → `scripts/backup_drill.py` (the S3 restore-readiness gate, which never touches the NAS at all). `render/data.py` is a **pure** registry parser — it holds `VOLUMES`, `share_root()`, the `data.d`/`shares.d`/`backup-tasks.d` loaders and the rule 1–17 lints — and is imported by verify-all to build the file-plane probe; it has no transport of its own.

The design centre of gravity is a taxonomy, `CHECK_NEEDS` (`scripts/verify-all:9119`), the declared source of truth for what each check needs: **47 `pure` · 20 `seam` · 12 `cloud-creds` · 4 `LAN`**. `docs.unknown-coverage` (`:9022`) is a build-breaking meta-check that asserts the taxonomy covers the registry exactly and that every `needs: seam` check has an entry in `UNREACHABLE_SUMMARIES` (`:157`) whose prose classifies as UNKNOWN. That is the whole architecture: *a check that cannot reach the NAS must be provably unable to print green.*

## Verb-usage inventory (exhaustive)

### A. Operator seam — `_seam_nas()` (`:3234`) → `ssh -F ~/.config/syrviscore-mcp/ssh_config -o ConnectTimeout=6 -o BatchMode=yes syrvis-nas -- '[sudo -n ]/volume4/syrviscore/bin/syrvis <verb>'`

The helper carries the sudo/non-sudo split explicitly because *the forced-command shim matches exact argv shapes* — a sudo-registry verb invoked bare is rejected and vice versa.

| # | Exact verb | sudo | Caller (check / file:line) | Cadence | Data flow | What the caller does with it |
|---|---|---|---|---|---|---|
| 1 | `stack hostnames --json` | no | `check_nas_reach` `:1699` | daily tick + manual | JSON read; only tested for `rc==0 && stdout.startswith("{")` | Liveness canary only. PASS or INFO (Tier B) |
| 2 | `status --json` | no | `_seam_status()` `:3269`, **memoized once per sweep** | daily | full JSON | Feeds four consumers: `nas.runstate` (`runstate.state == halted` → WARN + "syrvis resume", never FAIL), `nas.version` (vs `deployment.yaml instance.version`), `nas.monitoring`'s halted gate, `nas.status-vs-metrics` (`services{}`, `flapping`, `restart_count`) |
| 3 | `schedule list --json` | **yes** | `_schedule_list()` `:3320`, memoized; returns `(data, reason)` so *unreachable* ≠ *unparsable* | daily | `plan.{scripts,confs,pin,source,changed,actions,in_sync}` | Four checks share one round-trip: `nas.jobs` (per-job `{present,sha256}` vs `jobs/MANIFEST.sha256` **at the pinned commit**, plus crontab-block plane), `nas.jobs-source` (`plan.source` vs `config/jobs.source`), `nas.jobs-pin` (`plan.pin` vs `config/jobs.pin`), `nas.job-confs` plane 1 (`plan.confs`) |
| 4 | `service list --json` | no | `_service_list()` `:3368`, memoized, **three-valued** (rows / None=outage / False=unparsable) | daily | rows with `name,status,tier,image,intent,shed_reason,flapping` | `nas.monitoring` (13 hard-coded collector names must be `running`), `nas.enabled-drift` (join git `enabled:` × live × shed), `nas.privilege` (live `tier: infra` vs `config/service-policy.yaml`) |
| 5 | `logs -n 50 -- vmagent` | no | `check_monitoring` `:3464` | daily, only after collectors PASS | raw text | substring scan for `panic`/`connection refused`/`no route to host`/`authentication failure` → WARN |
| 6 | `updates --json` | **yes** | `check_image_updates` `:3496`, 90 s | daily | `{count, update_count, images[], cached}` | Report-only INFO; action explicitly says bump the pin in git, *not* `service set-image` |
| 7 | `export --json` | **yes** | `check_instance_drift` `:3539` | daily | `declarations{}` keys only | Set-compare NAMES vs `syrvis/services.d/*.yaml`. NAS-only → FAIL; git-only → WARN. Docstring states it is *structurally blind to `enabled:`* |
| 8 | `verify --json` | no | `check_nas_selfcheck` `:7201`, 90 s | daily | 16-check self-audit + `drift`/`l2_drift` | **Graded on payload, never on exit code** (`syrvis verify` exits 1 when unhealthy). Three checks are allow-listed as "privilege-blind" (`SELFCHECK_PRIVILEGE_BLIND` `:7264`) by (category, name, message-substring); zero checks returned → FAIL |
| 9 | `vm list --json` | no | `check_nas_vms` `:4336` | daily | — | **The verb does not exist.** Registered deliberately as a standing UNKNOWN |
| 10 | `stack hostnames --json` | **yes** (`stack_hostnames_full`, ≥0.5.3) | `scripts/lan-dns-plan:93` `refresh_report()` | manual, `--refresh` | writes `render/out/hostnames.json` | The sudo variant is required because the non-sudo one returns `domain`/`traefik_ip` null and `compute_desired` would raise. Cache-on-failure |

### B. Scoped reader identity — `_ssh_reader()` (`:1705`) → `ssh -o ConnectTimeout=6 -o BatchMode=yes syrvis-reader -- '<shell>'`

Not a verb: raw POSIX shell over an unprivileged, key-restricted, LAN-only login deliberately excluded from `jobs/login-alert`'s watch list so it never pages.

| Probe | Caller | Shape | Grading |
|---|---|---|---|
| pg-dump freshness | `check_backup_freshness` `:2707` | one `;`-joined `if/find -mmin -1500 -size +1024c` per `data.d` `pg-dump*` dataset, emitting `NAME fresh=N young=N total=N` \| `missing` \| `unreadable` | rc≠0 → INFO(1); rc=0 no output → INFO(2); unencrypted DEST missing → **FAIL**; fresh-but-<1 KiB reported as its own outcome; **coverage is part of the verdict** — any unaccounted dataset degrades to INFO |
| file plane | `check_file_plane` `:3063` via `_fileplane_probe()` `:2939` | top-level-only (never recursive, never `du` — "`du` returns 0 on a permission denial, which fabricates an empty share"), tagged lines `VOLDIR\|`, `VOLDENIED\|`, `SHAREOK/DENIED/MISSING\|`, `TOPDIR/TOPFILE\|`, `MTIME\|`, `STALE\|` | 5 rules (undeclared share FAIL, orphan top-dir FAIL, ACL-vs-declaration, 14-day inbox, frozen-means-frozen). The one detector that names a `syriscore_1` collision |
| account posture | `check_nas_accounts` `:6525` | `grep -E '^(admin\|syrvis-operator\|syrvis-reader\|kevin\|lisa\|anton):' /etc/passwd; echo ---; grep -E '^(administrators\|docker):' /etc/group` | nologin on either `SEAM_SHELL_USERS` → FAIL; unexpected `docker` member → escalation. **Self-defeating by construction** and says so |
| zero-byte `secrets.env` | `scripts/accept:274` | roots derived from `deployment.yaml` (`instance.volume`, `tuning.SYRVIS_APPS_ROOT_NAME`), `find … -name secrets.env -size 0`, sentinel `__SWEPT__` | hits → FAIL; no sentinel → UNKNOWN |

### C. VictoriaMetrics through Grafana — `_vm_query()` (`:7071`)

`curl --resolve grafana.konsume.org:443:<instance.traefik_ip> -H "Authorization: Bearer $GRAFANA_SA_TOKEN" -G --data-urlencode query=<PromQL> https://grafana.konsume.org<VM_QUERY_PATH>`. Classed `needs: seam` because Grafana and VictoriaMetrics are **L2 containers on the NAS**. Returns `(series, why)` with `why ∈ {no-token, no-traefik-ip, unreachable, refused, unparsable}`; `_vm_unavailable()` (`:7423`) is the single place a reason becomes a verdict, and **no reason maps to PASS**.

- `nas.appenv` — `app_secrets_env_keys`; keys ≤ 0 → FAIL; *series family absent entirely* → WARN, not PASS.
- `nas.job-confs` plane 2 — `count(count_over_time(<sel>[<max_age_hours>h]))` per row of `config/job-confs.yaml`, with a 30 d re-query to distinguish "never published" from "stale". Deliberately `count_over_time`, not `timestamp(last_over_time(…))`, which in MetricsQL returns the evaluation timestamp and would make the check vacuously green.
- `nas.status-vs-metrics` plane 2 — the `ContainerRestartLoop` `expr` read **verbatim from `syrvis/stacks/monitoring/config/rules-*.yml`** so check and alert cannot drift. Join key is the container name from the status payload, never the file stem.
- `cloudflare.public-challenge` — `auth_probe_success{expect="401"}` + `auth_probe_http_code{expect="401"}`; a live `<400` → FAIL "write-capable WebDAV open to the internet".

### D. LAN / cloud (no NAS transport)

`dig @192.168.8.1` (`dns.plan` via `sys.executable scripts/lan-dns-plan`, exit codes 0/1/3/2 are the contract; `dns.drift` via `./scripts/snapshot-lan-dns.sh --check`, codes 0/1/2 with rc=1-and-no-diff graded **FAIL**; `cloudflare.rotation` LAN-vs-1.1.1.1/8.8.8.8). `openssl s_client` against 20 `CERT_HOSTS` (`:6908`) — subprocess, not `ssl.get_server_certificate`, because the stdlib refuses the handshake on an expired cert. Healthchecks.io v3 API (memoized `_hc_checks()` `:1428`), UptimeRobot, `_cf_get()` (`:7107`, `CF_RO_TOKEN`, read-only scopes), hand-rolled stdlib SigV4 S3 (`scripts/s3_offsite.py`, shared with `backup_drill.py` §5 so the drill and the monitor cannot disagree).

### E. Cadence and channel summary

- **Daily**, one macbase LaunchAgent (`com.macbase.freshness` → `macbase/scripts/freshness-sweep`) → `sops exec-env secrets/monitoring.sops.env ./scripts/monitor-tick` → `sys.executable ./scripts/verify-all --json --unattended` with `VERIFY_TIMEOUT = 400`. Daily is a ratified decision (re-ratified 2026-08-16): vmalert on the NAS owns everything fast-moving; the tick is the backstop.
- **Pre-merge CI**: the pure slice only, named check-by-check (never `data`, because `data.file-plane` is seam-bound and would sit there looking like coverage).
- **Manual**: `lan-dns-plan --refresh`, `snapshot-lan-dns.sh`, `backup-drill`, `maintenance-mode`.
- **Incident/acceptance**: `scripts/accept` runs an explicitly-named 8-check slice so that *one* UNKNOWN exits non-zero.
- **Break-glass**: `CEREBRATE_CHECKS = set()` — zero routine cerebrate reads since 2026-07-28. `SYRVIS_READER_TARGET=ds` is the documented one-off escape hatch and "that WILL page (break-glass, deliberate)".

## Interaction patterns

**Memoization is the seam budget.** Three memoized readers (`_seam_status`, `_schedule_list`, `_service_list`) serve ten checks; a full sweep costs ~9 SSH round-trips, not 20. Each memo is deliberately multi-valued so callers can distinguish *outage* (INFO/UNKNOWN) from *malformed response* (WARN — "a SyrvisCore/shim fault, not an outage").

**Grading is text-classified, not typed.** `_is_unknown()` (`:9580`) matches two regexes against the summary *prose*: `\b(seam|reader|victoriametrics)\b` AND `not reachable|unreachable|did not answer|unavailable over the |answered with NO output`. `_unknown_cloud()` is a second, disjoint classifier scoped to `api\.cloudflare\.com`. The explicit reasoning for not adding a `DEGRADED` status is that it would touch ~25 branches and every JSON consumer; the summaries already follow a house convention, so the classifier reads the convention and `docs.unknown-coverage` gates it.

**Exit-code contract.** `0` clean · `1` a FAIL · `2` usage · `3` **BLIND**. A full sweep tolerates `UNKNOWN_EXIT_THRESHOLD = 3` seam-UNKNOWNs; an *explicitly selected* slice exits 3 on any single one ("when the caller names a check, silence about it is a failed answer"). JSON emits `unknown`, `unknown_seam`, `unknown_cloud`, `unknown_exit`, `explicit_selection`, and `warn` is now true whenever the run was blind.

**monitor-tick orchestrates entirely client-side.** `PUSH_ALLOW` (fnmatch patterns) × `TWIN_COVERED` (checks with a vmalert twin) is the paging boundary — vmalert is the sole pager for anything derivable from the TSDB. `TWIN_COVERED` values are `(twin, shared_fate)` pairs, and three of five name what kills both subject and twin ("vmalert + VictoriaMetrics are NAS containers"). `seam_blackout()` (`:347`) synthesises a fake check dict when `len(unknown_seam) >= 3`, then splits it by a **two-port TCP pre-flight** (`nas_answers()`, 5001 and 22): box answers + seam dead = `seam.blackout`, urgent, → `seam-dead-after-boot.md`; nothing answers = `seam.unreachable`, `high`, "confirm this Mac's network first". The id changes with the verdict so the dedup signature re-pages. Signatures are digit-masked (`mask_digits`) so a countdown isn't a new problem while 3→30 is, and state is written **only after confirmed ntfy delivery**.

**maintenance-mode is the pause channel, and it never touches the NAS.** It pauses `alertmanager-watchdog` then `nas-heartbeat` via the Healthchecks.io v3 management key (header only, never argv), verifying `manual_resume: false` rather than assuming it; `NEVER_PAUSE = {"nas-boot-integrity"}` is a hard refusal with no `--force` path. It writes windows/shed rows into `config/maintenance-state.yaml` with byte-preserving surgery (a row it did not write is never re-rendered; only `until:` is ever rewritten) and a `#~`-marked banner so re-emission can't grow the block. verify-all reads that file via `_maintenance_state()` / `_shed_rows()` / `_expired_shed()` — but **platform intent wins when it exists**: `_platform_intent_capable()` sniffs for `intent`/`shed_reason` on any `service list` row rather than version-sniffing, and a laptop-only shed is a WARN because `guard_enable_change` will not defend it.

**DNS reconciliation is a two-artifact loop.** `stack hostnames --json` → `render/out/hostnames.json` (cache) → `lan-dns-plan` computes desired = intent ∪ report, digs live, emits an ordered ADD/CHANGE list a human pastes into the SRM UI (the router has no API). `dns.report-freshness` grades the cache *purely* (content coverage of routed services.d, plus a 168 h advisory age), because the paging check's verdict must never rest on a cache nobody looked at.

## Workarounds & missing verbs (the negative space)

1. **`vm list` does not exist.** `nas.vms` is registered specifically so the gap is a *visible standing UNKNOWN* rather than absence. Live VM state is "checked by eye in DSM > Virtual Machine Manager".
2. **No metrics-push verb on the seam.** Both `monitor-tick.push_liveness()` and `maintenance-mode.push_window_metric()` document the same dead end: VictoriaMetrics is unrouted, `docker exec` is NAS-only, and "the operator verb registry is enumerated and has no metrics-push verb". `VM_IMPORT_URL` therefore has **no default** and both legs report SKIPPED. The owner ruling moved liveness to a Healthchecks.io ping instead.
3. **No verb enumerates DSM Task Scheduler entries pointing outside the managed block** — `check_jobs_installed`'s docstring drops that class from the automated sweep and flags it as a seam-observability gap. (`syrvis schedule dsm-tasks` in 0.5.16 is the answer; this repo has not adopted it.)
4. **`plan.confs` is not published yet**, so `nas.job-confs` runs an entire indirect metric plane (plane 2) plus a hand-maintained `config/job-confs.yaml` proof registry as a stand-in.
5. **`syrvis verify` is unprivileged over the seam**, so three of its sixteen checks are permanently unanswerable and must be allow-listed by message substring — and one of them (`Schedule block`) is a question `schedule list --json` already answers correctly *under sudo*.
6. **`export --json` reports names, not enablement**, so `nas.enabled-drift` had to be built from `service list` + intent.json + a laptop YAML mirror.
7. **`status --json` is core-tier only** (4 containers), so the crash-loop cross-check needs a second seam read for L2 and reports the scope difference rather than dropping it.
8. **`flapping` is present-but-inert** on 0.5.11 (`restart_count: null` because both call sites read `RestartCount` from `attrs["State"]` instead of the inspect top level) — the check grades `false`-with-`null`-counter as **no answer**, not health.
9. **No structured file-plane verb**, so shell probes are hand-built (`shlex.quote`d) and hand-parsed with pipe-tagged lines.
10. **Grafana annotations need an Editor token**; `GRAFANA_SA_TOKEN` is read-scoped, so a 403 is the expected answer and is reported as SKIPPED. Alertmanager silences remain a manual UI step.
11. **`CF_RO_TOKEN` is not in the tick's sops env**, so the two Cloudflare Access gates SKIP on every unattended run — named in `CREDS_REQUIRED` so the gap prints every tick.
12. **Signpost coherence (`.data-plane.yaml`) is a documented TODO** in `check_file_plane` — deliberately unimplemented because no writer exists yet and "a check nobody can satisfy is how a suite gets ignored".
13. Stale prose: several comments still say "sixteen checks carry `needs: seam`" / "eleven"; the table says **20**. The gate covers the mapping, not the prose.

## Observations for a v2 agent design

1. **Ship a batched read.** Ten of this slice's checks are field projections of four payloads. A single `agent snapshot --json` (status + services + schedule plan + export + verify + vm + volumes, one round-trip, one timestamp) would kill three hand-rolled memo caches and make the whole sweep atomic — today `nas.drift` and `nas.enabled-drift` can observe different instants.
2. **Make "I could not answer" a typed field, not prose.** The single most expensive construct here is a regex over summary text plus a build-breaking meta-check to keep it honest. A v2 agent should return `{answered: bool, unreachable_reason, observed_at}` per fact, and consumers should be able to count unanswered facts without a classifier.
3. **Preserve the sudo/non-sudo argv distinction or replace it wholesale.** `_seam_nas(sudo=)` exists purely to satisfy shim argv matching; it is a per-verb footgun. A consolidated agent should carry capability in the *identity*, not in the argv shape.
4. **Publish an enumerable read surface.** Every gap above is "the registry has no verb for X": VM state, DSM tasks, conf presence, metrics push, arbitrary path stat. A v2 agent should expose a small set of *general* reads (declared-object list, file-stat under declared roots, journal/intent state) so consumers stop building shell probes and metric proxies.
5. **Never let the exit code carry the verdict.** `syrvis verify` exits 1 when unhealthy; the first draft of `nas.selfcheck` read that as "seam down". Payload-first grading should be the contract, with a documented exit-code meaning that is *only* about transport.
6. **Intent must be readable, durable and authoritative.** `intent.json` (0.5.15) already inverted this slice's design: the laptop file demoted to a mirror, `_platform_intent_capable()` sniffs capability rather than version. A v2 agent should extend the same treatment to *windows* and *forbidden verbs* so maintenance-mode declares to the device rather than to a YAML the device never reads.
7. **Give the device an off-box liveness path.** The one structural hole is that nothing on the NAS can assert its own aliveness to an observer that survives its death; the residual is carried by three Healthchecks.io dead-men and a two-port TCP probe. A device agent that can push a heartbeat outward (or expose an authenticated import endpoint) closes both the `push_liveness` and `push_window_metric` dead ends at once.
8. **Watch the remediation strings.** The two most dangerous lines in this file were both *actions*: `apply-instance --apply --converge` (resurrected a 14-service shed twice in one day) and an unconditional `schedule sync` (on an unpinned platform, "install whatever is on origin right now, as root"). A v2 agent should make the safe repair the *default verb shape* — pinned sync, live-disabled-preserving apply — so a 3 a.m. responder copying a suggestion cannot execute the attack path.