"""Scheduled-jobs library — the PRIVILEGED half of the jobs.d capability.

Trust model (design/12 + home-tech design/66): the git source of job scripts +
declarations is ROOT-CONFIGURED in ``<home>/config/jobs.source`` (a single repo
URL the operator cannot write). There is NO per-declaration source — so a
compromised operator can neither point a job at an arbitrary repo (which would be
arbitrary root code via the fetched script) nor supply a command
(derive-not-declare).

⚠ THE CLAIM THAT USED TO END THIS PARAGRAPH — "it can at most re-apply the
already-synced, root-vetted set" — WAS FALSE, and was false for as long as this
module has existed. Nothing vetted anything: the clone took whatever the remote's
DEFAULT BRANCH HEAD was, and ``materialize_job_script`` wrote those bytes
root:root 0755 without looking at them. So "root-vetted" meant "whoever can push
to that repo", and home-tech's own drift detector hashed the NAS copy against the
same repo — a push made both sides the attacker's and the check went green.

What makes the set vetted NOW (design/66):

- ``<home>/config/jobs.pin`` (root-owned) records ONE reviewed commit and the
  sha256 of that commit's ``jobs/MANIFEST.sha256``. A sync with no argv
  materializes THAT commit and no other — a fresh push cannot move it, which is
  what makes ``schedule sync`` safe to hand an operator as a repair.
- the clone is pinned and PROVEN pinned (``git rev-parse HEAD`` equality) before
  a single byte is copied;
- every script is checked against its manifest digest at copy time. No row, or a
  different digest, is a refusal — never a warning.
- advancing the pin is explicit and argument-bearing:
  ``schedule sync --to <40-hex> --manifest sha256:<64-hex>``. Both values are
  re-verified against the fetched tree, and ``changed_scripts`` names every
  root-run script the advance would alter.

NOTE the asymmetry, deliberately: ``apply_schedule`` does NOT consult the pin.
It performs no fetch — the scripts are already on disk — so the boot hook, the
``*/5`` seam-selfheal and ``verify --fix`` self-heal exactly as before, pin or no
pin. Only the FETCHING verb is gated.

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
PIN_CONFIG_NAME = "jobs.pin"  # <home>/config/jobs.pin — ROOT-owned (design/66)
MANIFEST_FILENAME = "MANIFEST.sha256"  # <repo>/jobs/MANIFEST.sha256

# A full commit sha (never abbreviated: an abbreviation is ambiguous by
# construction and a supply-chain pin may not be ambiguous), and the manifest
# digest as it is written in the pin file.
_REV_RE = re.compile(r"^[0-9a-f]{40}$")
_MANIFEST_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")

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


def _git(args: List[str], timeout: int = 180, env: Optional[Dict[str, str]] = None):
    """Run git with the hardened env. Never raises for a non-zero exit."""
    return subprocess.run(["git"] + args, capture_output=True, text=True, timeout=timeout, env=env)


def _git_env() -> Dict[str, str]:
    env = dict(os.environ)
    # `file` is allowed alongside the network transports because the source is
    # ROOT-configured (never operator input); it is required so a local-repo
    # fixture (CI) — and a legitimate local mirror — can be cloned.
    env["GIT_ALLOW_PROTOCOL"] = "https:git:ssh:file"
    env["GIT_TERMINAL_PROMPT"] = "0"
    return env


def _clone_configured_source(git_url: str, rev: str) -> Tuple[bool, str, Optional[Path]]:
    """Clone the root-configured source AT AN EXACT COMMIT, and PROVE it landed there.

    Two strategies, in order:

    1. ``git init`` + ``git fetch --depth 1 <url> <rev>`` + ``checkout --detach
       FETCH_HEAD`` — one object, no branch, no default-branch trust. Works
       against any host that serves SHA-in-want (GitHub does).
    2. a full clone + ``checkout --detach <rev>`` — the fallback for a host or a
       git too old for (1). home-tech is small; this runs only on a pin advance
       or a repair, never on a schedule.

    Whichever path ran, the LAST thing this does is ``git rev-parse HEAD`` and
    compare it to ``rev``. If the checkout is not EXACTLY the pinned commit the
    tree is discarded and nothing downstream ever sees it. That equality is the
    whole gate — every digest check after it is defence in depth, not the anchor.

    The caller owns the temp dir on success.
    """
    if not _is_git_url(git_url):
        return (
            False,
            "configured jobs.source is not a supported git URL: {!r}".format(git_url),
            None,
        )
    if not _REV_RE.match(rev or ""):
        return False, "jobs pin rev is not a full 40-hex commit sha: {!r}".format(rev), None

    env = _git_env()
    temp_dir = tempfile.mkdtemp(prefix="syrvis-jobs-")
    temp_path = Path(temp_dir) / "repo"

    def _fail(msg: str) -> Tuple[bool, str, Optional[Path]]:
        shutil.rmtree(temp_dir, ignore_errors=True)
        return False, msg, None

    try:
        temp_path.mkdir(parents=True, exist_ok=True)
        r = _git(["init", "--quiet", str(temp_path)], env=env, timeout=60)
        if r.returncode != 0:
            return _fail("git init failed: {}".format(r.stderr.strip()))
        r = _git(
            ["-C", str(temp_path), "fetch", "--depth", "1", git_url, rev], env=env, timeout=180
        )
        if r.returncode == 0:
            r = _git(
                ["-C", str(temp_path), "checkout", "--quiet", "--detach", "FETCH_HEAD"],
                env=env,
                timeout=120,
            )
            if r.returncode != 0:
                return _fail("checkout of the pinned commit failed: {}".format(r.stderr.strip()))
        else:
            # The remote refused SHA-in-want (or git is too old to ask). Fall
            # back to a FULL clone — never a --depth 1 of the default branch,
            # which is the exact unpinned fetch this function exists to remove.
            shutil.rmtree(str(temp_path), ignore_errors=True)
            r = _git(["clone", "--", git_url, str(temp_path)], env=env, timeout=300)
            if r.returncode != 0:
                return _fail("failed to clone jobs.source: {}".format(r.stderr.strip()))
            r = _git(
                ["-C", str(temp_path), "checkout", "--quiet", "--detach", rev],
                env=env,
                timeout=120,
            )
            if r.returncode != 0:
                return _fail(
                    "pinned commit {} is not present in jobs.source: {}".format(
                        rev[:12], r.stderr.strip()
                    )
                )
    except subprocess.TimeoutExpired:
        return _fail("git timed out fetching the pinned commit from jobs.source")
    except FileNotFoundError:
        return _fail("git is not installed")

    # THE GATE. Everything below this line trusts that HEAD == the pin.
    try:
        head = _git(["-C", str(temp_path), "rev-parse", "HEAD"], env=env, timeout=60)
    except subprocess.TimeoutExpired:
        return _fail("git rev-parse timed out verifying the pinned checkout")
    if head.returncode != 0:
        return _fail("could not verify the checked-out commit: {}".format(head.stderr.strip()))
    got = (head.stdout or "").strip()
    if got != rev:
        return _fail(
            "REFUSING to materialize: the checkout is at {} but the pin names {} "
            "— nothing was installed".format(got[:12] or "(nothing)", rev[:12])
        )
    return True, "cloned at {}".format(rev[:12]), temp_path


def get_pin_config_path(syrvis_home: Path) -> Path:
    return Path(syrvis_home) / "config" / PIN_CONFIG_NAME


def parse_pin(text: str) -> Optional[Dict[str, str]]:
    """Parse jobs.pin -> {"rev", "manifest"}, or None when absent/malformed.

    FIRST non-comment, non-blank line, exactly two whitespace-separated tokens.
    The first-line rule is byte-identical to :func:`get_configured_source` on
    purpose: home-tech's verify-all mirrors both parsers, and a check that reads
    a different line from the one the platform reads is fiction (that exact bug
    was caught in verify-all's jobs.source reader on 2026-08-10).

    Malformed is None, i.e. FAIL-CLOSED: a sync with no usable pin refuses.
    """
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if len(parts) != 2:
            return None
        if not _REV_RE.match(parts[0]) or not _MANIFEST_DIGEST_RE.match(parts[1]):
            return None
        return {"rev": parts[0], "manifest": parts[1]}
    return None


def get_configured_pin(syrvis_home: Path) -> Optional[Dict[str, str]]:
    """The reviewed pin, or None (FAIL-CLOSED — an unpinned sync is refused)."""
    try:
        text = get_pin_config_path(syrvis_home).read_text()
    except (FileNotFoundError, PermissionError, IsADirectoryError, NotADirectoryError):
        return None
    return parse_pin(text)


def write_pin(syrvis_home: Path, rev: str, manifest_digest: str) -> Path:
    """Atomically record the reviewed pin (root:root 0644).

    Called ONLY from a successful ``sync_from_source(..., to_rev=, to_manifest=)``
    — and only AFTER the materialize + reconcile succeeded, so a half-applied
    sync leaves the OLD pin standing. That direction is the safe one: the next
    bare sync then restores the previously-reviewed bytes, and ``nas.jobs`` FAILs
    loudly in the meantime instead of quietly blessing a partial write.
    """
    path = get_pin_config_path(Path(syrvis_home))
    path.parent.mkdir(parents=True, exist_ok=True)
    body = (
        "# jobs.pin — the REVIEWED commit of config/jobs.source that `schedule sync`\n"
        "# materializes. Written ONLY by `syrvis schedule sync --to <rev> --manifest <sha256>`.\n"
        "# <40-hex commit sha> sha256:<64-hex digest of that commit's jobs/MANIFEST.sha256>\n"
        "{} {}\n".format(rev, manifest_digest)
    )
    fd, tmp_name = tempfile.mkstemp(prefix=".jobs.pin.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w") as f:
            f.write(body)
        os.chmod(tmp_name, 0o644)
        try:
            os.chown(tmp_name, 0, 0)  # root:root; needs privilege
        except (PermissionError, OSError):
            pass  # unprivileged/sim run: mode is still enforced above
        os.replace(tmp_name, str(path))
    finally:
        if Path(tmp_name).exists():
            Path(tmp_name).unlink()
    return path


def _sha256_file(path: Path) -> str:
    """THE digest function — one implementation, both sides of every comparison.

    :func:`_script_integrity` (what the seam reports and home-tech's `nas.jobs`
    grades) and :func:`materialize_job_script` (what actually gets installed)
    must agree by construction, not by two people writing the same line twice.
    """
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def parse_manifest(text: str) -> Dict[str, str]:
    """Parse ``jobs/MANIFEST.sha256`` -> {job name: sha256 hex}.

    The format is plain ``sha256sum`` output taken from the repo root, so
    ``sha256sum -c jobs/MANIFEST.sha256`` verifies it with no bespoke tool::

        <64 hex>  jobs/<name>

    A manifest may describe NOTHING but ``jobs/<name>``. Any other path, a
    duplicate name, a malformed digest, an illegal job name, or a line that does
    not split into exactly two tokens is a HARD REFUSAL — not a skipped row. A
    parser that silently ignores rows it does not understand is a parser an
    attacker writes rows for.
    """
    out: Dict[str, str] = {}
    for lineno, raw in enumerate((text or "").splitlines(), start=1):
        line = raw.strip()
        if not line:
            continue
        parts = line.split()
        if len(parts) != 2:
            raise ScheduleError(
                "jobs manifest line {}: expected '<sha256>  {}/<name>'".format(
                    lineno, REPO_SCRIPTS_SUBDIR
                )
            )
        digest, rel = parts
        if not re.match(r"^[0-9a-f]{64}$", digest):
            raise ScheduleError("jobs manifest line {}: malformed sha256".format(lineno))
        prefix = REPO_SCRIPTS_SUBDIR + "/"
        if not rel.startswith(prefix):
            raise ScheduleError(
                "jobs manifest line {}: {!r} is outside {}/ — a manifest may only "
                "describe job scripts".format(lineno, rel, REPO_SCRIPTS_SUBDIR)
            )
        name = rel[len(prefix) :]
        if not re.match(jobs_d._NAME_RE_STR, name):
            raise ScheduleError(
                "jobs manifest line {}: {!r} is not a legal job name".format(lineno, name)
            )
        if name in out:
            raise ScheduleError(
                "jobs manifest line {}: duplicate entry for {!r}".format(lineno, name)
            )
        out[name] = digest
    return out


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


def materialize_job_script(
    checkout: Path, name: str, jobs_dir: Path, expected_sha256: Optional[str] = None
) -> Tuple[bool, str]:
    """Copy ``jobs/<name>`` to ``<jobs_dir>/<name>`` (root:root 0755) IF it matches.

    ``checkout`` is the already-cloned, REV-VERIFIED source. ``expected_sha256``
    is that script's row in ``jobs/MANIFEST.sha256`` at the pinned commit.

    ⚠ ``expected_sha256=None`` is a REFUSAL, not a bypass. The default is None
    only so the failure mode of a caller that forgets to pass it is "nothing is
    installed", never "the old unchecked behaviour". This is the line that makes
    the whole scheme load-bearing: before design/66 these bytes became a
    root:root 0755 executable with no inspection whatsoever.

    Atomic replace via a temp file in the destination dir, so a half-written
    root-owned script is never schedulable. chown to root:root is best-effort
    (needs privilege); the 0755 mode is always enforced.
    """
    jobs_dir = Path(jobs_dir)
    script = _repo_script(checkout, name)
    if script is None:
        return False, "source repo has no {}/{} script".format(REPO_SCRIPTS_SUBDIR, name)
    if not expected_sha256:
        return False, (
            "{}/{} has no row in {}/{} at the pinned commit — refusing to install an "
            "unmanifested root script".format(
                REPO_SCRIPTS_SUBDIR, name, REPO_SCRIPTS_SUBDIR, MANIFEST_FILENAME
            )
        )
    actual = _sha256_file(script)
    if actual != expected_sha256:
        return False, (
            "{}/{} does not match the pinned manifest (want {}…, got {}…) — refusing".format(
                REPO_SCRIPTS_SUBDIR, name, expected_sha256[:12], actual[:12]
            )
        )
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
    # design/66: the reviewed pin goes on the wire beside the source URL, so
    # home-tech's `nas.jobs` can resolve its reference digests from the PINNED
    # commit instead of from its own (attacker-writable) working tree, and
    # `nas.jobs-pin` can assert the live pin against config/jobs.pin. None here
    # is meaningful and must never be smoothed over: it means UNPINNED.
    plan["pin"] = get_configured_pin(syrvis_home)
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
                entry["sha256"] = _sha256_file(script)
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


def sync_from_source(
    syrvis_home: Path,
    to_rev: Optional[str] = None,
    to_manifest: Optional[str] = None,
) -> Dict[str, Any]:
    """Clone the root-configured source AT THE REVIEWED PIN, install, reconcile.

    TWO MODES, and the difference is the whole security story:

    * **bare** (no argv) — materialize ``config/jobs.pin``'s commit. It cannot
      pick up anything pushed since the pin was set, which is what makes this
      safe to print as a remediation, safe on the boot path, and safe for an
      agent to run. Absent/malformed pin ⇒ REFUSE (fail-closed); an unpinned
      sync would install the default-branch HEAD as root cron.
    * **advancing** (``--to`` + ``--manifest``) — the deliberate review act. Both
      values are re-verified against the fetched tree (``rev-parse HEAD`` for the
      commit, a sha256 of ``jobs/MANIFEST.sha256`` for the manifest), and
      ``changed_scripts`` names every root-run script this advance would alter
      BEFORE anything is written. The pin file is updated LAST, only on success.

    Steps:
    1. Read ``config/jobs.source`` (root-owned). None ⇒ dormant no-op.
    2. Resolve the pin (argv, else the file). None ⇒ refuse.
    3. Clone AT the pin and prove ``HEAD == rev``.
    4. Verify + parse ``jobs/MANIFEST.sha256`` against the pin's digest.
    5. Install each ``jobs.d/*.yaml`` (re-validated; the source is authoritative).
    6. Materialize each declared+enabled ``jobs/<name>`` ONLY if it matches its
       manifest row (root:root 0755).
    7. LOCAL reconcile the managed crontab block; then, if advancing, write the pin.

    Only the ``jobs.d/`` + ``jobs/`` subtrees of the pinned tree are read.
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

    advancing = to_rev is not None or to_manifest is not None
    if advancing:
        if not (isinstance(to_rev, str) and _REV_RE.match(to_rev or "")):
            return {
                "ok": False,
                "applied": False,
                "source": source,
                "error": "--to must be a FULL 40-hex commit sha (an abbreviation is ambiguous)",
            }
        if not (isinstance(to_manifest, str) and _MANIFEST_DIGEST_RE.match(to_manifest or "")):
            return {
                "ok": False,
                "applied": False,
                "source": source,
                "error": "--manifest must be sha256:<64 hex> (the digest of jobs/{})".format(
                    MANIFEST_FILENAME
                ),
            }
        pin = {"rev": to_rev, "manifest": to_manifest}
    else:
        pin = get_configured_pin(syrvis_home)
        if pin is None:
            return {
                "ok": False,
                "applied": False,
                "source": source,
                "pin": None,
                "error": (
                    "no reviewed pin: <home>/config/{} is absent or malformed, so there is no "
                    "commit this sync may materialize. An UNPINNED sync would install whatever "
                    "the default branch happens to point at, as root cron. Establish the pin "
                    "with `schedule sync --to <40-hex commit> --manifest sha256:<64 hex>` "
                    "(home-tech wiki/runbooks/jobs-pin-cutover.md)".format(PIN_CONFIG_NAME)
                ),
            }

    ok, msg, checkout = _clone_configured_source(source, pin["rev"])
    if not ok or checkout is None:
        return {"ok": False, "applied": False, "source": source, "pin": pin, "error": msg}
    tmp_root = checkout.parent
    changed: List[str] = []
    try:
        manifest_path = checkout / REPO_SCRIPTS_SUBDIR / MANIFEST_FILENAME
        if not manifest_path.is_file():
            return {
                "ok": False,
                "applied": False,
                "source": source,
                "pin": pin,
                "error": "commit {} carries no {}/{}".format(
                    pin["rev"][:12], REPO_SCRIPTS_SUBDIR, MANIFEST_FILENAME
                ),
            }
        actual_manifest = "sha256:" + _sha256_file(manifest_path)
        if actual_manifest != pin["manifest"]:
            return {
                "ok": False,
                "applied": False,
                "source": source,
                "pin": pin,
                "error": (
                    "manifest digest mismatch at {}: the pin names {} but the tree carries {} "
                    "— the ref was rewritten under the pin, or the value was mistyped. "
                    "NOTHING was installed".format(
                        pin["rev"][:12], pin["manifest"], actual_manifest
                    )
                ),
            }
        try:
            expected = parse_manifest(manifest_path.read_text())
        except ScheduleError as exc:
            return {
                "ok": False,
                "applied": False,
                "source": source,
                "pin": pin,
                "error": str(exc),
            }

        # THE REVIEW SURFACE. Which root-run scripts this sync would change,
        # named, computed BEFORE anything is written — because after the writes
        # the evidence is gone. A pin advance whose changed set surprises you is
        # the signal this whole design exists to produce.
        jobs_dir = paths.get_jobs_script_dir(syrvis_home)
        for name in sorted(expected):
            installed = jobs_dir / name
            if not installed.is_file() or _sha256_file(installed) != expected[name]:
                changed.append(name)

        repo_decls = _repo_declarations(checkout)
        synced: List[Dict[str, Any]] = []
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

        declarations, _invalid = jobs_d.load_job_declarations(syrvis_home)
        for name in sorted(installed_names):
            job = declarations.get(name)
            if job is None or not job.enabled:
                continue
            m_ok, m_msg = materialize_job_script(
                checkout, name, jobs_dir, expected_sha256=expected.get(name)
            )
            synced.append({"name": name, "ok": m_ok, "message": m_msg})
    finally:
        shutil.rmtree(tmp_root, ignore_errors=True)

    applied = apply_schedule(syrvis_home)
    ok_all = bool(applied["ok"]) and all(s["ok"] for s in synced)
    pin_written = False
    if advancing and ok_all:
        write_pin(syrvis_home, pin["rev"], pin["manifest"])
        pin_written = True
    return {
        "ok": ok_all,
        "applied": True,
        "source": source,
        "pin": pin,
        "pin_written": pin_written,
        "changed_scripts": changed,
        "synced": synced,
        "reconcile": applied,
    }
