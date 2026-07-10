# NAS cleanup — SyrvisCore-only leftovers

Every path/resource below is created **exclusively by SyrvisCore** (the SPK,
`syrvisctl`, `syrvis setup`, or the dev bootstrap). Nothing here is a Synology
system file or anything unrelated — if a name doesn't contain `syrvis`,
`traefik`, `portainer`, `cloudflared`, or the `proxy`/`syrvis-macvlan` docker
networks, it is **not** on this list and should not be touched.

Context: as of 2026-07-09 there is **no production SyrvisCore install** — these
are the artifacts a prior/aborted install or a dev bootstrap would have left.
Use this list to verify a clean slate before the first real install, or to
tear down after testing. I could not SSH into the NAS (192.168.8.3) to
enumerate what's actually present — my key isn't authorized yet (see the
session summary), so this is the code-derived exhaustive list, not an observed
inventory.

## Safe to remove — 100% SyrvisCore

### Package (owned by Package Center)
- `/var/packages/syrviscore/` — the SPK install. **Remove via Package Center
  or `sudo synopkg uninstall syrviscore`**, not `rm`, so DSM's package DB stays
  consistent. Contains `target/venv` (manager venv), `target/syrviscore.profile`.

### Service home (owned by syrvisctl/syrvis)
- `/volume*/syrviscore/` — SYRVIS_HOME (production). Holds `versions/`,
  `config/` (`.env`, `docker-compose.yaml`), `data/` (traefik certs, portainer
  db, cloudflared creds), `backups/`, `current` symlink, `bin/`.
  **Back up `config/` + `data/` first if any real config was done.**
- `/volume*/syrviscore-dev/` — dev SYRVIS_HOME created by `bootstrap.sh`.
  Always safe to delete; purely for testing.

### Boot hooks and global symlink (need root)
- `/usr/local/etc/rc.d/S99syrviscore.sh` — the current boot hook (`syrvis setup`
  creates this).
- `/usr/local/etc/rc.d/S99syrviscore-docker.sh` — **legacy wrong-named boot
  hook.** A bug in the old postuninst created/looked for this name. If it exists
  from an old install, remove it — nothing creates it anymore.
- `/usr/local/bin/syrvis` — global CLI symlink (older installs).

### Dev bootstrap leftovers (in the SSH user's home)
- `~/syrviscore-devkit/` — extracted devkit tarball + `manager-venv/` +
  `bootstrap.log`. Safe to delete anytime.
- `/tmp/syrviscore-devkit-*.tar.gz` — shipped tarball.

### Temp/log files (regenerated; safe anytime)
- `/tmp/syrviscore-install.log`, `/tmp/syrviscore-pip.log`,
  `/tmp/syrviscore-uninstall.log`, `/tmp/syrviscore-stop.log`
- `/tmp/syrviscore_install_dir`, `/tmp/syrviscore_previous_version`,
  `/tmp/syrviscore_*.log` (per-script logs)

### Docker resources (the core stack + Layer 2 services)
- Containers: `traefik`, `portainer`, `cloudflared`, and any Layer 2 service
  containers (their names come from each `syrvis-service.yaml`).
  Remove: `docker rm -f traefik portainer cloudflared`
- Networks: `proxy`, `syrvis-macvlan` — `docker network rm proxy syrvis-macvlan`
  (only after the containers using them are gone).
- The macvlan shim interface `syrvis-shim` is ephemeral (recreated at boot /
  `syrvis start`, gone after reboot) — no manual cleanup needed.

### Legacy user account (older installs only)
- `syrvis-bot` — a package user some early installs created (referenced in the
  old postuninst). If present: `sudo synouser --del syrvis-bot`. Nothing in the
  current code creates it.

## Recommended clean-slate sequence (before the first real install)

```sh
# 1. Package (if installed via Package Center)
sudo synopkg uninstall syrviscore    # or use Package Center UI

# 2. Docker core stack + networks (safe if not running anything else on them)
docker rm -f traefik portainer cloudflared 2>/dev/null
docker network rm proxy syrvis-macvlan 2>/dev/null

# 3. Boot hooks + global symlink
sudo rm -f /usr/local/etc/rc.d/S99syrviscore.sh
sudo rm -f /usr/local/etc/rc.d/S99syrviscore-docker.sh   # legacy name
sudo rm -f /usr/local/bin/syrvis

# 4. Service data (BACK UP config/ + data/ first if it held anything real)
sudo rm -rf /volume*/syrviscore /volume*/syrviscore-dev

# 5. Dev + temp leftovers
rm -rf ~/syrviscore-devkit /tmp/syrviscore-* /tmp/syrviscore_*
```

Every command above targets only SyrvisCore-named resources.
