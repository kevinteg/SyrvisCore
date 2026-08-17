"""Scheduled-jobs library — the PRIVILEGED half of the jobs.d capability.

Trust model (design/12, owner-set boundary): the git source of job scripts +
declarations is ROOT-CONFIGURED in ``<home>/config/jobs.source`` (a single repo
URL the operator cannot write). There is NO per-declaration source — so a
compromised operator can neither point a job at an arbitrary repo (which would be
arbitrary root code via the fetched script) nor supply a command
(derive-not-declare). It can at most re-apply the already-synced, root-vetted set.

Two privileged operations (root-only in production):

1. **sync_from_source** — clone the ONE configured source, copy its
   ``jobs.d/*.yaml`` into ``config/jobs.d/`` (each re-validated), materialize each
   declared+enabled job's ``jobs/<name>`` script to ``<home>/jobs/<name>``
   (``root:root 0755``), then reconcile the managed crontab block. The deliberate
   "install/update jobs from the trusted repo" op.
2. **apply_schedule** — LOCAL reconcile only (no fetch): rewrite the managed
   ``/etc/crontab`` block from the already-synced ``jobs.d``. The self-heal path
   (boot hook / ``verify --fix``) after DSM regenerates ``/etc/crontab`` — the
   scripts persist on disk, so no re-clone is needed.

The managed block is delimited; only it is ever rewritten (DSM's own lines + the
header are preserved). The cron spec lives only in the YAML — never an MCP argv.
"""

import hashlib
import os
import re
import shutil
import stat
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from . import jobs_d, paths
from .errors import SyrvisError

CRONTAB_PATH = Path("/etc/crontab")
BLOCK_BEGIN = jobs_d.BLOCK_BEGIN
BLOCK_END = jobs_d.BLOCK_END
SOURCE_CONFIG_NAME = "jobs.source"  # <home>/config/jobs.source — ROOT-owned

# DSM's own scheduler, which SyrvisCore neither owns nor manages — it only
# ENUMERATES it (see :func:`dsm_task_census`). Module-level so tests can repoint
# it, exactly like privileged_ops.NETWORK_SCRIPTS_DIR.
SYNOSCHEDTASK_PATH = Path("/usr/syno/bin/synoschedtask")
DSM_TASK_TIMEOUT_S = 20
MAX_DSM_TASKS = 200
MAX_DSM_FIELD_CHARS = 500


class ScheduleError(SyrvisError):
    """A scheduled-jobs reconcile-level failure."""

    code = "schedule_failed"


# ---------------------------------------------------------------------------
# The single, root-configured source (operator cannot write it)
# ---------------------------------------------------------------------------


def get_source_config_path(syrvis_home: Path) -> Path:
    return Path(syrvis_home) / "config" / SOURCE_CONFIG_NAME


def get_configured_source(syrvis_home: Path) -> Optional[str]:
    """The single root-configured git source for jobs, or None (FAIL-CLOSED).

    First non-comment, non-blank line of ``<home>/config/jobs.source``. That file
    is root-owned — the operator cannot set it, so it cannot inject a source.
    Absent/empty/unreadable ⇒ None ⇒ sync is a no-op (the feature is dormant).
    """
    try:
        text = get_source_config_path(syrvis_home).read_text()
    except (FileNotFoundError, PermissionError, IsADirectoryError):
        return None
    for raw in text.splitlines():
        line = raw.strip()
        if line and not line.startswith("#"):
            return line
    return None


def _is_git_url(source: str) -> bool:
    """Transport gate: https / scp-style git@ / ssh:// / file:// only.

    ``file://`` is permitted because the source is ROOT-configured (config/jobs.source,
    which the operator cannot write) — not operator input — so a local-repo URL is a
    legitimate, safe deployment (and is how CI clones a fixture repo). The operator
    can never reach this: they cannot set jobs.source at all.
    """
    if not source or source.startswith("-"):
        return False
    return (
        source.startswith("https://")
        or source.startswith("git@")
        or source.startswith("ssh://")
        or source.startswith("file://")
    )


def _clone_configured_source(git_url: str) -> Tuple[bool, str, Optional[Path]]:
    """Clone the root-configured source with the SAME hardening services use.

    Restricted git protocols, no terminal prompt, shallow clone, ``--`` guard, and
    a timeout. The URL is NOT operator-supplied (it comes from the root-owned
    config file), but the hardening is kept regardless. Caller owns the temp dir.
    """
    if not _is_git_url(git_url):
        return (
            False,
            "configured jobs.source is not a supported git URL: {!r}".format(git_url),
            None,
        )

    env = dict(os.environ)
    # `file` is allowed alongside the network transports because the source is
    # ROOT-configured (never operator input); it is required so a local-repo
    # fixture (CI) — and a legitimate local mirror — can be cloned.
    env["GIT_ALLOW_PROTOCOL"] = "https:git:ssh:file"
    env["GIT_TERMINAL_PROMPT"] = "0"

    temp_dir = tempfile.mkdtemp(prefix="syrvis-jobs-")
    temp_path = Path(temp_dir) / "repo"
    try:
        result = subprocess.run(
            ["git", "clone", "--depth", "1", "--", git_url, str(temp_path)],
            capture_output=True,
            text=True,
            timeout=60,
            env=env,
        )
        if result.returncode != 0:
            shutil.rmtree(temp_dir, ignore_errors=True)
            return False, "failed to clone jobs.source: {}".format(result.stderr.strip()), None
    except subprocess.TimeoutExpired:
        shutil.rmtree(temp_dir, ignore_errors=True)
        return False, "git clone timed out for the configured jobs.source", None
    except FileNotFoundError:
        shutil.rmtree(temp_dir, ignore_errors=True)
        return False, "git is not installed", None
    return True, "cloned", temp_path


# ---------------------------------------------------------------------------
# Reading declarations + scripts FROM the cloned source repo
# ---------------------------------------------------------------------------
#
# Convention (repo layout): the trusted source repo provides jobs at its root:
#   jobs.d/<name>.yaml   — the declaration ({schedule, enabled}; no source/command)
#   jobs/<name>          — the script the derived command jobs/<name> runs
# Only these two well-known subtrees are read — never arbitrary repo contents.

REPO_DECLS_SUBDIR = "jobs.d"
REPO_SCRIPTS_SUBDIR = "jobs"


def _repo_declarations(checkout: Path) -> Dict[str, Path]:
    """Map name -> declaration path for jobs.d/*.yaml in the cloned repo."""
    decls_dir = checkout / REPO_DECLS_SUBDIR
    found: Dict[str, Path] = {}
    if decls_dir.is_dir():
        for path in sorted(decls_dir.glob("*.yaml")):
            found[path.stem] = path
    return found


def _repo_script(checkout: Path, name: str) -> Optional[Path]:
    """The script the repo contributes for <name> (jobs/<name> at the repo root).

    Only the one derived-name entrypoint is ever read — never arbitrary paths.
    A name is a single validated path component, so this cannot escape the subdir.
    """
    candidate = checkout / REPO_SCRIPTS_SUBDIR / name
    return candidate if candidate.is_file() else None


def _install_declaration(src_yaml: Path, name: str, syrvis_home: Path) -> None:
    """Validate a repo declaration and copy it into config/jobs.d/<name>.yaml.

    Re-validates through JobDefinition.from_dict (rejects source/command/unknown
    keys + bad cron) BEFORE it lands in the config tree. Atomic replace.
    """
    import yaml

    data = yaml.safe_load(src_yaml.read_text())
    if not isinstance(data, dict):
        raise ScheduleError("declaration {} is not a mapping".format(src_yaml.name))
    jobs_d.JobDefinition.from_dict(name, data)  # raises on any invalid/forbidden key

    dest_dir = jobs_d.get_jobs_declarations_dir(syrvis_home)
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / "{}.yaml".format(name)
    fd, tmp = tempfile.mkstemp(prefix=".{}.".format(name), dir=str(dest_dir))
    try:
        with os.fdopen(fd, "w") as f:
            f.write(src_yaml.read_text())
        os.replace(tmp, str(dest))
    finally:
        if Path(tmp).exists():
            Path(tmp).unlink()


def materialize_job_script(checkout: Path, name: str, jobs_dir: Path) -> Tuple[bool, str]:
    """Copy the trusted repo's ``jobs/<name>`` to ``<jobs_dir>/<name>`` (root:root 0755).

    ``checkout`` is the already-cloned ROOT-configured source (not operator input).
    Atomic replace via a temp file in the destination dir, so a half-written
    root-owned script is never schedulable. chown to root:root is best-effort
    (needs privilege); the 0755 mode is always enforced.
    """
    jobs_dir = Path(jobs_dir)
    script = _repo_script(checkout, name)
    if script is None:
        return False, "source repo has no {}/{} script".format(REPO_SCRIPTS_SUBDIR, name)
    jobs_dir.mkdir(parents=True, exist_ok=True)
    dest = jobs_dir / name
    fd, tmp_name = tempfile.mkstemp(prefix=".{}.".format(name), dir=str(jobs_dir))
    os.close(fd)
    tmp_path = Path(tmp_name)
    try:
        shutil.copyfile(str(script), str(tmp_path))
        os.chmod(str(tmp_path), 0o755)
        try:
            os.chown(str(tmp_path), 0, 0)  # root:root; needs privilege
        except (PermissionError, OSError):
            pass  # unprivileged/sim run: mode is still enforced above
        os.replace(str(tmp_path), str(dest))
    finally:
        if tmp_path.exists():
            tmp_path.unlink()
    return True, "materialized {}/{}".format(REPO_SCRIPTS_SUBDIR, name)


# ---------------------------------------------------------------------------
# Managed crontab block rendering + atomic rewrite
# ---------------------------------------------------------------------------


def render_managed_block(plan_desired: Dict[str, str]) -> List[str]:
    """Render the managed block's lines (markers + one line per scheduled job).

    An empty ``desired`` map yields JUST the two markers (a dormant, empty block).
    """
    lines = [BLOCK_BEGIN]
    for name in sorted(plan_desired):
        lines.append(plan_desired[name])
    lines.append(BLOCK_END)
    return lines


def _splice_block(crontab_text: str, block_lines: List[str]) -> str:
    """Replace the delimited block in ``crontab_text`` (create it if absent).

    ONLY the region between ``BLOCK_BEGIN`` and ``BLOCK_END`` is rewritten. Every
    other line — DSM's ``synoschedtask --run id=N`` entries and the
    ``SHELL``/``PATH``/``MAILTO`` header — is preserved verbatim and in order.
    """
    original_lines = crontab_text.splitlines()
    out: List[str] = []
    i = 0
    replaced = False
    n = len(original_lines)
    while i < n:
        if original_lines[i].strip() == BLOCK_BEGIN:
            j = i + 1
            while j < n and original_lines[j].strip() != BLOCK_END:
                j += 1
            out.extend(block_lines)
            replaced = True
            i = j + 1  # skip the old END marker too
            continue
        out.append(original_lines[i])
        i += 1

    if not replaced:
        if out and out[-1].strip() != "":
            out.append("")
        out.extend(block_lines)

    return "\n".join(out) + "\n"


def read_crontab(path: Path = CRONTAB_PATH) -> str:
    """Read /etc/crontab (empty string if it does not exist yet)."""
    try:
        return path.read_text()
    except FileNotFoundError:
        return ""


def write_crontab_atomic(new_text: str, path: Path = CRONTAB_PATH) -> None:
    """Atomically replace /etc/crontab, preserving its mode (default 0644)."""
    path = Path(path)
    try:
        mode = stat.S_IMODE(path.stat().st_mode)
    except FileNotFoundError:
        mode = 0o644
    fd, tmp_name = tempfile.mkstemp(prefix=".crontab.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w") as f:
            f.write(new_text)
        os.chmod(tmp_name, mode)
        os.replace(tmp_name, str(path))
    finally:
        if Path(tmp_name).exists():
            Path(tmp_name).unlink()


# ---------------------------------------------------------------------------
# Reconcile: compute plan (read-only), apply (local), sync (fetch + apply)
# ---------------------------------------------------------------------------


def compute_plan(syrvis_home: Path) -> Dict[str, Any]:
    """Load jobs.d + the current managed block and build the reconcile plan.

    Read-only. Re-validates each scheduled job's cron spec (defense in depth).
    """
    syrvis_home = Path(syrvis_home)
    jobs_dir = paths.get_jobs_script_dir(syrvis_home)
    declarations, invalid = jobs_d.load_job_declarations(syrvis_home)
    current = jobs_d.parse_managed_block(read_crontab())
    plan = jobs_d.build_jobs_reconcile_plan(declarations, current, jobs_dir)
    for _name, line in plan["desired"].items():
        jobs_d.validate_cron_spec(" ".join(line.split()[:5]))
    plan["invalid"] = invalid
    plan["source"] = get_configured_source(syrvis_home)
    plan["scripts"] = _script_integrity(declarations, jobs_dir)
    plan["confs"] = _conf_integrity(declarations, syrvis_home / "config")
    return plan


def _conf_integrity(declarations: Dict[str, Any], config_dir: Path) -> Dict[str, Any]:
    """Per-job installed-CONF state: presence + size. NEVER content, never a hash.

    The exact twin of :func:`_script_integrity`, for the THIRD artifact a job
    needs. ``schedule sync`` installs the declaration and the script; a separate
    operator step (``config set``) installs ``config/<name>.conf`` — the ntfy
    slug, the Healthchecks ping URL, an API token, a generated list. Nothing
    reported whether that step had ever run, and because a conf-consuming job
    treats a missing conf as ``exit 0`` plus a stderr line (correctly — a cron
    job must never page), a confless job is indistinguishable from a healthy one
    on every other plane: declared, scheduled, script present and hash-clean,
    exit 0, silent.

    NO sha256, unlike the script twin, and the asymmetry is deliberate: a job
    SCRIPT is public code whose digest is the whole point, while a conf holds
    live credentials and a digest of a short low-entropy value is a
    brute-forceable oracle over the seam. Presence and size answer "did the
    install step run" and nothing more, which is the question.

    Unreadable is reported as an ``error`` string rather than as absent —
    "never installed" and "installed but the operator cannot stat it" are
    different problems with different fixes.
    """
    out: Dict[str, Any] = {}
    for name in sorted(declarations):
        conf = config_dir / "{}.conf".format(name)
        entry: Dict[str, Any] = {"present": conf.is_file()}
        if entry["present"]:
            try:
                entry["bytes"] = conf.stat().st_size
            except OSError as e:
                entry["error"] = str(e)
        out[name] = entry
    return out


# ---------------------------------------------------------------------------
# DSM Task Scheduler census (READ-ONLY — SyrvisCore never writes DSM's scheduler)
# ---------------------------------------------------------------------------

# DSM prints one `Key: [value]` pair per line. The key charset is deliberately
# generous (DSM has used "Task name", "Run command", "Task ID" and friends
# across versions) and the value is captured whole, brackets excluded.
_DSM_FIELD_RE = re.compile(r"^\s*([A-Za-z][A-Za-z0-9 _/()-]{0,40}):\s*\[(.*)\]\s*$")

# key (normalized) -> the canonical name this census reports it under. Anything
# unmatched is still kept, under its normalized key, in ``fields``.
_DSM_FIELD_ALIASES = {
    "id": "id",
    "task_id": "id",
    "task": "name",
    "name": "name",
    "task_name": "name",
    "owner": "owner",
    "user": "owner",
    "state": "state",
    "status": "state",
    "type": "type",
    "task_type": "type",
    "command": "command",
    "run_command": "command",
    "user_defined_script": "command",
    "schedule": "schedule",
}


def _normalize_dsm_key(key: str) -> str:
    return key.strip().lower().replace(" ", "_").replace("-", "_")


def parse_dsm_tasks(text: str) -> List[Dict[str, Any]]:
    """Parse ``synoschedtask --get`` output into rows. Pure; never raises.

    DSM's format has drifted across releases and is not documented, so this is
    deliberately shape-tolerant rather than clever: any ``Key: [value]`` line
    joins the record being built, a blank line or a REPEATED key starts the next
    one, and every field survives under its normalized name in ``fields`` even
    when it has no canonical alias. A format this parser cannot read at all
    yields zero rows — and the caller reports that as ``parsed: 0`` beside a
    non-empty output, which is a visible "I could not read it", not a silent
    "there are no tasks".
    """
    tasks: List[Dict[str, Any]] = []
    current: Dict[str, Any] = {}

    def flush():
        if current:
            row: Dict[str, Any] = {"fields": dict(current)}
            for raw_key, value in current.items():
                canonical = _DSM_FIELD_ALIASES.get(raw_key)
                if canonical and canonical not in row:
                    row[canonical] = value
            tasks.append(row)
        current.clear()

    for line in (text or "").splitlines():
        if not line.strip():
            flush()
            continue
        match = _DSM_FIELD_RE.match(line)
        if not match:
            continue
        key = _normalize_dsm_key(match.group(1))
        if key in current:  # a repeated key means a new record began
            flush()
        current[key] = match.group(2).strip()[:MAX_DSM_FIELD_CHARS]
        if len(tasks) >= MAX_DSM_TASKS:
            break
    flush()
    return tasks[:MAX_DSM_TASKS]


def dsm_task_census(binary: Optional[Path] = None, run=None) -> Dict[str, Any]:
    """Enumerate DSM's OWN Task Scheduler entries (``synoschedtask --get``).

    The blind spot design/20 was written about and never closed (ops:F20): a DSM
    task pointing at a path outside SyrvisCore's managed crontab block is
    invisible to every seam verb — ``schedule list`` reports only the delimited
    block it owns — so "the repo declares jobs/login-alert, the NAS ran bin/…,
    and nothing flagged it" had no automated detector. This is the enumerator.

    STRICTLY READ-ONLY, and a census rather than a manager: SyrvisCore does not
    create, edit or delete DSM tasks, and nothing here ever will. It reports what
    else is scheduled on this box so a human (or a repo-side check) can compare
    it against what the repo believes.

    Never raises. Every failure — no DSM, no permission, a timeout, an
    unparseable format — comes back as data, because "the census could not run"
    and "there are no other tasks" must never look the same.
    """
    path = Path(binary) if binary is not None else SYNOSCHEDTASK_PATH
    out: Dict[str, Any] = {
        "tool": str(path),
        "available": False,
        "ok": False,
        "error": None,
        "count": 0,
        "tasks": [],
    }
    runner = run or _run_synoschedtask
    if run is None and not path.exists():
        out["error"] = (
            "{} is not present — this is not a DSM host (or the tool moved); "
            "no DSM Task Scheduler census is possible here".format(path)
        )
        return out
    out["available"] = True
    try:
        stdout, stderr, rc = runner(path)
    except Exception as exc:  # noqa: BLE001 - a census never fails its caller
        out["error"] = "could not run {}: {}".format(path, exc)
        return out
    if rc != 0:
        # The overwhelmingly likely rc!=0 is EACCES: synoschedtask is root-only,
        # which is why the seam row runs it under sudo. Say so rather than
        # printing a bare exit code.
        out["error"] = "{} exited {} ({})".format(
            path, rc, (stderr or stdout or "no output").strip()[:MAX_DSM_FIELD_CHARS]
        )
        return out
    tasks = parse_dsm_tasks(stdout)
    out["ok"] = True
    out["tasks"] = tasks
    out["count"] = len(tasks)
    if not tasks and (stdout or "").strip():
        out["ok"] = False
        out["error"] = (
            "{} produced output this parser did not recognise ({} line(s)) — the "
            "DSM format changed; treat the census as UNKNOWN, not empty".format(
                path, len((stdout or "").splitlines())
            )
        )
    return out


def _run_synoschedtask(path: Path) -> Tuple[str, str, int]:
    proc = subprocess.run(
        [str(path), "--get"],
        capture_output=True,
        text=True,
        timeout=DSM_TASK_TIMEOUT_S,
    )
    return proc.stdout or "", proc.stderr or "", proc.returncode


def _script_integrity(declarations: Dict[str, Any], jobs_dir: Path) -> Dict[str, Any]:
    """Per-job installed-script state: presence + sha256.

    Lets a deployment verify over the operator seam (``schedule list --json``)
    that the root-owned ``jobs/<name>`` scripts match its source — without a
    break-glass login just to hash files.
    """
    out: Dict[str, Any] = {}
    for name in sorted(declarations):
        script = jobs_dir / name
        entry: Dict[str, Any] = {"present": script.is_file()}
        if entry["present"]:
            try:
                entry["sha256"] = hashlib.sha256(script.read_bytes()).hexdigest()
            except OSError as e:
                entry["error"] = str(e)
        out[name] = entry
    return out


def apply_schedule(syrvis_home: Path) -> Dict[str, Any]:
    """LOCAL reconcile: rewrite the managed /etc/crontab block from config/jobs.d.

    No fetch — the scripts are already on disk from the last ``sync``. This is the
    self-heal path (boot hook / verify --fix) after DSM regenerates /etc/crontab.
    A declared+enabled job whose ``jobs/<name>`` script is MISSING on disk is
    dropped from the block for this run (never scheduled with a missing script).
    """
    syrvis_home = Path(syrvis_home)
    jobs_dir = paths.get_jobs_script_dir(syrvis_home)
    declarations, invalid = jobs_d.load_job_declarations(syrvis_home)

    desired: Dict[str, str] = {}
    skipped: List[Dict[str, str]] = []
    for name, job in declarations.items():
        if not job.enabled:
            continue
        if not (jobs_dir / name).is_file():
            skipped.append(
                {"name": name, "reason": "jobs/{} not present — run schedule sync".format(name)}
            )
            continue
        desired[name] = job.crontab_line(jobs_dir)

    current = jobs_d.parse_managed_block(read_crontab())
    plan = jobs_d.build_jobs_reconcile_plan(
        {n: declarations[n] for n in desired}, current, jobs_dir
    )
    plan["invalid"] = invalid

    new_text = _splice_block(read_crontab(), render_managed_block(desired))
    write_crontab_atomic(new_text)

    return {
        "ok": not invalid and not skipped,
        "applied": True,
        "plan": plan,
        "invalid": invalid,
        "skipped": skipped,
        "scheduled": sorted(desired),
    }


def sync_from_source(syrvis_home: Path) -> Dict[str, Any]:
    """Clone the ONE root-configured source, install its jobs, then reconcile.

    Steps (privileged):
    1. Read ``config/jobs.source`` (root-owned). None ⇒ dormant no-op.
    2. Clone it (hardened). For each ``jobs.d/*.yaml`` in the repo: re-validate +
       copy into ``config/jobs.d/`` (removing local declarations the source no
       longer provides — the source is authoritative).
    3. Materialize each declared+enabled job's ``jobs/<name>`` → ``<home>/jobs/``
       (root:root 0755).
    4. LOCAL reconcile the managed crontab block.

    Only the ``jobs.d/`` + ``jobs/`` subtrees of the trusted repo are read.
    """
    syrvis_home = Path(syrvis_home)
    source = get_configured_source(syrvis_home)
    if not source:
        return {
            "ok": True,
            "applied": False,
            "source": None,
            "synced": [],
            "message": "no config/jobs.source configured — scheduled jobs dormant",
        }

    ok, msg, checkout = _clone_configured_source(source)
    if not ok or checkout is None:
        return {"ok": False, "applied": False, "source": source, "error": msg}
    tmp_root = checkout.parent
    try:
        repo_decls = _repo_declarations(checkout)
        synced: List[Dict[str, Any]] = []
        # Install (validate + copy) every declaration the source provides.
        installed_names = set()
        for name, yaml_path in repo_decls.items():
            try:
                _install_declaration(yaml_path, name, syrvis_home)
                installed_names.add(name)
            except Exception as exc:  # noqa: BLE001
                synced.append({"name": name, "ok": False, "message": "declaration: {}".format(exc)})

        # The source is authoritative: drop local jobs.d declarations it no longer
        # provides (so a removed job disappears from the block on next reconcile).
        decls_dir = jobs_d.get_jobs_declarations_dir(syrvis_home)
        if decls_dir.is_dir():
            for existing in decls_dir.glob("*.yaml"):
                if existing.stem not in repo_decls:
                    existing.unlink()

        # Materialize scripts for the freshly installed, enabled declarations.
        declarations, _invalid = jobs_d.load_job_declarations(syrvis_home)
        jobs_dir = paths.get_jobs_script_dir(syrvis_home)
        for name in sorted(installed_names):
            job = declarations.get(name)
            if job is None or not job.enabled:
                continue
            m_ok, m_msg = materialize_job_script(checkout, name, jobs_dir)
            synced.append({"name": name, "ok": m_ok, "message": m_msg})
    finally:
        shutil.rmtree(tmp_root, ignore_errors=True)

    applied = apply_schedule(syrvis_home)
    return {
        "ok": applied["ok"] and all(s["ok"] for s in synced),
        "applied": True,
        "source": source,
        "synced": synced,
        "reconcile": applied,
    }
