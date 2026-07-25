"""VMs as a SyrvisCore workload — the `vms.d` declaration + the Synology VMM adapter.

A virtual machine is *just another declared workload* alongside the core tier and
Layer 2 containers: one validated ``config/vms.d/<name>.yaml`` per VM, the same
load → status → converge → health model. The (only) backend is **Synology VMM**,
driven through DSM's ``synowebapi --exec api=SYNO.Virtualization.API.Guest*`` —
which is a **lifecycle + inventory** API, not a provisioning one. So the design is
**adopt-first and non-destructive**: SyrvisCore owns power + autostart + health;
it never creates or deletes a VM, and the self-updating guest OS (e.g. HAOS) owns
its own software (VMs are excluded from ``syrvis updates``). Full rationale:
``docs/vms-workload-design.md``.

The declaration `name` (safe charset) is the operator/seam handle; the VMM
`guest_name` (a display name, may contain spaces) stays internal — the CLI/seam
never take a raw guest string. Kept import-light and Python 3.8-clean.
"""

import json
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from .errors import SyrvisError
from .service_schema import NAME_RE

VM_SCHEMA_VERSION = 1

VM_ALLOWED_KEYS = frozenset(
    {
        "name",
        "type",
        "backend",
        "guest_name",
        "enabled",
        "critical",
        "autostart",
        "source",  # provenance only (create is manual) — never a reconcile pin
        "resources",  # drift-reported, not auto-applied
        "passthrough",  # advisory; attach is a manual powered-off op
        "health",
    }
)
VM_TYPES = frozenset({"vm"})
VM_BACKENDS = frozenset({"synology-vmm"})

# Normalized power states the adapter maps VMM's raw status onto.
POWER_RUNNING = "running"
POWER_STOPPED = "stopped"
POWER_TRANSITION = "transition"
POWER_UNKNOWN = "unknown"

_VMM_STATUS_MAP = {
    "running": POWER_RUNNING,
    "shutdown": POWER_STOPPED,
    "stopped": POWER_STOPPED,
    "off": POWER_STOPPED,
    "poweroff": POWER_STOPPED,
    "booting": POWER_TRANSITION,
    "shutting_down": POWER_TRANSITION,
    "shutting down": POWER_TRANSITION,
    "suspending": POWER_TRANSITION,
    "suspended": POWER_TRANSITION,
    "moving": POWER_TRANSITION,
    "importing": POWER_TRANSITION,
}


class VmError(SyrvisError):
    """An invalid VM declaration or a VMM backend failure."""

    code = "vm_invalid"


def _no_control(value: str) -> bool:
    return not any(ord(c) < 0x20 or ord(c) == 0x7F for c in value)


@dataclass
class VmDefinition:
    """A declared VM workload (``config/vms.d/<name>.yaml``)."""

    name: str
    guest_name: str
    type: str = "vm"
    backend: str = "synology-vmm"
    enabled: bool = True
    critical: bool = False
    autostart: bool = True
    source: Dict[str, Any] = field(default_factory=dict)
    resources: Dict[str, Any] = field(default_factory=dict)
    passthrough: Dict[str, Any] = field(default_factory=dict)
    health: Dict[str, Any] = field(default_factory=dict)
    source_path: Optional[Path] = None

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "VmDefinition":
        if not isinstance(data, dict):
            raise VmError("VM definition must be a mapping")
        unknown = set(data.keys()) - VM_ALLOWED_KEYS
        if unknown:
            raise VmError(
                "Unknown keys in VM definition: {} (allowed: {})".format(
                    ", ".join(sorted(unknown)), ", ".join(sorted(VM_ALLOWED_KEYS))
                )
            )
        for req in ("name", "guest_name"):
            if req not in data:
                raise VmError("VM definition missing required field: {}".format(req))

        name = data["name"]
        if not isinstance(name, str) or not NAME_RE.fullmatch(name):
            raise VmError(
                "Invalid VM name {!r}: must match [a-z0-9][a-z0-9_-]{{0,63}}".format(name)
            )

        guest_name = data["guest_name"]
        if (
            not isinstance(guest_name, str)
            or not guest_name.strip()
            or len(guest_name) > 64
            or not _no_control(guest_name)
        ):
            raise VmError(
                "Invalid guest_name {!r}: 1-64 printable chars (the exact VMM name)".format(
                    guest_name
                )
            )

        vm_type = data.get("type", "vm")
        if vm_type not in VM_TYPES:
            raise VmError("Invalid type {!r}: only 'vm' is supported".format(vm_type))
        backend = data.get("backend", "synology-vmm")
        if backend not in VM_BACKENDS:
            raise VmError("Invalid backend {!r}: only 'synology-vmm' is supported".format(backend))

        for flag in ("enabled", "critical", "autostart"):
            if flag in data and not isinstance(data[flag], bool):
                raise VmError("{} must be a boolean".format(flag))
        for m in ("source", "resources", "passthrough", "health"):
            if m in data and not isinstance(data[m], dict):
                raise VmError("{} must be a mapping".format(m))

        return cls(
            name=name,
            guest_name=guest_name,
            type=vm_type,
            backend=backend,
            enabled=bool(data.get("enabled", True)),
            critical=bool(data.get("critical", False)),
            autostart=bool(data.get("autostart", True)),
            source=dict(data.get("source", {})),
            resources=dict(data.get("resources", {})),
            passthrough=dict(data.get("passthrough", {})),
            health=dict(data.get("health", {})),
        )

    def to_dict(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {
            "name": self.name,
            "type": self.type,
            "backend": self.backend,
            "guest_name": self.guest_name,
            "enabled": self.enabled,
            "critical": self.critical,
            "autostart": self.autostart,
        }
        for m in ("source", "resources", "passthrough", "health"):
            val = getattr(self, m)
            if val:
                out[m] = val
        return out


def get_vms_dir(home: Optional[Path] = None) -> Path:
    from . import paths

    base = Path(home) if home is not None else paths.get_syrvis_home()
    return base / "config" / "vms.d"


def load_vm_declarations(home: Optional[Path] = None) -> List[VmDefinition]:
    """Every valid ``vms.d/<name>.yaml``, sorted by name. Per-file failure
    isolation: a broken file is skipped, never fatal (mirrors services.d)."""
    import yaml

    try:
        vms_dir = get_vms_dir(home)
    except Exception:  # noqa: BLE001 - no home context
        return []
    if not vms_dir.exists():
        return []
    out: List[VmDefinition] = []
    for path in sorted(vms_dir.glob("*.yaml"), key=lambda p: p.name):
        try:
            data = yaml.safe_load(path.read_text()) or {}
            vm = VmDefinition.from_dict(data)
            vm.source_path = path
            out.append(vm)
        except Exception:  # noqa: BLE001 - broken declaration: skip
            continue
    return out


# ---------------------------------------------------------------------------
# The Synology VMM adapter (synowebapi) — the ONLY privileged surface.
# ---------------------------------------------------------------------------
class SynologyVmmAdapter:
    """Drives Synology VMM via ``synowebapi --exec`` (root-only).

    ``run`` is injectable so the adapter is fully testable without a NAS: it
    receives the argv list and returns (stdout, returncode). In production it
    defaults to a subprocess call to ``synowebapi`` (assumes the caller is root —
    the operator seam invokes vm verbs under ``sudo -n``).
    """

    GUEST_API = "SYNO.Virtualization.API.Guest"
    ACTION_API = "SYNO.Virtualization.API.Guest.Action"

    def __init__(
        self,
        binary: str = "/usr/syno/bin/synowebapi",
        runner: str = "admin",
        run: Optional[Callable[[List[str]], Any]] = None,
        timeout_s: int = 30,
    ) -> None:
        self._binary = binary
        self._runner = runner
        self._timeout = timeout_s
        self._run = run or self._subprocess_run

    def _subprocess_run(self, argv: List[str]):
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=self._timeout)
        return proc.stdout, proc.returncode

    def _exec(self, api: str, method: str, **params: str) -> Dict[str, Any]:
        argv = [
            self._binary,
            "--exec",
            "api={}".format(api),
            "version=1",
            "method={}".format(method),
        ]
        for key, value in params.items():
            argv.append("{}={}".format(key, value))
        try:
            stdout, rc = self._run(argv)
        except Exception as exc:  # noqa: BLE001 - synowebapi missing / timeout
            raise VmError("synowebapi call failed: {}".format(exc))
        try:
            data = json.loads(stdout or "{}")
        except ValueError:
            raise VmError("synowebapi returned non-JSON (rc={})".format(rc))
        if not data.get("success", False):
            err = data.get("error") or {}
            raise VmError("VMM API error on {}.{}: {}".format(api, method, err or "unknown"))
        return data.get("data") or {}

    @staticmethod
    def normalize_status(raw: Optional[str]) -> str:
        if not raw:
            return POWER_UNKNOWN
        return _VMM_STATUS_MAP.get(str(raw).strip().lower(), POWER_UNKNOWN)

    def list_guests(self) -> List[Dict[str, Any]]:
        data = self._exec(self.GUEST_API, "list", additional="true")
        guests = data.get("guests")
        if guests is None and isinstance(data, dict):
            guests = data.get("guest") or []
        return list(guests or [])

    def get_guest(self, guest_name: str) -> Optional[Dict[str, Any]]:
        data = self._exec(self.GUEST_API, "get", guest_name=guest_name)
        # DSM returns the guest either directly in `data` or under `data.guest`.
        guest = data.get("guest") if isinstance(data.get("guest"), dict) else data
        return guest or None

    def power(self, guest_name: str, action: str) -> None:
        if action not in ("poweron", "shutdown", "poweroff"):
            raise VmError("unknown VMM power action: {}".format(action))
        self._exec(self.ACTION_API, action, runner=self._runner, guest_name=guest_name)


# ---------------------------------------------------------------------------
# The manager — declarations + adapter, mirroring ServiceManager's shape.
# ---------------------------------------------------------------------------
@dataclass
class VmStatus:
    name: str
    guest_name: str
    declared: bool
    enabled: bool
    critical: bool
    power: str  # normalized power state, or "unknown"/"not_found"
    error: Optional[str] = None


class VmManager:
    def __init__(
        self, home: Optional[Path] = None, adapter: Optional[SynologyVmmAdapter] = None
    ) -> None:
        self._home = home
        self._adapter = adapter or SynologyVmmAdapter()

    def declarations(self) -> List[VmDefinition]:
        return load_vm_declarations(self._home)

    def _live_by_name(self) -> Dict[str, Dict[str, Any]]:
        """Live guests keyed by guest_name (best-effort; {} on backend failure)."""
        try:
            return {
                g.get("guest_name") or g.get("name") or "": g for g in self._adapter.list_guests()
            }
        except VmError:
            return {}

    def list(self) -> List[Dict[str, Any]]:
        """Declared VMs joined with their live power state (best-effort)."""
        live = self._live_by_name()
        out: List[Dict[str, Any]] = []
        for vm in self.declarations():
            g = live.get(vm.guest_name)
            power = self._adapter.normalize_status(g.get("status")) if g else "not_found"
            out.append(
                {
                    "name": vm.name,
                    "guest_name": vm.guest_name,
                    "enabled": vm.enabled,
                    "critical": vm.critical,
                    "autostart": vm.autostart,
                    "power": power,
                }
            )
        return out

    def _require(self, name: str) -> VmDefinition:
        for vm in self.declarations():
            if vm.name == name:
                return vm
        raise VmError("no VM declared with name {!r} (config/vms.d/{}.yaml)".format(name, name))

    def status(self, name: str) -> VmStatus:
        vm = self._require(name)
        try:
            guest = self._adapter.get_guest(vm.guest_name)
        except VmError as exc:
            return VmStatus(
                vm.name, vm.guest_name, True, vm.enabled, vm.critical, "unknown", str(exc)
            )
        if guest is None:
            return VmStatus(
                vm.name,
                vm.guest_name,
                True,
                vm.enabled,
                vm.critical,
                "not_found",
                "guest {!r} not present in VMM (import it first)".format(vm.guest_name),
            )
        power = self._adapter.normalize_status(guest.get("status"))
        return VmStatus(vm.name, vm.guest_name, True, vm.enabled, vm.critical, power)

    def start(self, name: str) -> str:
        vm = self._require(name)
        self._adapter.power(vm.guest_name, "poweron")
        return "powering on {} ({})".format(vm.name, vm.guest_name)

    def stop(self, name: str, hard: bool = False) -> str:
        vm = self._require(name)
        self._adapter.power(vm.guest_name, "poweroff" if hard else "shutdown")
        return "{} {} ({})".format("force-off" if hard else "shutting down", vm.name, vm.guest_name)

    def restart(self, name: str) -> str:
        self.stop(name)
        self.start(name)
        return "restarting {}".format(name)

    def adopt(
        self, guest_name: str, name: Optional[str] = None, home: Optional[Path] = None
    ) -> Path:
        """Write a ``vms.d/<name>.yaml`` from an EXISTING VMM guest (adopt-first;
        never creates the VM). ``name`` defaults to a safe slug of the guest name.
        """
        guest = self._adapter.get_guest(guest_name)
        if guest is None:
            raise VmError("guest {!r} not found in VMM — nothing to adopt".format(guest_name))
        slug = name or _slug(guest_name)
        if not NAME_RE.fullmatch(slug):
            raise VmError("derived name {!r} is invalid; pass --name explicitly".format(slug))
        vm = VmDefinition(
            name=slug,
            guest_name=guest_name,
            enabled=True,
            critical=False,
            autostart=bool(guest.get("autorun") or guest.get("autostart") or False),
            resources={
                k: guest[v]
                for k, v in (("vcpus", "vcpu_num"), ("memory_mb", "ram_size"))
                if guest.get(v) is not None
            },
        )
        return write_declaration(vm, home if home is not None else self._home)

    def verdict(self) -> Dict[str, Any]:
        """Health roll-up: a critical, enabled VM that isn't running fails.

        Returns ``{ok, checked, failures:[{name, power}]}``. Best-effort — a
        backend failure marks affected criticals as failing, not the whole verdict.
        """
        failures = []
        checked = 0
        for row in self.list():
            if not row["enabled"]:
                continue
            checked += 1
            if row["critical"] and row["power"] != POWER_RUNNING:
                failures.append({"name": row["name"], "power": row["power"]})
        return {"ok": not failures, "checked": checked, "failures": failures}


def _slug(guest_name: str) -> str:
    s = "".join(c if (c.isalnum() or c in "-_") else "-" for c in guest_name.strip().lower())
    s = s.strip("-_") or "vm"
    return s[:64]


def write_declaration(vm: VmDefinition, home: Optional[Path] = None) -> Path:
    import yaml

    vms_dir = get_vms_dir(home)
    vms_dir.mkdir(parents=True, exist_ok=True)
    path = vms_dir / "{}.yaml".format(vm.name)
    path.write_text(yaml.safe_dump(vm.to_dict(), default_flow_style=False, sort_keys=False))
    try:
        path.chmod(0o644)
    except OSError:
        pass
    return path
