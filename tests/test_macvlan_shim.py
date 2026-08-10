"""
Tests for DsmOperations.ensure_macvlan_shim reconciliation.

The macvlan shim gives the host a route to the Traefik container (which lives on
a macvlan network and can't otherwise talk to its host). When the operator
changes TRAEFIK_IP via setup, the shim's assigned SHIM_IP and its /32 host route
must be reconciled -- otherwise the stale address/route lingers and host->Traefik
reachability stays broken until a reboot.

These tests fake `subprocess.run` (the real thing needs root + `ip`), dispatching
on the `ip ...` argv so each scenario returns realistic output.

They also cover the shim's DSM ifcfg file (`TestShimIfcfgFile`): DSM auto-stubs
`/etc/sysconfig/network-scripts/ifcfg-syrvis-shim` with a lone BOOTPROTO line and
then log-floods every ~60s, so every ensure pass rewrites the full key set.
"""

import os
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


EXPECTED_IFCFG = (
    "DEVICE=syrvis-shim\n"
    "BOOTPROTO=static\n"
    "ONBOOT=no\n"
    "IPADDR=192.168.1.51\n"
    "NETMASK=255.255.255.255\n"
)


@pytest.fixture
def patch_ip(monkeypatch):
    def _install(fake):
        monkeypatch.setattr("syrviscore.privileged_ops.subprocess.run", fake)
        return fake

    return _install


@pytest.fixture(autouse=True)
def ifcfg_dir(tmp_path, monkeypatch):
    """Redirect DSM's ifcfg directory into tmp for EVERY test in this module.

    Autouse, and deliberately NOT created: the default state is "this host has
    no ifcfg tree" (the skip path), which keeps the reconcile tests focused and
    makes it impossible for any test here to write to the real /etc. Tests that
    exercise the file call ``ifcfg_dir.mkdir()`` first.
    """
    d = tmp_path / "network-scripts"
    monkeypatch.setattr("syrviscore.privileged_ops.NETWORK_SCRIPTS_DIR", d)
    return d


@pytest.fixture
def ifcfg_path(ifcfg_dir):
    """An existing ifcfg directory + the path the shim's file should land at."""
    ifcfg_dir.mkdir()
    return ifcfg_dir / f"ifcfg-{SHIM}"


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


class TestShimIfcfgFile:
    """The shim's DSM ifcfg file (incident 2026-08-10).

    DSM's health poller reads `/etc/sysconfig/network-scripts/ifcfg-<iface>` for
    every interface it sees. Meeting our macvlan shim, it writes a stub holding
    only `BOOTPROTO=static`, then fails to read the keys it wants and logs
    `SystemHealth.cpp:87 Failed to get interface: [syrvis-shim] information`
    every ~60s. `ensure_macvlan_shim` therefore writes the full key set on every
    pass -- a one-shot write at creation would be reverted by the next DSM update.
    """

    def test_fresh_write_when_shim_is_created(self, patch_ip, ifcfg_path):
        """No shim, no file: the create path lays down the full key set."""
        traefik_ip, shim_ip = "192.168.1.50", "192.168.1.51"
        patch_ip(_FakeIp(exists=False))

        ok, msg = DsmOperations().ensure_macvlan_shim(INTERFACE, traefik_ip, shim_ip)

        assert ok
        assert ifcfg_path.read_text() == EXPECTED_IFCFG
        assert "ifcfg created" in msg

    def test_dsm_stub_is_enriched(self, patch_ip, ifcfg_path):
        """DSM's one-line auto-stub is replaced with the full key set."""
        ifcfg_path.write_text("BOOTPROTO=static\n")  # exactly what DSM leaves
        traefik_ip, shim_ip = "192.168.1.50", "192.168.1.51"
        fake = patch_ip(_FakeIp(exists=True, current_shim_ip=shim_ip, route_dev=SHIM))

        ok, msg = DsmOperations().ensure_macvlan_shim(INTERFACE, traefik_ip, shim_ip)

        assert ok
        assert ifcfg_path.read_text() == EXPECTED_IFCFG
        assert "ifcfg updated" in msg
        # Enriching the file must not disturb the (already correct) interface.
        assert not fake.did("ip", "link", "del")
        assert not fake.did("ip", "addr", "add")

    def test_matching_file_is_not_rewritten(self, patch_ip, ifcfg_path):
        """Idempotent: a correct file is left byte- and inode-identical."""
        ifcfg_path.write_text(EXPECTED_IFCFG)
        before = os.stat(str(ifcfg_path))
        traefik_ip, shim_ip = "192.168.1.50", "192.168.1.51"
        patch_ip(_FakeIp(exists=True, current_shim_ip=shim_ip, route_dev=SHIM))

        ok, msg = DsmOperations().ensure_macvlan_shim(INTERFACE, traefik_ip, shim_ip)

        after = os.stat(str(ifcfg_path))
        assert ok
        assert ifcfg_path.read_text() == EXPECTED_IFCFG
        # No churn: the atomic writer would have replaced the inode.
        assert after.st_ino == before.st_ino
        assert after.st_mtime_ns == before.st_mtime_ns
        # A no-op pass says nothing about the file.
        assert "ifcfg" not in msg

    def test_extra_keys_are_replaced_not_merged(self, patch_ip, ifcfg_path):
        """Documented choice: the file is declared state, rendered in full.

        Hand-added keys are dropped rather than merged -- same philosophy as the
        boot hook (render -> compare -> rewrite on any drift). Keys that must
        survive belong in `render_shim_ifcfg`, not in a hand edit.
        """
        ifcfg_path.write_text(EXPECTED_IFCFG + "MTU=1500\nUSERCTL=no\n")
        traefik_ip, shim_ip = "192.168.1.50", "192.168.1.51"
        patch_ip(_FakeIp(exists=True, current_shim_ip=shim_ip, route_dev=SHIM))

        ok, msg = DsmOperations().ensure_macvlan_shim(INTERFACE, traefik_ip, shim_ip)

        assert ok
        assert ifcfg_path.read_text() == EXPECTED_IFCFG
        assert "MTU" not in ifcfg_path.read_text()
        assert "ifcfg updated" in msg

    def test_ip_drift_rewrites_ipaddr(self, patch_ip, ifcfg_path):
        """A recreated shim (new SHIM_IP) carries the new IPADDR into the file."""
        ifcfg_path.write_text(EXPECTED_IFCFG)  # holds the OLD 192.168.1.51
        traefik_ip, shim_ip = "192.168.1.80", "192.168.1.81"
        patch_ip(_FakeIp(exists=True, current_shim_ip="192.168.1.51", route_dev=SHIM))

        ok, _ = DsmOperations().ensure_macvlan_shim(INTERFACE, traefik_ip, shim_ip)

        assert ok
        assert "IPADDR=192.168.1.81\n" in ifcfg_path.read_text()
        assert "192.168.1.51" not in ifcfg_path.read_text()

    def test_route_only_reconcile_still_writes_the_file(self, patch_ip, ifcfg_path):
        """The route-reconcile path returns early -- it must write too."""
        traefik_ip, shim_ip = "192.168.1.50", "192.168.1.51"
        patch_ip(_FakeIp(exists=True, current_shim_ip=shim_ip, route_dev=None))

        ok, msg = DsmOperations().ensure_macvlan_shim(INTERFACE, traefik_ip, shim_ip)

        assert ok
        assert "route reconciled" in msg
        assert ifcfg_path.read_text() == EXPECTED_IFCFG

    def test_missing_directory_skips_without_creating_it(self, patch_ip, ifcfg_dir):
        """Degenerate case: no ifcfg tree -> skip, never mkdir system config."""
        traefik_ip, shim_ip = "192.168.1.50", "192.168.1.51"
        patch_ip(_FakeIp(exists=False))

        ok, msg = DsmOperations().ensure_macvlan_shim(INTERFACE, traefik_ip, shim_ip)

        # The shim itself still succeeds -- the file is only about DSM's logging.
        assert ok
        assert "created" in msg
        assert "ifcfg skipped" in msg
        assert not ifcfg_dir.exists()

    @pytest.mark.skipif(os.getuid() == 0, reason="root ignores directory permissions")
    def test_unwritable_directory_never_fails_the_shim(self, patch_ip, ifcfg_path):
        """A write error is reported in the message, not raised, and never fatal."""
        ifcfg_path.parent.chmod(0o500)
        try:
            traefik_ip, shim_ip = "192.168.1.50", "192.168.1.51"
            patch_ip(_FakeIp(exists=False))

            ok, msg = DsmOperations().ensure_macvlan_shim(INTERFACE, traefik_ip, shim_ip)

            assert ok  # interface + route are up; only DSM's log noise remains
            assert "ifcfg not written" in msg
        finally:
            ifcfg_path.parent.chmod(0o755)


class TestStartupScriptShimIfcfg:
    """The boot hook creates the shim in shell before any Python runs, so it
    writes the same file itself -- otherwise a halted instance (whose
    `reconcile --boot` starts nothing) log-floods for the whole window."""

    def _content(self, tmp_path):
        ok, _ = DsmOperations().ensure_startup_script(tmp_path / "install", "syrvisuser")
        assert ok
        return (tmp_path / "install" / "bin" / "syrvis-startup.sh").read_text()

    def test_boot_hook_writes_the_full_key_set(self, tmp_path, ifcfg_dir):
        content = self._content(tmp_path)
        # Rendered from the module constant (monkeypatched here -> proves it).
        assert f'[ -d "{ifcfg_dir}" ]' in content
        assert f'IFCFG_PATH="{ifcfg_dir}/ifcfg-$SHIM_NAME"' in content
        for key in ("DEVICE=$SHIM_NAME", "BOOTPROTO=static", "ONBOOT=no", "IPADDR=$SHIM_IP"):
            assert key in content
        assert "NETMASK=255.255.255.255" in content

    def test_boot_hook_checks_before_writing(self, tmp_path):
        content = self._content(tmp_path)
        # Check-then-write: no churn on a boot where the file is already right.
        assert '"$(cat "$IFCFG_PATH" 2>/dev/null)" != "$IFCFG_WANT"' in content

    def test_boot_hook_heals_an_existing_shim(self, tmp_path):
        """Placed OUTSIDE the create-if-missing guard, before the Docker wait."""
        content = self._content(tmp_path)
        assert content.index("IFCFG_PATH") > content.index("Created macvlan shim")
        assert content.index("IFCFG_PATH") < content.index("DOCKER_WAIT")

    def test_rendered_boot_hook_is_valid_shell(self, tmp_path):
        import subprocess

        script = tmp_path / "install" / "bin" / "syrvis-startup.sh"
        self._content(tmp_path)
        out = subprocess.run(
            ["bash", "-n", str(script)], capture_output=True, text=True, timeout=10
        )
        assert out.returncode == 0, out.stderr

    def test_render_matches_the_python_writer(self, tmp_path, ifcfg_path):
        """The shell block and the CLI writer must agree key-for-key.

        Runs the rendered shell fragment with SHIM_NAME/SHIM_IP bound, then
        compares the file it produces to EXPECTED_IFCFG -- the exact bytes the
        Python writer is asserted to produce above.
        """
        import subprocess

        content = self._content(tmp_path)
        # Up to (not including) the `fi` closing the NETWORK_INTERFACE guard, so
        # the extracted fragment is balanced on its own.
        head, _, _ = content.partition("\nfi\n\n# 4. Wait for Docker")
        block = head[head.index('if [ -d "') :]
        script = f'SHIM_NAME="{SHIM}"; SHIM_IP="192.168.1.51"\n{block}'
        out = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=10)
        assert out.returncode == 0, out.stderr
        assert ifcfg_path.read_text() == EXPECTED_IFCFG


class TestStartupScriptReconcile:
    """The boot startup script must reconcile declared Layer 2 services.

    Running at boot (via the rc.d hook), the script sets up Docker perms and the
    macvlan shim, then must also reconcile the declared L2 services. That last
    step is strictly best-effort: a failing declaration must never block boot.
    """

    def test_dsm_startup_script_runs_reconcile_best_effort(self, tmp_path):
        install_dir = tmp_path / "install"
        ok, _ = DsmOperations().ensure_startup_script(install_dir, "syrvisuser")
        assert ok

        content = (install_dir / "bin" / "syrvis-startup.sh").read_text()
        # The reconcile is invoked inside a retry loop (visible logging, no silent
        # swallow) rather than a single `|| true` line, but is still strictly
        # best-effort: the boot script always reaches `exit 0`.
        reconcile_line = f'"{install_dir}/bin/syrvis" reconcile --boot'
        assert reconcile_line in content
        # Boot script never aborts (retries, logs, then falls through to exit 0).
        assert content.rstrip().endswith("exit 0")
        # Placed after the macvlan shim setup and before the final exit.
        assert content.index(reconcile_line) > content.index("SHIM_NAME")
        assert content.index(reconcile_line) < content.rindex("exit 0")

        # The rc.d hook can run before ContainerManager starts the Docker daemon
        # on DSM 7, so reconcile must be preceded by a bounded wait-for-docker
        # poll -- otherwise every compose call no-ops and --boot swallows it.
        assert 'until "$DOCKER_BIN" info >/dev/null 2>&1; do' in content
        assert "DOCKER_WAIT" in content
        assert content.index("DOCKER_WAIT") < content.index(reconcile_line)


class TestStartupScriptSeamSelfHeal:
    """design/26 §8.3: DSM regenerates /etc/passwd on EVERY boot, resetting the
    seam accounts' shells to /sbin/nologin — the startup script must re-assert
    them (idempotent, guarded so an absent account is a no-op)."""

    def _content(self, tmp_path):
        install_dir = tmp_path / "install"
        ok, _ = DsmOperations().ensure_startup_script(install_dir, "syrvisuser")
        assert ok
        return (install_dir / "bin" / "syrvis-startup.sh").read_text()

    def test_reasserts_both_seam_shells(self, tmp_path):
        content = self._content(tmp_path)
        assert "syrvis-operator syrvis-reader" in content
        # guarded: only touches /etc/passwd when the account exists
        assert 'grep -q "^$SEAM_USER:" /etc/passwd' in content
        assert (
            'sed -i "s#^\\($SEAM_USER:.*\\):/sbin/nologin\\$#\\1:/bin/sh#" /etc/passwd'
            in content
        )
        # strictly in the boot path, before the reconcile step
        assert content.index("SEAM_USER") < content.index("reconcile --boot")

    def test_sed_expression_actually_flips_the_shell(self, tmp_path):
        # Extract the generated sed and run it (sans -i) over sample passwd
        # lines — pinning the escaping, not just the source text.
        import subprocess

        content = self._content(tmp_path)
        sed_line = next(
            line.strip() for line in content.splitlines() if line.strip().startswith("sed -i")
        )
        piped = sed_line.replace("sed -i ", "sed ").replace(" /etc/passwd", "")
        broken = "syrvis-operator:x:1027:100::/var/services/homes/o:/sbin/nologin"
        healthy = "syrvis-operator:x:1027:100::/var/services/homes/o:/bin/sh"
        other = "root:x:0:0::/root:/sbin/nologin"
        script = 'SEAM_USER=syrvis-operator; printf \'%s\\n\' "$1" | {}'.format(piped)
        for line_in, expected in ((broken, healthy), (healthy, healthy), (other, other)):
            out = subprocess.run(
                ["sh", "-c", script, "sh", line_in],
                capture_output=True,
                text=True,
                timeout=10,
            )
            assert out.returncode == 0, out.stderr
            assert out.stdout.strip() == expected
