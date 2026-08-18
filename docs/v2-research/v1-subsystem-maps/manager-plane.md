# The Manager Plane (`syrvisctl`), SPK packaging & build tooling

## Purpose & role in the system

The manager plane is SyrvisCore's *install and lifecycle substrate*. It is the only thing DSM's Package Center ever sees, and it owns exactly one job: getting a **service version** onto the box, making one of them **active**, and being able to put the box back the way it was. It deliberately knows nothing about Docker, Traefik, services, or the domain — those belong to `syrvis` (the service package) and to the deployment repo.

Three structural commitments define it:

1. **Split packages.** The SPK installs *only* the manager (`syrviscore-manager`, ~20 KB wheel + 6 pinned dependency wheels) into `/var/packages/syrviscore/target/venv`. Service versions are installed by `syrvisctl` into `/volumeX/syrviscore/versions/<v>/cli/venv`. The SPK is immutable and rarely reinstalled; the service updates frequently.
2. **The manager never imports the service.** Separate venvs, separate Pythons. Every cross-boundary call is a `subprocess.run` of the *version's own* console script or venv python (`_regenerate_boot_hooks`, `probe_min_manager_version`, `seam_sync._render`). This is what lets `syrvisctl doctor` diagnose a tree that is itself broken.
3. **Library/adapter split.** `cli.py` is a thin Click shell; every library module raises typed `SyrvisError` subclasses and never prints (progress goes through an optional `log` callback, decisions through a `confirm` callback).

## Key modules and files (path — role — approx size)

Manager package — `packages/syrviscore-manager/src/syrviscore_manager/`:

| File | Role | LOC |
|---|---|---|
| `cli.py` | Click shell: install/uninstall/list/activate/rollback/check/info/doctor/cleanup/backup/restore/seam; sudo re-exec; collision precheck | 1211 |
| `version_manager.py` | Staged install, activate, uninstall, cleanup, GitHub install orchestration, compat gate, boot-hook regen | 632 |
| `backup.py` | Archive create/list/restore/cleanup, digests + sidecars, containment checks | 585 |
| `paths.py` | Home resolution, version-dir containment, atomic `current` switch, `bin/syrvis` wrapper + profile generation | 430 |
| `doctor.py` | Rootfs-only diagnosis: boot hook, volume-root census, seam account shells | 258 |
| `downloader.py` | GitHub release resolution, download, SHA256SUMS verify, version compare | 228 |
| `seam_sync.py` | Re-render sudoers + shim from the active version's seam generator | 184 |
| `manifest.py` | `.syrviscore-manifest.json` atomic read/write, history | 171 |
| `errors.py` | 14-member typed error taxonomy with stable `code` strings | 113 |
| `locking.py` | Non-blocking `flock` on `<home>/.syrviscore.lock` | 44 |

SPK: `spk/INFO`, `spk/conf/{privilege,resource}`, `spk/scripts/{preinst,postinst,preupgrade,postupgrade,preuninst,postuninst,start-stop-status}` (POSIX `sh`, ~1250 lines total), `spk/icons/`.

Build tooling: `build-tools/{build-manager,build-service,build-spk,build-tarball,release-service,validate-spk,bootstrap,syrviscore-bootstrap}.sh`, `select-docker-versions.py`, plus `Makefile` (347 lines) and `packages/syrviscore-manager/constraints-bundle.txt`.

Tests: `tests/test_manager_core.py` (768), `test_home_collision.py` (449), `test_paths.py` (487), `test_manager_cli.py` (276), `test_seam_sync.py` (232), `test_wrapper_fallback.py` (97), `test_manifest_permissions.py` (48).

## How it actually works

### SPK install flow & privilege model

`conf/privilege` is `{"defaults": {"run-as": "package"}}` — **every** lifecycle script runs as the unprivileged package user, never root. That single fact shapes the whole design:

- `preinst` checks Python 3, `ensurepip` (hard-fail — "python3 -m venv would not produce a working pip"), a warn-only `docker` presence check, and ≥500 MB on the package volume derived from `SYNOPKG_PKGDEST`.
- `postinst` creates `${SYNOPKG_PKGDEST}/venv`, pip-installs the manager wheel **fully offline** (`--no-index --find-links wheels/`), verifies `syrvisctl --version` actually executes, and writes `${SYNOPKG_PKGDEST}/syrviscore.profile` (a PATH snippet — there is **no** `/usr/local/bin/syrvisctl` symlink; the package user cannot create one).
- `postupgrade` is the hardened path: it builds `venv.new`, verifies `syrvisctl --version` **in staging**, then `mv venv → venv.old; mv venv.new → venv`, then rewrites baked paths in `bin/*` + `pyvenv.cfg` for both the symlink form and the `readlink -f` resolved form, then re-verifies and hard-fails on failure ("Do NOT swallow the failure with `|| echo unknown` (the old bug)").
- `preuninst`/`postuninst` refuse to delete service data (`SYRVIS_HOME` is owned by syrvisctl/syrvis, not the SPK); they write `UNINSTALLED.txt` and print the exact `sudo rm -f` commands for the boot hook and global symlink they cannot remove unprivileged.
- `start-stop-status` is a no-op for start/stop (`startable="no"`), but its `status` verb carries the 2026-08-16 incident fix: a failed home scan now **exits 1** — "a red package status is the cheapest out-of-band signal this failure can produce."

Privilege escalation for `syrvisctl` itself is *self-elevation*: `check_sudo_needed(path)` → `reexec_with_sudo()`, which `os.execv`s `sudo SYRVIS_HOME=… <python> <argv…> <extra_args>`. Critically, **every decision is encoded as a flag before elevation** (`--path`, `-y`), because "the elevated process starts from scratch."

### Version tree & manifest

```
/volumeX/syrviscore/
├── current -> versions/0.5.16          # symlink = SOURCE OF TRUTH
├── versions/<v>/{cli/venv, wheel/<whl>, build/config.yaml}
├── config/, data/, backups/, bin/syrvis, syrvis.profile
├── .syrviscore-manifest.json
└── .syrviscore.lock
```

`.syrviscore-manifest.json` (schema_version 3):

```json
{"schema_version":3,"active_version":"0.5.16","install_path":"/volume4/syrviscore",
 "setup_complete":false,"created_at":"…","versions":{"0.5.16":{"installed_at":"…",
 "status":"active","activated_at":"…"}},"update_history":[{"from":"0.5.15","to":"0.5.16",
 "timestamp":"…","type":"upgrade"}],"privileged_setup":{}}
```

`manifest.get_active_version` reads the **symlink first** and only falls back to the file (`paths.active_version` → `os.readlink` → `validate_version` on the basename). Writes are atomic (`mkstemp` in the same dir → `chmod 0644` → `os.replace`). `set_active_version` derives `type: upgrade|rollback` from `compare_versions`.

### Install: staging, then swap

`install_version` (version_manager.py:225) under `hold_lock`:

1. Build the whole tree in `versions/.staging-<v>` (venv, wheel cached into `<v>/wheel/`, optional `build/config.yaml`).
2. On any exception, `rmtree` staging and re-raise — a failed download/pip can never destroy a working version.
3. Move any existing `versions/<v>` **aside** to `.old-<v>` (not deleted), `rename` staging into place.
4. `_fixup_relocated_venv` rewrites the staging path → final path in every non-symlink file in `venv/bin` (console-script shebangs).
5. `set_tree_readable` (dirs 755, `bin/*` 755, everything else 644), then `_verify_cli_executes(syrvis --version)`.
6. On failure at 4–6: delete the new tree, `rename` `.old-<v>` back, re-raise.
7. Only then `manifest.add_version_to_manifest`.

`download_and_install` orders things so nothing existing is touched before the artifact is proven: resolve release → download wheel/config/SHA256SUMS → **verify checksum** → pre-upgrade backup → staged install → activate. Absence of a `SHA256SUMS` asset is a hard `IntegrityError` unless `--no-verify`. A failed pre-upgrade backup **aborts the install** ("Aborting to preserve a rollback point"). Release selection skips `manager-*` tags, prereleases, and drafts; `find_wheel_asset` requires `.whl`, contains `syrviscore-`, and *not* `manager`.

`--wheel` is the parallel offline path: version inferred from the filename via `SERVICE_WHEEL_RE = ^syrviscore-(\d+\.\d+\.\d+)-py3-none-any\.whl$`, no network, no backup, no checksum.

### Activate

`activate_version`: validate → version dir exists → venv `bin/syrvis` exists → `check_manager_compatibility` → `paths.update_current_symlink` (tmp symlink `.current.tmp` + `os.replace`, refusing if `current` is a real directory) → regenerate `bin/syrvis` wrapper + `syrvis.profile` → manifest → **best-effort** `_regenerate_boot_hooks` (runs the version's own hidden `syrvis _regen-boot-hooks`). The comment names the root cause: "activation swapped the code + symlink but never regenerated `syrvis-startup.sh` / the rc.d boot script, so an old-version hook … stayed frozen and the estate stopped auto-resuming at boot."

Compatibility gate: `probe_min_manager_version` runs the version's own venv python to read `syrviscore.__compat__.MIN_MANAGER_VERSION` (currently `"0.2.0"`); older/absent = no constraint. Too-old manager → `CompatibilityError`.

### The generated `bin/syrvis` wrapper

Baked `INSTALL_DIR` is the fast path, but if `$INSTALL_DIR/current` is not a symlink it globs `/volume[0-9]*/syrviscore` and `/volume[0-9]*/syrviscore_[0-9]*` for a manifest + `current`. Exactly one candidate → run it with four WARNING lines; two → refuse ("guessing between two installs is worse than refusing"); zero → an error that points at `ls -d /volume*/syrviscore*` and explicitly forbids reinstalling.

### Backup / restore

`backups/<version>[-N].tar.gz` + a `<file>.sha256` sidecar. Mode 0600 from creation (`os.open(..., 0o600)`) because archives carry `acme.json` and tunnel credentials. Metadata member `backup-metadata.json`:

```json
{"backup_version":1,"created_at":"…","version":"0.5.16","manager_version":"0.3.6",
 "reason":"pre-upgrade|post-setup|manual|pre-rollback","syrvis_home":"…",
 "layer2_services":["…"],"file_digests":{"<arcname>":"<sha256>"},
 "upgraded_to":"…","scope":"code-rollback"}
```

Pre-upgrade backups are **declarative-only** (`include_l2_data=False`): "an upgrade swaps the service venv and never touches `data/`". Live-data churn is tolerated — files that vanish between hashing and `tar.add` are counted and skipped, not fatal.

Restore is staged and verified: sidecar digest checked first; every member extracted into `.restore-staging/`, digest-compared against `file_digests`, and only then `os.replace`d into place; modes restored from the archive with `acme.json`/`.env`/`cloudflared` clamped to 0600. `_safe_dest` rejects absolute paths, `..` components, and any member whose realpath parent escapes the install path. The venv is rebuilt from the cached wheel **before** the `current` symlink moves — "a restore can never claim success while leaving a non-runnable installation active."

### `syrvisctl doctor`

The 2026-08-16 artifact, and the only command deliberately **not** wrapped in `@handle_errors` — "a home-resolution failure is this command's PRIMARY FINDING." It runs from the SPK venv, imports nothing from the service package, and answers three questions from the rootfs: (1) `/usr/local/etc/rc.d/S99syrviscore.sh` present/executable/`# boot-hook-contract: N` ≥ `MIN_BOOT_HOOK_CONTRACT = 3`, plus `/usr/local/etc/syrviscore-boot.env` presence; (2) a `/volume*/{syrviscore,<apps-root>}*` census classifying each root `platform` / `renamed` (`<name>_\d+`) / `other`, with `manifest`/`apps`/`current_symlink` markers — the apps-root segment read from the rootfs boot-env cache via `_APPS_ROOT_RE`; (3) `syrvis-operator` / `syrvis-reader` login shells read straight from `/etc/passwd` (absent = fine, `nologin` = DENIED). Exit 1 on any finding.

`assert_no_collision_artifacts()` (cli.py:148) turns that census into a **precheck on `install`**, before path resolution and before elevation, raising `CollisionError` with a per-root `sudo mv` line: "`syrvisctl doctor` has said 'Do NOT run syrvisctl install' since 0.5.9; prose is not a guard." Break-glass: `--ignore-collision`.

### Seam sync (manager side)

`/var/log/syrviscore-mcp-provision/seam-policy.json` (written by `syrviscore.seam.gen provision`): `{auto_seam_update, operator, syrvis_home, syrvisctl_path, shim_path}`. `_render` execs `<home>/current/cli/venv/bin/python -m syrviscore.seam.gen {sudoers|shim} --home … --operator … --syrvisctl … --shim-path …`; "No module named" is translated into a legible "needs service >= 0.4". `_install` writes atomically via a **dotted** temp name in the target dir (sudo's `#includedir` ignores dotfiles), validates with `visudo -cf` when present, installs sudoers 0440 / shim 0755. `auto_sync_after_activate` is called from `install`, `activate` and `rollback`, and never fails the switch.

### Build chain

`build-manager.sh` cleans `dist/`, builds the wheel, then `pip download --only-binary=:all: --python-version 3.8 --platform any --constraint constraints-bundle.txt` (click 8.1.7, requests 2.32.3, urllib3 2.2.3, certifi 2025.1.31, idna 3.10, charset-normalizer 3.4.0) — pure-Python only, **no silent fallback**. `build-spk.sh` stages a version-rewritten `INFO` copy (never mutating the source), pins the bundled wheel to `MANAGER_VERSION` (guarding the "stale `ls | head -1` shipped an old manager" bug), builds `package.tgz` (gzip) then the outer SPK as **uncompressed** tar ("gzip causes Synology error 263"), and sets 755 on `scripts/` ("prevents error 313"). `release-service.sh` pins `EXPECTED_WHEEL` to `$VERSION` (the "0.3.10-shipped-0.3.9 bug"), guards `dashboard __version__ == pinned compose image tag`, and generates the `SHA256SUMS` the downloader requires. `build-tarball.sh` produces the devkit (bootstrap + wheels + SHA256SUMS), reproducible under GNU tar via `--sort=name --mtime=@$SOURCE_DATE_EPOCH`.

## Design decisions & their rationale

- **Symlink, not manifest, is truth** — manifest.py:9-12; the manifest "mirrors it for convenience and history."
- **Stage-then-swap everywhere** — version_manager.py:8-12 and postupgrade's `venv.new`. Same shape in three places.
- **Non-blocking lock** — locking.py:24-26: "raises LockError immediately … the right behavior for both interactive use and automation."
- **Checksum-by-default with explicit opt-out** — downloader.py:10-12.
- **Declarative-only rollback point** — backup.py:165-172: file-copying a running datastore is "both huge and inconsistent."
- **Auto seam sync is a *provision-time trust choice*** — seam_sync.py:11-14: "the trust anchor becomes the release channel plus this root-held policy file — not a human re-provision."
- **Best-effort boot-hook regen** — "a boot-hook regen failure must never fail an activation that already succeeded."
- **Duplicate the boot-hook contract integer across the package boundary on purpose** — doctor.py:46-51: "duplicated precisely so the manager never has to import the service package."
- **Refuse rather than guess** — the wrapper's 2-candidate branch, `AmbiguousHomeError`, and `assert_no_collision_artifacts`.

## Invariants & contracts

- `<home>/current` is a **relative** symlink `versions/<N.N.N>`; its basename must validate as strict semver or `active_version()` returns `None`.
- A version is "installed" iff `versions/<v>/cli/venv` exists (`list_installed_versions`); "activatable" iff `cli/venv/bin/syrvis` exists.
- Every version tree caches its wheel at `versions/<v>/wheel/*.whl` — **restore depends on this**.
- `.syrviscore-manifest.json` is the discovery marker: SPK shell scripts, `paths._candidate_homes`, the wrapper fallback, `syrviscore-bootstrap.sh`, and `doctor` all key off it.
- Error `code` strings (`home_not_found`, `ambiguous_home`, `version_not_found`, `active_version`, `integrity`, `lock`, `incompatible_manager`, `volume_root_collision`, …) are the machine contract for the MCP/dashboard adapters.
- `syrvisctl info --json` emits `active` — the manifest **file** uses `active_version`. `syrviscore-bootstrap.sh` carries an explicit comment about this mismatch.
- The service must ship `syrviscore.seam.gen` (≥0.4) and the hidden `syrvis _regen-boot-hooks` verb, or activate degrades to warnings.
- SPK version = **service** version; manager version is only inside. `MIN_MANAGER_VERSION` is the one-way compatibility declaration.

## Gaps, debt & sharp edges

- **Composite operations are not atomic.** `hold_lock` is taken *per primitive*. `download_and_install` releases the lock between `install_version` and `activate_version`; `cleanup_old_versions` takes and releases it once per version; `restore_from_backup` performs the entire extraction *and the final `update_current_symlink` + manifest write* with **no lock at all** (it only inherits one transiently if it has to rebuild a venv). Two concurrent restores, or a restore racing a cron-driven install, can interleave.
- **Backup's model of disk layout has drifted.** `_gather_backup_items` walks `home/services` and `home/compose` and `home/data/<non-core>` — but 0.5.x L2 services live at `/volume<N>/<apps-root>/apps/<name>/` (`service_manager.py:364`), entirely outside `SYRVIS_HOME`, and declarations live in `config/services.d` (caught only incidentally by the `config/` rglob). `layer2_services` is derived from a `home/services` directory that modern installs may not have. A "full disaster-recovery backup" therefore captures **none** of the app homes or their data. Nothing tests this against the current layout.
- **TOCTOU in `cleanup`.** `cli.cleanup` computes `to_remove` with `dry_run=True`, prints it, prompts, then recomputes with `dry_run=False`. The reported set and the removed set are computed at different times, outside any lock.
- **`preupgrade` writes into `backups/`** as `backups/upgrade-<ts>/` — a directory `list_backups` ignores (globs `*.tar.gz`) and `cleanup_old_backups` never removes. Unbounded junk.
- **The SPK shell scripts never got the incident treatment that `start-stop-status` did.** `preupgrade`, `preuninst`, `postuninst` and `syrviscore-bootstrap.sh` each reimplement home discovery in shell, and **none** of them knows about `<name>_1` collision renames or the configurable apps-root. `preupgrade` still probes a legacy `$VOLUME/docker/syrviscore` path.
- **Docs are materially stale.** `docs/cli-syrvisctl.md` documents a `syrvisctl migrate` command that does not exist, claims `/usr/local/bin/syrvisctl` (postinst creates only a profile snippet), and publishes an exit-code table (2/3/4/5) that is fiction — every `SyrvisError` has `exit_code = 1`. It documents neither `doctor` nor `seam`. `spk-installation-guide.md` describes an installation wizard (`WIZARD_UIFILES`) that the SPK does not ship. `build-tools/README.md` still shows the old multi-`--platform` pip invocation and a `bin/` directory in `package.tgz` that build-spk never creates.
- **`spk/INFO` is pinned at `version="0.1.21"`** and only corrected in the staged copy. Anyone reading the repo sees a four-year-stale number, and the SPK filename tracks the *service* version while its contents track the *manager* version — a naming decision that reliably confuses.
- **No CI job builds or validates the SPK.** `test.yml` covers 3.8 tests/lint, mcp, dashboard, and a devkit-tarball bootstrap job — `build-spk.sh` and `validate-spk.sh` are manual-only, so error-263/313 regressions can only be caught by hand.
- **`_fixup_relocated_venv` rewrites `bin/*` only**, while the SPK's postupgrade also rewrites `pyvenv.cfg`. Harmless on 3.8 (no `command =` key) but latent if the DSM interpreter ever moves forward.
- **`set_tree_readable` chmods the entire version tree 644/755** including the venv, unconditionally, on every install — O(files) and it will silently flatten any deliberate mode.
- **Doctor's `SEAM_ACCOUNTS` and both rootfs paths are hardcoded**, so a deployment that renames the operator account gets a false "not provisioned". `MIN_BOOT_HOOK_CONTRACT` is a manual duplicate of `privileged_ops.BOOT_HOOK_CONTRACT`, held together only by `test_home_collision.py::test_the_manager_contract_matches_what_the_platform_renders`.
- **`seam status` force-elevates.** It calls `ensure_privileges(/etc/sudoers.d/syrviscore-mcp)` unconditionally, so a read-only status check re-execs under sudo — while `_read_or_none` exists specifically to tolerate the unprivileged case.
- **`GITHUB_TOKEN` is read from the ambient environment** with no redaction discipline, and `download_file` streams to disk with no size cap.

## Raw material worth citing in the retrospective

- errors.py:104-113, `CollisionError`: "`<volume>/<name>_1` means DSM renamed one of our volume roots at boot … Installing over that state writes a NEW empty tree beside the real one and **reads as data loss** (incident 2026-08-16); the fix is a `mv`, never an install."
- cli.py:149, `assert_no_collision_artifacts`: "**THE ONE COMMAND THAT WOULD HAVE SOLVED 2026-08-16 IN SECONDS** (fnd:F16) … `syrvisctl doctor` has said 'Do NOT run syrvisctl install' since 0.5.9; **prose is not a guard**."
- doctor.py:5-9: "The 22-method self-audit contains **not one check that looks at a volume root**. A human found the cause with `ls`."
- paths.py:346-351: "'No service version installed. Run syrvisctl install' is advice that, in the one situation that produces it on a working box, would install a SECOND tree beside the intact one and make the recovery materially worse."
- start-stop-status:73-78: "DSM's own package status stayed green through a fully decapitated install. Exit 1 — a red package status is the cheapest out-of-band signal this failure can produce."
- postupgrade:218-220: "Do NOT swallow the failure with `|| echo unknown` (the old bug: it logged 'unknown' and shipped a broken syrvisctl that only surfaced on the next invocation)."
- build-spk.sh:113-118: the stale-wheel guard, "the same class as release-service.sh's EXPECTED_WHEEL guard."
- release-service.sh:77-80: "the 0.3.10-shipped-0.3.9 bug."
- Numbers: manifest `schema_version` **3**; backup `backup_version` **1**; `MIN_BOOT_HOOK_CONTRACT` **3**; `MIN_MANAGER_VERSION` **0.2.0**; manager **0.3.6**, service **0.5.17**; SPK `os_min_ver` **7.0-40000**, `arch=noarch`, `startable=no`; preinst floor **500 MB**; cleanup defaults keep **2** versions / **3** backup versions; `_verify_cli_executes` timeout **60 s**, compat probe **15 s**, seam render **60 s**, `visudo` **30 s**; SPK-script docker timeout **30 s**; six pinned dependency wheels.