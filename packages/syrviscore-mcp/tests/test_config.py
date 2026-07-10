"""Config loading + startup validation."""

import pytest

from syrviscore_mcp.config import load_config
from syrviscore_mcp.errors import ConfigError

GOOD = """
[nas]
host = "192.168.8.3"
ssh_target = "syrvis-nas"
ssh_config_file = "{ssh}"

[layout]
profile = "prod"
syrvisctl_path = "/var/packages/syrviscore/target/venv/bin/syrvisctl"
syrvis_wrapper = "/volume1/syrviscore/bin/syrvis"
syrvis_home = "/volume1/syrviscore"

[safety]
environment = "test"
"""

SSH_CONF = "Host syrvis-nas\n    User {user}\n"


def _write(tmp_path, toml_text, user="syrvis-operator"):
    ssh = tmp_path / "ssh_config"
    ssh.write_text(SSH_CONF.format(user=user))
    cfg_file = tmp_path / "config.toml"
    cfg_file.write_text(toml_text.format(ssh=ssh))
    return cfg_file


def test_load_good(tmp_path):
    cfg = load_config(str(_write(tmp_path, GOOD)))
    assert cfg.host == "192.168.8.3"
    assert cfg.ssh_user == "syrvis-operator"
    assert cfg.syrvis_home == "/volume1/syrviscore"


def test_missing_file():
    with pytest.raises(ConfigError):
        load_config("/nonexistent/config.toml")


def test_forbidden_ssh_user_rejected(tmp_path):
    with pytest.raises(ConfigError):
        load_config(str(_write(tmp_path, GOOD, user="root")))
    with pytest.raises(ConfigError):
        load_config(str(_write(tmp_path, GOOD, user="cerebrate")))


def test_non_absolute_path_rejected(tmp_path):
    bad = GOOD.replace('syrvis_home = "/volume1/syrviscore"', 'syrvis_home = "relative/path"')
    with pytest.raises(ConfigError):
        load_config(str(_write(tmp_path, bad)))


def test_bad_profile_rejected(tmp_path):
    bad = GOOD.replace('profile = "prod"', 'profile = "wild"')
    with pytest.raises(ConfigError):
        load_config(str(_write(tmp_path, bad)))


def test_missing_host_rejected(tmp_path, monkeypatch):
    monkeypatch.delenv("SYRVISCORE_NAS_HOST", raising=False)
    bad = GOOD.replace('host = "192.168.8.3"', 'host = ""')
    with pytest.raises(ConfigError):
        load_config(str(_write(tmp_path, bad)))


PROD = GOOD.replace(
    'environment = "test"',
    'environment = "production"\ngit_url_allowed_hosts = ["github.com"]',
)


def test_production_requires_token_secret(tmp_path, monkeypatch):
    monkeypatch.delenv("SYRVISCORE_MCP_TOKEN_SECRET", raising=False)
    cfg = load_config(str(_write(tmp_path, PROD)))
    with pytest.raises(ConfigError):
        cfg.token_secret()


def test_production_requires_git_allowlist(tmp_path):
    prod_no_hosts = GOOD.replace('environment = "test"', 'environment = "production"')
    with pytest.raises(ConfigError):
        load_config(str(_write(tmp_path, prod_no_hosts)))


def test_prod_shorthand_also_requires_git_allowlist(tmp_path):
    # the 'prod' shorthand must be treated as production, not silently downgraded
    prod = GOOD.replace('environment = "test"', 'environment = "prod"')
    with pytest.raises(ConfigError):
        load_config(str(_write(tmp_path, prod)))


def test_unknown_environment_rejected(tmp_path):
    bad = GOOD.replace('environment = "test"', 'environment = "prodction"')  # typo
    with pytest.raises(ConfigError):
        load_config(str(_write(tmp_path, bad)))
