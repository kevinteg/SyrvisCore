"""Shared fixtures for the MCP test suite (all offline; no NAS, no fastmcp needed)."""

import tempfile
from pathlib import Path

import pytest

from syrviscore_mcp.config import NASConfig
from syrviscore_mcp.remote import build_remote_tokens

# Isolated control-socket dir so the RemoteRunner's 0700 chmod never touches a
# shared system dir like /tmp (which would be dangerous under a root CI runner).
_CM_DIR = tempfile.mkdtemp(prefix="syrvis-mcp-cm-")


def make_config(**overrides) -> NASConfig:
    base = dict(
        host="192.168.1.10",
        ssh_target="syrvis-nas",
        ssh_config_file=Path("/dev/null"),
        control_path=f"{_CM_DIR}/cm-%r@%h:%p",
        command_timeout_s=120,
        profile="prod",
        syrvisctl_path="/var/packages/syrviscore/target/venv/bin/syrvisctl",
        syrvis_wrapper="/volume1/syrviscore/bin/syrvis",
        syrvis_home="/volume1/syrviscore",
        use_sudo=True,
        sudo_binary="sudo",
        managed_marker="syrviscore",
        environment="test",
        git_url_allowed_hosts=[],
        token_secret_env="SYRVISCORE_MCP_TOKEN_SECRET",
        token_ttl_s=300,
        ssh_user="syrvis-operator",
    )
    base.update(overrides)
    return NASConfig(**base)


@pytest.fixture
def cfg():
    return make_config()


class FakeRunner:
    """Records calls and returns canned responses; still exercises real argv build."""

    def __init__(self, cfg, responses=None):
        self.cfg = cfg
        self.calls = []  # list of dicts: {id, args, tokens}
        self.responses = responses or {}

    def run(self, command, args=None):
        args = args or {}
        tokens = build_remote_tokens(self.cfg, command, args)
        self.calls.append({"id": command.id, "args": dict(args), "tokens": tokens})
        resp = self.responses.get(command.id)
        if callable(resp):
            return resp(args)
        if resp is not None:
            return resp
        return {"ok": True}

    def ids(self):
        return [c["id"] for c in self.calls]


@pytest.fixture
def fake_runner(cfg):
    return FakeRunner(cfg)
