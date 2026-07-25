# Image provenance + freshness readout (`syrvis images`) — design

> Status: **design** (2026-07-24). Answers "can we monitor whether a base image
> is reputable and getting patched?" (owner, 2026-07-24). Complements — does not
> replace — the Renovate + Trivy plan (home-tech design/19). Buildable in pure
> Python on the existing `image_updates.py`; not yet implemented.

## 1. The idea

A read-only verb, `syrvis images`, that reports **provenance + freshness** per
pinned image (core tier + installed L2), plus a one-glyph **trust column in
`syrvis status`**. Reputability is not a live-answerable question, so the core of
the design is a **committed, curated trust registry** that cheap live/local
signals *validate against* — the same "assert it in git, diff it in review"
philosophy as pinning itself.

## 2. What already exists to build on

- `image_updates.py`: `parse_image_ref()`/`ImageRef` (registry/repo/tag/digest,
  Hub-normalized), `_bearer_token()` (anonymous pull auth), `list_tags()`,
  `find_newer_tags()` (semver/flavor-aware), `collect_pinned_images()` (core + L2),
  and a `data/.image-updates-cache.json` (6h TTL). **Pure `requests` + OCI spec —
  no skopeo/crane binary.** The new module reuses all of it.
- `docker_manager.py`: the Python `docker` SDK — `client.images.get(ref).attrs`
  gives local `Config.Labels`, `Created`, `RepoDigests` at **zero network cost**.
- `catalog.py`: the bundled-YAML + site-override pattern the trust registry mirrors.
- `service_schema._validate_image`: already proves digest-pinned-or-not for free.

## 3. The committed trust registry

Ship `syrviscore/image_trust.yaml` in the wheel + a site override at
`$SYRVIS_HOME/trust/*.yaml`. Keyed by repository (covers core-tier pins too, which
aren't `ServiceDefinition`s):

```yaml
docker.io/library/traefik:
  publisher_class: official          # official | verified | sponsored-oss | trusted-org | community | unknown
  expected_base:   alpine            # curated: what base you expect
  eol_product:     traefik           # endoflife.date product id (nullable)
  source:          https://github.com/traefik/traefik
ghcr.io/kevinteg/docker-state-exporter:
  publisher_class: trusted-org       # "this is us" — the only way to assert GHCR trust
  expected_base:   distroless/static
  eol_product:     null
```

The check reports **drift** against this (base changed, publisher badge changed,
an unexpected image not in the registry) — turning "is it reputable?" into a
deterministic, PR-reviewable assertion.

## 4. Fields + the cheap/heavy split

**CHEAP — zero network, safe on every interactive `status`/`images` call**
(local `docker inspect` + ref parse + committed YAML):
- `image`/`tag`/`registry`/`repository`, `digest_pinned` (bool),
  `resolved_digest` (running `RepoDigests`), `local_created` +
  `created_trusted` (false if epoch/rewound — distroless/ko zero it),
  `base_from_config_label` (rare; our own images set it — hence the
  `image.base.name` label we added to docker-state-exporter),
  `publisher_class` + `expected_base` + `trust_tier` (from the registry;
  fallback derive: `library`⇒official, `ghcr.io`⇒ghcr-org, else community).

**HEAVY — network, scheduled-only, 6h-cached** (never in interactive `status`):
- `base_from_manifest` (`org.opencontainers.image.base.*` are **manifest/index
  annotations**, not config labels — so a manifest GET, ~1 pull/img; Docker
  Official Images carry them, most third-party don't),
  `newer_tag`/`update_available` (existing `check_updates()`),
  `last_pushed` (Hub `tag_last_pushed` / GHCR `updated_at` — the real "patched"
  date, better than `created`), `base_eol`/`base_eol_date`
  (endoflife.date; hardcode `postgres→postgresql`, `node→nodejs`,
  `alpine→alpine-linux`), `signed`/`has_sbom` (OCI Referrers API),
  `cve_counts` (Trivy — design/19, weekly).

## 5. Shape

- **New module** `image_provenance.py` reusing `image_updates`' `ImageRef`,
  `_bearer_token`, `collect_pinned_images`, cache pattern. Adds a pure-Python
  manifest+config-blob fetch (GET `/v2/<repo>/manifests/<ref>` → `.annotations`;
  config blob is free on Hub) and a referrers query. **No new binary dependency**
  — skopeo/crane/trivy/cosign are *optional enrichers*; absent ⇒ the field degrades
  to `null`, never a crash (DSM ships none of them). Python-3.8-clean (`requests`
  + `dataclasses`, exactly like `image_updates.py`).
- **New verb** `syrvis images [--json] [--refresh]`, cache
  `data/.image-provenance-cache.json` (6h). Interactive default renders the CHEAP
  tier live + folds HEAVY fields from cache (labeled `(cached)`/`(scan pending)`);
  `--refresh` does the network pass.
- **`syrvis status`**: add one trust glyph per image from the CHEAP tier
  (✓ trusted+pinned / ⚠ community-or-unpinned / ● scan-stale). **No network in
  `status`, ever.**
- **Scheduled refresh** via `jobs.d`/`schedule.py`: a daily/weekly
  `syrvis images --refresh` warms the cache (and can run Trivy weekly, design/19).
- **MCP/seam**: an `@mcp.tool(RO)` `images` wrapper + a `syrvis images --json`
  entry in `seam/registry.py` (read verb, no sudo — like `stack_hostnames`), so
  the estate and the dashboard can read it over the operator seam.

## 6. Why this shape

- Interactive `status` stays instant and offline — the cheap tier is local-only.
- The reputability answer is a **curated, diffable git assertion**, not an
  unanswerable live lookup (Docker Hub badges are classifiable; **GHCR gives no
  reputability signal at all**, so our own images can only be asserted).
- Base-from-label is unreliable in practice (BuildKit deliberately omits
  `base.*`; DOIs carry it only as manifest annotations `docker inspect` can't
  see), so the registry's `expected_base` is the foundation and a live read is a
  bonus that *validates* it.
- Everything heavy is nullable + best-effort + cached, so the readout is fully
  useful offline and on a low-power Avoton.

## 7. First step

`image_trust.yaml` for the current fleet (publisher_class from the 2026-07-24
audit) + the CHEAP-tier `image_provenance.py` + the `status` glyph + tests
(registry lookup, ref parse, JSON shape — all testable without docker/network).
Then the HEAVY enrichers + `jobs.d` schedule + MCP/seam.
