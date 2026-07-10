# SyrvisCore MCP — Security Review (July 2026)

Adversarial red-team of the Phase 5 MCP server (5 attack lenses → per-finding
adversarial verification → synthesis). 23 candidate findings, 17 verified, **10
confirmed**. All 10 are now fixed; this documents them and the fixes.

## Verdict

No critical, no unauthenticated RCE, no forgeable confirmation token, no
sandbox/managed-by escape. The layered model (validate → `--` positional
discipline → forced-command shim → enumerated NOPASSWD sudoers → HMAC
confirmation) held. The confirmed findings were: two reachable through the MCP
tool path (a fail-open git allowlist and a nonce race), several defense-in-depth
erosions in the NAS-side shim/sudoers, and a few correctness/telemetry gaps. All
fixed and covered by new regression tests (MCP suite 159 → 180).

## Findings and fixes

| # | Finding | Sev | Via MCP | Fix |
|---|---|---|---|---|
| F1 | `service_add` runs attacker-supplied code with a fail-open host allowlist and no confirmation | High | Yes | `validate_git_url` fails **closed** on an empty allowlist; `service_add` now requires a confirmation token; `service_add` marked `destructiveHint` |
| F2 | Empty `git_url_allowed_hosts` default silently disabled the allowlist | Med | Yes | `config.load_config` **requires** a non-empty allowlist in production (fail-closed at startup) |
| F3 | Shim `exec $cmd` unquoted → word-split + glob re-injection | High | No (stolen key) | Shim rewritten to a **precise argv matcher**: charset whitelist + `set -f` + `set -- $cmd` + exact-argc/per-token validation + `exec "$@"` |
| F4/F5 | Shim `--` guard bypassable via a second `--`; sudoers `*` spans arg boundaries | Low/Med | No | Same argv-matcher rewrite: exact argc + literal/slot matching makes double-`--` and extra tokens impossible to match |
| F6 | FlagValue slots (`--keep`, `-n`) not behind `--`, so the old guard skipped them | Low | No | Argv matcher validates FlagValue slots with the per-kind regex too |
| F7 | Single-use nonce check-then-add race (concurrent confirms double-apply) | Med | Yes | `tokens.verify` consumes the nonce under a `ToolContext.nonce_lock` |
| F8 | Tokens replayable across a production restart (stable secret + in-memory nonces) | Med | No | `ToolContext` mixes a per-process random salt into the signing key, so a restart voids outstanding tokens |
| F9 | Audit log omitted rejected/attacked calls (G16 was false) | Low | No | `server._call` records rejected calls via `RemoteRunner.audit_event` |
| — | `environment == "production"` vs `"prod"` shorthand footgun | Low | No | `is_production` accepts both; `environment` validated against a known set at load |
| — | Unicode-digit homoglyphs could pass version/name regex | Low | Yes | MCP regexes compiled with `re.ASCII` (pattern string unchanged, drift test passes) |

Also hardened: the ControlMaster socket directory is created `0700`
(`RemoteRunner.__init__`), and the companion CLI git-clone change
(`git clone --depth 1 --` + `GIT_ALLOW_PROTOCOL`, stricter `_is_git_url`) closes
the git-flag-injection path for the human/DR route too.

## Residual risks (accepted)

- A **stolen operator SSH key** is inherently powerful (it can invoke every
  enumerated command legitimately). `restrict` + forced command + `from=` bound
  it; F3–F6 mattered because they *widened* that surface and are now closed.
- **click post-`--` positional handling** is a backstop for extra-token cases;
  the CLI-shape invariant (no `--`-gated subcommand uses variadic positionals /
  `ignore_unknown_options`) should be asserted by a test if that ever changes.
- **Reserved-core-name protection** is a hand-maintained frozen list (drift
  test keeps it in sync with the source) — acceptable for MVP, tech debt to
  derive from the live stack later.
- **Raw remote stderr** is surfaced in errors; current command outputs aren't
  sensitive, but this is a place to add redaction if that changes.

## New regression tests

`test_validate` (fail-closed allowlist, unicode digits), `test_config`
(production requires allowlist + token secret, environment validation),
`test_deploy` (glob/double-`--`/extra-flag denials, `set -f` + no bare
`exec $cmd`, charset whitelist), `test_tokens` (concurrent-confirm one-winner),
`test_tools` (service_add confirmation + fail-closed, per-process key voids
tokens), `test_audit` (rejected calls logged).
