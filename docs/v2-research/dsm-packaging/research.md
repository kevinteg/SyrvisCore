# DSM 7 packaging facts

Research date 2026-08-18. Verdict key: **VERIFIED** = official Synology developer guide / KB; **CORROBORATED** = two or more credible community sources agreeing; **UNCERTAIN** = single source, inference, or contradictory evidence.

---

## 1. Privilege: can a DSM 7 third-party package run its daemon as root?

**Verdict: mostly NO for an unsigned third-party package — CORROBORATED, with a VERIFIED mechanism that is gated behind Synology signing.**

Evidence:

- The `conf/privilege` spec is real and richer than SyrvisCore uses. `defaults.run-as` officially accepts **`package`** (files owned by the package user, scripts run with the package UID) **and `root`** (files owned by root, scripts execute as root). There is also a `ctrl-script` array that overrides run-as **per lifecycle action** — the allowed actions are `preinst`, `postinst`, `preuninst`, `postuninst`, `preupgrade`, `postupgrade`, `start`, `stop`, `status`, `prestart`, `prestop`. So "can specific ctrl scripts be granted root?" — **the file format says yes** ([Privilege Config](https://help.synology.com/developer-guide/privilege/privilege_config.html)). VERIFIED as syntax.
- The DSM 7 breaking-changes page is the counterweight: *"All packages should provide `conf/privilege` with `package` in `run-as` explicitly"*, and *"any privileged operation should be accomplished via resource worker"*; `run-as: system` was removed outright ([Breaking Changes In 7.0](https://help.synology.com/developer-guide/breaking_changes.html)). The preface frames DSM 7 as *"packages are forced to lower the privilege by applying privilege mechanism explicitly"* ([privilege preface](https://help.synology.com/developer-guide/privilege/preface.html)). VERIFIED.
- The gate: **a package declaring root privilege will not install unless signed by Synology**; Synology issues a development token to bypass this during development only. Multiple independent sources say the same thing — the developer-guide search surface, community threads, and packagers who hit it (e.g. [synology-autorun](https://github.com/schmidhorst/synology-autorun), which documents that DSM 7 refuses root-requiring packages and requires a manual post-install step). CORROBORATED. This is exactly your **Error 276**, seen from the other side.
- **The escape hatch that is NOT root:** the `tool` section of `conf/privilege` sets file ownership, mode, and — since **7.0-40656** — **Linux file capabilities** on a named binary: `{"relpath": "bin/mytool", "user": "package", "group": "package", "capabilities": "cap_chown,cap_net_raw", "permission": "0700"}`. Note the constraint: `user` and `group` **must be `"package"`** ([Privilege Config](https://help.synology.com/developer-guide/privilege/privilege_config.html)). VERIFIED. This is the single most useful finding for a one-binary redesign: a Go daemon can hold specific capabilities (`cap_net_admin`, `cap_net_raw`, `cap_dac_override`, `cap_chown`…) without being root and without a signature.
- **What Tailscale actually does** is the reality check. On DSM 6 Tailscale ran as root; on DSM 7 it does not, and the consequences are visible in their own docs: the package *"does not have permission to create a TUN device"*, so outbound/subnet use from other apps is off by default, and the `tailscale` user cannot rewrite `/etc/resolv.conf`. Their documented workaround is a **DSM Task Scheduler task with User: root** ([Tailscale Synology integration docs](https://tailscale.com/docs/integrations/synology), [issue #6153](https://github.com/tailscale/tailscale/issues/6153)). CORROBORATED. If a company with Synology's ear ships an unprivileged package and tells users to bolt on a root scheduled task, an independent package will not do better.
- SynoCommunity's DSM 7 rules are blunter: services run as `sc-<packagename>`, and *"assuming root privileges in install/startup scripts or while running service"* is disallowed, naming `chmod`, `chown`, `su`, `synouser`, `synogroup` specifically ([spksrc DSM 7.0 wiki](https://github.com/SynoCommunity/spksrc/wiki/DSM-7.0), [issue #4215](https://github.com/SynoCommunity/spksrc/issues/4215)). CORROBORATED — and it matches your archive's list of what failed as the package user, line for line.

**Bottom line:** root-by-declaration is a signed-vendor privilege. Root-by-capability on one binary is available to you today. Root-by-out-of-band-escalation (`sudo syrvisd …`, a root Task Scheduler task, an rc.d hook) remains what everyone actually ships.

---

## 2. Service management: what does DSM natively supervise?

**Verdict: DSM supervises start/stop/status transitions, not process liveness. VERIFIED for the contract, UNCERTAIN-to-negative for restart-on-crash.**

- **The contract is a three-verb shell script with meaningful exit codes.** `start-stop-status` takes `start`, `stop`, `status`, and `status` must return: **0** running, **1** dead but PID file exists, **2** dead but lock file exists, **3** not running, **4** unknown, **150** package broken/needs reinstall ([Scripts](https://help.synology.com/developer-guide/synology_package/scripts.html)). VERIFIED. SyrvisCore's current script only ever returns 0/1 — codes 3 and 150 are unused signal you already paid for.
- **DSM 7 replaced upstart with systemd**, and package control units appear as `pkgctl-<package>.service` under `/usr/local/lib/systemd/system`, drivable with `systemctl`/`synosystemctl` ([DSM 7 service control writeup](https://dannyda.com/2022/11/09/how-to-use-command-manually-restart-start-stop-services-in-synology-dsm-7-and-newer-versions/)). CORROBORATED.
- **A package may ship its own systemd units.** Official text: *"If you want to have systemd unit inside the system, you may just put your units at `conf/systemd/pkg-[customname]`. The package framework would copy systemd units to `/usr/local/lib/systemd/system` on acquired and remove them on released."* User-scoped units go to `conf/systemd/pkguser-[customname]`, and scripts should drive them with `synosystemctl start|stop` ([Systemd User Unit](https://help.synology.com/developer-guide/resource_acquisition/systemd_user_unit.html)). VERIFIED as mechanism. **UNCERTAIN and worth a NAS experiment:** a *system* unit with no `User=` runs as root; whether DSM's framework validates unit contents for an unsigned third-party package, or whether this is a de-facto root path with `Restart=on-failure` and proper ordering, is not documented anywhere I could find. If it works, it collapses questions 1 and 2 at once.
- **No documented crash watchdog.** Neither the developer guide nor the SynoCommunity service framework describes automatic restart of a crashed package; SynoCommunity's generic wrapper only tracks a PID file, and the community answer to "restart a crashed package" is a cron/Task-Scheduler loop around `synopkg start` ([spksrc Service Support](https://github.com/SynoCommunity/spksrc/wiki/Service-Support), [zarino.co.uk](https://zarino.co.uk/post/synology-package-start-stop/)). CORROBORATED-negative. A shipped systemd unit with `Restart=` is the only native supervision on the table.
- **Boot ordering is declarative, not scriptable.** INFO fields `install_dep_packages` (must be installed first), `start_dep_services` (services required before startup), `install_conflict_packages`, `install_break_packages` ([INFO optional fields](https://help.synology.com/developer-guide/synology_package/INFO_optional_fields.html)). VERIFIED. Depending on Container Manager is therefore a one-line declaration rather than the `synopkg status Docker` call that Error 276 forbade in `preinst`.
- **Boot is detectable:** `SYNOPKG_PKG_STATUS` is `INSTALL|UPGRADE|UNINSTALL|START|STOP` **or empty during NAS boot/shutdown** ([Script Environment Variables](https://help.synology.com/developer-guide/synology_package/script_env_var.html)). VERIFIED. That empty string is a first-class "this is boot" signal the S99 hook currently has to infer.

---

## 3. Volume selection and the shared-folder resource worker

**Verdict: Package Center already owns install-volume selection natively — VERIFIED. Declaring shared folders is real but volume-blind — VERIFIED behavior, UNCERTAIN placement.**

- *"When a package is installing, `package.tgz` will be extracted to `/var/packages/[package identity]/target`, which is a symbolic link pointing to a folder in a data volume **selected by the end user**"*; `SYNOPKG_PKGDEST_VOL` carries that volume to every script ([Script Environment Variables](https://help.synology.com/developer-guide/synology_package/script_env_var.html)). VERIFIED. **The custom `pkgwizard_volume` combobox in the v1 wizard was re-implementing a native DSM feature.**
- `support_move` (INFO): *"package can be moved to a different volume"* — DSM will relocate the package for the user. `install_on_cold_storage` permits cold-storage volumes ([INFO optional fields](https://help.synology.com/developer-guide/synology_package/INFO_optional_fields.html)). VERIFIED.
- **`conf/resource` `data-share` worker**: `{"data-share": {"shares": [{"name": "...", "permission": {"ro": [...], "rw": [...]}, "once": <bool>}]}}`. It *"creates a shared folder and sets its permission"* during package startup, **skips creation if the share already exists**, and `Release()` **does nothing** — shares survive uninstall to protect user data ([Data Share worker](https://help.synology.com/developer-guide/resource_acquisition/data_share.html)). VERIFIED. **UNCERTAIN:** the spec does not say which volume the share lands on, and does not expose a volume field — so it cannot express "put the app share on volume4 while the package lives on volume1". SynoCommunity's `SERVICE_WIZARD_SHARE` exists precisely to let the wizard collect a share name/location, which suggests the vanilla worker's placement isn't user-steerable ([spksrc Service Support](https://github.com/SynoCommunity/spksrc/wiki/Service-Support)).
- Other workers worth knowing: **`usr-local-linker`** symlinks package files into `/usr/local/{bin,lib,etc}` on start and removes them on stop, aborting with rollback if a link fails ([usr/local linker](https://help.synology.com/developer-guide/resource_acquisition/usrlocal_linker.html)) — that is the sanctioned replacement for the `/usr/local/bin/syrvis` symlink that Error 276 blocked. Also `docker`, `docker-project`, `port-config`, `php-ini`, `maria-db`, `index-db`, `apache-*`.
- **Big packages:** Container Manager creates a user-visible `docker` shared folder and keeps its own internal state in `/volume1/@docker` — the same split (hidden system dir + visible share) DSM uses everywhere ([Container Manager KB](https://kb.synology.com/en-us/DSM/help/ContainerManager/docker_desc), [wundertech](https://www.wundertech.net/container-manager-on-a-synology-nas/)). CORROBORATED. They do not offer a second volume picker; the package volume is the package volume, and per-container bind mounts do the rest.

---

## 4. Install wizard: what it can collect and how values arrive

**Verdict: two generations coexist. VERIFIED for structure, CORROBORATED for the `pkgwizard_` env-var handoff, UNCERTAIN for API-populated comboboxes on 7.2+.**

- Files live in `WIZARD_UIFILES/`: `install_uifile`, `upgrade_uifile`, `uninstall_uifile` (JSON), with **localized variants by DSM language suffix** (`install_uifile_cht`) ([WIZARD_UIFILES v2](https://help.synology.com/developer-guide/synology_package/wizard/WIZARD_UIFILES_v2.html)). VERIFIED.
- **Dynamic wizards are supported and under-used:** `install_uifile.sh` (and peers) run at wizard time and **emit JSON to `SYNOPKG_TEMP_LOGFILE`**, so the wizard can be built from live system state ([WIZARD_UIFILES v2](https://help.synology.com/developer-guide/synology_package/wizard/WIZARD_UIFILES_v2.html); community usage in [AirConnect-Synology](https://github.com/eizedev/AirConnect-Synology) and [spksrc #3389](https://github.com/SynoCommunity/spksrc/issues/3389)). VERIFIED. This is how you'd enumerate NICs, volumes, or existing installs at install time instead of hard-coding `ovs_eth0`.
- **DSM 7.2.2's v2 wizard is Vue.js 2.7.14** — components are `{name, render}` pairs wrapped in `<pkg-center-step-content>`, using `v-form`, `v-form-item`, `v-input`, `v-checkbox`; a `setup()` returns `getValues()` which Package Center harvests. VERIFIED. The legacy v1 JSON grammar (`textfield`, `password`, `combobox`, `multiselect`, `validator.regex`) is what your archive documents and what most community packages still ship ([turesheim example](https://github.com/turesheim/synology-packages/blob/master/archiva-dsm-package/WIZARD_UIFILES/install_uifile)). CORROBORATED.
- **Handoff:** keys prefixed `pkgwizard_` become environment variables in `preinst`/`postinst`/`preupgrade`/`postupgrade`. The official v2 page describes `getValues()` and says values "populate script environment variables"; it does not restate the `pkgwizard_` prefix rule, which is documented in DSM 6 material and universally observed in the wild — and in your own archive. CORROBORATED.
- **UNCERTAIN:** the remote-API combobox (`mode: "remote"`, `api_store: "SYNO.Core.Storage.Volume"`) is community lore only; I found no official DSM 7 page for it. Given DSM 7.2 moved to Vue components, treat it as unsupported and use `install_uifile.sh` instead.
- **No secrets in the wizard.** Password fields exist, but wizard values land in env vars visible to the whole script chain and to DSM logs. Your `pkgwizard_cloudflare_token` step was a mistake worth not repeating.

---

## 5. Upgrade contract

**Verdict: DSM replaces the payload and preserves the data dirs; it has no concept of multiple versions or rollback. VERIFIED.**

- Order — install: `prereplace → preinst → postinst → postreplace → (optional start)`. Upgrade: **stop old → `preupgrade` → uninstall old → install new → `postupgrade` → restart if applicable**. Uninstall: `stop → preuninst → postuninst`. Non-zero from a `pre*` script aborts; a failing `post*` script can leave the package in a corrupted state ([Scripts](https://help.synology.com/developer-guide/synology_package/scripts.html)). VERIFIED. Note "uninstall old, install new": **`target` is not preserved across upgrade** — that is what `SYNOPKG_TEMP_UPGRADE_FOLDER` (*"staging directory for version migration in `preupgrade`/`postupgrade`"*) and `SYNOPKG_OLD_PKGVER` are for ([env vars](https://help.synology.com/developer-guide/synology_package/script_env_var.html)). VERIFIED.
- **What survives:** everything outside `target` — `var` (`@appdata`), `home` (`@apphome`), `etc` (`/usr/syno/etc/packages/<pkg>`). `tmp` (`@apptemp`) is removed on uninstall; data shares created by `data-share` persist even through uninstall. VERIFIED/CORROBORATED (§3, §7).
- **No native multi-version or rollback.** Package Center holds exactly one installed version; downgrade means manually installing an older `.spk`, and dependent packages complicate even that ([community gist on downgrading](https://gist.github.com/halliba/c0c07002bdd6ca89a839f63543021aa1)). CORROBORATED. Related INFO knobs: `auto_upgrade_from` (minimum version eligible for automatic upgrade), `use_deprecated_replace_mechanism`, `exclude_versions` ([INFO optional fields](https://help.synology.com/developer-guide/synology_package/INFO_optional_fields.html)). **A `versions/<v>` + `current` symlink scheme stays your own problem** — but with one static binary it costs a directory and an atomic rename, not a venv.

---

## 6. Distribution

**Verdict: self-hosted feeds are a first-class, unsigned-friendly path. CORROBORATED protocol, VERIFIED UI.**

- Package Center → **Package Sources → Add**, name + arbitrary URL; SynoCommunity is just the best-known instance of this ([Configure Package Center](https://kb.synology.com/en-af/DSM/help/DSM/PkgManApp/configure?version=7), [synocommunity.com](https://synocommunity.com/)). VERIFIED.
- **Protocol:** Package Center queries the source with `arch`, `build`, `major`/`minor`/`micro`, `language`, and `package_update_channel` (e.g. `beta`), and gets back a package list. `spkrepo` is an open-source reference implementation you can read or run ([SynoCommunity/spkrepo](https://github.com/SynoCommunity/spkrepo), [docs](https://spkrepo.readthedocs.io/en/latest/)). CORROBORATED. A static-file feed behind Cloudflare is entirely feasible.
- **Auto-update:** Package Center has an Auto-update tab and a **Trust Level** setting (`Synology Inc.` / `Synology Inc. and trusted publishers`); users choose. CORROBORATED. **Tension worth flagging:** DSM 7 breaking-changes says *"Packages are no longer able to do signing in packing stage"* and that the keyring/trust-level mechanism is deprecated ([Breaking Changes](https://help.synology.com/developer-guide/breaking_changes.html)), yet the trust-level control still ships in the DSM 7 UI. UNCERTAIN how they reconcile. The operative fact for you: **an unsigned third-party `.spk` installs fine — until it asks for root** (§1).
- Manual `.spk` install remains available and is your DR path. `offline_install` hides a package from Synology's server listing.

---

## 7. `@appdata` and the per-package var directories

**Verdict: VERIFIED, and they live on the user-chosen volume.**

```
/var/packages/<pkg>/target → /volume<N>/@appstore/<pkg>   (payload; replaced on upgrade)
/var/packages/<pkg>/var    → /volume<N>/@appdata/<pkg>    (persistent package data)
/var/packages/<pkg>/home   → /volume<N>/@apphome/<pkg>    (private, mode 0700)
/var/packages/<pkg>/tmp    → /volume<N>/@apptemp/<pkg>    (removed on uninstall)
/var/packages/<pkg>/etc    → /usr/syno/etc/packages/<pkg> (config, on the system partition)
```

Sources: [spksrc DSM 7.0 wiki](https://github.com/SynoCommunity/spksrc/wiki/DSM-7.0) and the developer guide's FHS section; `SYNOPKG_PKGVAR` no longer means `target/var`. `<N>` is the volume the user picked at install. These are **hidden system directories, not shared folders** — invisible in File Station, not directly backed up by Hyper Backup's share selection. That is the deliberate trade: DSM-managed lifecycle vs. operator-visible, rsync-able state. SyrvisCore's user-visible `/volume4/syrviscore` is the opposite choice, and it is the choice that produced the 2026-08-16 share-rename incident.

---

## 8. Enumerating volumes from a root daemon; how stable is `/volumeN`?

**Verdict: several usable interfaces — CORROBORATED. Volume identity is NOT stable — CORROBORATED, and locally proven.**

- **Cheapest and most stable:** `/proc/mounts` + `statvfs`. Synology data volumes appear as btrfs/ext4 mounts at `/volume<N>`; a Go daemon needs no vendor tool for the census SyrvisCore actually does.
- **Vendor metadata:** `synospace --meta` is the tool that reads/writes volume and storage-pool numbering; community documentation shows `synospace --meta -s -i reuse_<pool> <device>` and notes **renumbering was possible in DSM 6 and is not in DSM 7** ([blog.bianxi.com](https://blog.bianxi.com/2021/12/09/renumber-storage-pools-and-volumes-in-synology-nas/), [Synology community thread](https://community.synology.com/enu/forum/1/post/139763)). CORROBORATED.
- **Web API from root:** `synowebapi --exec api=<API> method=<m> version=<v>` invokes DSM's own APIs locally as root; storage lives behind `SYNO.Core.Storage.Volume` (DSM 7) and historically `SYNO.Storage.CGI.Storage` `load_info` ([usage examples](https://blog.differentpla.net/blog/2025/07/12/synowebapi-container-manager-projects/), [kwent/syno #36](https://github.com/kwent/syno/issues/36)). CORROBORATED, version-fragile — wrap it, never depend on it.
- **Stability:** the `/volumeN` number is Synology *metadata*, assigned per storage pool, not a kernel-stable identity. It changes across migration, pool removal, and re-creation, and DSM 7 removed the supported way to fix it. **Plus the local proof:** DSM renamed `/volume4/syrviscore` to `syrviscore_1` at a cold boot when a share-reclaim race lost. Anything a daemon persists must key on volume **UUID / filesystem identity plus a self-describing marker file**, and must treat "my path resolved to nothing" as an error, never as "nothing installed" — which is precisely the fix already burned into `start-stop-status`.

---

## What this means for a one-binary package

A single static Go binary changes the arithmetic more than the privilege model does. The privilege wall from Error 276 has not moved; what has moved is how much you need on the far side of it.

**Move into DSM natively, with confidence:**

- **Volume selection.** Delete the wizard volume step. `SYNOPKG_PKGDEST_VOL` is the answer, `support_move="yes"` lets DSM relocate you, and the payload is small enough that relocation is cheap.
- **Payload delivery and upgrade.** One binary in `target` means `postinst` becomes "verify checksum, exec `syrvisd install`" — no venv, no offline pip, no shebang rewriting, no `venv.new` staging. The two-package split (`syrvisctl` fetching `syrviscore` from GitHub) existed largely because Python venvs are expensive to ship and version; a 20 MB static binary can just ride in the SPK. Keep `versions/<v>` + `current` only if instant rollback is still a requirement — it is now a rename, not a rebuild.
- **Dependency and ordering declarations.** `install_dep_packages="ContainerManager"` and `start_dep_services` replace the `preinst` Docker probe that the package user was never allowed to make.
- **Command exposure.** `usr-local-linker` puts `syrvis` on `PATH` without root and without the profile-sourcing dance.
- **Status.** Use the real exit-code vocabulary (0/1/3/**150**), not 0/1. `150` tells DSM "reinstall me" — the honest answer when the home tree has vanished.
- **Boot detection.** Empty `SYNOPKG_PKG_STATUS` is DSM telling you "this is boot."
- **Distribution.** A self-hosted package source over Cloudflare gives you Package Center update notifications; keep manual `.spk` as the DR path.

**Cannot move, and should stop pretending otherwise:**

- **Root.** Docker socket ownership, macvlan shim creation, group management, `/usr/local/etc/rc.d/` writes. Tailscale can't do it either. The three honest options are (a) file **capabilities** via `conf/privilege`'s `tool` block — genuinely promising for a Go daemon and worth prototyping first, since `cap_net_admin` + `cap_dac_override` may cover the macvlan and socket cases without any root at all; (b) a shipped **system systemd unit** via `conf/systemd/pkg-<name>` — high value if it runs as root, needs one NAS experiment to settle; (c) the status quo, one explicit `sudo syrvisd bootstrap` after install.
- **Supervision.** DSM does not restart crashed packages. If `syrvisd` must survive its own death, that is a systemd `Restart=on-failure` unit you ship, or nothing.
- **Multiple installed versions.** DSM has one. Rollback stays yours.

The shape that falls out: **SPK = payload + declarations; `syrvisd` = everything else**, with the wizard reduced to near-zero (or replaced by `install_uifile.sh` reading live system state) and every privileged action concentrated in one explicitly-elevated bootstrap verb that is idempotent, reports `(changed, reason)`, and is the same code path `doctor --fix` runs.

---

## Local scar tissue worth honoring

These come from the archives and remain binding regardless of language:

**The error-code taxonomy** ([chat-import-notes-2026-07.md §1](file:///Users/kevinteg/code/SyrvisCore/docs/archives/chat-import-notes-2026-07.md)):

- **263** — outer `.spk` must be **uncompressed** `tar cf`. Gzipping it yields "failed to sort spks."
- **261** — `start-stop-status` is **mandatory**, even for `startable="no"`.
- **313** — "failed to revise file attributes": `scripts` must be a **directory**, never a tar; also fires on bad ownership/perms. Normalize owner/mode before packing.
- **276** — insufficient privilege in a lifecycle script. Not a packaging bug; the security model. Now understood as the unsigned-third-party side of the run-as gate.
- `INFO` must be clean ASCII, LF, no BOM. No `firmware=` field. Wizard JSON must validate.
- Reproducible builds: `COPYFILE_DISABLE=1`, `--sort=name --owner=0 --group=0 --mtime=@$SOURCE_DATE_EPOCH` — macOS `tar` leaks `._*` AppleDouble files.
- **Debuggability is nil.** Package Center shows a number; `/var/log/synopkg.log` shows `Use default volume to install`. Per-script `/tmp/syrviscore_<script>.log` with `set -x` is the only forensics that survives a failed install. DSM 7 adds `/var/log/packages/<pkg>.log` and `SYNOPKG_TEMP_LOGFILE` — use both.

**Package-user lessons that still bind:**

- `sudo synopkg install` does **not** give scripts root. UI and CLI installs behave identically.
- **`preupgrade` runs on a fresh install** and must `exit 0` when there's nothing to upgrade, or the install aborts.
- **Correction to the archive:** `spk-scripts-analysis.md` asserts "Synology DSM runs `postuninst` as root" and builds a whole architectural exception on it. Under DSM 7 with `run-as: package` that is **false** — every ctrl script runs as the package user unless `ctrl-script` says otherwise *and* the package is signed. Any DSM-7 design that leans on privileged uninstall cleanup is leaning on DSM 6 memory.
- **POSIX only.** DSM's `/bin/sh` is BusyBox ash in package context: no `command -v` (use `which`), no `[[ ]]`, no arrays, no `source`, no `trap … ERR`. A one-binary payload shrinks the shell surface to near nothing — which is the point.
- **Refuse rather than guess.** A failed home scan is an *error*, not "nothing installed" — the 2026-08-16 rename let a green package status hide a decapitated install. Carry that into `syrvisd`: collision-rename detection is a guard, not prose, and `doctor` must answer from the rootfs with no resolvable home.
- **The SPK has no CI** ([v1-retrospective §6.11](file:///Users/kevinteg/code/SyrvisCore/docs/v1-retrospective.md)). The one artifact DSM installs is the least-gated thing in the repo, and `spk/INFO` still reads `version="0.1.21"`. Whatever the v2 SPK becomes, `sh -n` + shellcheck + a tar-shape assertion + an INFO-version-matches-source check are the cheapest insurance available.

**Sources:** [Privilege Config](https://help.synology.com/developer-guide/privilege/privilege_config.html) · [Privilege preface](https://help.synology.com/developer-guide/privilege/preface.html) · [Breaking Changes in 7.0](https://help.synology.com/developer-guide/breaking_changes.html) · [Package Scripts](https://help.synology.com/developer-guide/synology_package/scripts.html) · [Script Environment Variables](https://help.synology.com/developer-guide/synology_package/script_env_var.html) · [INFO optional fields](https://help.synology.com/developer-guide/synology_package/INFO_optional_fields.html) · [WIZARD_UIFILES v2](https://help.synology.com/developer-guide/synology_package/wizard/WIZARD_UIFILES_v2.html) · [Data Share worker](https://help.synology.com/developer-guide/resource_acquisition/data_share.html) · [usr/local linker](https://help.synology.com/developer-guide/resource_acquisition/usrlocal_linker.html) · [Systemd User Unit](https://help.synology.com/developer-guide/resource_acquisition/systemd_user_unit.html) · [Package Developer Guide TOC](https://help.synology.com/developer-guide/) · [DSM 7 Developer Guide PDF](https://global.download.synology.com/download/Document/Software/DeveloperGuide/Os/DSM/All/enu/DSM_Developer_Guide_7_enu.pdf) · [spksrc DSM 7.0 wiki](https://github.com/SynoCommunity/spksrc/wiki/DSM-7.0) · [spksrc Service Support](https://github.com/SynoCommunity/spksrc/wiki/Service-Support) · [spksrc #4215](https://github.com/SynoCommunity/spksrc/issues/4215) · [spkrepo](https://github.com/SynoCommunity/spkrepo) · [Tailscale Synology docs](https://tailscale.com/docs/integrations/synology) · [tailscale#6153](https://github.com/tailscale/tailscale/issues/6153) · [Configure Package Center](https://kb.synology.com/en-af/DSM/help/DSM/PkgManApp/configure?version=7) · [Container Manager KB](https://kb.synology.com/en-us/DSM/help/ContainerManager/docker_desc) · [DSM 7 systemctl services](https://dannyda.com/2022/11/09/how-to-use-command-manually-restart-start-stop-services-in-synology-dsm-7-and-newer-versions/) · [Renumber pools/volumes](https://blog.bianxi.com/2021/12/09/renumber-storage-pools-and-volumes-in-synology-nas/) · [synowebapi usage](https://blog.differentpla.net/blog/2025/07/12/synowebapi-container-manager-projects/) · [synology-autorun DSM 7 root note](https://github.com/schmidhorst/synology-autorun)