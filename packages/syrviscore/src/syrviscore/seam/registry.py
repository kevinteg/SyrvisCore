"""
The single source of truth for every remote command a seam client may run (G18).

Both the MCP runtime (syrviscore_mcp.remote builds an ssh argv from a Command)
and the generator (syrviscore.seam.gen renders the sudoers file and the
forced-command shim) consume this same registry, so the enumerated sudoers
boundary, the shim allowlist, and the actual argv can never drift apart. A
drift test asserts the committed sudoers/shim match what gen produces from
this registry.

Command construction rules (mirroring the real CLI, verified against the code):
- `syrvis` commands run via the WRAPPER (it exports SYRVIS_HOME) — no --path.
- `syrvisctl` commands run via the venv binary. Only `install` accepts --path;
  the others resolve SYRVIS_HOME via the single-install volume scan (works
  under sudo in production). So we pass --path ONLY where the CLI supports it.
- A user-supplied positional value is always placed after a literal `--`
  separator, after all server-controlled flags/options (so it can never be
  parsed as a flag). Flag-VALUE slots (e.g. `--keep N`) are server-gated ints.
- Optional positionals expand to TWO accepted forms (present / absent) in the
  sudoers + shim allowlist.
"""

from dataclasses import dataclass, field
from typing import List, Optional

# Slot kinds — how the user value for a slot is validated (see validate.py).
KIND_VERSION = "version"
KIND_NAME = "name"
KIND_GIT_URL = "git_url"
KIND_KEEP = "keep"
KIND_TAIL = "tail"
KIND_IMAGE = "image"
KIND_SUBDOMAIN = "subdomain"
KIND_EXPOSURE = "exposure"
KIND_PORT = "port"
KIND_PRUNE_POLICY = "prune_policy"
KIND_BOOLEAN = "boolean"
KIND_STACK_SERVICE = "stack_service"
KIND_REVISION = "revision"
KIND_HALT_REASON = "halt_reason"
# A shed reason is a short machine token (`md6-resync`), never prose — it rides
# argv through the shim's char-allowlist and becomes a metric label. Mirrors
# syrviscore.intent.SHED_REASON_RE.
KIND_SHED_REASON = "shed_reason"
# A review date/timestamp for a shed row. Mirrors intent.SHED_UNTIL_RE; both
# accepted shapes use only characters already on the shim's allowlist.
KIND_TIMESTAMP = "timestamp"

# design/66. A FULL 40-hex commit sha and a `sha256:<64 hex>` digest — the two
# values that advance the jobs pin. Both are pure hex under the shim's existing
# character allowlist ([A-Za-z0-9 ._@:/-]), so no allowlist widening is needed.
KIND_GIT_REV = "git_rev"
KIND_SHA256 = "sha256_digest"

# The core-tier service set for KIND_STACK_SERVICE slots. Duplicated from
# syrviscore.stack (ALL_SERVICES / PRIMORDIAL) so the generator stays
# stdlib-only when run straight from the source tree; a drift test asserts
# the two stay identical.
STACK_SERVICES = ("traefik", "portainer", "cloudflared", "dashboard")
STACK_PRIMORDIAL = ("traefik", "portainer")


@dataclass(frozen=True)
class Slot:
    name: str
    kind: str
    optional: bool = False


@dataclass(frozen=True)
class Command:
    id: str
    cli: str  # "syrvis" | "syrvisctl"
    subcommand: List[str]  # literal tokens after the binary, e.g. ["service", "stop"]
    sudo: bool = False
    read_only: bool = False
    destructive: bool = False
    expect_json: bool = True
    flags: List[object] = field(default_factory=list)  # literals + FlagValue Slots (e.g. --keep N)
    install_path: bool = False  # append --path <home> (only 'install' supports it)
    positional: Optional[Slot] = None  # user value placed after '--'
    timeout_s: int = 120


# FlagValue: a Slot that supplies the value of a preceding literal flag
# (e.g. --keep {keep}). Distinct from `positional` which goes after '--'.
@dataclass(frozen=True)
class FlagValue:
    slot: Slot


COMMANDS: List[Command] = [
    # ---- read-only (no sudo, no token) ----
    Command("status", "syrvis", ["status"], read_only=True, flags=["--json"]),
    Command("verify", "syrvis", ["verify"], read_only=True, flags=["--json"]),
    Command("verify_smoke", "syrvis", ["verify"], read_only=True, flags=["--smoke", "--json"]),
    Command("service_list", "syrvis", ["service", "list"], read_only=True, flags=["--json"]),
    Command(
        "logs",
        "syrvis",
        ["logs"],
        read_only=True,
        expect_json=False,
        flags=["-n", FlagValue(Slot("tail", KIND_TAIL))],
        positional=Slot("service", KIND_NAME, optional=True),
    ),
    Command("stack_hostnames", "syrvis", ["stack", "hostnames"], read_only=True, flags=["--json"]),
    # Same read-only `stack hostnames`, but UNDER SUDO so root can read the 0600
    # instance config — the report then carries domain + traefik_ip + record
    # targets. The non-sudo variant above runs as the operator, cannot read the
    # config, and returns those as null (fine for a reach check, but useless for
    # the LAN-DNS planner, which raises without traefik_ip). sudo + read_only
    # mirrors `export`/`reconcile_plan`; the `sudo -n` prefix makes it a distinct
    # argv shape, so both variants coexist on the shim.
    Command(
        "stack_hostnames_full",
        "syrvis",
        ["stack", "hostnames"],
        sudo=True,
        read_only=True,
        flags=["--json"],
    ),
    # dashboard generate projects the declared service set into a Grafana
    # dashboard JSON — read-only, no sudo (it reads the 0644 stack.yaml + public
    # manifests; a 0600 manifest just drops out, like service_list). The estate
    # pulls this over the seam to provision Grafana.
    Command(
        "dashboard_generate",
        "syrvis",
        ["dashboard", "generate"],
        read_only=True,
        flags=["--json"],
    ),
    # --all emits the overview + one deep-dive dashboard per service (the estate
    # provisions each into Grafana). Fixed flag order so the shim matches exactly.
    Command(
        "dashboard_generate_all",
        "syrvis",
        ["dashboard", "generate"],
        read_only=True,
        flags=["--all", "--json"],
    ),
    Command("service_catalog", "syrvis", ["service", "catalog"], read_only=True, flags=["--json"]),
    # history reads the deployment-revision records (data/deployments/) — always
    # redacted (env NAMES only), 0640/0644 files, so no sudo like service_list.
    Command(
        "deployment_history",
        "syrvis",
        ["history"],
        read_only=True,
        flags=["--json"],
        positional=Slot("name", KIND_NAME, optional=True),
    ),
    Command("profile_list", "syrvis", ["profile", "list"], read_only=True, flags=["--json"]),
    # images: per-image provenance + freshness (trust registry + cached network
    # reads). Read-only, no sudo — reads the compose config + public manifests +
    # the committed image_trust.yaml; degrades gracefully like updates/service_list.
    Command("images", "syrvis", ["images"], read_only=True, flags=["--json"]),
    # export snapshots the live instance as a syrvis-instance bundle. Read-only,
    # but sudo so it can read the 0600 config over the seam (like reconcile_plan);
    # over the seam it is ALWAYS redacted (no --reveal-secrets shape exists, so a
    # secret value can never transit the MCP/seam).
    Command("export", "syrvis", ["export"], sudo=True, read_only=True, flags=["--json"]),
    # image_updates queries external registries (openWorldHint on the MCP side)
    # but only READS + caches; it never pulls or changes anything. sudo (like
    # reconcile_plan) so the operator can WRITE the shared cache under the
    # root-owned data/ dir — otherwise every seam/MCP call re-queries every
    # registry, defeating the cache.
    Command(
        "image_updates",
        "syrvis",
        ["updates"],
        sudo=True,
        read_only=True,
        flags=["--json"],
        timeout_s=120,
    ),
    # schedule list parses the managed crontab block + jobs.d — read-only. It runs
    # under sudo so the 0600-ish jobs.d declarations are readable over the seam,
    # but the CLI itself performs no privileged action (like reconcile_plan).
    Command(
        "schedule_list",
        "syrvis",
        ["schedule", "list"],
        sudo=True,
        read_only=True,
        flags=["--json"],
    ),
    # schedule dsm-tasks enumerates DSM's OWN Task Scheduler (synoschedtask
    # --get) — the one scheduler surface the seam could not see, so a task
    # pointing outside SyrvisCore's managed crontab block was undetectable
    # (design/20's gap #2, ops:F20). READ-ONLY in the strongest sense: the verb
    # has no write path at all, and SyrvisCore never creates or edits a DSM task.
    # sudo because synoschedtask is root-only — same shape as schedule_list.
    Command(
        "schedule_dsm_tasks",
        "syrvis",
        ["schedule", "dsm-tasks"],
        sudo=True,
        read_only=True,
        flags=["--json"],
    ),
    Command("versions_list", "syrvisctl", ["list"], read_only=True, flags=["--json"]),
    Command("check_updates", "syrvisctl", ["check"], read_only=True, flags=["--json"]),
    Command("info", "syrvisctl", ["info"], read_only=True, flags=["--json"]),
    # syrvisctl doctor is the ONE verb that still answers when SYRVIS_HOME is
    # renamed, unmounted or gone: it runs from the SPK's rootfs venv, imports
    # nothing from the service package (which lives inside the tree under
    # suspicion) and reports a failed home resolution as a FINDING instead of
    # exiting. It is therefore the highest-value read on the seam during the
    # exact failure where `syrvis status`/`verify` cannot run at all — so it is
    # read_only (no sudo, reader-callable), like versions_list/check_updates.
    # NB it exits 1 when it finds something; the caller reads the JSON, not the
    # exit code, to tell "unreachable" from "reachable and broken".
    Command("doctor", "syrvisctl", ["doctor"], read_only=True, flags=["--json"]),
    Command("backup_list", "syrvisctl", ["backup", "list"], read_only=True, flags=["--json"]),
    Command(
        "cleanup_preview",
        "syrvisctl",
        ["cleanup"],
        read_only=True,
        expect_json=False,
        flags=["--keep", FlagValue(Slot("keep", KIND_KEEP)), "--dry-run"],
    ),
    # ---- privileged, non-destructive (sudo, no token) ----
    # reconcile --dry-run is READ-ONLY by construction (side-effect-free plan);
    # it runs under sudo only so the 0600 services.d declaration files are
    # readable over the seam — the CLI itself skips privilege elevation here.
    Command(
        "reconcile_plan",
        "syrvis",
        ["reconcile"],
        sudo=True,
        read_only=True,
        flags=["--dry-run", "--json"],
    ),
    # WITHOUT --prune, reconcile never removes anything (non-destructive, like
    # verify_fix): it converges to config/services.d declarations only.
    Command("reconcile", "syrvis", ["reconcile"], sudo=True, flags=["--json", "-y"], timeout_s=600),
    # The same reconcile, but overriding guard_bulk_degraded (a rebuilding RAID
    # array). Its own argv shape because the shim matches exactly; a separate
    # command id so an MCP client has to ASK for the override rather than
    # inherit it, and so the audit line names which one ran. The override is
    # additionally journaled NAS-side in logs/overrides.log.
    Command(
        "reconcile_force",
        "syrvis",
        ["reconcile"],
        sudo=True,
        flags=["--json", "-y", "--force"],
        timeout_s=600,
    ),
    Command("start", "syrvis", ["start"], sudo=True, expect_json=False),
    Command("stop", "syrvis", ["stop"], sudo=True, expect_json=False),
    Command("restart", "syrvis", ["restart"], sudo=True, expect_json=False),
    # shutdown/resume: the graceful instance halt + bring-up (the UPS path).
    # NON-destructive like stop/start/restart — deliberately token-free so an
    # unattended NUT low-battery hook can fire shutdown with no human in the
    # loop; halting is reversible (resume) and loses no data. The reason slot
    # is enumerated (ups|maintenance), triple-validated (CLI Choice, MCP
    # validator, shim predicate).
    Command(
        "shutdown",
        "syrvis",
        ["shutdown"],
        sudo=True,
        flags=["--reason", FlagValue(Slot("reason", KIND_HALT_REASON)), "--json"],
        timeout_s=240,
    ),
    Command("resume", "syrvis", ["resume"], sudo=True, flags=["--json"], timeout_s=300),
    Command(
        "restart_graceful",
        "syrvis",
        ["restart"],
        sudo=True,
        flags=["--graceful", "--json"],
        timeout_s=600,
    ),
    Command("stack_apply", "syrvis", ["stack", "apply"], sudo=True, expect_json=False),
    # stack enable/disable write core-tier intent to config/stack.yaml only;
    # nothing runs until stack_apply + start (like service_declare). Rich
    # settings (subdomain/exposure) flow through the instance bundle (apply) —
    # the seam keeps the imperative form minimal. The CLI rejects disabling a
    # primordial service; the client validates the same set fail-closed.
    Command(
        "stack_enable",
        "syrvis",
        ["stack", "enable"],
        sudo=True,
        expect_json=False,
        positional=Slot("name", KIND_STACK_SERVICE),
    ),
    Command(
        "stack_disable",
        "syrvis",
        ["stack", "disable"],
        sudo=True,
        expect_json=False,
        positional=Slot("name", KIND_STACK_SERVICE),
    ),
    # profile enable writes services.d declarations for a platform-curated set
    # (catalog-pinned images) + seeds default configs, never overwriting —
    # intent-only like service_declare (reconcile converges later).
    Command(
        "profile_enable",
        "syrvis",
        ["profile", "enable"],
        sudo=True,
        flags=["--json"],
        positional=Slot("name", KIND_NAME),
    ),
    # backup create reads the whole tree (incl. 0600 config) into an archive —
    # sudo, but additive and idempotent: no token.
    Command(
        "backup_create",
        "syrvisctl",
        ["backup", "create"],
        sudo=True,
        expect_json=False,
        timeout_s=600,
    ),
    Command("verify_fix", "syrvis", ["verify"], sudo=True, flags=["--fix", "--json"]),
    Command(
        "verify_fix_smoke", "syrvis", ["verify"], sudo=True, flags=["--smoke", "--fix", "--json"]
    ),
    Command(
        "service_start",
        "syrvis",
        ["service", "start"],
        sudo=True,
        expect_json=False,
        positional=Slot("name", KIND_NAME),
    ),
    Command(
        "service_stop",
        "syrvis",
        ["service", "stop"],
        sudo=True,
        expect_json=False,
        positional=Slot("name", KIND_NAME),
    ),
    # service recreate replaces a service's CONTAINER from its already-installed
    # manifest (`up -d --force-recreate`) and writes no declared intent. It is the
    # only verb that re-bakes an env_file — Docker fixes a container's environment
    # at CREATE time, so `restart` cannot (incident 2026-08-16). Same trust class
    # and argv shape as service_start/service_stop: sudo, one is_name-gated
    # positional, no operator-supplied content. Longer timeout than start because
    # a force-recreate stops the old container under its own stop grace first.
    Command(
        "service_recreate",
        "syrvis",
        ["service", "recreate"],
        sudo=True,
        expect_json=False,
        positional=Slot("name", KIND_NAME),
        timeout_s=300,
    ),
    # service task runs a DECLARED, schema-audited one-shot argv inside the
    # service's own RUNNING container (docker exec) — the encapsulated
    # alternative to a break-glass docker exec (e.g. a DB bootstrap). The argv
    # comes from the installed manifest only; the operator picks a task NAME,
    # never supplies code, and the task runs under the container's existing
    # confinement (no authority the container did not already have).
    Command(
        "service_task",
        "syrvis",
        ["service", "task"],
        sudo=True,
        expect_json=False,
        flags=["--task", FlagValue(Slot("task", KIND_NAME))],
        positional=Slot("name", KIND_NAME),
        timeout_s=600,
    ),
    Command(
        "service_update",
        "syrvis",
        ["service", "update"],
        sudo=True,
        expect_json=False,
        positional=Slot("name", KIND_NAME),
    ),
    # service shed/unshed write DECLARED INTENT to data/state/intent.json — the
    # durable "this service is deliberately down" that lives outside the
    # declaration set and therefore survives a GitOps apply (incident
    # 2026-08-16: a repo apply resurrected 14 load-shed services mid-rebuild).
    # Mutating (sudo), non-destructive: shedding stops a container, which is
    # reversible and loses no data — the same trust class as service_stop, and
    # deliberately token-free so an unattended degradation response can shed
    # without a human in the loop. TWO shed shapes because the shim matches
    # exact argv: with and without the optional --until.
    Command(
        "service_shed",
        "syrvis",
        ["service", "shed"],
        sudo=True,
        flags=["--reason", FlagValue(Slot("reason", KIND_SHED_REASON)), "--json"],
        positional=Slot("name", KIND_NAME),
    ),
    Command(
        "service_shed_until",
        "syrvis",
        ["service", "shed"],
        sudo=True,
        flags=[
            "--reason",
            FlagValue(Slot("reason", KIND_SHED_REASON)),
            "--until",
            FlagValue(Slot("until", KIND_TIMESTAMP)),
            "--json",
        ],
        positional=Slot("name", KIND_NAME),
    ),
    Command(
        "service_unshed",
        "syrvis",
        ["service", "unshed"],
        sudo=True,
        flags=["--json"],
        positional=Slot("name", KIND_NAME),
    ),
    # VM workloads (config/vms.d/*.yaml → Synology VMM via synowebapi, which is
    # root-only — so even the read verbs are sudo, like export/schedule_list). The
    # operator picks the declaration NAME (safe charset, gated to vms.d/ by the
    # CLI); the VMM guest_name (a display name, may contain spaces) stays internal.
    # ADOPT is deliberately OFF the seam (a one-time root/GUI setup step, like
    # `setup`/`restore`), and VM create/delete never touch the seam at all.
    Command("vm_list", "syrvis", ["vm", "list"], sudo=True, read_only=True, flags=["--json"]),
    Command(
        "vm_status",
        "syrvis",
        ["vm", "status"],
        sudo=True,
        read_only=True,
        flags=["--json"],
        positional=Slot("name", KIND_NAME),
    ),
    Command(
        "vm_start",
        "syrvis",
        ["vm", "start"],
        sudo=True,
        expect_json=False,
        positional=Slot("name", KIND_NAME),
    ),
    Command(
        "vm_stop",
        "syrvis",
        ["vm", "stop"],
        sudo=True,
        expect_json=False,
        positional=Slot("name", KIND_NAME),
    ),
    Command(
        "vm_restart",
        "syrvis",
        ["vm", "restart"],
        sudo=True,
        expect_json=False,
        positional=Slot("name", KIND_NAME),
    ),
    Command(
        "service_add",
        "syrvis",
        ["service", "add"],
        sudo=True,
        expect_json=False,
        positional=Slot("git_url", KIND_GIT_URL),
        timeout_s=600,
    ),
    # service set-image re-pins an installed image-first service to a new pinned
    # image and PULLS + RUNS it — like service_run, it runs new code, so the MCP
    # gates it with a confirmation token. Fixed argv: --image <ref> -- <name>.
    Command(
        "service_set_image",
        "syrvis",
        ["service", "set-image"],
        sudo=True,
        expect_json=False,
        flags=["--image", FlagValue(Slot("image", KIND_IMAGE))],
        positional=Slot("name", KIND_NAME),
        timeout_s=600,
    ),
    Command(
        "service_run",
        "syrvis",
        ["service", "run"],
        sudo=True,
        expect_json=False,
        # Fixed flag order mirrors remote.build_remote_tokens so the shim matches
        # the real argv exactly. name is the trailing positional (after '--').
        flags=[
            "--image",
            FlagValue(Slot("image", KIND_IMAGE)),
            "--subdomain",
            FlagValue(Slot("subdomain", KIND_SUBDOMAIN)),
            "--exposure",
            FlagValue(Slot("exposure", KIND_EXPOSURE)),
            "--port",
            FlagValue(Slot("port", KIND_PORT)),
        ],
        positional=Slot("name", KIND_NAME),
        timeout_s=600,
    ),
    # service declare authors a services.d declaration through the schema trust
    # boundary and applies NOTHING (reconcile applies later) — non-destructive.
    # Fixed flag order mirrors remote.build_remote_tokens so the shim matches
    # the real argv exactly. name is the trailing positional (after '--').
    Command(
        "service_declare",
        "syrvis",
        ["service", "declare"],
        sudo=True,
        flags=[
            "--image",
            FlagValue(Slot("image", KIND_IMAGE)),
            "--subdomain",
            FlagValue(Slot("subdomain", KIND_SUBDOMAIN)),
            "--exposure",
            FlagValue(Slot("exposure", KIND_EXPOSURE)),
            "--port",
            FlagValue(Slot("port", KIND_PORT)),
            "--enabled",
            FlagValue(Slot("enabled", KIND_BOOLEAN)),
            "--critical",
            FlagValue(Slot("critical", KIND_BOOLEAN)),
            "--json",
        ],
        positional=Slot("name", KIND_NAME),
    ),
    # service adopt generates a declaration from an existing install; the
    # install itself is not touched — non-destructive.
    Command(
        "service_adopt",
        "syrvis",
        ["service", "adopt"],
        sudo=True,
        flags=["--json"],
        positional=Slot("name", KIND_NAME),
    ),
    Command(
        "install",
        "syrvisctl",
        ["install"],
        sudo=True,
        expect_json=False,
        flags=["-y"],
        install_path=True,
        positional=Slot("version", KIND_VERSION, optional=True),
        timeout_s=600,
    ),
    # ---- privileged + destructive (sudo, confirmation token) ----
    # reconcile --prune additionally acts on installed-but-undeclared services;
    # 'remove'/'purge' are DESTRUCTIVE, so the whole command takes the token.
    Command(
        "reconcile_prune",
        "syrvis",
        ["reconcile"],
        sudo=True,
        destructive=True,
        flags=["--json", "-y", "--prune", FlagValue(Slot("prune", KIND_PRUNE_POLICY))],
        timeout_s=600,
    ),
    # service rollback re-deploys a PRIOR deployment revision (old image runs
    # again) — destructive class like set-image/activate. Over the seam the
    # target revision is ALWAYS explicit (--to N); the default-to-previous
    # convenience is CLI-interactive only, so automation must name its target.
    Command(
        "service_rollback",
        "syrvis",
        ["service", "rollback"],
        sudo=True,
        destructive=True,
        expect_json=False,
        flags=["--to", FlagValue(Slot("revision", KIND_REVISION)), "-y"],
        positional=Slot("name", KIND_NAME),
        timeout_s=600,
    ),
    Command(
        "activate",
        "syrvisctl",
        ["activate"],
        sudo=True,
        destructive=True,
        expect_json=False,
        positional=Slot("version", KIND_VERSION),
    ),
    Command(
        "rollback",
        "syrvisctl",
        ["rollback"],
        sudo=True,
        destructive=True,
        expect_json=False,
        flags=["-y"],
        positional=Slot("version", KIND_VERSION, optional=True),
    ),
    Command(
        "uninstall",
        "syrvisctl",
        ["uninstall"],
        sudo=True,
        destructive=True,
        expect_json=False,
        flags=["-y"],
        positional=Slot("version", KIND_VERSION),
    ),
    Command(
        "cleanup",
        "syrvisctl",
        ["cleanup"],
        sudo=True,
        destructive=True,
        expect_json=False,
        flags=["--keep", FlagValue(Slot("keep", KIND_KEEP)), "-y"],
    ),
    Command(
        "service_remove",
        "syrvis",
        ["service", "remove"],
        sudo=True,
        destructive=True,
        expect_json=False,
        flags=["-y"],
        positional=Slot("name", KIND_NAME),
    ),
    # backup cleanup DELETES old backup archives (disaster-recovery artifacts),
    # so it takes the two-call token like cleanup.
    Command(
        "backup_cleanup",
        "syrvisctl",
        ["backup", "cleanup"],
        sudo=True,
        destructive=True,
        expect_json=False,
        flags=["--keep", FlagValue(Slot("keep", KIND_KEEP)), "-y"],
    ),
    # schedule apply reconciles config/jobs.d -> jobs/ scripts + the managed
    # /etc/crontab block. It mutates root cron, so it is DESTRUCTIVE (two-call HMAC
    # handshake, like reconcile_prune). It takes NO cron argv: the schedule lives
    # only in jobs.d (its '*'/',' would fail the shim char-allowlist), and the
    # command is derived as jobs/<name> — the operator never supplies either.
    Command(
        "schedule_apply",
        "syrvis",
        ["schedule", "apply"],
        sudo=True,
        destructive=True,
        flags=["--json"],
        timeout_s=600,
    ),
    # schedule sync clones the ONE root-configured source (config/jobs.source),
    # installs its jobs.d declarations + materializes root-owned jobs/<name>
    # scripts, then reconciles the managed /etc/crontab block. It fetches + runs
    # a root-vetted repo and mutates root cron, so it is DESTRUCTIVE (two-call HMAC
    # handshake, like schedule_apply). It takes NO argv: the source is root-owned
    # (the operator cannot pass or influence it), the cron spec lives only in the
    # YAML, and the command is derived as jobs/<name> — nothing operator-supplied.
    Command(
        "schedule_sync",
        "syrvis",
        ["schedule", "sync"],
        sudo=True,
        destructive=True,
        flags=["--json"],
        timeout_s=600,
    ),
    # design/66: ADVANCE the jobs pin. The bare schedule_sync row above now means
    # "re-materialize the already-reviewed commit" — it cannot pick up a push, so
    # it is safe to hand an operator as a repair. THIS row is the deliberate
    # review act, and it is deliberately reachable from the seam: an operator who
    # can run bare `schedule sync` could ALREADY install any repo content as root
    # cron before this change, so making the rev explicit grants no new
    # capability — it only makes the existing one argument-bearing, journaled,
    # and diffable against home-tech's config/jobs.pin record. What stays
    # root-only is config/jobs.source (the URL), unchanged since design/12 §1.
    # Both slots are strict hex; neither can carry a path, a URL or a shell char.
    Command(
        "schedule_sync_pin",
        "syrvis",
        ["schedule", "sync"],
        sudo=True,
        destructive=True,
        flags=[
            "--to",
            FlagValue(Slot("to", KIND_GIT_REV)),
            "--manifest",
            FlagValue(Slot("manifest", KIND_SHA256)),
            "--json",
        ],
        timeout_s=600,
    ),
    # secret set writes a Layer 2 service's env_file secret atomically as root:root
    # 0600. The secret arrives on stdin ONLY — it is never a CLI argument, never
    # logged, never a token. destructive=False (idempotent per-service overwrite,
    # analogous to service_declare). expect_json=False (plain "wrote <path>" line).
    # No --json flag: apply-immich-secrets only needs the exit code.
    Command(
        "secret_set",
        "syrvis",
        ["secret", "set"],
        sudo=True,
        destructive=False,
        expect_json=False,
        positional=Slot("name", KIND_NAME),
    ),
    # config set writes a scheduled job's config/<name>.conf atomically as
    # root:root 0600 — the jobs analog of secret_set (which writes a service's
    # env_file). The conf body arrives on stdin ONLY: never a CLI argument, never
    # logged, never a token. destructive=False (idempotent per-job overwrite).
    # expect_json=False (plain "wrote <path>" line). No --json flag: the caller
    # only needs the exit code. The name is gated to a DECLARED job in
    # config/jobs.d/ by the CLI impl (write_config), just as secret_set gates on
    # services.d — so the operator can render a VETTED job's conf but not create
    # confs for arbitrary names.
    Command(
        "config_set",
        "syrvis",
        ["config", "set"],
        sudo=True,
        destructive=False,
        expect_json=False,
        positional=Slot("name", KIND_NAME),
    ),
    # deploy applies a resolved syrvis-bundle (manifest + non-secret configs +
    # secret values) to ONE service atomically — the encapsulated services-plane
    # apply (design/21). The whole bundle arrives on stdin ONLY (secrets never on
    # argv/ps/logs); the sole argv token is the is_name-gated service name, which
    # is authoritative (a bundle claiming a different service.name is rejected by
    # the CLI). destructive=False (idempotent per-service install/update, like
    # service_declare/secret_set — the CLI's deploy_bundle rolls back on failure).
    # expect_json=False (plain "deployed <name> ..." line). No --json flag: the
    # caller (deploy-stack) needs the exit code + message. timeout_s mirrors
    # service_run (a fresh deploy may pull the image + start the container).
    Command(
        "deploy",
        "syrvis",
        ["deploy"],
        sudo=True,
        destructive=False,
        expect_json=False,
        positional=Slot("name", KIND_NAME),
        timeout_s=600,
    ),
    # deploy overriding guard_bulk_degraded — same reasoning as reconcile_force:
    # a distinct argv shape for the shim, a distinct id so the override is
    # requested and audited rather than inherited.
    Command(
        "deploy_force",
        "syrvis",
        ["deploy"],
        sudo=True,
        destructive=False,
        expect_json=False,
        flags=["--force"],
        positional=Slot("name", KIND_NAME),
        timeout_s=600,
    ),
    # apply writes the core-tier configuration from a syrvis-instance bundle
    # (.env + stack.yaml + the services.d declaration set) atomically — the
    # core-tier sibling of deploy. The whole bundle arrives on stdin ONLY
    # (tokens never on argv/ps/logs); there are no argv slots at all. It only
    # WRITES config (converge stays reconcile/stack_apply), and re-applying is
    # idempotent, so destructive=False like deploy/secret_set. Three enumerated
    # variants because the shim matches exact argv shapes: the read-only plan,
    # the normal apply, and the deliberate secret-rotation apply.
    Command(
        "apply_plan",
        "syrvis",
        ["apply"],
        sudo=True,
        read_only=True,
        flags=["--dry-run", "--json"],
    ),
    Command("apply", "syrvis", ["apply"], sudo=True, flags=["--json"]),
    Command(
        "apply_secrets",
        "syrvis",
        ["apply"],
        sudo=True,
        flags=["--allow-secret-change", "--json"],
    ),
    # The deliberate-resurrection apply: permits the bundle to re-enable
    # services that are declared OFF on this instance. Its own shape and id for
    # the same reason as apply_secrets — the override must be asked for, not
    # inherited, and the audit line must say which one ran. NB there is
    # deliberately NO combined secret+enable shape: two overrides in one call
    # is two decisions in one, and the operator can run them in sequence.
    Command(
        "apply_enable_change",
        "syrvis",
        ["apply"],
        sudo=True,
        flags=["--allow-enable-change", "--json"],
    ),
]

COMMANDS_BY_ID = {c.id: c for c in COMMANDS}

DESTRUCTIVE_IDS = frozenset(c.id for c in COMMANDS if c.destructive)


def get_command(cmd_id: str) -> Command:
    if cmd_id not in COMMANDS_BY_ID:
        raise KeyError(f"unknown command id: {cmd_id}")
    return COMMANDS_BY_ID[cmd_id]
