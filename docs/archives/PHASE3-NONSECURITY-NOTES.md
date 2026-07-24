# Phase 3 — non-security changes for review

Everything in the Phase 3 commit that is **not** part of the security fixes
(C1 name traversal, C2 volume/privilege policy, H2 sudo elevation). Broken
out here so a follow-up review can focus on the correctness/quality changes
separately from the security boundary. All are covered by the 206-test suite
(green) unless noted.

## Changed behavior

| # | Change | File(s) | Audit ref | Notes for reviewer |
|---|---|---|---|---|
| 1 | `doctor --fix` now applies the `boot_script` and `manifest_perms` fixers it previously advertised as fixable but silently skipped; unknown fix actions now print an explicit "not wired up" line instead of counting as unfixed | `doctor.py`, `privileged_ops.py` (new `ensure_manifest_permissions`) | H3 | `ensure_manifest_permissions` is a plain module-level chmod-to-0644 (manifest carries no secrets). It does **not** go through the DSM/Sim provider split — a chmod is identical either way. Confirm that's acceptable vs. adding it to the `SystemOperations` ABC for consistency. |
| 2 | Layer 2 services now use `docker compose` (v2) with an explicit per-service project name `-p syrvis-<name>`; core stack already used v2. All compose invocations funnel through one `_compose()` helper | `service_manager.py` | M2, M3 | Previously each Layer 2 service shared the implicit `compose` project (poor isolation) and used a v1/v2 mix. Verify `docker compose` v2 is what the NAS actually has (Container Manager ships v2). |
| 3 | Failed `service add` now rolls back **all** artifacts (service dir, data dir, compose file, Traefik config, stopped container) via `_rollback_add()` | `service_manager.py` | M4 | Previously a mid-add failure left partial state that made retry report "already installed". |
| 4 | Obsolete top-level `version:` key dropped from generated Layer 2 compose files | `service_manager.py` | L2 | Compose v2 warns on it. |
| 5 | Portainer admin password minimum raised 8 → 12 chars | `setup.py` | M7 | Portainer CE 2.x rejects < 12 at first-run init. This changes the interactive prompt's validation only. |
| 6 | `setup.py` self-elevation collapsed onto the single `privilege.self_elevate` implementation | `setup.py` | L1 | Was a second, subtly different `os.execvp` copy. (The elevation *fix* itself is security — H2 — but de-duplicating the two implementations is cleanup.) |
| 7 | homeassistant example: image pinned (`:stable` → `:2024.12.5`), redundant `/etc/localtime` absolute mount removed | `examples/syrvis-homeassistant/syrvis-service.yaml` | — | Required to satisfy the new (security) schema. The **policy question worth a decision:** should a small read-only allowlist (`/etc/localtime`, `/etc/timezone`) be permitted for absolute host mounts? Current stance: no — reject all absolute host paths, use `TZ` env instead. Left strict on purpose. |

## Deliberately deferred (NOT done — for the follow-up model)

These audit findings are non-security and I left them untouched to keep this
change set focused:

- **M1 — boot-time `SHIM_IP` drift** (`privileged_ops.py` startup script): the
  generated startup script recomputes `SHIM_IP` from `TRAEFIK_IP` and ignores a
  configured `SHIM_IP`, so the shim can differ between `syrvis start` and a
  post-reboot boot. Fix is `${SHIM_IP:-<computed>}`.
- **M5 — `doctor` checks the wrong user under sudo** (`validators.py:506,929`):
  `os.environ.get("USER", os.environ.get("SUDO_USER", ...))` resolves to `root`
  under sudo, so the docker-group/symlink checks evaluate root instead of the
  invoking user. Prefer `SUDO_USER` when euid==0.
- **M6 — `setup_date` vs `setup_completed_at` key mismatch** (`validators.py:450`
  reads `setup_date`, `setup.py:820` writes `setup_completed_at`): the doctor
  "Setup: Completed (…)" line always shows `unknown`.
- **L5 — fragile `.env` export in the startup script** (`export $(grep ... | xargs)`):
  breaks on values with spaces/`#` and leaks the Cloudflare token into the shell
  env. Prefer `set -a; . "$env"; set +a`.
- **L3 — Traefik dashboard `api.insecure: true`** on :8080.
- The whole **verify/drift engine** is Phase 4, not attempted here.

## Test coverage added

- `tests/test_service_security.py` (48 tests) — the security boundary (C1/C2/H2).
  The non-security changes above are exercised incidentally by the compose-
  generation and elevation tests in that file (project name, `no-new-privileges`,
  no `version:` key, `SYRVIS_HOME` forwarding) but do **not** yet have dedicated
  tests for the doctor fixers (#1), add-rollback (#3), or the password change (#5).
  Those are thin and would be worth adding in the follow-up.
