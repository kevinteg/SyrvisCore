"""Tests for remote.py stdin plumbing (secret_set path).

Verifies:
- args['_stdin'] is popped BEFORE build_remote_tokens (never in tokens/audit)
- _exec receives stdin_data correctly
- existing callers (no _stdin key) are unaffected
"""

from pathlib import Path
from typing import List, Optional
from unittest.mock import MagicMock

import pytest

from syrviscore_mcp.commands import COMMANDS_BY_ID, get_command
from syrviscore_mcp.remote import RemoteRunner, build_remote_tokens

from .conftest import make_config


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class CapturingSubprocess:
    """Records every subprocess.run() call; returns a canned success result."""

    def __init__(self, stdout="wrote /volume1/syrviscore/data/myapp/secrets.env"):
        self.calls: list = []
        self._stdout = stdout

    def __call__(self, argv, **kwargs):
        self.calls.append({"argv": argv, "kwargs": kwargs})
        result = MagicMock()
        result.returncode = 0
        result.stdout = self._stdout
        result.stderr = ""
        return result

    @property
    def last(self):
        return self.calls[-1]


def make_runner(fake_proc=None):
    cfg = make_config()
    proc = fake_proc or CapturingSubprocess()
    runner = RemoteRunner(cfg=cfg, subprocess_run=proc, audit_path=Path("/dev/null"))
    return runner, proc


# ---------------------------------------------------------------------------
# _stdin is popped before token-building
# ---------------------------------------------------------------------------


class TestStdinNotInTokens:
    def test_secret_not_in_remote_tokens(self):
        """args['_stdin'] must not appear anywhere in the SSH argv."""
        runner, proc = make_runner()
        cmd = get_command("secret_set")

        runner.run(cmd, {"name": "myapp", "_stdin": "PASSWORD=topsecret\n"})

        argv = proc.last["argv"]
        full_cmd = " ".join(str(a) for a in argv)
        assert "topsecret" not in full_cmd, (
            f"secret leaked into argv: {full_cmd}"
        )

    def test_stdin_passed_as_input_kwarg(self):
        """The captured subprocess call must receive input='<secret content>'."""
        runner, proc = make_runner()
        cmd = get_command("secret_set")

        runner.run(cmd, {"name": "myapp", "_stdin": "PASSWORD=topsecret\n"})

        assert proc.last["kwargs"].get("input") == "PASSWORD=topsecret\n"

    def test_stdin_key_absent_from_build_remote_tokens(self):
        """_stdin must never reach build_remote_tokens — call it directly to confirm."""
        cfg = make_config()
        cmd = get_command("secret_set")
        # _stdin is an extra key; build_remote_tokens must not see it or raise
        # (it would raise ValidationError/KeyError if it tried to resolve it as a slot)
        tokens = build_remote_tokens(cfg, cmd, {"name": "myapp", "_stdin": "PASSWORD=s\n"})
        for tok in tokens:
            assert "secret\n" not in str(tok)
            assert "PASSWORD" not in str(tok)
        # 'myapp' must appear as the final positional
        assert tokens[-1] == "myapp"

    def test_name_appears_in_tokens(self):
        """The validated name must still appear in the remote tokens after pop."""
        runner, proc = make_runner()
        cmd = get_command("secret_set")

        runner.run(cmd, {"name": "immich-db", "_stdin": "DB_PASSWORD=s\n"})

        argv = proc.last["argv"]
        full_cmd = " ".join(str(a) for a in argv)
        assert "immich-db" in full_cmd


# ---------------------------------------------------------------------------
# Existing callers unaffected (no _stdin)
# ---------------------------------------------------------------------------


class TestNoStdinCallerUnaffected:
    def test_service_list_no_input(self):
        """A command without _stdin must NOT pass input= to subprocess."""
        runner, proc = make_runner(CapturingSubprocess(stdout='{"services": []}'))
        cmd = get_command("service_list")

        runner.run(cmd, {})

        # input kwarg must be absent or None
        assert proc.last["kwargs"].get("input") is None

    def test_reconcile_no_input(self):
        """reconcile (sudo, no stdin) must not receive input=."""
        runner, proc = make_runner(CapturingSubprocess(stdout='{"ok": true}'))
        cmd = get_command("reconcile")

        runner.run(cmd, {})

        assert proc.last["kwargs"].get("input") is None


# ---------------------------------------------------------------------------
# Secret_set command shape
# ---------------------------------------------------------------------------


class TestSecretSetCommandShape:
    def test_command_registered(self):
        assert "secret_set" in COMMANDS_BY_ID

    def test_command_is_sudo(self):
        assert COMMANDS_BY_ID["secret_set"].sudo is True

    def test_command_is_not_destructive(self):
        assert COMMANDS_BY_ID["secret_set"].destructive is False

    def test_command_expect_json_false(self):
        assert COMMANDS_BY_ID["secret_set"].expect_json is False

    def test_command_has_name_positional(self):
        cmd = COMMANDS_BY_ID["secret_set"]
        assert cmd.positional is not None
        assert cmd.positional.name == "name"

    def test_command_has_no_flags(self):
        """No --json flag: apply-immich-secrets only needs the exit code."""
        cmd = COMMANDS_BY_ID["secret_set"]
        assert cmd.flags == []

    def test_token_shape_includes_separator(self):
        """Remote tokens must include '--' before the service name."""
        cfg = make_config()
        cmd = get_command("secret_set")
        tokens = build_remote_tokens(cfg, cmd, {"name": "immich-db"})
        # ['sudo', '-n', '/volume1/syrviscore/bin/syrvis', 'secret', 'set', '--', 'immich-db']
        assert "--" in tokens
        assert tokens[-1] == "immich-db"
        assert "secret" in tokens
        assert "set" in tokens


# ---------------------------------------------------------------------------
# Config_set command shape (the jobs analog of secret_set — same contract)
# ---------------------------------------------------------------------------


class TestConfigSetCommandShape:
    def test_command_registered(self):
        assert "config_set" in COMMANDS_BY_ID

    def test_command_is_sudo(self):
        assert COMMANDS_BY_ID["config_set"].sudo is True

    def test_command_is_not_destructive(self):
        assert COMMANDS_BY_ID["config_set"].destructive is False

    def test_command_expect_json_false(self):
        assert COMMANDS_BY_ID["config_set"].expect_json is False

    def test_command_has_name_positional(self):
        cmd = COMMANDS_BY_ID["config_set"]
        assert cmd.positional is not None
        assert cmd.positional.name == "name"

    def test_command_has_no_flags(self):
        """No --json flag: the caller only needs the exit code (like secret_set)."""
        cmd = COMMANDS_BY_ID["config_set"]
        assert cmd.flags == []

    def test_token_shape_includes_separator(self):
        """Remote tokens must include '--' before the job name."""
        cfg = make_config()
        cmd = get_command("config_set")
        tokens = build_remote_tokens(cfg, cmd, {"name": "login-alert"})
        # ['sudo', '-n', '/volume1/syrviscore/bin/syrvis', 'config', 'set', '--', 'login-alert']
        assert "--" in tokens
        assert tokens[-1] == "login-alert"
        assert "config" in tokens
        assert "set" in tokens

    def test_stdin_passed_and_not_in_tokens(self):
        """The conf body arrives on stdin only — never in argv (like secret_set)."""
        runner, proc = make_runner()
        cmd = get_command("config_set")

        runner.run(cmd, {"name": "login-alert", "_stdin": "NTFY_URL=topsecret\n"})

        argv = proc.last["argv"]
        full_cmd = " ".join(str(a) for a in argv)
        assert "topsecret" not in full_cmd, f"conf body leaked into argv: {full_cmd}"
        assert proc.last["kwargs"].get("input") == "NTFY_URL=topsecret\n"
