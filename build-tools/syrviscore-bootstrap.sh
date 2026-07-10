#!/bin/sh
# syrviscore-bootstrap.sh - one-pass production install/upgrade for SyrvisCore.
#
# Run ONCE with sudo on the NAS. It drives the SANCTIONED tools only
# (synopkg for the manager SPK, syrvisctl for the service) so the whole
# migration is declarative and reversible:
#
#   1. (optional) upgrade the manager SPK          -> synopkg install <spk>
#   2. install the bundled service wheel           -> syrvisctl install --wheel
#   3. verify the install is runnable and speaks --json
#   4. (optional) run privileged setup             -> syrvis setup
#   5. (optional) back up, then remove old versions -> syrvisctl backup/cleanup
#
# It NEVER hand-edits system files: the manager lives in the SPK, the service
# in per-version venvs, and cleanup goes through syrvisctl (which always keeps
# the active version). Idempotent; safe to re-run; --dry-run previews everything.
#
# Artifacts are auto-discovered next to this script (a devkit/SPK bundle), so
# the common case is just:  sudo ./syrviscore-bootstrap.sh --cleanup --setup
#
# Usage:
#   sudo ./syrviscore-bootstrap.sh [options]
#     --home DIR     SYRVIS_HOME (default: auto-discovered from the manifest)
#     --wheel FILE   service wheel (default: syrviscore-*.whl next to this script)
#     --config FILE  config.yaml to bundle (default: config.yaml next to script)
#     --spk FILE     upgrade the manager SPK first (default: skip; use the
#                    already-installed manager). Pass to make it truly one-pass.
#     --setup        run 'syrvis setup' after install (docker group, boot hook)
#     --cleanup      back up, then remove non-active versions + build junk
#     --keep N       versions to keep with --cleanup (default: 1 = active only)
#     --dry-run      print every action; change nothing
#     --yes, -y      assume yes (non-interactive)

set -u

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
LOG_FILE="/tmp/syrviscore-bootstrap.log"
REQUIRED_MANAGER="0.2.0"   # manager must be >= this to speak --wheel + --json
# SPK paths are overridable for the DSM simulator / tests (DSM_SIM_ACTIVE=1);
# defaults are the real DSM locations.
SPK_SYRVISCTL="${SYRVISCORE_SYRVISCTL_BIN:-/var/packages/syrviscore/target/venv/bin/syrvisctl}"
SPK_TARGET="${SYRVISCORE_SPK_TARGET:-/var/packages/syrviscore/target}"

# --- args ------------------------------------------------------------------
HOME_DIR=""; WHEEL=""; CONFIG=""; SPK=""
DO_SETUP=0; DO_CLEAN=0; KEEP=1; DRYRUN=0; ASSUME_YES=0

while [ $# -gt 0 ]; do
    case "$1" in
        --home)   HOME_DIR="${2:?--home needs a value}"; shift 2 ;;
        --wheel)  WHEEL="${2:?--wheel needs a value}"; shift 2 ;;
        --config) CONFIG="${2:?--config needs a value}"; shift 2 ;;
        --spk)    SPK="${2:?--spk needs a value}"; shift 2 ;;
        --keep)   KEEP="${2:?--keep needs a value}"; shift 2 ;;
        --setup)  DO_SETUP=1; shift ;;
        --cleanup) DO_CLEAN=1; shift ;;
        --dry-run) DRYRUN=1; shift ;;
        --yes|-y) ASSUME_YES=1; shift ;;
        --help|-h) sed -n '2,42p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) echo "unknown argument: $1 (see --help)" >&2; exit 2 ;;
    esac
done

STEP="starting"
trap 'ec=$?; if [ "$ec" -ne 0 ]; then printf "\n== ABORTED during: %s (exit %s). Nothing further changed.\n" "$STEP" "$ec" >&2; fi' EXIT

log()  { printf '%s\n' "== $*"; printf '[%s] %s\n' "$(date "+%Y-%m-%d %H:%M:%S")" "$*" >> "$LOG_FILE" 2>/dev/null || true; }
warn() { printf 'WARNING: %s\n' "$*" >&2; }
die()  { printf 'ERROR: %s\n' "$*" >&2; exit 1; }
# run(): the ONLY thing that mutates state. In --dry-run it just prints.
run()  { if [ "$DRYRUN" = 1 ]; then printf '   [dry-run] %s\n' "$*"; else eval "$*" || die "command failed: $*"; fi; }

# ver_ge A B  ->  true if A >= B  (semantic version compare via sort -V)
ver_ge() { [ "$1" = "$2" ] || [ "$(printf '%s\n%s\n' "$1" "$2" | sort -V | tail -1)" = "$1" ]; }

# --- preflight -------------------------------------------------------------
STEP="preflight"
[ "$(id -u)" = "0" ] || [ "${DSM_SIM_ACTIVE:-0}" = "1" ] || die "run as root: sudo sh $0"
: > "$LOG_FILE" 2>/dev/null || true
log "SyrvisCore bootstrap ($([ "$DRYRUN" = 1 ] && echo DRY-RUN || echo LIVE)); log: $LOG_FILE"

# service wheel
if [ -z "$WHEEL" ]; then
    for f in "$SCRIPT_DIR"/syrviscore-[0-9]*.whl; do [ -f "$f" ] && WHEEL="$f" && break; done
fi
[ -n "$WHEEL" ] && [ -f "$WHEEL" ] || die "no service wheel (syrviscore-*.whl) found; pass --wheel FILE"
TARGET_VER=$(basename "$WHEEL" | sed -n 's/^syrviscore-\([0-9][0-9.]*\)-.*/\1/p')
[ -n "$TARGET_VER" ] || die "could not parse version from wheel name: $(basename "$WHEEL")"
log "Service wheel: $(basename "$WHEEL") (version $TARGET_VER)"

# config (optional)
if [ -z "$CONFIG" ] && [ -f "$SCRIPT_DIR/config.yaml" ]; then CONFIG="$SCRIPT_DIR/config.yaml"; fi
[ -n "$CONFIG" ] && log "Config: $CONFIG"

# --- 1. optional manager SPK upgrade (sanctioned: synopkg) -----------------
if [ -n "$SPK" ]; then
    STEP="upgrade manager SPK"
    [ -f "$SPK" ] || die "--spk file not found: $SPK"
    command -v synopkg >/dev/null 2>&1 || die "synopkg not found; upgrade the SPK via Package Center instead"
    log "Upgrading manager SPK from $(basename "$SPK") (synopkg install)"
    run "synopkg install '$SPK'"
fi

# --- locate + gate the manager ---------------------------------------------
STEP="check manager"
[ -x "$SPK_SYRVISCTL" ] || die "manager not found at $SPK_SYRVISCTL — install/upgrade the SyrvisCore SPK first (Package Center), or pass --spk FILE"
HAVE_MGR=$("$SPK_SYRVISCTL" --version 2>/dev/null | sed -n 's/.*version \([0-9][0-9.]*\).*/\1/p')
[ -n "$HAVE_MGR" ] || die "could not read manager version from $SPK_SYRVISCTL"
if [ "$DRYRUN" = 1 ] && [ -n "$SPK" ]; then
    log "(dry-run) manager is $HAVE_MGR now; a live --spk run would upgrade it to >= $REQUIRED_MANAGER"
elif ! ver_ge "$HAVE_MGR" "$REQUIRED_MANAGER"; then
    die "manager is $HAVE_MGR but >= $REQUIRED_MANAGER is required. Upgrade the SPK (Package Center) or pass --spk <the 0.2.0 spk>, then re-run."
fi
log "Manager: syrvisctl $HAVE_MGR (>= $REQUIRED_MANAGER OK)"
SC="$SPK_SYRVISCTL"

# --- discover SYRVIS_HOME (manifest scan, like the SPK upgrade scripts) -----
STEP="discover home"
if [ -z "$HOME_DIR" ]; then
    for VOL in /volume[0-9]*; do
        if [ -f "$VOL/syrviscore/.syrviscore-manifest.json" ]; then HOME_DIR="$VOL/syrviscore"; break; fi
    done
fi
if [ -z "$HOME_DIR" ]; then
    # fresh install: no prior home. syrvisctl install --path will create it.
    warn "no existing SYRVIS_HOME found; this looks like a fresh install"
    die "pass --home DIR to choose the install location (e.g. --home /volume4/syrviscore)"
fi
log "SYRVIS_HOME: $HOME_DIR"
# NB: `syrvisctl info --json` emits the key "active" (the manifest FILE uses
# "active_version"; the CLI output does not). Parse the CLI's key.
BEFORE_ACTIVE=$("$SC" info --json 2>/dev/null | sed -n 's/.*"active": *"\([^"]*\)".*/\1/p')
[ -n "$BEFORE_ACTIVE" ] && log "Currently active service version: $BEFORE_ACTIVE"

# --- 2. safety backup (only if we're going to clean up) --------------------
if [ "$DO_CLEAN" = 1 ]; then
    STEP="safety backup"
    log "Taking a safety backup before any removal (syrvisctl backup create)"
    if [ "$DRYRUN" = 1 ]; then
        printf '   [dry-run] %s backup create\n' "$SC"
    else
        "$SC" backup create >> "$LOG_FILE" 2>&1 || warn "backup create failed (continuing; cleanup still keeps the active version)"
    fi
fi

# --- 3. install the service from the local wheel ---------------------------
STEP="install service $TARGET_VER"
if [ "$BEFORE_ACTIVE" = "$TARGET_VER" ] && [ "$(readlink "$HOME_DIR/current" 2>/dev/null)" = "versions/$TARGET_VER" ]; then
    log "Service $TARGET_VER already installed and active — skipping install (idempotent re-run)"
else
    log "Installing service $TARGET_VER from the local wheel (offline)"
    INSTALL_CMD="'$SC' install --wheel '$WHEEL' --path '$HOME_DIR' --force -y"
    [ -n "$CONFIG" ] && INSTALL_CMD="$INSTALL_CMD --config '$CONFIG'"
    run "$INSTALL_CMD"
fi

# --- 4. verify -------------------------------------------------------------
STEP="verify install"
if [ "$DRYRUN" = 1 ]; then
    log "(dry-run) would verify: current -> $TARGET_VER, syrvis --version, status --json"
else
    ACTIVE=$("$SC" info --json 2>/dev/null | sed -n 's/.*"active": *"\([^"]*\)".*/\1/p')
    [ "$ACTIVE" = "$TARGET_VER" ] || die "active version is '$ACTIVE', expected '$TARGET_VER' after install"
    [ -L "$HOME_DIR/current" ] || die "current symlink missing under $HOME_DIR"
    SYRVIS_BIN="$HOME_DIR/bin/syrvis"
    [ -x "$SYRVIS_BIN" ] || SYRVIS_BIN="$HOME_DIR/current/cli/venv/bin/syrvis"
    RUNVER=$("$SYRVIS_BIN" --version 2>/dev/null | sed -n 's/.*version \([0-9][0-9.]*\).*/\1/p')
    [ "$RUNVER" = "$TARGET_VER" ] || die "installed syrvis reports '$RUNVER', expected '$TARGET_VER'"
    # must emit JSON (the whole point of the migration) — not "No such option"
    "$SYRVIS_BIN" status --json 2>/dev/null | head -c1 | grep -q '{' \
        || die "syrvis status --json did not emit JSON — the installed build predates the --json contract"
    log "Verified: service $TARGET_VER active, syrvis --version=$RUNVER, status --json OK"
fi

# --- 5. optional privileged setup ------------------------------------------
if [ "$DO_SETUP" = 1 ]; then
    STEP="syrvis setup"
    log "Running 'syrvis setup' (docker group, boot hook, global symlink)"
    run "'$HOME_DIR/bin/syrvis' setup $([ "$ASSUME_YES" = 1 ] && echo -y)"
fi

# --- 6. cleanup: remove non-active versions + build junk -------------------
if [ "$DO_CLEAN" = 1 ]; then
    STEP="cleanup old versions"
    log "Removing non-active service versions (keep $KEEP; the active version is always kept)"
    if [ "$DRYRUN" = 1 ]; then
        printf '   [dry-run] would remove: '
        "$SC" cleanup --keep "$KEEP" --dry-run 2>/dev/null | tr '\n' ' '; echo
    else
        "$SC" cleanup --keep "$KEEP" -y >> "$LOG_FILE" 2>&1 || warn "cleanup reported an error (see $LOG_FILE)"
    fi
    # AppleDouble/build junk that older SPK builds leaked into the target.
    # NB: /var/packages/<pkg>/target is a SYMLINK (-> @appstore/...); find won't
    # descend a symlink unless it's resolved first, so resolve to the real dir.
    STEP="cleanup build junk"
    JUNK_DIR=$(readlink -f "$SPK_TARGET" 2>/dev/null || echo "$SPK_TARGET")
    JUNK=$(find "$JUNK_DIR" -maxdepth 2 -name '._*' 2>/dev/null)
    if [ -n "$JUNK" ]; then
        log "Removing macOS AppleDouble junk from $JUNK_DIR:"
        printf '%s\n' "$JUNK" | sed 's/^/     /'
        run "find '$JUNK_DIR' -maxdepth 2 -name '._*' -delete"
    fi
fi

# --- done ------------------------------------------------------------------
STEP="done"
log ""
log "Bootstrap complete ($([ "$DRYRUN" = 1 ] && echo 'DRY-RUN — nothing changed' || echo "service now $TARGET_VER"))."
[ "$DO_SETUP" = 0 ] && [ "$DRYRUN" = 0 ] && log "If this is a fresh setup, configure services with: sudo $HOME_DIR/bin/syrvis setup"
exit 0
