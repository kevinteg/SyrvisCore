"""syrvisctl seam sync — regenerating the operator seam from the active version.

Covers the policy file contract (written by the provision script, read by the
manager), the render-via-active-venv subprocess boundary, atomic installs with
visudo validation, and the activate/rollback auto-sync gate. The manager never
imports syrviscore, so everything here stubs the generator subprocess.
"""

import json
import stat
import subprocess

import pytest

from syrviscore_manager import seam_sync
from syrviscore_manager.errors import SyrvisError


def write_policy(tmp_path, **over):
    policy = {
        "auto_seam_update": True,
        "operator": "syrvis-operator",
        "syrvis_home": str(tmp_path),
        "syrvisctl_path": "/var/packages/syrviscore/target/venv/bin/syrvisctl",
        "shim_path": str(tmp_path / "shim" / "syrvis-mcp-shim"),
    }
    policy.update(over)
    path = tmp_path / "seam-policy.json"
    path.write_text(json.dumps(policy))
    return path, policy


# ---------------------------------------------------------------------------
# Policy file
# ---------------------------------------------------------------------------


class TestLoadPolicy:
    def test_absent_returns_none(self, tmp_path):
        assert seam_sync.load_policy(tmp_path / "nope.json") is None

    def test_invalid_json_raises(self, tmp_path):
        p = tmp_path / "seam-policy.json"
        p.write_text("{nope")
        with pytest.raises(SyrvisError, match="unreadable"):
            seam_sync.load_policy(p)

    def test_non_object_raises(self, tmp_path):
        p = tmp_path / "seam-policy.json"
        p.write_text('["a"]')
        with pytest.raises(SyrvisError, match="JSON object"):
            seam_sync.load_policy(p)

    def test_valid_roundtrip(self, tmp_path):
        p, policy = write_policy(tmp_path)
        assert seam_sync.load_policy(p) == policy


# ---------------------------------------------------------------------------
# Render via the active version's venv
# ---------------------------------------------------------------------------


class TestRender:
    def _home_with_venv(self, tmp_path):
        python = tmp_path / "current" / "cli" / "venv" / "bin" / "python"
        python.parent.mkdir(parents=True)
        python.write_text("#!/bin/sh\n")
        return tmp_path

    def test_missing_venv_raises(self, tmp_path):
        _, policy = write_policy(tmp_path)
        with pytest.raises(SyrvisError, match="no active version venv"):
            seam_sync._render(tmp_path, policy, "sudoers")

    def test_argv_uses_policy_paths(self, tmp_path, monkeypatch):
        home = self._home_with_venv(tmp_path)
        _, policy = write_policy(tmp_path, operator="op-x")
        seen = {}

        def fake_run(argv, **kw):
            seen["argv"] = argv
            return subprocess.CompletedProcess(argv, 0, stdout="RENDERED", stderr="")

        monkeypatch.setattr(seam_sync.subprocess, "run", fake_run)
        out = seam_sync._render(home, policy, "sudoers")
        assert out == "RENDERED"
        argv = seen["argv"]
        assert argv[1:4] == ["-m", "syrviscore.seam.gen", "sudoers"]
        assert "--operator" in argv and argv[argv.index("--operator") + 1] == "op-x"
        assert "--home" in argv and argv[argv.index("--home") + 1] == str(home)

    def test_old_version_without_seam_module(self, tmp_path, monkeypatch):
        home = self._home_with_venv(tmp_path)
        _, policy = write_policy(tmp_path)
        monkeypatch.setattr(
            seam_sync.subprocess,
            "run",
            lambda argv, **kw: subprocess.CompletedProcess(
                argv, 1, stdout="", stderr="No module named syrviscore.seam"
            ),
        )
        with pytest.raises(SyrvisError, match="service >= 0.4"):
            seam_sync._render(home, policy, "shim")


# ---------------------------------------------------------------------------
# Install + sync
# ---------------------------------------------------------------------------


class TestSync:
    def _stub_render(self, monkeypatch, sudoers="SUDOERS-1\n", shim="SHIM-1\n"):
        monkeypatch.setattr(
            seam_sync,
            "_render",
            lambda home, policy, what: sudoers if what == "sudoers" else shim,
        )
        # The dev host may have a real visudo, which would reject the stub
        # content; visudo behavior gets its own dedicated test below.
        monkeypatch.setattr(seam_sync.shutil, "which", lambda name: None)

    def test_first_sync_installs_both(self, tmp_path, monkeypatch):
        _, policy = write_policy(tmp_path)
        self._stub_render(monkeypatch)
        sudoers_path = tmp_path / "sudoers.d" / "syrviscore-mcp"
        report = seam_sync.sync_seam(tmp_path, policy, sudoers_path=sudoers_path)
        assert report == {"dry_run": False, "sudoers": "updated", "shim": "updated"}
        assert sudoers_path.read_text() == "SUDOERS-1\n"
        assert stat.S_IMODE(sudoers_path.stat().st_mode) == 0o440
        shim_path = tmp_path / "shim" / "syrvis-mcp-shim"
        assert shim_path.read_text() == "SHIM-1\n"
        assert stat.S_IMODE(shim_path.stat().st_mode) == 0o755

    def test_second_sync_unchanged(self, tmp_path, monkeypatch):
        _, policy = write_policy(tmp_path)
        self._stub_render(monkeypatch)
        sudoers_path = tmp_path / "sudoers.d" / "syrviscore-mcp"
        seam_sync.sync_seam(tmp_path, policy, sudoers_path=sudoers_path)
        report = seam_sync.sync_seam(tmp_path, policy, sudoers_path=sudoers_path)
        assert report == {"dry_run": False, "sudoers": "unchanged", "shim": "unchanged"}

    def test_dry_run_writes_nothing(self, tmp_path, monkeypatch):
        _, policy = write_policy(tmp_path)
        self._stub_render(monkeypatch)
        sudoers_path = tmp_path / "sudoers.d" / "syrviscore-mcp"
        report = seam_sync.sync_seam(tmp_path, policy, sudoers_path=sudoers_path, dry_run=True)
        assert report["sudoers"] == "would update" and report["shim"] == "would update"
        assert not sudoers_path.exists()

    def test_failed_visudo_blocks_install(self, tmp_path, monkeypatch):
        _, policy = write_policy(tmp_path)
        self._stub_render(monkeypatch)
        monkeypatch.setattr(seam_sync.shutil, "which", lambda name: "/usr/sbin/visudo")

        def fake_run(argv, **kw):
            if argv[0] == "visudo":
                return subprocess.CompletedProcess(argv, 1, stdout="", stderr="syntax error")
            raise AssertionError("unexpected subprocess {}".format(argv))

        monkeypatch.setattr(seam_sync.subprocess, "run", fake_run)
        sudoers_path = tmp_path / "sudoers.d" / "syrviscore-mcp"
        with pytest.raises(SyrvisError, match="visudo"):
            seam_sync.sync_seam(tmp_path, policy, sudoers_path=sudoers_path)
        assert not sudoers_path.exists()  # nothing installed on validation failure
        assert list((tmp_path / "sudoers.d").glob(".*")) == []  # temp cleaned up


# ---------------------------------------------------------------------------
# The activate/rollback gate
# ---------------------------------------------------------------------------


class TestAutoSyncGate:
    def test_not_provisioned_skips(self, monkeypatch, tmp_path):
        monkeypatch.setattr(seam_sync, "load_policy", lambda *a, **k: None)
        assert seam_sync.auto_sync_after_activate(tmp_path) is None

    def test_auto_off_skips(self, monkeypatch, tmp_path):
        monkeypatch.setattr(seam_sync, "load_policy", lambda *a, **k: {"auto_seam_update": False})
        assert seam_sync.auto_sync_after_activate(tmp_path) is None

    def test_auto_on_syncs(self, monkeypatch, tmp_path):
        policy = {"auto_seam_update": True, "operator": "syrvis-operator"}
        monkeypatch.setattr(seam_sync, "load_policy", lambda *a, **k: policy)
        seen = {}

        def fake_sync(home, pol, **kw):
            seen["home"], seen["policy"] = home, pol
            return {"dry_run": False, "sudoers": "updated", "shim": "unchanged"}

        monkeypatch.setattr(seam_sync, "sync_seam", fake_sync)
        report = seam_sync.auto_sync_after_activate(tmp_path)
        assert report["sudoers"] == "updated"
        assert seen["home"] == tmp_path and seen["policy"] is policy


# ---------------------------------------------------------------------------
# Provision script records the policy
# ---------------------------------------------------------------------------


class TestProvisionPolicy:
    def _render(self, **kw):
        from syrviscore.seam import gen

        cfg = gen.DeployConfig(syrvis_home="/volume2/syrviscore")
        return gen.render_provision(cfg, "ssh-ed25519 AAAATESTKEY comment", **kw)

    def test_policy_block_present_with_defaults(self):
        script = self._render()
        assert 'POLICY_PATH="$STATE_DIR/seam-policy.json"' in script
        assert '"auto_seam_update": true' in script
        assert '"syrvis_home": "/volume2/syrviscore"' in script
        assert 'chmod 600 "$POLICY_PATH"' in script

    def test_no_auto_flag_recorded(self):
        script = self._render(auto_seam_update=False)
        assert '"auto_seam_update": false' in script
        assert "auto_seam_update=false" in script
