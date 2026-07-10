#!/bin/sh
# bootstrap.sh - SyrvisCore dev-loop installer (runs from an extracted devkit tarball)
# POSIX shell compatible; works on Synology DSM and on Linux/macOS for local testing.
#
# The devkit is the fast iteration path: build a tarball, scp it to the NAS,
# extract, run this script. No Package Center, no GitHub. The SPK remains the
# production/DR install; this never touches the production SYRVIS_HOME
# (/volumeX/syrviscore) unless explicitly forced.
#
# Usage:
#   ./bootstrap.sh [--home DIR] [--setup] [--clean] [--yes]
#
#   --home DIR   Dev SYRVIS_HOME (default: /<volume-of-this-dir>/syrviscore-dev)
#   --setup      Also run 'sudo syrvis setup' (modifies system state: docker
#                group, boot hook). Default: skipped, guidance printed.
#   --clean      Tear down the dev install (stop services best-effort, remove
#                the dev home and the local manager venv) and exit.
#   --yes        Non-interactive (assumed for all prompts).

set -u

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
cd "$SCRIPT_DIR"

LOG_FILE="$SCRIPT_DIR/bootstrap.log"
MANAGER_VENV="$SCRIPT_DIR/manager-venv"

log() {
    echo "[bootstrap] $*"
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" >> "$LOG_FILE" 2>/dev/null || true
}

die() {
    log "ERROR: $*"
    exit 1
}

# === Parse arguments ===
HOME_DIR=""
DO_SETUP=0
DO_CLEAN=0
ASSUME_YES=0

while [ $# -gt 0 ]; do
    case "$1" in
        --home)
            [ $# -ge 2 ] || die "--home requires a directory argument"
            HOME_DIR="$2"
            shift 2
            ;;
        --setup) DO_SETUP=1; shift ;;
        --clean) DO_CLEAN=1; shift ;;
        --yes|-y) ASSUME_YES=1; shift ;;
        --help|-h)
            sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//'
            exit 0
            ;;
        *) die "Unknown argument: $1 (see --help)" ;;
    esac
done

# === Resolve the dev home ===
if [ -z "$HOME_DIR" ]; then
    VOL=$(echo "$SCRIPT_DIR" | cut -d/ -f2)
    case "$VOL" in
        volume*)
            HOME_DIR="/$VOL/syrviscore-dev"
            ;;
        *)
            die "Not on a /volumeX path; pass --home DIR explicitly"
            ;;
    esac
fi

# Production guard: the SPK scripts and syrvisctl discover installations at
# /volumeX/syrviscore — a dev install there would be mistaken for production.
case "$HOME_DIR" in
    /volume*/syrviscore)
        die "$HOME_DIR is the PRODUCTION discovery path; use a different --home (e.g. ${HOME_DIR}-dev)"
        ;;
esac

log "Devkit dir:  $SCRIPT_DIR"
log "Dev home:    $HOME_DIR"

# === --clean: tear down and exit ===
if [ "$DO_CLEAN" = "1" ]; then
    log "Cleaning dev installation..."
    if [ -x "$HOME_DIR/bin/syrvis" ]; then
        log "  Stopping services (best-effort)..."
        "$HOME_DIR/bin/syrvis" stop >> "$LOG_FILE" 2>&1 || log "  (stop failed or nothing to stop)"
    fi
    rm -rf "$HOME_DIR"
    log "  Removed $HOME_DIR"
    rm -rf "$MANAGER_VENV"
    log "  Removed $MANAGER_VENV"
    if [ -f /usr/local/etc/rc.d/S99syrviscore.sh ] \
        && grep -q "$HOME_DIR" /usr/local/etc/rc.d/S99syrviscore.sh 2>/dev/null; then
        log "  NOTE: boot hook /usr/local/etc/rc.d/S99syrviscore.sh references this"
        log "  dev home; remove it with: sudo rm -f /usr/local/etc/rc.d/S99syrviscore.sh"
    fi
    log "Clean complete."
    exit 0
fi

# === Preflight ===
command -v python3 >/dev/null 2>&1 || die "python3 not found"
python3 -c "import ensurepip" >/dev/null 2>&1 || die "python3 lacks ensurepip; venv creation would fail"

# === Verify tarball integrity ===
if [ -f SHA256SUMS ]; then
    log "Verifying SHA256SUMS..."
    if command -v sha256sum >/dev/null 2>&1; then
        sha256sum -c SHA256SUMS >> "$LOG_FILE" 2>&1 || die "Checksum verification FAILED (see $LOG_FILE)"
    elif command -v shasum >/dev/null 2>&1; then
        shasum -a 256 -c SHA256SUMS >> "$LOG_FILE" 2>&1 || die "Checksum verification FAILED (see $LOG_FILE)"
    else
        log "  WARNING: no sha256sum/shasum available; skipping verification"
    fi
    log "  Checksums OK"
else
    log "WARNING: no SHA256SUMS in devkit; skipping verification"
fi

# === Locate artifacts ===
SERVICE_WHEEL=""
for f in "$SCRIPT_DIR"/syrviscore-[0-9]*.whl; do
    [ -f "$f" ] && SERVICE_WHEEL="$f" && break
done
[ -n "$SERVICE_WHEEL" ] || die "No service wheel (syrviscore-*.whl) in devkit"

MANAGER_WHEEL=""
for f in "$SCRIPT_DIR"/wheels/syrviscore_manager-*.whl; do
    [ -f "$f" ] && MANAGER_WHEEL="$f" && break
done
[ -n "$MANAGER_WHEEL" ] || die "No manager wheel in devkit wheels/"

log "Manager wheel: $(basename "$MANAGER_WHEEL")"
log "Service wheel: $(basename "$SERVICE_WHEEL")"

# === [1/4] Manager venv (offline, from bundled wheels) ===
log "[1/4] Creating manager venv..."
rm -rf "$MANAGER_VENV"
python3 -m venv "$MANAGER_VENV" >> "$LOG_FILE" 2>&1 || die "venv creation failed (see $LOG_FILE)"
if ! "$MANAGER_VENV/bin/pip" install --no-cache-dir --no-index \
    --find-links "$SCRIPT_DIR/wheels" "$MANAGER_WHEEL" >> "$LOG_FILE" 2>&1; then
    tail -20 "$LOG_FILE" >&2
    die "Offline manager install failed - a bundled wheel is likely wrong for this platform"
fi
"$MANAGER_VENV/bin/syrvisctl" --version >> "$LOG_FILE" 2>&1 || die "syrvisctl not functional after install"
log "  $("$MANAGER_VENV/bin/syrvisctl" --version)"

# === [2/4] Install the service version from the local wheel ===
log "[2/4] Installing service from local wheel..."
CONFIG_ARGS=""
[ -f "$SCRIPT_DIR/config.yaml" ] && CONFIG_ARGS="--config $SCRIPT_DIR/config.yaml"

# shellcheck disable=SC2086
SYRVIS_HOME="$HOME_DIR" "$MANAGER_VENV/bin/syrvisctl" install \
    --wheel "$SERVICE_WHEEL" \
    --path "$HOME_DIR" \
    --force -y $CONFIG_ARGS || die "syrvisctl install --wheel failed"

# === [3/4] Gate: the install must be runnable ===
log "[3/4] Verifying install..."
SYRVIS_HOME="$HOME_DIR" "$MANAGER_VENV/bin/syrvisctl" info --json >> "$LOG_FILE" 2>&1 \
    || die "syrvisctl info failed against $HOME_DIR"
[ -L "$HOME_DIR/current" ] || die "current symlink missing in $HOME_DIR"
"$HOME_DIR/current/cli/venv/bin/syrvis" --version >> "$LOG_FILE" 2>&1 \
    || die "installed syrvis CLI is not functional"
log "  $("$HOME_DIR/current/cli/venv/bin/syrvis" --version) at $HOME_DIR"

# === [4/4] Optional privileged setup ===
if [ "$DO_SETUP" = "1" ]; then
    log "[4/4] Running syrvis setup (modifies system state)..."
    sudo "$HOME_DIR/bin/syrvis" setup || die "syrvis setup failed"
else
    log "[4/4] Skipping privileged setup (pass --setup to run it)"
    log "      To configure services: sudo $HOME_DIR/bin/syrvis setup"
fi

log ""
log "Bootstrap complete."
log "  syrvisctl: $MANAGER_VENV/bin/syrvisctl   (use with SYRVIS_HOME=$HOME_DIR)"
log "  syrvis:    $HOME_DIR/bin/syrvis"
log "  Teardown:  ./bootstrap.sh --clean --home $HOME_DIR"
exit 0
