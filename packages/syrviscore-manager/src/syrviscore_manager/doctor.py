"""``syrvisctl doctor`` — diagnose the platform WITHOUT a resolvable home.

Why this exists (incident 2026-08-16). Every content-aware check the platform
owns lives on a validator constructed from the install directory, so the two
that would have spoken — "Boot hook absent, the homebase will not auto-resume"
and the startup-script drift check — could not run, because the install
directory was the thing that moved. The 22-method self-audit contains not one
check that looks at a volume root. A human found the cause with ``ls``.

This module is the answer to exactly that morning. It runs from the SPK's own
rootfs venv, imports nothing from the service package (that package lives inside
the tree under suspicion), catches every home-resolution failure and REPORTS it,
and answers three questions:

    1. Does the rc.d boot hook exist, is it executable, and is it current?
    2. What does a ``/volume*/syrviscore*`` scan actually find — in particular,
       is there a ``syrviscore_1`` sibling? (DSM renames a volume root whose name
       collides with a shared folder at the first COLD boot.)
    3. Are the operator-seam accounts' login shells intact? (DSM regenerates
       ``/etc/passwd`` on every boot and reverts them to ``/sbin/nologin``.)

Library discipline: this module computes and returns a dict; it never prints and
never exits. ``cli.doctor`` renders it.
"""

import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import paths
from .errors import SyrvisError

# DSM rootfs constants. Hardcoded on purpose: their whole value is that they are
# reachable when nothing under SYRVIS_HOME is.
BOOT_SCRIPT_PATH = Path("/usr/local/etc/rc.d/S99syrviscore.sh")
BOOT_ENV_PATH = Path("/usr/local/etc/syrviscore-boot.env")
PASSWD_PATH = Path("/etc/passwd")

# The seam identities whose shells DSM reverts on every boot.
SEAM_ACCOUNTS = ("syrvis-operator", "syrvis-reader")

# The minimum boot-hook contract this manager expects to find deployed. The
# marker is rendered into the hook by ``syrviscore.privileged_ops`` (see
# BOOT_HOOK_CONTRACT there, which is the source of truth); this is the one fact
# duplicated across the package boundary, and it is duplicated precisely so the
# manager never has to import the service package to answer "is it current?".
# A deployed hook BELOW this is missing the 2026-08-16 reclaim guard / seam heal
# / failure alarm (contract 2) or its extension to the configured apps-root
# segment (contract 3), and must be regenerated with `sudo syrvis verify --fix`.
MIN_BOOT_HOOK_CONTRACT = 3

_CONTRACT_RE = re.compile(r"^#\s*boot-hook-contract:\s*(\d+)\s*$", re.MULTILINE)
_VOLUME_RE = re.compile(r"^volume\d+$")
_NOLOGIN_RE = re.compile(r"(nologin|/false)$")
_APPS_ROOT_RE = re.compile(r"^SYRVIS_APPS_ROOT_NAME=['\"]?([^'\"\n]*)['\"]?\s*$", re.MULTILINE)


def _volumes_root() -> Path:
    sim_root = paths.get_sim_root()
    return sim_root if sim_root else Path("/")


def apps_root_name() -> str:
    """The configured apps-root segment, read from the ROOTFS boot-env cache.

    The manager must answer "is this a renamed platform root?" without importing
    the service package or resolving a home — both live inside the tree under
    suspicion. The cache at :data:`BOOT_ENV_PATH` is written beside the boot hook
    on every ensure precisely so this fact survives on the rootfs. Absent or
    unreadable -> the package name, i.e. 0.5.13 behaviour.
    """
    try:
        match = _APPS_ROOT_RE.search(BOOT_ENV_PATH.read_text())
    except OSError:
        return paths.PACKAGE_NAME
    if not match:
        return paths.PACKAGE_NAME
    return match.group(1).strip() or paths.PACKAGE_NAME


def scan_volume_roots() -> List[Dict[str, Any]]:
    """Every platform-named volume-root directory on this host, classified.

    The one command the responder never ran. ``kind`` is ``platform`` for an
    expected name (the install root ``syrviscore`` or the configured apps-root
    segment), ``renamed`` for a DSM collision sibling (``<name>_1``), and
    ``other`` for anything else sharing a prefix (a ``.scaffold-*`` set aside by
    hand, say). ``manifest`` and ``apps`` say WHICH platform root it is: the
    install root carries the manifest, an app-home root carries ``apps/``.
    """
    found: List[Dict[str, Any]] = []
    root = _volumes_root()
    watched = [paths.PACKAGE_NAME]
    segment = apps_root_name()
    if segment not in watched:
        watched.append(segment)
    collision_re = re.compile(r"^(?:{})_\d+$".format("|".join(re.escape(n) for n in watched)))
    try:
        volumes = sorted(root.iterdir())
    except OSError:
        return found
    for volume in volumes:
        try:
            if not _VOLUME_RE.match(volume.name) or not volume.is_dir():
                continue
            entries = sorted(volume.iterdir())
        except OSError:
            continue
        for entry in entries:
            try:
                if not any(entry.name.startswith(n) for n in watched) or not entry.is_dir():
                    continue
                if entry.name in watched:
                    kind = "platform"
                elif collision_re.match(entry.name):
                    kind = "renamed"
                else:
                    kind = "other"
                found.append(
                    {
                        "path": str(entry),
                        "kind": kind,
                        "manifest": (entry / paths.MANIFEST_FILENAME).exists(),
                        "apps": (entry / "apps").is_dir(),
                        "current_symlink": (entry / "current").is_symlink(),
                    }
                )
            except OSError:  # pragma: no cover - vanishing/denied entry
                continue
    return found


def check_boot_hook() -> Dict[str, Any]:
    """Presence, executability and contract level of the rootfs rc.d hook."""
    out: Dict[str, Any] = {
        "path": str(BOOT_SCRIPT_PATH),
        "present": BOOT_SCRIPT_PATH.is_file(),
        "executable": False,
        "contract": None,
        "min_contract": MIN_BOOT_HOOK_CONTRACT,
        "stale": True,
        "env_cache": {"path": str(BOOT_ENV_PATH), "present": BOOT_ENV_PATH.is_file()},
    }
    if not out["present"]:
        out["detail"] = (
            "Boot hook absent — this box will NOT auto-resume, self-heal the seam "
            "shells, or reclaim a renamed platform root after a reboot. "
            "Fix: sudo syrvis verify --fix (or sudo syrvis setup)."
        )
        return out
    try:
        out["executable"] = os.access(str(BOOT_SCRIPT_PATH), os.X_OK)
        text = BOOT_SCRIPT_PATH.read_text()
    except OSError as e:
        out["detail"] = "Boot hook unreadable: {}".format(e)
        return out
    match = _CONTRACT_RE.search(text)
    out["contract"] = int(match.group(1)) if match else 0
    out["stale"] = out["contract"] < MIN_BOOT_HOOK_CONTRACT
    if out["stale"]:
        out["detail"] = (
            "Deployed boot hook is at contract {} — this manager expects {}. It predates "
            "the collision reclaim guard / inlined seam heal / boot-failure alarm. "
            "Fix: sudo syrvis verify --fix.".format(out["contract"], MIN_BOOT_HOOK_CONTRACT)
        )
    elif not out["executable"]:
        out["detail"] = "Boot hook present and current but NOT executable — DSM will skip it."
    elif not out["env_cache"]["present"]:
        out["detail"] = (
            "Boot hook current, but the rootfs boot-env cache is missing: a boot failure "
            "will be logged, not paged. It is written by `sudo syrvis verify --fix` from "
            "NTFY_URL in config/.env."
        )
    return out


def check_seam_accounts() -> List[Dict[str, Any]]:
    """Login shells of the operator-seam accounts, read straight from /etc/passwd.

    No ``pwd`` module lookup: this must report on a box where NSS or the tree may
    be in an odd state, and the file is the fact anyway.
    """
    rows = []
    try:
        lines = PASSWD_PATH.read_text().splitlines()
    except OSError as e:
        return [
            {"user": u, "present": False, "shell": None, "ok": False, "error": str(e)}
            for u in SEAM_ACCOUNTS
        ]
    shells = {}
    for line in lines:
        parts = line.split(":")
        if len(parts) >= 7 and parts[0] in SEAM_ACCOUNTS:
            shells[parts[0]] = parts[6]
    for user in SEAM_ACCOUNTS:
        shell = shells.get(user)
        rows.append(
            {
                "user": user,
                "present": shell is not None,
                "shell": shell,
                # An ABSENT account is not a failure: syrvis-reader is optional
                # on an instance that never provisioned it. A present account
                # with a nologin shell IS — that is the boot revert.
                "ok": shell is None or not _NOLOGIN_RE.search(shell),
            }
        )
    return rows


def resolve_home_report(explicit: Optional[Path] = None) -> Dict[str, Any]:
    """Home resolution as DATA — the failure is the diagnosis, not an exit."""
    try:
        return {"resolved": str(paths.resolve_home(explicit)), "error": None, "code": None}
    except SyrvisError as e:
        return {"resolved": None, "error": str(e), "code": e.code}


def run(explicit_home: Optional[Path] = None) -> Dict[str, Any]:
    """The whole diagnosis. Never raises; every failure becomes a finding."""
    home = resolve_home_report(explicit_home)
    boot_hook = check_boot_hook()
    roots = scan_volume_roots()
    accounts = check_seam_accounts()

    renamed = [r["path"] for r in roots if r["kind"] == "renamed"]
    findings: List[str] = []
    if renamed:
        findings.append(
            "COLLISION: {} renamed platform root(s) — {}. DSM renamed these at boot "
            "because a shared folder shares the name ({}). Check the target is absent "
            "or empty, then mv each back. Do NOT run 'syrvisctl install'.".format(
                len(renamed),
                ", ".join(renamed),
                " / ".join(sorted({paths.PACKAGE_NAME, apps_root_name()})),
            )
        )
    if home["resolved"] is None:
        findings.append("HOME: {}".format(home["error"]))
    if not boot_hook["present"] or boot_hook["stale"] or not boot_hook["executable"]:
        findings.append("BOOT HOOK: {}".format(boot_hook.get("detail", "not current")))
    for row in accounts:
        if not row["ok"]:
            findings.append(
                "SEAM: {} has shell {!r} — the seam is DENIED until it is /bin/sh "
                "(DSM reverts this on every boot).".format(row["user"], row["shell"])
            )

    return {
        "home": home,
        "boot_hook": boot_hook,
        "volume_roots": roots,
        "seam_accounts": accounts,
        "findings": findings,
        "ok": not findings,
    }
