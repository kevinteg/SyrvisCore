# v2 packaging: through the native installer

## The one-package model

v2 ships **one SPK, one payload binary, no bootstrapper**. `package.tgz` contains `bin/syrvisd` (static linux/amd64 or arm64, stdlib-only, ~25 MB), a `syrvis` symlink to the same binary (subcommand dispatch on argv[0]), the dashboard SPA under `ui/`, and nothing else executable. Everything v1 did *after* install — download a wheel, build a venv, symlink a version, run an interactive setup — is now either DSM's job or the daemon's first-start convergence.

**INFO** (CI-generated from source; the `version="0.1.21"` drift class dies with a build assertion):

```
package="syrviscore"
version="2.0.0-0001"              # CI: must equal internal/version.Version
displayname="SyrvisCore"
arch="x86_64 apollolake geminilake denverton broadwellnk"   # UNCERTAIN, see below
os_min_ver="7.0-40656"            # floor for conf/privilege capabilities
startable="yes"                   # v1's "no" is why package status was decorative
support_move="yes"                # DSM relocates the payload; it is one file
install_dep_packages="ContainerManager"
start_dep_services="pkgctl-ContainerManager"
install_conflict_packages="syrviscore-manager"
auto_upgrade_from="2.0.0"
thirdparty="yes"
silent_install="no"
```

`arch` is **UNCERTAIN** — Synology arch families are enumerated, not generic, and a static Go binary runs on all x86_64 families regardless. Defensive design: CI emits one `.spk` per declared family from the same two binaries, plus a `noarch`-free fallback list, and the feed's `arch` query selects. `install_dep_packages` is also **UNCERTAIN** across DSM 7.0/7.1 (`Docker`) vs 7.2+ (`ContainerManager`); CI emits two INFO variants keyed by `os_min_ver`, and — the load-bearing part — **the daemon never assumes the declaration worked**: a missing `/var/run/docker.sock` is the named state `waiting-for-docker`, polled with backoff, reported in status, never a crash and never a failed install.

**conf/privilege** — the one place where the design must be honest:

```json
{
  "defaults": { "run-as": "package" },
  "tool": [
    { "relpath": "bin/syrvisd", "user": "package", "group": "package",
      "permission": "0750",
      "capabilities": "cap_dac_override,cap_chown,cap_fowner,cap_net_admin,cap_net_raw" }
  ]
}
```

Root-by-declaration (`run-as: root`, or a `ctrl-script` override) is real syntax but gated behind Synology signing — that is v1's Error 276 seen from the other side, and it does not move. Capabilities on a named binary are available today and are the interesting finding: `cap_dac_override` covers the Docker socket, foreign-uid app homes, and writes under `/usr/local/etc/rc.d`; `cap_net_admin`+`cap_net_raw` cover the macvlan shim. But whether DSM actually applies the capability set for an unsigned third-party package, and which caps it will accept, is **UNCERTAIN**.

So privilege is a **declared tier, probed at first start, and reported**:

| Tier | How obtained | What works |
|---|---|---|
| `tier-root` | shipped `conf/systemd/pkg-syrvisd.service` with no `User=`, **or** one-time `sudo syrvisd bootstrap` | everything, incl. share creation, group management, rc.d writes |
| `tier-cap` | `conf/privilege` `tool` capabilities | Docker API, app-home ownership, macvlan, rc.d writes |
| `tier-user` | neither | read plane, declaration validation, `Census`, `Status`; every converge method refuses with a named recovery |

`syrvisd selftest --privileges` runs at every start, writes the tier to `state.db`, and every API method declares its required tier. A method that cannot run returns `{"guard":"privilege_tier","have":"tier-user","need":"tier-cap","recovery":"sudo /var/packages/syrviscore/target/bin/syrvisd bootstrap"}` — refusal with recovery, never silent degradation. This is the same doctrine v1 earned the hard way, applied to the thing v1 papered over.

The systemd path (`conf/systemd/pkg-syrvisd.service`, copied by the framework to `/usr/local/lib/systemd/system` on acquire) is **UNCERTAIN and worth one NAS experiment before the design freezes**, because it settles privilege *and* supervision at once: a system unit with `Restart=on-failure`, `RestartSec=5`, `WatchdogSec=60` (Go implements `sd_notify` by writing to `$NOTIFY_SOCKET` — stdlib only) is the only native crash supervision DSM offers. If the experiment fails, the fallback is explicit and small: `start-stop-status start` execs the binary with a pidfile, and the rootfs rescue stub owns liveness — **one** dead-man that starts the daemon if absent, not v1's relay of heals.

`conf/resource`:

```json
{
  "usr-local-linker": { "bin": ["bin/syrvis", "bin/syrvisd"] },
  "data-share": { "shares": [ { "name": "SyrvisCore",
      "permission": { "rw": ["administrators"] }, "once": true } ] }
}
```

`usr-local-linker` is the sanctioned replacement for the `/usr/local/bin/syrvis` symlink Error 276 blocked and for the profile-sourcing dance. `data-share` is used for **exactly one** share — the operator-visible anchor on the package volume — because the worker exposes no volume field (**UNCERTAIN placement**); every other share is declared in `shares.d/` and created by the daemon at `tier-root`/`tier-cap`. If neither is available, the daemon places under an *existing* share and says so. What it never does again is create a bare directory at a volume root.

`start-stop-status` uses the full exit vocabulary it always had available: `0` running, `1` dead-with-pidfile, `3` not running, **`150` broken/reinstall** — which is the honest answer when the managed root has vanished or a collision sibling exists. Empty `SYNOPKG_PKG_STATUS` is DSM saying "this is boot"; the daemon reads it instead of inferring boot from the S99 hook's existence.

**Delta table — every v1 chain step and its v2 owner:**

| v1 mechanism | v2 owner |
|---|---|
| SPK as bootstrapper; 4-step README in `description` | SPK payload = the product (DSM) |
| `syrvisctl install` → GitHub download → wheel | Package Center + own package source (DSM) |
| venv-per-version, offline wheels, shebang/`pyvenv.cfg` rewrite, tree chmod | deleted |
| `versions/<v>` + `current` symlink | `var/binaries/<ver>` + `current` symlink (one file, atomic rename) — syrvisd |
| `syrviscore.profile`, "source this" | `usr-local-linker` (DSM) |
| `bin/syrvis` wrapper with `SYRVIS_HOME` baked in | `/usr/local/bin/syrvis` → socket client |
| `syrvis setup` interactive + self-elevation | wizard defaults + first-start convergence (`syrvisd install --from-wizard`, idempotent, returns `(changed, reason)`) |
| `pkgwizard_volume` custom combobox | `SYNOPKG_PKGDEST_VOL` (DSM) |
| `preinst` Docker probe (blocked by 276) | `install_dep_packages` / `start_dep_services` (DSM) + daemon's `waiting-for-docker` state |
| `S99syrviscore.sh` → `startup.sh` → cron heals | systemd unit (or rc.d stub) → `syrvisd --boot` phase machine |
| four writers of boot artifacts | one writer: `syrvisd bootstrap`, same code path as `doctor --fix` |
| `seam-selfheal` cron, `/etc/passwd` heals | forced command is the binary's own subcommand |
| sudoers glob + shim + charset allowlist + 18 slot kinds | UNIX socket + in-process role check |
| docker group membership | `cap_dac_override` on the socket |
| `.syrviscore-manifest.json` as truth | `state.db` (derived) + `.syrvis-volume.json` markers (identity) |
| dashboard container + image lockstep gate | SPA in `ui/`, served by the daemon |
| manager/service package split | one binary, subcommands; the split's real value survives as the rootfs rescue copy |

## Install and upgrade flows

**First install.** The operator adds the package source (or drops the `.spk`), picks a volume in Package Center's native picker, and answers a wizard that is deliberately near-empty. `WIZARD_UIFILES/install_uifile.sh` runs at wizard time and emits JSON to `$SYNOPKG_TEMP_LOGFILE`, built from live system state — `/proc/mounts` + `df` — so the volume options are real, labelled, and capacity-annotated rather than hard-coded:

| Field | Type | Default | Note |
|---|---|---|---|
| `pkgwizard_default_app_volume` | combobox (generated) | `$SYNOPKG_PKGDEST_VOL` | label: `volume4 — btrfs — 12.1 TB free / 21.8 TB — healthy` |
| `pkgwizard_default_bulk_volume` | combobox (generated) | largest healthy volume | may equal app volume |
| `pkgwizard_apps_root` | textfield, regex-validated | `syrvis-apps` | v1's `SYRVIS_APPS_ROOT_NAME`, now first-class |
| `pkgwizard_create_shares` | checkbox | on | register roots as DSM shares — the backup-visibility and anti-reclaim decision |
| `pkgwizard_instance_name` | textfield | hostname | identity in markers and attestation |

**No secrets.** Wizard values land in environment variables visible to the whole script chain and to DSM logs; v1's `pkgwizard_cloudflare_token` was a mistake and is not repeated. Operator SSH keys and service secrets arrive later over stdin (`syrvisd seam enroll`, `Config.SetSecret`).

The `pkgwizard_` → env handoff is documented for DSM 6 and universally observed, but not restated on the 7.2 v2 (Vue) wizard page — **UNCERTAIN**. Defensive design: every wizard field has a working default, the answers are only a *seed*, and if nothing arrives, first start uses defaults and `Status` reports `placement.source: "defaulted (wizard values not received)"` rather than pretending they were chosen.

`postinst` does no privileged work. It writes `$SYNOPKG_PKGVAR/bootstrap.json` from the wizard env, then execs `target/bin/syrvisd install --from-wizard --seed-only`, tees to `$SYNOPKG_TEMP_LOGFILE`, `/var/log/packages/syrviscore.log`, and `/tmp/syrviscore_postinst.log` with `set -x` — Package Center's error surface is a number, and per-script logs are the only forensics that survive. `preupgrade` exits 0 unconditionally when there is nothing to upgrade (it runs on fresh installs; v1 learned this by aborting).

First start, in order: privilege probe → volume census → marker resolution → `state.db` open/migrate → share/root materialization → seam socket up → reconcile loop. The whole thing is idempotent and re-runnable as `syrvisd doctor --fix`; there is no second manual step, which is the reproducibility rule the project already writes down.

**Upgrade.** DSM's contract is: stop old → `preupgrade` → **uninstall old** → install new → `postupgrade` → restart. `target` is not preserved, so nothing durable may live there — which is already true here. `var` (`@appdata`), `etc` (`/usr/syno/etc/packages`), and data shares survive.

Versioned rollback without venvs: on every successful start, `syrvisd` copies its own binary to `$SYNOPKG_PKGVAR/binaries/<version>/syrvisd` (verified by sha256, retention N=3, ~75 MB total vs v1's 962 MB of retained versions) and points `$SYNOPKG_PKGVAR/current` at it. Rollback is `Version.Activate{version}` — an atomic symlink swap plus a restart, no Package Center round trip. Because DSM believes it installed exactly one version, the pin is recorded as **intent** and reported loudly: `version: running 2.0.3 (pinned), package 2.0.4 — rollback active since …`. Rollback is bounded by the state schema: `state.db` records a `schema_floor`, migrations are forward-only, and activating a binary below the floor **refuses** — unknown schema ⇒ report unknown and refuse to act, the journal's rule promoted to the whole protocol.

**Rescue / DR.** The SPK alone restores: the payload binary, the rootfs rescue copy at `/usr/local/lib/syrviscore/syrvisd-rescue`, the six-line `/usr/local/etc/rc.d/S99syrviscore.sh` stub, the `/usr/local/bin` links, and `syrvisd rescue` — which answers `Census.Get` and `Status.Get` **with no resolvable home**: roots and collision siblings across all volumes, `current`, mounts, `mdstat`, `df`. That is `syrvisctl doctor`'s lesson made permanent, and it ships before the agent owns anything.

The SPK does not restore declarations (Home Kit re-pushes them — that is the point of the deployment path), app data (Hyper Backup restore of visible shares), or `state.db`. Derived state is by definition rebuildable; the two exceptions — **durable intent** and the **revision index** — are mirrored to files under the visible share on every write, so a rebuilt database re-imports them. `rootfs/boot-integrity` stays a hand-installed gate outside the agent, forever: a check that shares fate with the thing it checks is not a check.

**Distribution.** A self-hosted package source (`https://pkg.<domain>/`), a static JSON feed generated by CI behind Cloudflare, answering Package Center's `arch`/`build`/`major`/`minor`/`micro`/`language`/`package_update_channel` query — `spkrepo` is the readable reference implementation. Channels: `stable` and `beta`. That buys native update notifications and Package Center's auto-update. Manual `.spk` install stays the DR path and the air-gap path.

Signing: DSM 7 removed third-party packing-stage signing while the trust-level UI still ships — **UNCERTAIN and not depended on**. Integrity is asserted out-of-band: CI publishes `SHA256SUMS` plus a detached minisign/cosign signature; the binary embeds the public key and verifies **itself** at start, reporting `payload: verified | unverified (sha mismatch)`. An unsigned third-party `.spk` installs fine — until it asks for root, which this one does not.

CI gates the SPK from day one (v1 never built it in CI, which is why `spk/INFO` still says `0.1.21`): `sh -n` + shellcheck on every script; outer tar is **uncompressed** (Error 263); `scripts` is a **directory**, owner 0/group 0, mode normalized (Error 313); `start-stop-status` present (Error 261); INFO is ASCII/LF/no-BOM with no `firmware=`; INFO `version` equals source version; wizard JSON validates; reproducible tar (`COPYFILE_DISABLE=1 --sort=name --owner=0 --group=0 --mtime=@$SOURCE_DATE_EPOCH`); and an install/upgrade/uninstall smoke run in a DSM-shaped fixture.

## What dies

The `syrvisctl install` downloader, the release `config.yaml` asset and its bundling, the entire venv-per-version machinery (staging dirs, `venv.new`, shebang relocation, `pyvenv.cfg` rewriting, offline wheel bundles, recursive chmods), the `syrviscore-manager` package and the two-CLI split, `syrviscore.profile` and the source-this instruction, the hand-made `/usr/local/bin/syrvis` symlink, the `bin/syrvis` wrapper with `SYRVIS_HOME` baked into it, `.syrviscore-manifest.json` as a source of truth, `setup_complete` as a status proxy, `sudo` self-elevation and its re-exec, `startable="no"` and the 0/1 status vocabulary, the `S99syrviscore.sh` → `syrvis-startup.sh` → cron-heal relay (reduced to one stub whose only job is `exec syrvisd-rescue --boot`), all four writers of the boot artifacts, `seam-selfheal` and the three duplicated `/etc/passwd` heals, `/etc/sudoers.d/syrviscore-mcp` and the forced-command shim with its charset allowlist and 18 slot kinds, the auto-seam-sync fail-open-on-width hole, docker-group membership as a mechanism, the custom `pkgwizard_volume` combobox, the wizard secret field, the dashboard container image and its prose-enforced lockstep gate, the dead DSM simulator (replaced by a real DSM integration lane plus the fixture harness), and `BOOT_HOOK_CONTRACT` as a hand-maintained integer.

# v2 volume model: placement as a first-class concept

## Volume identity

The daemon builds a **volume census** at start, on mount/unmount events, and on a slow timer. Per volume, from `/proc/mounts` + `statvfs` + `/dev/disk/by-uuid` (stdlib only, no vendor tool):

```go
type Volume struct {
    ID        string // filesystem UUID — the stable identity
    Path      string // "/volume4" — a *label*, not an identity
    Device    string // "/dev/mapper/vg1-volume_4"
    FSType    string // btrfs | ext4
    Mounted   bool
    ReadOnly  bool
    TotalB, FreeB uint64
    Health    Health // healthy | degraded | unknown  (mdstat + best-effort DSM API)
    Role      Role   // fast | bulk | cold | unassigned
    IsPackageVolume bool
}
```

`synowebapi --exec api=SYNO.Core.Storage.Volume` enriches health and pool naming, wrapped behind one adapter, timeout-bounded, **never load-bearing** — a failure yields `Health: unknown`, and unknown is never a pass.

`/volumeN` is Synology metadata assigned per storage pool. It changes across migration and pool re-creation, DSM 7 removed the supported renumber path, and this box has the local proof: DSM renamed `/volume4/syrviscore` to `syrviscore_1` at a cold boot. So **identity is UUID plus a self-describing marker**, written at every managed root:

```
/volume4/SyrvisCore/.syrvis-volume.json
{"schema":1,"instance_id":"…","volume_id":"e3b0c442-…","role":"fast",
 "apps_root":"syrvis-apps","share":"SyrvisCore","path_at_write":"/volume4/SyrvisCore",
 "created":"2026-09-01T…Z"}
```

and at every app home (`.syrvis-app.json` with `name`, `instance_id`, `volume_id`, `path_at_write`).

Declarations stop naming raw paths. `location:` becomes a parsed union — v1's `LOCATION_RE = ^/volume\d+$` is replaced by:

| Form | Meaning |
|---|---|
| `vol:fast` / `vol:bulk` / `vol:cold` | by declared **role** — the preferred form |
| `vol:uuid:e3b0c442` | by identity (prefix-matched, ambiguity is an error) |
| `vol:default` | the device's default app volume |
| `/volume4` | legacy literal: accepted during migration, resolved to a UUID, and reported as `deprecated_placement` |

Resolution happens at **plan time**, and an unresolvable reference is a `blocked` bucket with named recovery — never a guess, never a silent fallback to another volume.

**Renumber/rename survival** is discovery-by-marker, not path memory. At boot the daemon scans every mounted volume for `.syrvis-volume.json` matching its `instance_id`, then compares against `state.db`:

| Observed | Classification | Action |
|---|---|---|
| marker found, `volume_id` matches, path differs | **renumbered/renamed** | accept, rewrite derived paths, log loudly, emit event |
| recorded path absent, no marker anywhere | **root missing** | `halted: root-missing`, `sss` exits 150 |
| recorded path absent, sibling `<name>_1` present with our marker | **root collision** | `halted: root-collision`, 150, refuse all converge, refuse install |
| path present, marker absent or foreign `instance_id` | **foreign root** | refuse; never adopt, never overwrite |

That table is the collision-incident answer, and it has four legs. First, **nothing SyrvisCore owns is a bare directory at a volume root** — managed roots are registered DSM shares, so `synocheckshare` has a record and no reclaim race exists to lose. Second, discovery is marker-driven, so even a renamed root is *found*. Third, `Census.Get` explicitly reports `collision_siblings: ["/volume4/SyrvisCore_1"]` from the rootfs with no resolvable home. Fourth, the failure is **loud in DSM's own UI** via exit 150, and every write path refuses — v1's 0.5.16 "refuse to install while a collision-renamed root exists" guard survives verbatim, generalized from install to all converge methods.

## Placement policy

Device defaults are seeded once by the wizard and thereafter live as a plain declaration file that Home Kit can own:

```yaml
# <root>/declarations/placement.yaml
apps_root: syrvis-apps
volumes:
  e3b0c442-…: { role: fast, alias: nvme }
  9f2a1d77-…: { role: bulk, alias: array }
defaults:
  app_volume:  vol:uuid:e3b0c442
  bulk_volume: vol:uuid:9f2a1d77
  class_map:   { database: fast, media: bulk, logs: bulk }
guards:
  min_free_pct: 10
  refuse_on_degraded: true
```

Precedence, reported with its source on every resolution: per-volume override (`volume_locations:`) → per-service `location:` → service `class:` → device default → refuse. `Status.Get{sections:["placement"]}` returns, per service and per named volume, `{declared, resolved_path, volume_id, source: "service"|"class"|"default", state: ok|blocked}` — so "which volume am I on" is one read, per byte, with its reason.

**All volumes are presented everywhere, from one census.** `Volumes.List` is the single API; `syrvis volumes` renders it as a table, the dashboard renders it as a picker with capacity bars and health chips, and `install_uifile.sh` renders the same *shape* at wizard time from `/proc/mounts` + `df` (it cannot call a daemon that does not exist yet). Because the wizard's view is independently derived, first start **re-validates every wizard answer** against the real census and refuses an unmounted or unknown volume with a named error rather than quietly defaulting.

Validations retained from v1, unchanged in intent: containment assertion (the resolved app path, symlink-resolved, must live under `<root>/<apps-root>/apps/<name>`), mounted-volume check on every materialize and start path, per-volume immutability while populated (a changed placement is **refused**, with `Placement.Move` named as the recovery), purge coverage for every placed volume, and the override named in a comment in the exported compose projection. Added: a capacity precheck (`min_free_pct`, read-only volume refusal), the `bulk_degraded` guard promoted to a placement-time check, and `Placement.Move{service, volume}` as a real job — drain → copy → digest-verify → repoint → verify → release — rather than an edit a human is expected to perform out of band.

## Storage layout

```
/var/packages/syrviscore/target/            → /volume<P>/@appstore/syrviscore   (replaced on upgrade)
    bin/syrvisd, bin/syrvis→syrvisd, ui/
/var/packages/syrviscore/var/               → /volume<P>/@appdata/syrviscore    (survives upgrade)
    state.db (WAL, single writer), binaries/<ver>/syrvisd, current→, bootstrap.json,
    run/syrvisd.sock (root:operator 0660), logs/
/var/packages/syrviscore/etc/               → /usr/syno/etc/packages/syrviscore (system partition)
    instance.json   # instance_id + last-known root pointers; tiny, boot-critical
/usr/local/lib/syrviscore/syrvisd-rescue    # rootfs copy, zero volume dependency
/usr/local/etc/rc.d/S99syrviscore.sh        # 6-line stub: exec syrvisd-rescue --boot
/usr/local/bin/{syrvis,syrvisd}             # usr-local-linker

/volume<A>/SyrvisCore/                      # a REAL DSM shared folder
    declarations/{stack.yaml, services.d/, jobs.d/, shares.d/, data.d/, placement.yaml, .env}
    state-export/{status.json, intent.json, revisions.json, state.db.bak}
    logs/
/volume<X>/<apps-root>/apps/<name>/{data,config,secrets,logs}   # apps-root is a share too
```

The split is deliberate and follows DSM's own convention (Container Manager: visible `docker` share, hidden `@docker` state). **Derived state goes to `@appdata`** because DSM preserves it across upgrade, it is single-writer, and no human authors it. **Everything a human or another repo authors goes in a visible share** because that is what `rsync`, `git`, File Station, and Hyper Backup's share selection can see. `@appdata` is invisible to Hyper Backup's share picker — so the daemon exports `status.json`, `intent.json`, `revisions.json`, and a consistent `state.db.bak` into `state-export/` on every intent write and on a timer. That is design/53's backup-visibility lesson kept, with its **hidden-share-for-backup construction deleted**: the anchor is an ordinary share, the hidden flag is cosmetic and never load-bearing, and the backup story is "select the share."

The light-payload doctrine survives with a new number. v1 targeted 1.5–2 GB for `SYRVIS_HOME` and actually held 962 MB of retained venvs. v2's package volume carries a binary, three retained binaries, `state.db`, and bounded logs — **budget 500 MB, alarmed at 80%** — and no L2 service keeps durable or bulky data there unless a declaration explicitly places it there. The doctrine's substance is unchanged: every byte has one declared owner, one declared home, one declared backup story.

## Migration from v1

1. **Pre-flight on 0.5.17.** `syrvis export --json` into Home Kit; `syrvisctl backup create`; `ls -d /volume*/syrviscore*` — if a `syrviscore_1` sibling exists, resolve it before anything else. Record `SYRVIS_APPS_ROOT_NAME`, every `location:`/`volume_locations:` value, `intent.json`, and the deploy journal.
2. **Register the root as a share.** Create the DSM shared folder `SyrvisCore` on volume4 and move `/volume4/syrviscore`'s config/data into it (Control Panel, or `synoshare --add` under break-glass). Do the same for the apps-root. This single step ends the reclaim race that decapitated the platform, and it is worth doing even if v2 slips.
3. **Install the v2 SPK** on volume4 via Package Center. Wizard: app volume = volume4, bulk = the array volume, apps-root = the existing segment verbatim, create-shares = on.
4. **Import.** `syrvisd import --from-v1 /volume4/syrviscore --dry-run` reads the manifest, `services.d/`, `stack.yaml`, `.env`, `jobs.d/`, `shares.d/`, `intent.json`, `deploy-journal.json`, `breakers.json`, and the revision store, and prints a plan. `--apply` writes `state.db`, moves declarations into the share, lowers shed rows into service-scoped drains, and inverts `maintenance-state.yaml` from source to generated mirror.
5. **Rewrite placement.** The daemon emits the declaration set with `/volume4` literals rewritten to `vol:uuid:…` plus role tags; **Home Kit commits that diff** — declarations stay repo-owned, the daemon only proposes.
6. **Cutover.** `syrvis shutdown --reason maintenance` on v1; `syrvisd converge --plan` must show only expected diffs; release the drain. Running containers are **adopted by label** and compared by spec-hash — a byte-identical service is not recreated, which is G1 generalized.
7. **Retire the old plane.** Enroll the operator key against the new forced command (`syrvisd seam-proxy`); delete `/etc/sudoers.d/syrviscore-mcp`, the shim, `seam-selfheal`, the S99 → startup.sh chain, the profile snippet, and the `/usr/local/bin/syrvis` hand symlink. Keep `rootfs/boot-integrity`.
8. **Decommission.** Remove `/volume4/syrviscore/versions` (962 MB), then uninstall the v1 SPK — last, and only after v2 has answered `Census` from the rootfs across at least one reboot.
9. **Acceptance gate: two reboots.** A cold boot with Docker slow to start (daemon must sit in `waiting-for-docker` and converge, not fail), and a boot with the share deliberately renamed to `SyrvisCore_1` (daemon must report `root-collision`, `sss` must exit 150, and every converge method must refuse). The second test is the incident, written as a test — which is the rule this project already holds itself to.