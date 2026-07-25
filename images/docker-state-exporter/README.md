# docker-state-exporter (owned build)

An **owned, non-root** rebuild of [`karugaru/docker_state_exporter`][up] (MIT),
the Docker-API container-state exporter the estate uses for
`docker_container_running` / `_health_status` / `_restart_count` /
`_started_at_seconds`. Built and published by SyrvisCore CI to
`ghcr.io/<org>/docker-state-exporter`.

## Why this exists

The 2026-07-24 supply-chain audit flagged the previous exporter
(`fviolence/docker-health-exporter`) as the estate's top risk: anonymous author,
unmaintained, root, unsigned, touching the Docker API. The immediate fix pinned
the auditable upstream `karugaru/docker_state_exporter` **by digest** (see
`catalog_templates/docker-health-exporter.yaml`). This directory is the
**hardening follow-through** — the same ~240 lines of MIT Go, but:

- **built by our CI** → provenance we control, cosign/SBOM available from the same
  pipeline as the dashboard image;
- **distroless static** runtime (no shell, no apk) instead of `alpine`;
- **runs as non-root** (`USER nonroot`, uid 65532) — the upstream image has no
  `USER` and runs as root;
- **an `org.opencontainers.image.base.name` label** so `syrvis images` can read the
  base cheaply (the one place base-from-label is reliably present — our own images).

`main.go` is vendored verbatim (with `LICENSE`) so the build is self-contained and
auditable in this repo; there is no upstream `go.mod`, so the Dockerfile pins the
handful of deps to API-compatible versions and lets the build compute `go.sum`.

## Re-pinning the estate after a build

CI publishes `ghcr.io/<org>/docker-state-exporter:<tag>` on push. To adopt it,
resolve the pushed digest and set it as the `image:` in
`catalog_templates/docker-health-exporter.yaml` and the deployment's
`services.d/docker-health-exporter.yaml` (Renovate `pinDigests` keeps it current
thereafter). Nothing else changes — the metric contract and the socket-proxy
config are identical to the upstream-digest deployment.

[up]: https://github.com/karugaru/docker_state_exporter
