# v2 research — raw evidence behind docs/v1-retrospective.md

Produced 2026-08-17/18 by multi-agent fan-outs (Opus readers + analysts)
during the v1 retrospective sessions, then synthesized into
`docs/v1-retrospective.md`. Preserved here so the v2 kickoff
(`docs/v2-kickoff-prompt.md`) can reference stable paths.

**Trust level:** raw agent output. The retrospective's load-bearing claims
were spot-verified against the tree (no `flock` in the service package,
zero journal/breaker consumers, backup path drift, the relic image
workflow); everything else — especially `file:line` citations — is a
guide, not gospel. Re-verify before building on a specific claim.

| Dir | Contents | Fed |
|---|---|---|
| `v1-subsystem-maps/` | 12 subsystem maps of this repo (manager, core-config, L2 declarations, deploy plane, seam, MCP, lifecycle/host, dashboard, tests/sim, docs/history) + 2 home-tech vision maps (data doctrine, ops doctrine) + `critique.md` (ranked gaps) + `rewrite.md` (language/architecture analysis) | retro Part I (§1–§9) |
| `hometech-usage/` | 8 slices of how home-tech consumes the v1 seam (deploy tooling, verify plane, NAS jobs, declarations fleet stats, runbooks, MCP/agent plane, incidents, exhaustive verb census) + `consolidation.md` (verb→v2 disposition map) + `lifecycle.md` (the full syrvisd drains/orchestrator design draft) | retro §10, §12 |
| `dsm-packaging/` | `research.md`: DSM 7 SPK capabilities with VERIFIED/CORROBORATED/UNCERTAIN verdicts and citations (privilege/signing gate, capabilities, systemd units, volume picker, wizards, upgrade contract, feeds, @appdata, volume identity). `design.md`: the one-package + volume-model design draft | retro §13 |

The `UNCERTAIN` verdicts in `dsm-packaging/research.md` are the Phase-0
experiment list — do not build on them until they are settled on a real
DSM box (see the kickoff prompt).
