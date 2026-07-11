# Chat-Import Notes — SPK Packaging & Privilege History (July 2026)

**Status:** import fodder / reference. **Created:** 2025-11-30 (earliest source conversation).

Distilled from five design/debugging conversations (2025-11-30 → 2025-12-01) that
predate the v2 rewrite. Purpose: capture the packaging mechanics and privilege
findings that produced today's architecture but are **not** written down in the
existing docs, so they can be folded into the real design docs and not
re-discovered the hard way.

Cross-references (already-captured, don't duplicate):
- `docs/v2-design.md` §5 (privilege model), §6 (SPK-as-DR + tarball dev loop) — the
  current design; this file is the *why* behind it.
- `docs/code-audit-2026-07.md` §3 (SPK scripts/build tooling) — already enumerates
  the broken-privilege findings in the v1 scripts; this file adds the DSM-level
  root causes and the packaging-format lessons the audit assumes.
- `docs/design-doc.md` (Privilege Model, Security) and `docs/spk-installation-guide.md`
  / `docs/spk-troubleshooting.md` — user-facing; this file is the builder-facing
  companion.
- `docs/archives/spk-scripts-analysis.md`, `docs/archives/dsm-wizard-guide.md` —
  older analyses superseded by the v2 direction.

---

## 1. SPK archive format — the hard-won mechanics (error progression 263 → 261 → 313 → 276)

An SPK is a **plain `tar` (NOT gzipped)** whose top level contains, at minimum,
`INFO`, `package.tgz`, and `scripts/`. The nested compression rules are the trap:

| Member | Format | Command | Failure if wrong |
|---|---|---|---|
| **outer `.spk`** | uncompressed tar | `tar cf out.spk INFO package.tgz scripts/ …` | **Error 263** "failed to sort spks / invalid file format" if gzipped (`tar czf`) |
| `package.tgz` | gzipped tar | `tar czf package.tgz package/` | — |
| `scripts` | **directory** named `scripts/` (per official DSM 7 docs) | include the dir directly | see note below |

Error taxonomy learned by installing repeatedly on the real NAS (DSM 7.1,
`/volume4`, Synology avoton):

- **263** — invalid/unsortable format. Root cause was gzipping the *outer* archive.
  Fix: outer archive is uncompressed `tar cf`.
- **261** — missing mandatory lifecycle script. Fix: add `start-stop-status`
  (mandatory even for `startable="no"` packages).
- **313** — "failed to revise file attributes". Root cause: `scripts` was packed as
  a **tar file** instead of a directory; DSM couldn't chmod files inside an archive.
  Also triggered by bad ownership/perms on packaged files → normalize
  owner/perms **before** building the SPK.
- **276** — insufficient privileges during a lifecycle script (see §2). This one is
  not a packaging bug; it's the DSM 7 security model.

**`scripts` file-vs-directory saga (important, non-obvious):** three variants were
tried — `scripts` as a gzipped file, as an uncompressed tar named `scripts` (no
extension), and as a plain `scripts/` directory. The official DSM 7 developer docs
say `scripts` is a **directory** containing `postinst/preinst/postuninst/
preuninst/postupgrade/preupgrade/start-stop-status`. Packing it as any kind of tar
(`scripts` or `scripts.tar`) produces 313 or "invalid file format." **Ship it as a
directory.** (Note: `tar cf scripts scripts/` "can't add archive to itself"; and
`tar -xf scripts` into a dir named `scripts/` collides — both are red herrings from
the archive-based attempts, gone once you use a directory.)

### Other format gotchas that each cost an install cycle
- **`INFO` must be clean ASCII, LF line endings, no BOM.** Verified with
  `hexdump -C INFO`. Keep `os_min_ver="7.0-40000"`; **do not** add a `firmware=`
  field — it was too restrictive across DSM patch levels and was removed.
- **`WIZARD_UIFILES/install_uifile.json` must be valid JSON.** A single trailing
  `%` (shell prompt artifact pasted into the file) made it invalid and produced
  "invalid file format" with no useful log line. Validate with
  `python3 -m json.tool`.
- **Icons:** 72×72 (`PACKAGE_ICON.PNG`) and 256×256 (`PACKAGE_ICON_256.PNG`),
  8-bit RGB PNG. (One thread also cited 64×64 — 72/256 is what actually shipped.)
- **Reproducibility:** macOS `tar` leaks AppleDouble `._*` files; use
  `COPYFILE_DISABLE=1` and `tar --sort=name --owner=0 --group=0 --mtime=@$SOURCE_DATE_EPOCH`.
  (Already in `v2-design.md` §6; noted here because it was learned during these builds.)
- **Debugging visibility is poor.** Package Center only surfaces the numeric error;
  `/var/log/synopkg.log` often shows just `Use default volume to install,
  volume=[/volumeN]`. The workable technique: per-script log files
  (`/tmp/syrviscore_<script>.log`) with `set -x` tracing, so logs survive a failed
  install. Fold this into any future SPK script scaffolding.

---

## 2. DSM 7 privilege reality — the root cause of the whole rewrite

**The single most important finding.** With `conf/privilege` set to
`{"defaults":{"run-as":"package"}}`, DSM 7 runs **every** lifecycle script as the
unprivileged **package user** (observed: `syrvis-bot`, UID 203102) — **never root,
even when installed via Package Center UI, even when the admin ran
`sudo synopkg install`.** `sudo` elevates the *invoking* command; DSM deliberately
drops back to the package user inside the scripts. UI and CLI installs behave
identically. This is Error 276.

Operations that consequently **fail inside install scripts** (all confirmed
"Permission denied" as the package user):
- `synopkg status/is_onoff Docker` (needs admin) — so **preinst cannot check Docker**.
- `synogroup --add docker` / `synogroup --member docker <user>` (group management).
- `chown root:docker /var/run/docker.sock`, `chmod 660 /var/run/docker.sock`.
- Writing to `/usr/local/etc/rc.d/` (boot-persistence scripts).
- Creating symlinks under `/usr/local/bin/`.

What the package user **can** do: create/populate its install dir on the chosen
volume, build a venv + offline `pip install` a bundled wheel, write config/manifest
files, set perms on files it owns.

Secondary DSM-lifecycle facts learned:
- **`preupgrade` runs even on a fresh install.** It must `exit 0` when no prior
  installation exists, or the install aborts. (This exact bug bit the v1 scripts.)
- **`start-stop-status` is mandatory** and its `status` exit code drives the
  Package Center running/stopped state (v1 keyed it off
  `setup_complete: true` in the manifest).

Implication (now realized in `v2-design.md` §5/§6): the SPK is a **bootstrapper
only** — no privileged ops in scripts — and all root work happens later via the
self-elevating CLI (`sudo syrvis setup`). Reference: today's `spk/INFO` describes
exactly this bootstrap chain (source → `syrvisctl install` → `syrvis setup` →
`syrvis doctor`).

---

## 3. The two-phase install pattern (the pivot that shaped v2)

The design that came out of the Error-276 dead-end, before it was folded into the
CLI-driven model now in `v2-design.md`:

- **Phase 1 (SPK, unprivileged):** create dirs, venv + offline pip of the CLI
  wheel, generate `.env`/manifest with `setup_complete: false`, create a `bin/syrvis`
  wrapper that exports `SYRVIS_HOME`, drop a privileged-setup helper on disk.
- **Phase 2 (user runs with sudo, later):** create docker group, add user to it,
  set `docker.sock` to `root:docker 0660`, install the boot-persistence hook,
  create the `/usr/local/bin/syrvis` symlink, flip `setup_complete: true`.

Key decisions from this pivot that carried forward:
- **Drop the dedicated `syrvis-bot` user entirely** — Kevin uses his own account
  with `sudo`; no reason to manage a service user given DSM won't grant it the
  privileges anyway. (v2 confirms: `spk/package/setup-privileges.py` and the
  wizard are slated for deletion.)
- **Phase-2 helper must be idempotent + declarative:** every step is a
  check-then-act returning `(changed: bool, message)`; safe to re-run; resolves the
  target user from `SUDO_USER` (and refuses if that's `root`, i.e. `sudo su`).
- This idempotent-reconcile shape is the direct ancestor of the v2
  **`verify`/`doctor --fix`** drift engine (`v2-design.md` §7). The setup helper's
  "compare desired vs actual permission/group/socket state, then reconcile" logic
  became the fixer dispatch.

The concrete Phase-1 `postinst` and the `setup-privileges.py` class from these
chats are a useful reference implementation for the boot-hook script content
(recreate `docker` group + reset `docker.sock` to `root:docker 660` on `start`) —
worth mining when rewriting the SPK lifecycle scripts and the privileged provider.

---

## 4. Ansible / native-Synology alternatives that were CONSIDERED and DROPPED

Kevin explicitly floated a "hybrid SPK + Ansible" and a Chef-style approach for the
privileged Phase 2. Outcome and rationale (so this isn't re-litigated):

- **Ansible playbook** — evaluated as the declarative/idempotent Phase-2 tool.
  **Rejected** because it's an external dependency not present on DSM, and the CLI
  is already Python; a Python-native, stdlib-only idempotent setup (using `grp`,
  `pwd`, `pathlib`, `subprocess`) gives the same idempotency with zero install
  burden and reuses existing CLI utilities. Decision: **Python-native, folded into
  the `syrvis` CLI** (`sudo syrvis setup`), not a separate playbook.
- **DSM Task Scheduler boot task** — considered as the boot-persistence mechanism
  (attractive because it's captured by Synology config backup and shows in the DSM
  UI). Notable but **not** the chosen path; v2 uses an rc.d boot hook installed by
  privileged setup. Keep in mind as a backup-friendly option if rc.d persistence
  proves fragile.
- **`conf/resource` Docker worker** — investigated (DSM's mechanism to request
  managed resources). Left empty/unused; the design does not lean on DSM's
  container management. `conf/resource` currently ships as `{}`.

Net: the "hybrid Ansible" branch is a **dead end on purpose** — the real answer was
"unprivileged SPK bootstrapper + self-elevating Python CLI," which is the v2 design.

---

## 5. Synology shell / toolchain constraints (bit us repeatedly)

DSM 7.1's `/bin/sh` is **BusyBox ash**, not bash. All lifecycle scripts and the
CLI wrapper must be POSIX-only:
- **No `command -v`** — not available in BusyBox ash; use `which`. (This exact
  substitution was needed; a stray `command -v` reference kept reappearing.)
- No `[[ ]]`, no `${var//…}`, no bash arrays, no `source` → use `.`.
- **`trap … ERR` is not POSIX** — it only worked in v1 because DSM's sh happened to
  be bash in some contexts; don't rely on it (flagged in `code-audit-2026-07.md` §3).
- Available: BusyBox coreutils (`grep/sed/awk/find`), Synology CLIs
  (`synogroup`, `synouser`, `synopkg`, `synoservice`), Python 3 (DSM 7.0+; the
  NAS runs **3.8.12** — CI must gate on that), `docker`.

---

## 6. MVP / repo-bootstrap decisions worth preserving

From the "Setting up Synology for MVP development" conversation (2025-11-30):

- **SynoCommunity package source** is a prerequisite to document in README:
  Package Center → Package Sources → Add → Name `SynoCommunity`,
  Location `https://packages.synocommunity.com/`.
- **Python packaging discipline settled early** and still holds: dependencies live
  **only in `pyproject.toml`** (no `requirements.txt`, no manual `pip install`);
  dev extras via `pip install -e ".[dev]"`; `build/config.yaml` holds **Docker
  image tags only**, never Python deps; SPK build uses standard `python -m build`
  wheels + offline `pip install` rather than custom tarball extraction.
- **`build-tools/select-docker-versions.py`** queries the Docker Hub tags API to
  pin Traefik / Portainer-CE / Cloudflared versions into `build/config.yaml`.
  Bug learned: the Hub API **ignores `ordering=-last_updated`** and returns oldest
  tags first — the tool must sort client-side (semver) to surface current versions.
- Original per-image pins seen during MVP (historical reference, not current):
  Traefik `v3`, Portainer-CE, Cloudflared date-tagged (e.g. `2024.11.1`). Current
  pins are whatever `build/config.yaml` holds now.
- Dev on the NAS uses a **distinct `SYRVIS_HOME`** (e.g. `~/syrviscore-dev` /
  `/volumeN/docker/syrviscore-dev`) so the SPK's manifest-scan discovery never
  confuses a dev tree with production — same rule now codified in `v2-design.md` §6.
- venv auto-activation for an SSH user on `/bin/sh`: append `. …/venv/bin/activate`
  to `~/.profile` using `.` (not `source`).

---

## 7. TODO hooks for folding into the real docs

- Move the §1 error table + `scripts`-is-a-directory rule into a "Building the SPK"
  section (or `build-tools/README.md`); it's currently tribal knowledge.
- The §2 privilege facts justify `v2-design.md` §5 — add a one-line pointer there to
  "Error 276 / package-user model" so the reasoning is discoverable.
- The §3 idempotent setup helper is a concrete reference for the SPK lifecycle
  rewrite (audit §3) and the `verify --fix` fixer — reuse its check-then-act shape.
- §5 POSIX/BusyBox constraints belong in a lint/CI check (grep the scripts for
  `command -v`, `[[`, `source`, `trap … ERR`) so regressions can't ship.
