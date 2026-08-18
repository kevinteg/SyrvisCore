# The NAS-side job plane (local root invocations)

## What this slice is

29 declarations in `jobs.d/*.yaml` (25 `enabled: true`), 28 scripts in `jobs/`, one shared-but-uninstallable helper (`jobs/_vm-push.sh`), a digest manifest (`jobs/MANIFEST.sha256`) with its generator (`scripts/jobs_manifest.py`), and one job deliberately **outside** the plane (`rootfs/boot-integrity`). Everything here runs **ON the NAS as root**, from the SyrvisCore-managed `/etc/crontab` block, at the derived path `/volume4/syrviscore/jobs/<name>`. A declaration is exactly `{schedule, enabled}` — SyrvisCore's `jobs_d.py _ALLOWED_KEYS` permits nothing else, so *every* other fact about a job (what it runs, what it needs, what proves it ran) lives in repo-side registries the NAS never sees (`config/job-confs.yaml`, `config/jobs.source`, `config/jobs.pin`).

The striking finding for a v2 design: **this is 25 root cron jobs on the platform's box and only two of them call the platform at all.** The job plane is not a consumer of the verb surface; it is a parallel root plane that reaches around SyrvisCore straight to `docker`, `/proc`, `/etc/passwd`, DSM's binaries and the container network — precisely because the verb surface does not offer what it needs.

## Verb-usage inventory (exhaustive)

Channel legend: **LOCAL-CLI** = on-NAS root invocation of the syrvis/syrvisctl binaries; **LOCAL-STATE** = root reads SyrvisCore's own on-disk state, bypassing the CLI; **LOCAL-DOCKER** = root talks to dockerd directly; **LOCAL-DSM/HOST** = root touches DSM or the kernel; **SEAM** = the operator seam from the Mac (listed where it materializes this plane).

### A. The only two jobs that invoke a SyrvisCore binary

| Job | Exact argv | Channel | Caller | Cadence | Data flow | What the caller does with it |
|---|---|---|---|---|---|---|
| nas-heartbeat | `timeout 120 /volume4/syrviscore/bin/syrvis verify --smoke --json` (wrapper path pinned in-script, `PATH=/usr/local/bin:$PATH` prepended because `verify` shells a bare `docker info`) | LOCAL-CLI | `jobs/nas-heartbeat:43,65-67` | `*/5 * * * *` | JSON on stdout + **exit code as the gate**; awk pulls `checks[].name` where `"passed": false`, and `drift.items[]/l2_drift.items[]` `service:kind` where `"failure": true` | rc 0 → `curl $HC_PING_URL`; rc≠0/124/127 → `curl $HC_PING_URL/fail` with a ≤1 KB body, failing items **first** (raw JSON alone truncated the diagnosis away, found live 2026-07-28). Never pages locally — the missed ping is the signal |
| syrviscore-backup | `/var/packages/syrviscore/target/venv/bin/syrvisctl backup create -o "$DEST/syrviscore-$STAMP.tar.gz"` (manager CLI, absolute SPK path, distinct from the service wrapper) | LOCAL-CLI | `jobs/syrviscore-backup:90,255` | `40 2 * * *`, **`enabled: false`** (arming gate: `$SYRVIS_HOME/data` must be < 2 GiB — `backup create` sweeps the whole home including the L2 data plane) | exit code + archive on disk; job then does its own `sha256sum` sidecar, size-vs-previous sanity, retention prune of `syrviscore-*.tar.gz` | ntfy page on failure; archive lands where a Hyper Backup task (`syrvis/backup-tasks.d/backups-sensitive.yaml`) picks it up so recovery is `syrvisctl restore`, not rsync archaeology |

### B. Jobs that read SyrvisCore's state files instead of calling a verb

| Job | What it reads | Why not the verb | Cadence |
|---|---|---|---|
| intent-metrics | `$SYRVIS_HOME/config/services.d/*.yaml` (regex parse of column-zero `name:`/`enabled:`/`container_name:`), `$SYRVIS_HOME/data/state/intent.json`, `docker ps --format {{.Names}}` | Header states it explicitly: *"Not `syrvis service list --json`: this job runs as root from cron INSIDE the box and must keep working when the CLI cannot (a half-installed version, a `current` symlink that does not resolve — the 2026-08-16 shape)."* Also: DSM's `/usr/bin/python3` has no PyYAML and the job refuses to grow a dependency | `*/5` |
| onyx-db-backup | `$SYRVIS_HOME/data/state/intent.json`, matched with `tr -d ' \n\t' \| grep -q "\"service\":\"$CONTAINER\""` | A shed gate with no read verb behind it — a raw JSON grep on the platform's private intent store | `10 2 * * *` |
| appenv-metrics | globs `/volume[1-6]/syrviscore/apps/*/secrets/secrets.env` **and** `/volume[1-6]/syrviscore-apps/apps/*/secrets/secrets.env` | Counts non-empty keys + bytes per app because nothing in the platform can see an *empty* secret — exists because `resume` re-rendered four `secrets.env` empty on 2026-08-16 | `*/5` |

### C. The metrics-push transport — 13 jobs, one shape

Every metrics job pushes with the `vm_push` block copied **verbatim** from `jobs/_vm-push.sh`:

```
printf '%s\njob_last_success_timestamp{job="X"} <now>\njob_push_failures_total{job="X"} <n>\n' \
  | "$DOCKER" exec -i victoria-metrics sh -c "wget -q -O /dev/null --post-data \"\$(cat)\" http://127.0.0.1:8428/api/v1/import/prometheus"
```

`$DOCKER` is pinned `/usr/local/bin/docker` in every job (root cron's PATH lacks it). The receipt series rides **inside the same POST** as the payload, so a landed stamp proves a landed payload; the lifetime failure counter is kept in `$SYRVIS_HOME/data/job-push/<job>.failures` and republished on the next success (a failure-time push would travel the transport that just failed). Adopters: appenv-metrics, auth-probe, authentik-events-metrics, backup-drill-metrics, cert-metrics, cf-expiry-metrics, dsp-endpoint-audit, hyperbackup-metrics, secret-metrics, share-metrics, site-probe, workspace-liveness (shell) + intent-metrics, immich-stats-metrics (python twin). `seam-selfheal` uses the older bare form for `seam_selfheal_last_success_timestamp`.

### D. Full cadence + touchpoint table

| Job | Schedule | Enabled | Non-platform touchpoints | Alerting exit |
|---|---|---|---|---|
| alerting-integrity | `*/5` | ✅ | `docker exec alertmanager` (/api/v2), `docker exec victoria-metrics` (query) | ntfy **security** topic direct + withholds an HC ping |
| alerting-watchdog | `*/5` | ✅ | `docker exec alertmanager` → `/api/v2/alerts?filter=alertname="Watchdog"` and `…="HomebaseMaintenanceWindow"` | HC ping only when Watchdog is firing |
| appenv-metrics | `*/5` | ✅ | volume globs + vm_push | never pages |
| auth-probe | `*/5` | ✅ | `/usr/bin/curl` against 5 Traefik/authentik hosts, per-row expectation (302 / 200 / 401) | never pages; vmalert owns it |
| authentik-db-backup | `20 2 * * *` | ✅ | `docker exec authentik-db pg_dump --clean --if-exists` → gz + sha256 | ntfy on failure |
| authentik-events-metrics | `*/5` | ✅ | curl authentik Events API through Traefik/LAN DNS | never pages |
| backup-drill-metrics | `50 3` | ✅ | conf projected from `syrvis/data.d` `restore.verified` | never pages |
| cert-metrics | `*/5` | ✅ | `openssl s_client` × ~15 declared hosts | never pages; cadence is load-bearing (vmalert 5m lookback) |
| cert-renew | `0 4 * * 1` | ✅ | acme.sh 3.1.1 (pinned tarball) DNS-01 via CF token, `synology_dsm` deploy hook, `/usr/syno/bin/synow3tool --gen-all`, `systemctl reload nginx` / `synosystemctl`, `openssl s_client 127.0.0.1:5001` verify; **chains `/volume4/syrviscore/jobs/cert-metrics` directly** | ntfy on failure; exits non-zero if an owed page didn't land |
| cf-expiry-metrics | `17 */6` | ✅ | curl Cloudflare API; hand-rolled `last-run.log` | never pages |
| dsp-endpoint-audit | `*/10` | ✅ | `docker logs --since 11m --tail 20000 docker-socket-proxy`, regex counts | never pages |
| host-sysctls | `37 * * * *` | ❌ staged | writes `/proc/sys/vm/overcommit_memory` | logger + non-zero rc |
| hourly-reconcile | `47 * * * *` | ❌ staged, **script does not exist** | would call `syrvis reconcile` | — |
| hyperbackup-metrics | `*/5` | ✅ | parses `synobackup.conf` + `last_result` under `/usr/syno/etc` | never pages |
| immich-db-backup | `0 2` | ✅ | `docker exec immich_postgres pg_dump` × 2 DBs (family + legal) | ntfy on failure |
| immich-stats-metrics | `*/15` | ✅ | curl both Immich admin stats APIs (scoped read-only keys) | never pages |
| intent-metrics | `*/5` | ✅ | see §B; emits `syrvis_service_declared_enabled`, `syrvis_service_running`, `syrvis_service_shed`, `syrvis_shed_count`, `homebase_maintenance_window`, `homebase_blast_radius` | never pages |
| login-alert | `*/3` | ✅ | byte cursor over `/var/log/auth.log`, watch `cerebrate\|admin\|root`, never `syrvis-operator` | ntfy **security**, cursor only advances on confirmed delivery |
| logrotate | `52 * * * *` | ✅ | `/usr/bin/logrotate` with its **own** conf + state file, `include`-ing DSM's stanzas | logger; non-zero rc |
| nas-heartbeat | `*/5` | ✅ | see §A | HC dead-man |
| ntfy-delivery-probe | `*/15` | ❌ staged | curl publish + poll ntfy.sh round trip | HC ping on success only |
| onyx-db-backup | `10 2` | ✅ | shed gate then `docker exec onyx-relational-db pg_dump` | ntfy on failure; **silent skip when shed** |
| seam-selfheal | `*/5` | ✅ | rewrites `/etc/passwd` (`sed` + atomic rename) restoring `/bin/sh` for `syrvis-operator`/`syrvis-reader` | logger + liveness metric |
| secret-metrics | `*/5` | ✅ | conf projected from `secrets/expiries.yaml` | never pages |
| share-metrics | `40 3` | ✅ | `/sbin/btrfs qgroup show -f --raw` per declared share | never pages |
| site-probe | `*/5` | ✅ | curl 11 Cloudflare Worker sites, assert 302→cloudflareaccess | never pages |
| syno-hdd-db | `17 3 * * 0` | ✅ | `/volume4/scripts/Synology_HDD_db/syno_hdd_db.sh -nr --autoupdate=3` (third-party, self-updating, root) | logger + non-zero rc |
| syrviscore-backup | `40 2` | ❌ gated | see §A | ntfy on failure |
| workspace-liveness | `37 */6` | ✅ | Google OAuth refresh-token exchange × 2 tenants | never pages |
| **rootfs/boot-integrity** | `*/10`, `/etc/crontab` **outside** the managed block | installed by break-glass runbook | asserts A0–A4: no renamed platform root on any `/volume[1-6]`, `bin/syrvis` executable **and** `current` resolves to a dir, managed block present + populated, every declared root non-empty | HC dead-man; a failure pings **nothing** |

## Interaction patterns

**Materialization is a two-channel dance.** `syrvis schedule sync` (SEAM) clones the root-configured `jobs.source` (`git@github.com:kevinteg/home-tech.git`, mirrored repo-side in `config/jobs.source`) and installs, for each *enabled* declaration, `jobs/<name>` as a root:root 0755 script plus the `/etc/crontab` line `<sched> root /volume4/syrviscore/jobs/<name>`. It does **not** install confs. Those go by a second channel: `scripts/apply-jobs --apply` renders `jobs.d/<name>.conf.tmpl` with sops values in memory and pipes it to `ssh -F ~/.config/syrviscore-mcp/ssh_config syrvis-nas -- sudo -n /volume4/syrviscore/bin/syrvis config set -- <name>` (`scripts/apply-jobs:169-186`) — stdin only, never argv. Ordering is load-bearing and documented in every declaration header: **commit + push first** (sync clones origin, not a working tree), regenerate `jobs/MANIFEST.sha256` in the same commit, sync, *then* `config set` (which refuses a name not already declared on the NAS).

**Supply-chain pinning.** `scripts/jobs_manifest.py` builds `<sha256>  jobs/<name>` rows for every file matching SyrvisCore's own `_NAME_RE_STR`, and `--print-pin` emits `<40-hex HEAD> sha256:<digest of the manifest>` for `syrvis schedule sync --to <rev> --manifest <sha256>` (0.5.17+). `config/jobs.pin` currently reads `PENDING` — the NAS still clones default-branch HEAD, i.e. **a push to this repo is still root code execution on the RS1221+, arriving through the remediation the monitoring itself recommends.**

**No shared library, so: copy, not source.** `jobs/_vm-push.sh` can never reach the NAS (no declaration names it, and the only other per-job file the platform installs is the operator-writable conf — sourcing that into an executed position violates design/20's config-render invariant). So 13 jobs carry the block byte-identically and `verify-all`'s `intent.vm-push-block` FAILs on any divergence.

**The config-render invariant shapes every script.** Confs hold only inert data (a URL, a path, an integer) and are parsed with `grep -m1 -E "^KEY=" | sed`, never `source`d. Anything reaching an executed position — the docker binary, the VM container name, the import URL, the syrvis wrapper path, logrotate's stanza list, cert-metrics' HOSTS — is **pinned in-script**.

**Result consumption is entirely out-of-band.** Cron discards stdout. So results leave via: VictoriaMetrics (13 jobs), Healthchecks.io dead-men (nas-heartbeat, alerting-watchdog, boot-integrity, ntfy-delivery-probe), ntfy direct-to-topic (the backups, cert-renew, login-alert, alerting-integrity — deliberately **not** through Alertmanager, whose plane some of them report on), `logger -t <job>`, and five hand-rolled truncate-every-run `last-run.log` files.

## Workarounds & missing verbs (the negative space)

1. **`syrvis-run-job` / `job-wrap` was never built** (design/13 §2, amended 2026-08-16). No per-job log, no rotation, no non-zero-exit escalation. Five jobs hand-roll a strictly worse substitute (`cf-expiry-metrics`, `logrotate`, `site-probe`, `workspace-liveness`, `ntfy-delivery-probe`): one `last-run.log`, truncated every run, zero history. The other ~20 have nothing. Cost, on the record: two metrics jobs killed invisibly for 24h by ash `set -e`; `authentik-events-metrics` dark ~6h; `backup-drill-metrics` dark **its entire life** while showing installed, hash-clean and `plan.in_sync`.
2. **No "hourly reconcile" and no verb safe enough to schedule one.** `jobs.d/hourly-reconcile.yaml` exists with no script, because an hourly reconcile before the shed overlay is an hourly *resurrect* of 14 deliberately-stopped services. The repair path design/60 §3.5 assumes exists is reached only by a human typing `syrvis resume`.
3. **No metrics-push verb**, so `intent-metrics` derives `homebase_maintenance_window` on the box from the shed rows' `until` — the header says outright that `scripts/maintenance-mode` cannot produce it because "the seam verb registry has no metrics-push verb."
4. **No read verb for arbitrary platform state**, so `intent-metrics` regex-parses `services.d` and `onyx-db-backup` greps `intent.json`. The `hyperbackup-metrics` header notes its paths could not be verified before shipping because "the enumerated operator verb registry has no 'read an arbitrary file' verb."
5. **No `logging:` key in the service schema** → `jobs/logrotate` exists purely as a cadence over DSM's own stanzas. **No per-container `sysctls:`** (blocked by `ALLOWED_TOP_LEVEL_KEYS`) → `jobs/host-sysctls` writes `/proc/sys` directly and its real fix is a hand-created DSM Task Scheduler boot-up task, because a 5-field cron spec cannot say `@reboot`.
6. **DSM reverts login shells on any user-db write** → `seam-selfheal` rewrites `/etc/passwd` every 5 minutes to keep the seam alive. The plane that keeps the seam alive was itself unmonitored until a liveness metric was bolted on (gap G-7).
7. **`restart` cannot re-read `env_file`** (env baked at CREATE) — `appenv-metrics` exists because the platform's documented mitigation does not work, and `onyx-opensearch` needed a manual stop+start.
8. **The self-heal that isn't**: the S99 boot hook trampolines *into* `$SYRVIS_HOME`, so the 2026-08-16 share-collision rename took out the healer, the wrapper, the seam and all 24 jobs at once — including `nas-heartbeat`'s `rc=127` "syrvis binary missing" branch, which was structurally unreachable. The answer was to move a gate **off the platform entirely**: `rootfs/boot-integrity` on the DSM rootfs, in `/etc/crontab` outside the managed block, installed by hand.
9. **Preventive fixes blocked by maintenance windows are re-implemented as jobs.** `dsp-endpoint-audit` and `alerting-integrity` both ship as detective controls *because* `schedule sync` "bounces nothing, pulls nothing, and touches no /volume5 IO" while `deploy-stack --apply` is forbidden until 2026-08-24. The jobs plane is the escape hatch from the deploy plane's own gating.

## Observations for a v2 agent design

- **Own the wrapper, or the plane will keep hand-rolling it.** A consolidated agent must run jobs itself: timestamped per-run log with rotation, exit-code capture, non-zero → notification, and a durable success assertion. That single feature retires five `last-run.log` hacks, the whole `vm_push` copy-13-times contract, and the lying-collector class. Two hard constraints inherited: the job-name-as-argv exception needs design/12 §1's five validation conditions, and **the log backstop must not live under `$SYRVIS_HOME`** — it would share fate with the failure it exists to survive.
- **A declaration of `{schedule, enabled}` is too thin and too rigid.** It cannot express `@reboot`, dependencies, a timeout policy, a conf binding, or a proof contract — so all of that migrated to repo-side registries (`config/job-confs.yaml`, `jobs.pin`, `jobs.source`) that the box cannot read, and to DSM Task Scheduler tasks nothing in git can see. v2 should carry richer declarations *and* absorb the boot-time class.
- **Give the plane a local read API.** Jobs need declared-enabled, shed state, container↔service mapping and version/root health from a source that works when the CLI does not. Today that is a regex over YAML and a grep over JSON. A stable, root-readable local state contract (or a static-binary read path with no venv/symlink dependency) removes both hacks and the `current`-doesn't-resolve failure mode.
- **Add a metrics/telemetry sink verb.** Thirteen jobs `docker exec` into a container to reach a loopback port because the platform offers no push path — which also means the whole observability plane dies with one container and depends on the flat `proxy` bridge that `alerting-integrity` exists to police.
- **Shed/intent must be a first-class query for job authors, not a JSON grep.** `onyx-db-backup`'s "skip and don't page, let the age climb" is exactly the right semantics and is implemented by string-matching a private file.
- **Preserve the escape hatch, and design the reconcile verb so it can be scheduled.** The jobs plane's value is that it lands without bouncing anything; a v2 agent that couples job installation to deploys loses that. Conversely, an agent-owned reconcile with breakers, backoff, an enable-change guard and a shed overlay is what would finally let `hourly-reconcile` exist — the single largest missing verb in this slice.
- **Keep a gate outside the agent's own tree.** `rootfs/boot-integrity` is the lesson written down: a check that shares fate with the thing it checks is not a check. Whatever v2 self-heals, something on the rootfs must still assert that v2 itself is present, resolvable and scheduled.