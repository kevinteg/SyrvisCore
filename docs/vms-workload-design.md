# VMs as a SyrvisCore workload (`vms.d`) — design

> Status: **design** (2026-07-24). Motivated by running Home Assistant OS as a
> managed VM on the NAS (home-tech design/10 + `pursuits/.../ha-interim-vmm.md`).
> Not yet implemented — this is the spec + a bounded first step. Needs on-NAS
> `synowebapi` validation before coding the adapter.

## 1. Goal

Make a **virtual machine just another declared workload**, alongside the core
tier (`stack.yaml`) and Layer 2 containers (`services.d/`): the same
load → plan → converge → health → seam machinery, a per-workload-type adapter
underneath. The first (and only planned) backend is **Synology VMM**; the first
guest is **HAOS**. This keeps the platform promise — *anything an adapter can do,
`ssh nas && syrvis …` can do* — true for VMs, not just containers.

## 2. The DSM control surface (what's actually possible)

Synology VMM is controlled through DSM's Web API family `SYNO.Virtualization.API.*`,
reachable **as root over SSH** via `synowebapi --exec` (no session/_sid juggling):

```sh
synowebapi --exec api=SYNO.Virtualization.API.Guest        version=1 method=list additional=true
synowebapi --exec api=SYNO.Virtualization.API.Guest        version=1 method=get  guest_name="Home Assistant"
synowebapi --exec api=SYNO.Virtualization.API.Guest.Action version=1 method=poweron  runner=admin guest_name="Home Assistant"
synowebapi --exec api=SYNO.Virtualization.API.Guest.Action version=1 method=shutdown runner=admin guest_name="Home Assistant"  # graceful ACPI
synowebapi --exec api=SYNO.Virtualization.API.Guest.Action version=1 method=poweroff runner=admin guest_name="Home Assistant"  # hard
```

**This is a lifecycle + inventory API, not a provisioning one.** Verified limits:

- **Create-from-image is NOT a reliable, documented API path.** VM birth stays a
  one-time manual GUI import (OVA/qcow2, set vCPU/RAM/disk, attach USB radio while
  powered off). SyrvisCore **adopts** an existing guest; it never creates one.
- **libvirt / `virsh` / `qemu` are NOT the surface.** VMM runs a proprietary
  QEMU/KVM stack behind its own daemon; the reference CLI (`scttfrdmn/syno-vm`)
  uses `synowebapi` "instead of libvirt". Driving qemu directly is unsupported and
  upgrade-fragile — don't.

⇒ **SyrvisCore owns the VM's lifecycle + resource envelope; it does not own the
VM's birth or its OS version.** That single fact shapes everything below.

## 3. Shape: a parallel `config/vms.d/`

Mirror `services_d.py` exactly (one validated `<name>.yaml`, filename == name,
per-file failure isolation, a strict `ALLOWED_TOP_LEVEL_KEYS` trust boundary, a
tolerant reader, dual-write of imperative edits). A **separate directory** (not a
`type:` field inside `services.d/`) is cleaner because a VM shares almost none of
the container schema.

```yaml
# config/vms.d/homeassistant.yaml   (filename == name)
name: homeassistant
type: vm
backend: synology-vmm          # the only adapter today; explicit for forward-compat
guest_name: "Home Assistant"   # EXACT VMM name — the adapter's handle
enabled: true                  # gates whether reconcile powers it on (== services.d)
critical: true                 # a down critical VM fails the reconcile verdict
autostart: true                # desired VMM boot-autostart policy

source:                        # PROVENANCE ONLY (create is manual); never a reconcile pin
  image: "https://github.com/home-assistant/operating-system/releases/download/<v>/haos_ova-<v>.qcow2.xz"
  sha256: "..."
resources: { vcpus: 2, memory_mb: 4096, disk_gb: 32 }   # drift-REPORTED, not auto-applied
passthrough:
  usb: ["10c4:ea60"]           # advisory; attach is a manual, powered-off op
health:
  power_state: running         # primary signal
  probe: { tcp: "192.168.8.50:8123" }   # optional: HA actually serving, not just booted
```

## 4. Reconcile action → DSM mechanism

| Action | Container today | **VM adapter (synowebapi)** |
|---|---|---|
| status | Docker SDK `.status` | `Guest.get` → normalize → running/stopped/shutting_down/unknown |
| start (declared, not running) | compose up | `Guest.Action poweron` |
| stop (`enabled:false` + running) | compose stop | `Guest.Action shutdown` (ACPI); `poweroff` after a timeout |
| add | materialize + start | **ADOPT-ONLY**: guest must already exist; if absent → a **`blocked`** action, never a silent create |
| replace (content differs) | recreate | **envelope drift → REPORT**; apply autostart via API; vcpu/mem/disk/passthrough need a powered-off guest → surface as an operator action, don't auto-mutate |
| prune | stop/remove/purge | **stop only** (`poweroff`); **never delete a VM** — a VM disk is not a reproducible compose project |

**VMs are adopt-first and non-destructive by construction** — the reconciler
converges only *power + autostart*, refuses create/delete, and has no
`prune_purge`.

## 5. Health, updates, seam

- **Health** (mirrors `critical` semantics): primary = `Guest.get` power_state ==
  running; optional secondary = a TCP/HTTP probe to the guest's LAN IP (distinguishes
  "kernel up" from "HA serving"). A `critical: true` VM that's down fails
  `reconcile`/`verify`, exactly like a critical container.
- **Updates**: HAOS self-updates OS/Core/Supervisor/add-ons OTA, so
  `source.image` is consumed only at import and kept as provenance. **VMs are
  EXCLUDED from `syrvis updates`** (no pinned-image diff); there is no
  `vm set-image`. SyrvisCore owns lifecycle; HAOS owns its own software.
- **Seam** (`seam/registry.py` — the generator re-renders shim+sudoers):
  `vm_list`/`vm_status` (read, no sudo), `vm_start`/`vm_stop`/`vm_restart`/`vm_adopt`
  (converge, sudo). Note `synowebapi` is **root-only**, so the privileged step is a
  single tightly-scoped sudoers entry; the operator picks a **declared `guest_name`**
  (gated to names present in `vms.d/`, like `secret set` gates to `services.d/`).
  **VM delete stays off the seam entirely** (break-glass GUI only).

## 6. Availability trade-off (reversing home-tech design/10)

design/10 put HA on a dedicated N100 (Tier A) to keep it independent of NAS
uptime. HAOS-on-NAS is **Tier B**: HA — and, because the Zigbee/Z-Wave stick is
USB-passed-through to the guest, **local radio control** — goes down whenever the
NAS does (DSM updates, volume repair, reboots). For a smart home driving locks and
alarms that's a real regression. It buys: one box instead of two, VMM VM snapshots
as a second backup line, SyrvisCore-managed declarative lifecycle, and the only
clean USB-radio passthrough DSM 7 offers. Proceed only if Tier B is accepted +
mitigated (low-impact maintenance windows; Zigbee bindings/Z-Wave associations
that keep working locally while HA is down; `critical: true` as the down-alarm).
If 24/7 HA is non-negotiable, keep the N100 — the VM workload type is still worth
building, just pointed at a future second node.

## 7. Minimal viable first step

Ship the smallest slice fully inside DSM's proven surface:

1. Owner **creates the HAOS VM once by hand** in VMM (no automation for birth).
2. Implement the **`synology-vmm` read + power adapter only** (`Guest.list`/`get`
   → normalized status; `Guest.Action` poweron/shutdown/poweroff).
3. `syrvis vm adopt homeassistant` → writes `vms.d/homeassistant.yaml` from the
   live guest (the `services_d.adopt` pattern).
4. `syrvis vm list/status/start/stop` + fold VMs into `verify`/health.
5. Add the six `vm_*` seam Commands; the generator emits shim+sudoers.

**Deferred** (unsupported by DSM or inherently powered-off/manual):
create-from-image, VM delete/purge, declarative passthrough attach, envelope
auto-apply, any image-bump/`updates` behavior.

## 8. Files this builds on

`services_d.py` (the reconcile template to mirror), `service_schema.py`
(the `ALLOWED_TOP_LEVEL_KEYS` trust-boundary pattern), `seam/registry.py`
(the six `vm_*` Commands), `service_manager._get_service_status` (status
normalization). Estate side: home-tech design/10 (HA placement) +
`pursuits/home-automation/projects/ha-interim-vmm.md` (the bridge project this
makes declarative).
