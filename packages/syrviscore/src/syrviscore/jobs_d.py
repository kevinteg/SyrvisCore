"""Declarative scheduled jobs: the ``config/jobs.d/`` reconciler.

This is the *load + plan* half of the OPTIONAL "scheduled jobs" capability (the
privileged materialize/crontab-rewrite half lives in ``schedule.py``). It mirrors
``services_d.py``: one validated declaration per file, filename == ``name``,
per-file failure isolation.

Security model (the part that must be right — see design/12):

- **Derive-not-declare.** A declaration's schema is exactly
  ``{schedule, enabled}``. Both ``command`` AND ``source`` are REJECTED keys:
  the command a job schedules is DERIVED as ``<jobs_dir>/<name>`` in
  ``schedule.py`` (the declaration can never influence it), and the git source of
  the script is ROOT-configured in ``config/jobs.source`` — never per-declaration.
  So a compromised operator (who can only write ``jobs.d/``) can at most schedule
  an *already-vetted* root-owned script at a time of its choosing — it can neither
  supply a command nor point a job at an arbitrary repo (arbitrary root code).
- **Cron spec is validated in Python** (5 whitespace-separated fields, each field
  drawn from ``[0-9*/,-]`` only). The spec lives ONLY in the YAML; it is never an
  MCP/shim argv token (the shim char-allowlist blocks ``*`` ``,`` ``?``).

The *schedule* travels as file content (jobs.d), never over the enumerated command
seam; the *source* is the single root-owned ``config/jobs.source``.
"""

from pathlib import Path
from typing import Any, Dict, List, Tuple

import yaml

from .errors import SyrvisError

DECLARATIONS_DIRNAME = "jobs.d"

# A job name: same charset as a service name (lowercase, digits, _ and -). This
# must also be a safe single path component (no '/', '..', leading '-') because
# it is used to derive the script path <jobs_dir>/<name> and the crontab command.
_NAME_RE_STR = r"^[a-z0-9][a-z0-9_-]{0,63}$"

# The 5 cron fields (minute hour day-of-month month day-of-week). Each field may
# only contain digits, '*', '/', ',', '-'. This is deliberately narrower than
# full crontab syntax (no names like 'MON', no '@reboot', no '%'): it is a
# security allowlist, not a feature-complete parser.
_CRON_FIELD_CHARS = set("0123456789*/,-")
_CRON_FIELD_COUNT = 5

# The only keys a declaration may carry. `command` AND `source` are intentionally
# NOT here — both are hard rejections:
#  - `command`: derive-not-declare (the command is jobs/<name>, never declared).
#  - `source`:  the script source is ROOT-configured (config/jobs.source), never
#    per-declaration — an operator must not be able to point a job at an arbitrary
#    git repo (that would be arbitrary root code via the fetched script).
_ALLOWED_KEYS = frozenset({"schedule", "enabled"})


class JobDeclarationError(SyrvisError):
    """A per-file job declaration validation failure (isolated, non-fatal)."""

    code = "job_declaration_invalid"


class JobDefinition:
    """A validated ``jobs.d/<name>.yaml`` declaration.

    Deliberately minimal — it carries NO command. ``derived_command(jobs_dir)``
    is the single source of the scheduled command, computed from the name.
    """

    __slots__ = ("name", "schedule", "enabled")

    def __init__(self, name: str, schedule: str, enabled: bool = True):
        self.name = name
        self.schedule = schedule
        self.enabled = enabled

    @classmethod
    def from_dict(cls, name: str, data: Dict[str, Any]) -> "JobDefinition":
        if not isinstance(data, dict):
            raise JobDeclarationError("declaration must be a mapping")

        # Derive-not-declare: refuse a `command` field outright, plus any other
        # unknown key (a typo like `cmd:` must not silently pass). This is the
        # load-bearing security check — reject, never ignore-and-continue.
        extra = set(data) - _ALLOWED_KEYS
        if "command" in extra:
            raise JobDeclarationError(
                "a job declaration may not contain a 'command' field: the command is "
                "derived as jobs/{} (derive-not-declare)".format(name)
            )
        if "source" in extra:
            raise JobDeclarationError(
                "a job declaration may not contain a 'source' field: the script source "
                "is root-configured (config/jobs.source), never per-declaration"
            )
        if extra:
            raise JobDeclarationError(
                "unknown key(s) {}: a job declaration accepts only {}".format(
                    ", ".join(sorted(extra)), ", ".join(sorted(_ALLOWED_KEYS))
                )
            )

        import re

        if not re.match(_NAME_RE_STR, name):
            raise JobDeclarationError(
                "invalid job name {!r}: must match {}".format(name, _NAME_RE_STR)
            )

        schedule = data.get("schedule")
        if not isinstance(schedule, str) or not schedule.strip():
            raise JobDeclarationError("'schedule' is required and must be a cron string")
        validate_cron_spec(schedule)  # raises JobDeclarationError on a bad spec

        enabled = data.get("enabled", True)
        if not isinstance(enabled, bool):
            raise JobDeclarationError("'enabled' must be a boolean")

        return cls(name=name, schedule=schedule.strip(), enabled=enabled)

    def derived_command(self, jobs_dir: Path) -> str:
        """The ONLY command this job may schedule: ``<jobs_dir>/<name>``.

        Derived purely from the (charset-validated) name — never from the
        declaration. This is the derive-not-declare invariant in code.
        """
        return str(Path(jobs_dir) / self.name)

    def crontab_line(self, jobs_dir: Path) -> str:
        """The root-installed crontab line for this job (schedule + root + cmd)."""
        return "{} root {}".format(self.schedule, self.derived_command(jobs_dir))


def validate_cron_spec(spec: str) -> str:
    """Validate a cron spec as exactly 5 safe-charset fields.

    Enforced in Python (the design's requirement) because the spec's ``*``/``,``
    would be rejected by the MCP shim char-allowlist — it must never travel as an
    argv token. Returns the normalized (single-space-joined) spec, or raises
    :class:`JobDeclarationError`.
    """
    if not isinstance(spec, str):
        raise JobDeclarationError("cron spec must be a string")
    fields = spec.split()
    if len(fields) != _CRON_FIELD_COUNT:
        raise JobDeclarationError(
            "cron spec must have exactly {} whitespace-separated fields (got {}): {!r}".format(
                _CRON_FIELD_COUNT, len(fields), spec
            )
        )
    for field in fields:
        bad = set(field) - _CRON_FIELD_CHARS
        if bad or not field:
            raise JobDeclarationError(
                "cron field {!r} contains disallowed characters; only [0-9*/,-] "
                "are permitted".format(field)
            )
    return " ".join(fields)


def get_jobs_declarations_dir(syrvis_home: Path) -> Path:
    return Path(syrvis_home) / "config" / DECLARATIONS_DIRNAME


def declaration_path(syrvis_home: Path, name: str) -> Path:
    return get_jobs_declarations_dir(syrvis_home) / "{}.yaml".format(name)


def load_job_declarations(
    syrvis_home: Path,
) -> Tuple[Dict[str, JobDefinition], List[Dict[str, str]]]:
    """Load every ``jobs.d/*.yaml`` with per-file failure isolation.

    Returns ``(valid, invalid)`` where ``valid`` maps name -> JobDefinition and
    ``invalid`` is a list of ``{"file", "error"}`` rows. A broken/malicious file
    marks only itself invalid; the others still load. An empty/absent directory
    yields empty maps (the feature is dormant when nothing is declared).
    """
    directory = get_jobs_declarations_dir(syrvis_home)
    valid: Dict[str, JobDefinition] = {}
    invalid: List[Dict[str, str]] = []
    if not directory.exists():
        return valid, invalid

    for path in sorted(directory.glob("*.yaml")):
        try:
            data = yaml.safe_load(path.read_text())
            if not isinstance(data, dict):
                raise JobDeclarationError("declaration must be a mapping")
            job = JobDefinition.from_dict(path.stem, data)
            # filename == name: the name is a security-relevant path component
            # (it derives the script path + crontab command), so the file it
            # lives in must match it — no aliasing a script under another file.
            if job.name != path.stem:
                raise JobDeclarationError(
                    "declares name {!r} — it must match its filename".format(job.name)
                )
            valid[job.name] = job
        except Exception as exc:  # noqa: BLE001 - isolation: report, keep loading
            invalid.append({"file": path.name, "error": str(exc)})
    return valid, invalid


# ---------------------------------------------------------------------------
# Managed crontab block: parse + diff
# ---------------------------------------------------------------------------

BLOCK_BEGIN = "### SYRVISCORE JOBS BEGIN (managed)"
BLOCK_END = "### SYRVISCORE JOBS END"


def parse_managed_block(crontab_text: str) -> Dict[str, str]:
    """Parse the delimited managed block out of ``/etc/crontab`` text.

    Returns a map of ``name -> crontab line`` for each managed job line, derived
    by matching the trailing ``.../jobs/<name>`` path component of the command.
    Everything OUTSIDE the ``BLOCK_BEGIN``/``BLOCK_END`` markers is ignored — the
    caller must never diff against DSM's own crontab lines.
    """
    installed: Dict[str, str] = {}
    in_block = False
    for raw in crontab_text.splitlines():
        line = raw.strip()
        if line == BLOCK_BEGIN:
            in_block = True
            continue
        if line == BLOCK_END:
            in_block = False
            continue
        if not in_block or not line or line.startswith("#"):
            continue
        # A managed line: "<5 cron fields> root <path>/jobs/<name>". The name is
        # the final path component of the command (the last token).
        name = line.rsplit("/", 1)[-1]
        installed[name] = line
    return installed


def build_jobs_reconcile_plan(
    declarations: Dict[str, JobDefinition],
    current_block: Dict[str, str],
    jobs_dir: Path,
) -> Dict[str, Any]:
    """Diff declared jobs against the parsed managed crontab block (read-only).

    Actions:
    - ``add``:    declared+enabled, not present in the block.
    - ``update``: declared+enabled, present but its line differs (schedule change).
    - ``remove``: present in the block but not declared, or declared ``enabled:
      false`` — it must be dropped from the managed block.

    Only enabled declarations produce ``add``/``update``. A disabled declaration
    is treated like an undeclared one for the block (its line is removed) — the
    job's vetted script stays on disk, it is simply unscheduled.
    """
    actions: List[Dict[str, Any]] = []
    in_sync: List[str] = []

    desired_lines: Dict[str, str] = {}
    for name, job in declarations.items():
        if not job.enabled:
            continue  # declared-off: unscheduled (its block line, if any, is removed)
        desired_lines[name] = job.crontab_line(jobs_dir)

    for name, line in desired_lines.items():
        current = current_block.get(name)
        if current is None:
            actions.append({"kind": "add", "name": name, "line": line})
        elif current != line:
            actions.append({"kind": "update", "name": name, "line": line, "was": current})
        else:
            in_sync.append(name)

    for name in current_block:
        if name not in desired_lines:
            actions.append({"kind": "remove", "name": name, "was": current_block[name]})

    return {
        "changed": bool(actions),
        "actions": actions,
        "in_sync": sorted(in_sync),
        "desired": desired_lines,
        "summary": {
            "declared": len(declarations),
            "scheduled": len(desired_lines),
            "total_actions": len(actions),
        },
    }
