# The Web Dashboard Adapter (base-tier container)

## Purpose & role in the system

The dashboard is the **third thin adapter** over the deterministic `syrviscore` library, after the `syrvis` CLI and the MCP server, and "the first one that runs *on* the NAS" (`docs/dashboard.md:4-5`). Where the MCP server shells `syrvis … --json` over SSH from the operator's Mac, the dashboard runs as a **base-tier Docker container declared in `config/stack.yaml`** and **imports the library in-process**. Its product claim is *observability + safe management*: everything read-only it can do freely; everything host-root it refuses and instead hands back the exact `ssh <target> '…'` string.

It is also the only adapter that exports a machine contract for *someone else's* console: `GET /api/summary` (`syrvis-summary/v1`), a folded one-pill answer to "is this platform worth diving into right now?"

## Key modules and files (path — role — approx size)

Backend (`packages/syrviscore-dashboard/src/syrviscore_dashboard/`, 2,495 LOC total):

- `app.py` — FastAPI factory; mounts auth, `api_router` behind `Depends(require_user)`, SPA last (55)
- `settings.py` — pydantic-settings env model, `AuthMode` literal, `ssh_target_effective` (90)
- `aggregator.py` — TTL-cached, lock-guarded, concurrent probe snapshot (61)
- `probes/` — `base.py` (`ProbeResult`/`Status`/`guard`/severity, 69), `core.py` (69), `traefik.py` (70), `portainer.py` (32), `cloudflared.py` (51), `config.py` (32), `_config.py` (28), `__init__.py` = the `PROBES` registry (36)
- `api/` — `summary.py` (386, by far the largest), `routes.py` (180), `updates.py` (126), `declarations.py` (97), `services.py` (89), `links.py` (75), `health.py` (50), `logs.py` (50), `events.py` (48), `config_routes.py` (62), `deployments.py` (40), `system.py` (27), `core.py` (20), `me.py` (12), `_errors.py` (19)
- `auth/` — `deps.py` (`require_user` + fail-closed `setup_auth`, 81), `cloudflare.py` (JWKS/JWT verifier, 40), `oidc.py` (authlib Auth-Code+PKCE, 58)
- `manage.py` (88) — core lifecycle via Docker SDK; L2 lifecycle via `ServiceManager`, gated
- `ssh_actions.py` (114) — the privileged-op catalog (9 actions), rendered not executed
- `docker_util.py` (73) — managed-container resolution + typed exceptions
- `static.py` (44), `sse.py` (15), `deps.py` (14), `__version__.py` (19)

Frontend (`frontend/`, ~1,800 LOC TS/TSX): React 18 + Vite 5 + Tailwind 3 + `@tanstack/react-query` 5 + `lucide-react`. Six tabs in `App.tsx` (Overview/Services/Deploys/Routes/Logs/Config), `lib/api.ts` (291) as the typed client, `lib/useHealthStream.ts` (38) as the SSE/poll hybrid.

Elsewhere: `packages/syrviscore-dashboard/Dockerfile` (2-stage: node:20-alpine SPA build → python:3.12-slim runtime), `packages/syrviscore/src/syrviscore/compose.py::_generate_dashboard_service` (the container's actual mounts/env), `packages/syrviscore/src/syrviscore/dashboard.py` (747 — **the Grafana model generator, not this container's config**), `images/docker-state-exporter/` (Dockerfile + vendored 463-line `main.go`), `docs/dashboard.md` (158), 16 test files / ~108 tests.

## How it actually works

**Reaching platform state from inside a container.** There is no exporter and no RPC in this path — the container is given the *install tree itself* plus the docker socket. `compose.py:400-471` emits:

```
volumes:
  /var/run/docker.sock:/var/run/docker.sock[:ro]   # :ro unless management: true
  ../config:/syrvis/config:ro
  ../data:/syrvis/data[:ro]
  ../services:/syrvis/services[:ro]
  ../.syrviscore-manifest.json:/syrvis/.syrviscore-manifest.json:ro
environment: SYRVIS_HOME=/syrvis, CLOUDFLARED_URL=http://cloudflared:<metrics_port>, …
networks: [proxy]; security_opt: [no-new-privileges:true]; container_name: syrviscore-dashboard
```

`SYRVIS_HOME=/syrvis` makes `syrviscore.paths.get_syrvis_home()` resolve the bind-mounted tree, and the manifest mount is what satisfies its install-root check. `app.py:28-29` does `os.environ.setdefault("SYRVIS_HOME", …)` from the setting as a belt-and-braces. Read/write posture is a single declared switch: `stack.setting("dashboard", "management", False)` flips the socket, `data/` and `services/` mounts between `:ro` and rw. Routing is by the Traefik **file provider** (`traefik_config._core_service_routes`, router prefix `syrvis-dashboard`) — deliberately no compose labels.

**Aggregator + probes.** `PROBES` is a 5-tuple registry `(name, needs_http, fn)`: `core`, `traefik`, `portainer`, `cloudflared`, `config`. `HealthAggregator.get_snapshot()` is double-checked-locked around an `asyncio.Lock` with a monotonic expiry (`aggregator_ttl_s`, default 5.0s), so "the Docker daemon and component APIs are hit at most once per window regardless of load." `_build` opens one shared `httpx.AsyncClient` and `asyncio.gather`s every probe wrapped in `guard`, which times it and converts *any* exception into a clean `DOWN` result. Snapshot shape:

```json
{"generated_at": "...", "overall": "ok|degraded|down", "healthy": true,
 "components": {"<name>": {"component","status","detail","latency_ms","extra"}}}
```

Severity fold: `{OK:0, NOT_CONFIGURED:0, DEGRADED:1, DOWN:2}` — **`not_configured` is explicitly not a failure** (`probes/base.py:23-28`). The two synchronous probes (`core`, `config`) run under `asyncio.to_thread` to keep the loop free.

Probe semantics carry real product judgement. `traefik` tries `/ping` then falls back to `/api/overview`: API-up-but-no-`/ping` is **degraded, not down**. `portainer` uses `/api/system/status` and notes the old `/api/status` spelling "logs a WRN on every hit and drops the route at its next major." `cloudflared` returns `NOT_CONFIGURED` when no token is set, `DEGRADED` when the token exists but `/ready` is unreachable ("TUNNEL_METRICS may need a `syrvis start`"), and only `OK` when `readyConnections > 0` — "whether the tunnel actually has live edge connections, not just whether the container is up." `core` reuses one `DockerManager.get_container_status()` call for both container states and `verify.gather_core_drift(actual=…)`.

**SSE.** `/api/events` streams `event: health` frames of the same snapshot, one per `max(aggregator_ttl_s, 0.5)` (`_MIN_INTERVAL_S` "so a tiny TTL can't turn into a busy loop"), terminating on `request.is_disconnected()`. The generator is factored out as `health_event_stream(agg, settings, is_disconnected)` explicitly so it can be unit-tested without an HTTP server. `sse.py` supplies `SSE_HEADERS` (`Cache-Control: no-cache`, `X-Accel-Buffering: no`, `Connection: keep-alive`) — "These headers stop Traefik and Cloudflare from buffering the stream, which would otherwise defeat the point." `/api/logs/{service}?stream=true` reuses the same headers with a hard `LOG_STREAM_MAX_LINES = 5000` cap. Client side: react-query polls at 5s and an `EventSource` pushes into the query cache, flipping `refetchInterval` to `false` while live — SSE is an *optimization over* polling, not a dependency.

**Auth.** Three providers behind one `require_user`: `none` (returns `{"email":"lan-dev","sub":"dev","via":"none"}`), `oidc` (authlib client, S256 PKCE, Synology SSO Server as default IdP; the session cookie is the credential), `cloudflare` (PyJWT + `PyJWKClient` against `https://<team>.cloudflareaccess.com/cdn-cgi/access/certs`, enforcing signature+`aud`+`iss`+expiry in one `jwt.decode`; token from `Cf-Access-Jwt-Assertion` header or `CF_Authorization` cookie), and `both`. `setup_auth` **fails closed at startup**: cloudflare mode without team+aud, or oidc mode without issuer+client_id, raises `RuntimeError` before the app exists. Session secret defaults to `secrets.token_hex(32)` if unset.

**Write paths.** Two, both narrow. Core lifecycle (`manage.core_lifecycle`) uses the Docker SDK on `DockerManager.CORE_SERVICES` only, with a stated refusal: "We deliberately do NOT call `DockerManager.start_core_services()` — that shells the compose v1 binary and tries to (re)create the host macvlan shim, which needs host root." L2 lifecycle (add/remove/start/stop/restart/update/rollback) delegates to `ServiceManager` (which shells `docker compose` + `git`) and is double-gated: `ENABLE_L2_MUTATIONS` at runtime *and* the `WITH_L2_TOOLS=true` build arg that installs docker-ce-cli/compose-plugin/git into the image at all. `docker_util.get_managed_container` enforces `SAFE_NAME = ^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,63}$`, then an allowlist: name in `CORE_SERVICES`, or `syrviscore-dashboard`, or `com.docker.compose.project` starting `syrvis`. Typed errors (`InvalidName/ContainerNotFound/NotManaged/DockerUnavailable`) map 1:1 to 400/404/403/503 in `api/_errors.py`.

Everything else is `ssh_actions`: a 9-entry catalog (`setup`, `verify-fix`, `core-reconcile`, `reconcile`, `restart-core`, `shutdown-instance`, `resume-instance`, `install-version`, `rollback`), each carrying `why_privileged`, rendered as `ssh {ssh_target_effective} '{command}'` and returned with the note *"Run this yourself over SSH — the dashboard never executes host-root commands."* `shutdown-instance`'s rationale is the sharpest: "stops the whole instance incl. this dashboard's own container — must run from the host, not from inside it."

**`/api/summary` — `syrvis-summary/v1`.** Pure aggregation, zero new probes, zero outbound calls. `asyncio.gather`s six threaded library reads (`lifecycle.read_runstate`, `intent.summary`, `ServiceManager.list`-derived L2 counts, `verify.gather_l2_drift`, `deployments.load_history(limit=1)`, cached versions) and folds them via a **pure** `fold(containers, drift, runstate, tunnel) -> (state, detail)` that the tests drive directly. `count_containers` splits *not running* into `unhealthy` vs `shed`.

## Design decisions & their rationale

- **In-process library import, not an API call.** `docs/dashboard.md:12-21` tabulates exactly what is reused (`DockerManager`, `verify.gather_core_drift`, `config_reader.read_config`, `ServiceManager`, `paths`) — the adapter re-derives no platform semantics.
- **Everything degrades, nothing 500s.** Stated in `docs/dashboard.md:25-26` and enforced in `guard`, in every `except Exception: # noqa: BLE001 - degrade, never 500`, and in the `{"…": [], "error": str(exc)}` envelopes of `/api/links`, `/api/routes`, `/api/declarations`, `/api/deployments`, `/api/config`.
- **`verify`'s validator suite is deliberately excluded from the fold.** `summary.py:11-15`: run inside this container it reports `healthy: false` for "Python venv: Not found" — a container-context false negative (issue #16) — "so folding it in would paint a healthy platform red." The drift *gatherers* from the same module are used.
- **Shed is not unhealthy.** The most explicitly-argued decision in the package (`summary.py:23-28`): "Before this split, stopping fourteen services for a load-shed rendered as '14 unhealthy' in amber on every console screen for a week — factually false … and corrosive: an amber that never clears is an amber nobody reads."
- **Absent beats invented.** Any block whose source can't answer is `null`, never `0` (`_traefik_block`, `_version_sync`).
- **A summary poll must never touch the network.** `updates.cached_platform_version()` is "Cache-only by design … expiry-tolerant — a stale answer to 'is a newer release out?' beats no answer."
- **Independent dashboard versioning** (`__version__.py:3-11`, owner decision 2026-07-31): the dashboard advances only on a real dashboard change, not per service release. "The invariant is running == PINNED."
- **`tolerant=True` reads, strict writes** (`services_d.load_declarations:86-93`): the dashboard's baked schema may lag the CLI, so unknown top-level keys are dropped for display; "The strict default is for the deploy/reconcile path, which must NEVER silently ignore an unaudited key — that rejection is the trust boundary."
- **The `metrics_url_template` boundary** (`settings.py:43-48`): a fully-resolved URL with `{service}`, config-driven, "names nothing platform-side (design/15 boundary, the design/23 §6 generic-links precedent)."

## Invariants & contracts

1. **Pin lockstep.** `DEFAULT_DOCKER_IMAGES["dashboard"]["tag"] == syrviscore_dashboard.__version__`, asserted by `tests/test_compose.py::TestImagePinLockstep` and re-checked by `build-tools/release-service.sh:64-74` ("re-pin the dashboard image in compose.py, or bump the dashboard `__version__` — they must match"). Current: `0.5.11`, digest `sha256:05dfa013…`.
2. **Cloudflared metrics port lockstep.** `settings.cloudflared_url` default port must equal `compose.CLOUDFLARED_METRICS_PORT` (20241) — asserted by `test_matches_dashboard_package_default`, which regexes the dashboard's `settings.py` from the service test suite. Compose also renders `CLOUDFLARED_URL` explicitly so a changed `metrics_port` moves listener and probe together.
3. **`syrvis-summary/v1`** is a stable external contract: `schema`, `state ∈ {ok,degraded,down}`, `detail`, `runstate`, `intent`, `version`, `containers.{core,l2}.{desired,running,unhealthy[],shed[]}`, `drift.{core_in_sync,l2_in_sync,items}`, `tunnel.{enabled,ready_connections}`, `traefik.{routers,errors}`, `last_deploy.{at,target,revision}`. `drift.items` counts **failing** items only so `items == 0` always agrees with `in_sync`. Nothing site-specific crosses it (asserted by `test_summary_carries_no_site_specific_facts`).
4. **`/healthz` is unauthenticated** (container liveness + the Dockerfile HEALTHCHECK); every `/api/*` route is not. `static.py` reserves `("api/", "auth/", "healthz")` from the SPA fallback.
5. **`SYRVIS_HOME` content-check, not path-identity.** `paths.get_syrvis_home` explicitly documents the dashboard's bind-mount as the reason (`paths.py:210-231`).
6. `PRIMORDIAL_UIS` / `SYNOLOGY_SERVICES` come from `syrviscore.traefik_config` "so the service→subdomain mapping cannot drift between the CLI, the hostnames report, the validators, and this launcher."

## The design/63 D2 reader-enumeration gate

`depends_on` was *already* in `ALLOWED_TOP_LEVEL_KEYS` before 0.5.16, with a **hard reject** in `ServiceDefinition.from_dict`: `"depends_on is not supported: each service is its own compose project…"` (visible in `git show HEAD~3:…/service_schema.py:1027-1043`). 0.5.16 supersedes that reject for the *orchestration* meaning.

The trap: `tolerant=True` only drops keys **outside** the allowlist. `depends_on` is *inside* it, so a stale image's `from_dict` still raises — the tolerant reader gives no protection at all here. Consequence chain in `api/declarations.py`: the raise moves the file into `invalid` → the name is absent from `declared` → `build_reconcile_plan` sees it as installed-but-undeclared → `state_of()` falls through every bucket and returns `"unmanaged"` — "the one state it definitively is not." Hence the release gate in `CLAUDE.md`: **the dashboard image must be rebuilt + repinned before any `depends_on` lands in a real `services.d`**, and while any edge exists **platform rollback below 0.5.16 is forbidden.**

The mitigation for the *total* failure case is elsewhere and accidental-looking: `build_reconcile_plan`'s FLOOR CHECK raises `ReconcileError` when there are 0 declarations but installed services, so if *every* declaration carries an edge the dashboard degrades to an explicit `error` rather than a confident wall of `unmanaged`. Partial adoption gets no such protection.

The repin history is itself the evidence: `compose.py:80-88` records that **the 0.5.10 image's bundled lib returned an EMPTY L2 list with `error: None` against 0.5.15+ intent state — "the lockstep trap's worst variant"** — and that **0.5.9 shipped an over-strict `SYRVIS_HOME` identity check that "emptied the dashboard's entire Layer-2 list, silently"** (`paths.py:229-230`). Two silent-blindness regressions in three image releases.

## docker-state-exporter

Not part of the web dashboard's data path at all — it feeds the *Grafana* dashboards `syrviscore/dashboard.py` generates. It is an owned, non-root, distroless rebuild of `karugaru/docker_state_exporter` (MIT, `main.go` vendored verbatim), born from the 2026-07-24 supply-chain audit that flagged `fviolence/docker-health-exporter` as "the estate's top risk: anonymous author, unmaintained, root, unsigned, touching the Docker API."

`main.go` carries two heavily-annotated incident fixes. **2026-08-10:** upstream passed `context.Background()` to every Docker call and `errCheck()` (= `os.Exit(1)`) on every failure; one wedged container (`onyx-background`) blocked each sweep for the socket-proxy's hardcoded `timeout server 10m`, took a 504, and killed the process — "the homebase lost EVERY `docker_container_*` series on an ~11 minute cycle for hours. One sick container blinded the monitoring for all 33." Fix: `collectBudget = 25s` / `listTimeout = 10s` / `inspectTimeout = 5s` (sized against `scrape_interval 60s, scrape_timeout 55s`, healthy sweep ≈750ms for ~39 containers), per-container `continue`, and a `container_state_collect_errors` gauge (`-1` = list failed) so partial collection is visible. **2026-08-14:** an unbounded startup ping turned transient dependency gaps (`lookup docker-socket-proxy on 127.0.0.11:53: no such host`) into a slow crash loop that `ContainerRestartLoop`'s 30m window never caught; fix is `pingRetryFor = 90s` / `pingTimeout = 5s` / `pingRetryWait = 3s`. Two latent upstream bugs also fixed: the `info.Config` nil-guard ran *after* the by-value append and was therefore inert, and unparseable timestamps were fatal.

## Gaps, debt & sharp edges

- **Two competing image-publish workflows with different tag sources.** `.github/workflows/dashboard-image.yml:32-38` tags at the **service** `__version__` ("ships in LOCKSTEP with the service") while `test.yml`'s `dashboard-image` job and `build-dashboard.sh` tag at the **dashboard** `__version__`. Both fire on merge-to-main and both push mutable `:latest`. `dashboard-image.yml` is an unremoved relic of the pre-2026-07-31 force-sync era and will publish `syrviscore-dashboard:0.5.16` while the compose pin says `0.5.11` — the exact confusion the version comment exists to prevent. No test guards this.
- **`/api/docs` and `/api/openapi.json` are unauthenticated.** They are registered by the `FastAPI()` constructor, not by `api_router`, so `dependencies=[Depends(require_user)]` never covers them; `static.py`'s `_RESERVED` prefix prevents the SPA from shadowing them. `test_auth.py` never asserts it. Low severity (schema only), but it contradicts "auth on every `/api/*` route" (`docs/dashboard.md:106`).
- **The frontend has not caught up with the 0.5.11 backend.** `api.ts:262` types `DeclarationState = "in_sync" | "disabled" | "unmanaged" | \`pending_${string}\`` and `ServicesPanel.driftPill` has no branch for `shed`, `terminal`, or `blocked` — all three fall through to the "Unmanaged" pill, reproducing in the UI precisely the mislabel the backend comment says is "the one state it definitively is not." `blocked_by` is typed nowhere and rendered nowhere. `intent` from `/api/summary` is unused in the SPA.
- **Docs describe a probe that no longer exists.** `docs/dashboard.md:23-24` claims a Cloudflare DDNS probe; `probes/cloudflare_ddns.py` survives only in `build/lib/`, and `settings.public_ip_url` / `cloudflare_api_url` are now dead config with no reader.
- **Read-only `data/` vs the update cache.** `image_updates._cache_path` is `<home>/data/.image-updates-cache.json`. With the default (`management: false`) `:ro` data mount, `/api/updates?refresh=true` and the cold-cache `check_updates()` write cannot succeed; they degrade into the `{"count": 0, …, "error": …}` envelope, so `/api/summary`'s `image_updates` stays `null` forever on a read-only instance unless the CLI/MCP populates it.
- **Two independent update caches, both process-local.** `updates._cache` is a module global with a 3600s TTL — lost on every container restart, and never shared with the on-disk image cache. `cached_platform_version()` returns *expired* data by design, so `/api/summary`'s `update_available` can be arbitrarily stale with no staleness marker in the payload.
- **Path-traversal guard is a string prefix.** `static.py:42` uses `str(candidate).startswith(str(root_resolved))` — a sibling directory named `<static>-anything` would satisfy it. Harmless in the shipped image (`/app/static`), fragile as a pattern.
- **Auth-mode ordering and cookie posture.** `require_user` checks the OIDC session *before* the Cloudflare JWT in `both` mode, so a stale session cookie outranks a live Access assertion. `SessionMiddleware` is added with `https_only=False` unconditionally, even in `cloudflare`/`both` mode where the dashboard is by definition behind TLS.
- **Log streaming blocks a thread per line.** `api/logs.py:42` does `asyncio.to_thread(next, iterator, None)` per line; a quiet container parks a threadpool worker indefinitely (the disconnect check only runs *between* lines), so N idle log tabs pin N threads until the 5,000-line cap or a line arrives.
- **`/api/routes` fans out with TLS verification off** (`verify=False`) and probes every declared hostname on every call, with no TTL cache of its own — the one endpoint that is neither cheap nor bounded by the aggregator.
- **`_last_deploy_sync` compares ISO strings lexically** (`at > newest["at"]`), which is correct only while every record uses the same UTC `Z` format.
- **Test blind spots.** No test drives a declaration into the `blocked` state (`test_api_declarations.py` only asserts `blocked_by: None`); no test exercises a *version-skewed* library against a newer declaration — the very failure mode the D2 gate exists for; no test asserts the docs/OpenAPI auth posture; no frontend unit tests at all (CI runs `tsc --noEmit` + `vite build` only); `conftest` forces `DOCKER_HOST` at a dead socket, so every management path is exercised against mocks, never a real daemon.
- **Unclear ownership between `syrviscore/dashboard.py` and this package.** They share a name and nothing else: the former projects declared services into *Grafana* JSON (`DASHBOARD_SCHEMA_VERSION = 1`, `__syrviscore: {generated: true}` provenance marker), the latter is the web app. `_CORE_ABOUT` in the generator even contains a blurb for the web dashboard. The `Collector` dataclass still defaults to the docker-health-exporter metric names (`docker_container_running`, …) while the owned exporter emits `container_state_*` / `container_restartcount` — the generated Grafana model and the owned exporter do **not** speak the same metric contract out of the box.
- **Known-stale shipped string.** `main.go:160-166`: the `container_state_collect_errors` HELP text "still says 'their inspect failed' and is what the SHIPPED image emits into Grafana … Correct it with the next image rebuild."

## Raw material worth citing in the retrospective

- "the **third thin adapter** over the deterministic core library — after the `syrvis` CLI and the MCP server — and the first one that runs *on* the NAS" (`docs/dashboard.md:4-5`).
- "Management requires the docker socket mounted **read-write** — the same authority as Portainer (effectively host root)." (`docs/dashboard.md:104-105`)
- "the *initial* create + macvlan shim stays with the CLI (host-root, chicken-and-egg)" (`docs/dashboard.md:147`).
- "Run this yourself over SSH — the dashboard never executes host-root commands." (`api/system.py:26`)
- "stopping fourteen services for a load-shed rendered as '14 unhealthy' in amber on every console screen for a week — factually false … an amber that never clears is an amber nobody reads." (`api/summary.py:25-28`)
- "Any block whose source is unavailable is `null` rather than a fabricated zero (absent beats invented)." (`api/summary.py:31-32`)
- "the 0.5.10 image's bundled lib returned an EMPTY L2 list with error:None against 0.5.15+ intent state, **the lockstep trap's worst variant**" (`compose.py:84-86`).
- "Regression 2026-08-16: the strict check here emptied the dashboard's entire Layer-2 list, silently, on the 0.5.9 image." (`paths.py:229-230`)
- "`.get()` so this reader still works against an older platform lib (the dashboard image bundles its own copy)." (`api/declarations.py:50`)
- "One sick container must never be able to blind the sensor for all the healthy ones." (`images/docker-state-exporter/main.go:265-266`)
- Numbers: aggregator TTL 5.0s / probe timeout 3.0s / SSE floor 0.5s; `LOG_STREAM_MAX_LINES = 5000`; platform-update cache 3600s, image cache ~6h; `_NAME_CAP = 3`; `CLOUDFLARED_METRICS_PORT = 20241`; dashboard `0.5.11` against service `0.5.17`; exporter budgets 25s/10s/5s and 90s/5s/3s; published ghcr tags 0.5.2–0.5.7 are force-sync-era relics.