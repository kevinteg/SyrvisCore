# SyrvisCore — RETIRED (v1)

**Retired 2026-09-01.** This repo is v1 of the Synology deployment system. It
is kept for history and for the DSM-side artifacts it documents. It is not a
product any more and it is not a fallback.

## Do not

- Do not install it, run it, extend it, or bump its version.
- Do not run `make env`, `make dev-install`, or any other bootstrap target here.
- Do not reinstall pyenv 3.8.12; it is no longer declared on this Mac.
- Do not rebuild the v1 seam or anything shaped like it. **R11-1 forbids it.**

## Successor

`~/code/syrvis` — syrvis v2: the static Go agent (`syrvisd`, shipped as an SPK)
plus the `syrvis` CLI. The RS1221+ is reached only as
`syrvis --context rs1221-ops <verb>` over T-api (mTLS). Read that repo's
`CLAUDE.md` for the contract, and `~/code/home-tech/syrvis-v2/DEVICE.md` for the
device's measured state.

## The old contract

The previous 27 KB contract lives in git history (`git log --follow -- CLAUDE.md`).
It carries runnable v1 instructions and must not be followed.
