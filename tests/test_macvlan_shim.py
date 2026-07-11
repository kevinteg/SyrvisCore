"""
Tests for DsmOperations.ensure_macvlan_shim reconciliation.

The macvlan shim gives the host a route to the Traefik container (which lives on
a macvlan network and can't otherwise talk to its host). When the operator
changes TRAEFIK_IP via setup, the shim's assigned SHIM_IP and its /32 host route
must be reconciled -- otherwise the stale address/route lingers and host->Traefik
reachability stays broken until a reboot.

These tests fake `subprocess.run` (the real thing needs root + `ip`), dispatching
on the `ip ...` argv so each scenario returns realistic output.
"""

from unittest.mock import Mock

import pytest

from syrviscore.privileged_ops import DsmOperations

SHIM = "syrvis-shim"
INTERFACE = "ovs_eth0"


def _addr_output(ip):
    """Realistic-ish `ip addr show syrvis-shim` output carrying one inet addr."""
    return (
        f"42: {SHIM}@{INTERFACE}: <BROADCAST,MULTICAST,UP,LOWER_UP> mtu 1500\n"
        f"    link/ether 02:42:ac:11:00:02 brd ff:ff:ff:ff:ff:ff\n"
        f"    inet {ip}/32 scope global {SHIM}\n"
        f"       valid_lft forever preferred_lft forever\n"
    )


def _route_output(traefik_ip, dev=SHIM):
    """Realistic `ip route show <traefik_ip>/32` output."""
    return f"{traefik_ip} dev {dev} scope link\n"


class _FakeIp:
    """
    Dispatches faked `ip` invocations based on argv and records calls.

    State (the shim's currently-assigned address, whether the interface exists,
    and the current route device) is configurable so each test can model a
    starting condition. Mutating verbs (link add/del, addr add, route add/del)
    update the recorded state so assertions can inspect the resulting config.
    """

    def __init__(self, *, exists, current_shim_ip=None, route_dev=None):
        self.exists = exists
        self.current_shim_ip = current_shim_ip
        self.route_dev = route_dev
        self.calls = []

    def __call__(self, argv, *args, **kwargs):
        self.calls.append(list(argv))
        # argv always starts with "ip"; branch on the subcommand.
        sub = argv[1:]

        if sub[:2] == ["link", "show"]:
            rc = 0 if self.exists else 1
            return Mock(returncode=rc, stdout="", stderr="")

        if sub[:2] == ["addr", "show"]:
            stdout = _addr_output(self.current_shim_ip) if self.current_shim_ip else ""
            return Mock(returncode=0, stdout=stdout, stderr="")

        if sub[:2] == ["route", "show"]:
            traefik_ip = sub[2].split("/")[0]
            stdout = _route_output(traefik_ip, self.route_dev) if self.route_dev else ""
            return Mock(returncode=0, stdout=stdout, stderr="")

        if sub[:2] == ["link", "add"]:
            self.exists = True
            return Mock(returncode=0, stdout="", stderr="")

        if sub[:2] == ["link", "del"]:
            self.exists = False
            self.current_shim_ip = None
            self.route_dev = None
            return Mock(returncode=0, stdout="", stderr="")

        if sub[:2] == ["link", "set"]:
            return Mock(returncode=0, stdout="", stderr="")

        if sub[:2] == ["addr", "add"]:
            self.current_shim_ip = sub[2].split("/")[0]
            return Mock(returncode=0, stdout="", stderr="")

        if sub[:2] == ["route", "add"]:
            self.route_dev = sub[-1]  # ... dev <shim_name>
            return Mock(returncode=0, stdout="", stderr="")

        if sub[:2] == ["route", "del"]:
            self.route_dev = None
            return Mock(returncode=0, stdout="", stderr="")

        raise AssertionError(f"unexpected ip invocation: {argv}")

    def argv_list(self):
        """The recorded argv lists, one per subprocess.run call."""
        return self.calls

    def did(self, *prefix):
        """True if any recorded call starts with the given argv prefix."""
        return any(call[: len(prefix)] == list(prefix) for call in self.calls)


@pytest.fixture
def patch_ip(monkeypatch):
    def _install(fake):
        monkeypatch.setattr("syrviscore.privileged_ops.subprocess.run", fake)
        return fake

    return _install


class TestEnsureMacvlanShimReconcile:
    def test_matching_ip_and_route_no_churn(self, patch_ip):
        """(a) Existing shim already at the desired IP + route: idempotent no-op."""
        traefik_ip, shim_ip = "192.168.1.50", "192.168.1.51"
        fake = patch_ip(_FakeIp(exists=True, current_shim_ip=shim_ip, route_dev=SHIM))

        ok, msg = DsmOperations().ensure_macvlan_shim(INTERFACE, traefik_ip, shim_ip)

        assert ok
        assert "already configured" in msg
        # No mutation of any kind.
        assert not fake.did("ip", "link", "del")
        assert not fake.did("ip", "link", "add")
        assert not fake.did("ip", "addr", "add")
        assert not fake.did("ip", "route", "add")
        assert not fake.did("ip", "route", "del")

    def test_stale_ip_triggers_teardown_and_recreate(self, patch_ip):
        """(b) Shim exists but with the OLD IP: delete + recreate at the new IP + route."""
        traefik_ip, shim_ip = "192.168.1.80", "192.168.1.81"
        old_shim_ip = "192.168.1.51"  # left over from a previous TRAEFIK_IP
        fake = patch_ip(_FakeIp(exists=True, current_shim_ip=old_shim_ip, route_dev=SHIM))

        ok, msg = DsmOperations().ensure_macvlan_shim(INTERFACE, traefik_ip, shim_ip)

        assert ok
        assert "created" in msg
        # Drifted IP -> the stale interface is torn down...
        assert fake.did("ip", "link", "del", SHIM)
        # ...and rebuilt with the new address + route.
        assert fake.did("ip", "link", "add", SHIM)
        assert fake.did("ip", "addr", "add", f"{shim_ip}/32", "dev", SHIM)
        assert fake.did("ip", "route", "add", f"{traefik_ip}/32", "dev", SHIM)
        # End state reflects the desired values.
        assert fake.current_shim_ip == shim_ip
        assert fake.route_dev == SHIM

    def test_correct_ip_missing_route_reconciles_route_only(self, patch_ip):
        """(c) Right IP, but route absent: reconcile the route without tearing down."""
        traefik_ip, shim_ip = "192.168.1.50", "192.168.1.51"
        fake = patch_ip(_FakeIp(exists=True, current_shim_ip=shim_ip, route_dev=None))

        ok, msg = DsmOperations().ensure_macvlan_shim(INTERFACE, traefik_ip, shim_ip)

        assert ok
        assert "route reconciled" in msg
        # No teardown/recreate when only the route drifted.
        assert not fake.did("ip", "link", "del")
        assert not fake.did("ip", "link", "add")
        assert not fake.did("ip", "addr", "add")
        # Route is (re)added on the shim.
        assert fake.did("ip", "route", "add", f"{traefik_ip}/32", "dev", SHIM)
        assert fake.route_dev == SHIM

    def test_correct_ip_stale_route_device_reconciles_route(self, patch_ip):
        """Right IP, but route points at the WRONG device: delete + re-add on shim."""
        traefik_ip, shim_ip = "192.168.1.50", "192.168.1.51"
        fake = patch_ip(_FakeIp(exists=True, current_shim_ip=shim_ip, route_dev="eth1"))

        ok, msg = DsmOperations().ensure_macvlan_shim(INTERFACE, traefik_ip, shim_ip)

        assert ok
        assert "route reconciled" in msg
        assert fake.did("ip", "route", "del", f"{traefik_ip}/32")
        assert fake.did("ip", "route", "add", f"{traefik_ip}/32", "dev", SHIM)
        assert not fake.did("ip", "link", "del")

    def test_missing_interface_creates_from_scratch(self, patch_ip):
        """No shim yet: existing create path runs (link add, addr add, up, route add)."""
        traefik_ip, shim_ip = "192.168.1.50", "192.168.1.51"
        fake = patch_ip(_FakeIp(exists=False))

        ok, msg = DsmOperations().ensure_macvlan_shim(INTERFACE, traefik_ip, shim_ip)

        assert ok
        assert "created" in msg
        assert fake.did(
            "ip", "link", "add", SHIM, "link", INTERFACE, "type", "macvlan", "mode", "bridge"
        )
        assert fake.did("ip", "addr", "add", f"{shim_ip}/32", "dev", SHIM)
        assert fake.did("ip", "link", "set", SHIM, "up")
        assert fake.did("ip", "route", "add", f"{traefik_ip}/32", "dev", SHIM)
        # Never tears anything down on a clean create.
        assert not fake.did("ip", "link", "del")
