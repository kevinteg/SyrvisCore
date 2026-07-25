"""Tests for the vms.d workload type + the Synology VMM adapter (mocked synowebapi)."""

import json

import pytest

from syrviscore import vms
from syrviscore.vms import SynologyVmmAdapter, VmDefinition, VmError, VmManager


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------
def _decl(**over):
    base = {"name": "homeassistant", "guest_name": "Home Assistant"}
    base.update(over)
    return base


def test_valid_declaration_defaults():
    vm = VmDefinition.from_dict(_decl(critical=True))
    assert vm.name == "homeassistant" and vm.guest_name == "Home Assistant"
    assert vm.type == "vm" and vm.backend == "synology-vmm"
    assert vm.enabled is True and vm.critical is True and vm.autostart is True


def test_guest_name_may_contain_spaces_but_not_control_chars():
    assert VmDefinition.from_dict(_decl(guest_name="My VM 2")).guest_name == "My VM 2"
    with pytest.raises(VmError):
        VmDefinition.from_dict(_decl(guest_name="bad\nname"))
    with pytest.raises(VmError):
        VmDefinition.from_dict(_decl(guest_name=""))


def test_rejects_unknown_keys_and_bad_fields():
    with pytest.raises(VmError):
        VmDefinition.from_dict(_decl(bogus=1))
    with pytest.raises(VmError):
        VmDefinition.from_dict(_decl(name="Bad Name"))  # space not allowed in decl name
    with pytest.raises(VmError):
        VmDefinition.from_dict(_decl(type="container"))
    with pytest.raises(VmError):
        VmDefinition.from_dict(_decl(backend="proxmox"))
    with pytest.raises(VmError):
        VmDefinition.from_dict(_decl(enabled="yes"))  # not a bool


def test_roundtrip_to_dict():
    vm = VmDefinition.from_dict(_decl(critical=True, resources={"vcpus": 2, "memory_mb": 4096}))
    again = VmDefinition.from_dict(vm.to_dict())
    assert again.to_dict() == vm.to_dict()


# ---------------------------------------------------------------------------
# Adapter (injected runner — no synowebapi needed)
# ---------------------------------------------------------------------------
def _runner(response):
    calls = []

    def run(argv):
        calls.append(argv)
        return (response if isinstance(response, str) else json.dumps(response)), 0

    run.calls = calls
    return run


def test_status_normalization():
    n = SynologyVmmAdapter.normalize_status
    assert n("running") == "running"
    assert n("shutdown") == "stopped"
    assert n("Shutting_Down") == "transition"
    assert n("wat") == "unknown"
    assert n(None) == "unknown"


def test_power_builds_exact_argv_no_shell():
    run = _runner({"success": True, "data": {}})
    SynologyVmmAdapter(run=run).power("Home Assistant", "poweron")
    assert run.calls[-1] == [
        "/usr/syno/bin/synowebapi",
        "--exec",
        "api=SYNO.Virtualization.API.Guest.Action",
        "version=1",
        "method=poweron",
        "runner=admin",
        "guest_name=Home Assistant",  # a space is one argv element — never shell-split
    ]


def test_power_rejects_unknown_action():
    with pytest.raises(VmError):
        SynologyVmmAdapter(run=_runner({"success": True, "data": {}})).power("X", "nuke")


def test_get_guest_parses_both_shapes():
    flat = SynologyVmmAdapter(
        run=_runner({"success": True, "data": {"guest_name": "X", "status": "running"}})
    )
    assert flat.get_guest("X")["status"] == "running"
    nested = SynologyVmmAdapter(
        run=_runner({"success": True, "data": {"guest": {"status": "shutdown"}}})
    )
    assert nested.get_guest("X")["status"] == "shutdown"


def test_exec_raises_on_api_failure_and_bad_json():
    with pytest.raises(VmError):
        SynologyVmmAdapter(run=_runner({"success": False, "error": {"code": 401}})).list_guests()
    with pytest.raises(VmError):
        SynologyVmmAdapter(run=_runner("not json")).list_guests()


# ---------------------------------------------------------------------------
# Manager (fake adapter + a real vms.d on disk)
# ---------------------------------------------------------------------------
class FakeAdapter:
    def __init__(self, guests):
        self.guests = guests
        self.actions = []

    def list_guests(self):
        return list(self.guests)

    def get_guest(self, gn):
        return next((g for g in self.guests if g.get("guest_name") == gn), None)

    def normalize_status(self, raw):
        return SynologyVmmAdapter.normalize_status(raw)

    def power(self, gn, action):
        self.actions.append((gn, action))


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / "syrviscore"
    (h / "config" / "vms.d").mkdir(parents=True)
    monkeypatch.setenv("SYRVIS_HOME", str(h))
    return h


def _write(home, name, **over):
    import yaml

    data = {"name": name, "guest_name": over.pop("guest_name", "Home Assistant")}
    data.update(over)
    (home / "config" / "vms.d" / (name + ".yaml")).write_text(yaml.safe_dump(data))


def test_load_declarations_skips_broken(home):
    _write(home, "homeassistant", critical=True)
    (home / "config" / "vms.d" / "broken.yaml").write_text("name: Bad Name\nguest_name: x\n")
    decls = vms.load_vm_declarations(home)
    assert [d.name for d in decls] == ["homeassistant"]  # broken one skipped


def test_manager_list_joins_live_power(home):
    _write(home, "homeassistant", critical=True, guest_name="Home Assistant")
    _write(home, "other", guest_name="Other VM")
    mgr = VmManager(
        home=home, adapter=FakeAdapter([{"guest_name": "Home Assistant", "status": "running"}])
    )
    rows = {r["name"]: r for r in mgr.list()}
    assert rows["homeassistant"]["power"] == "running" and rows["homeassistant"]["critical"] is True
    assert rows["other"]["power"] == "not_found"  # declared but not present in VMM


def test_manager_start_stop_restart_map_to_power_actions(home):
    _write(home, "homeassistant", guest_name="Home Assistant")
    fake = FakeAdapter([{"guest_name": "Home Assistant", "status": "shutdown"}])
    mgr = VmManager(home=home, adapter=fake)
    mgr.start("homeassistant")
    mgr.stop("homeassistant")
    mgr.stop("homeassistant", hard=True)
    assert fake.actions == [
        ("Home Assistant", "poweron"),
        ("Home Assistant", "shutdown"),
        ("Home Assistant", "poweroff"),
    ]


def test_manager_status_not_found_and_unknown_name(home):
    _write(home, "homeassistant", guest_name="Home Assistant")
    mgr = VmManager(home=home, adapter=FakeAdapter([]))  # guest not in VMM
    st = mgr.status("homeassistant")
    assert st.power == "not_found" and "import it first" in st.error
    with pytest.raises(VmError):
        mgr.status("nonexistent")


def test_verdict_critical_down_fails(home):
    _write(home, "homeassistant", critical=True, guest_name="Home Assistant")
    down = VmManager(
        home=home, adapter=FakeAdapter([{"guest_name": "Home Assistant", "status": "shutdown"}])
    )
    v = down.verdict()
    assert v["ok"] is False and v["failures"][0]["name"] == "homeassistant"
    up = VmManager(
        home=home, adapter=FakeAdapter([{"guest_name": "Home Assistant", "status": "running"}])
    )
    assert up.verdict()["ok"] is True


def test_adopt_writes_declaration_from_live_guest(home):
    fake = FakeAdapter(
        [{"guest_name": "Home Assistant", "status": "running", "vcpu_num": 2, "ram_size": 4096}]
    )
    mgr = VmManager(home=home, adapter=fake)
    path = mgr.adopt("Home Assistant")
    assert path.name == "home-assistant.yaml"  # "Home Assistant" → safe slug
    vm = VmDefinition.from_dict(__import__("yaml").safe_load(path.read_text()))
    assert vm.guest_name == "Home Assistant" and vm.resources == {"vcpus": 2, "memory_mb": 4096}


def test_adopt_refuses_absent_guest(home):
    with pytest.raises(VmError):
        VmManager(home=home, adapter=FakeAdapter([])).adopt("Ghost VM")
