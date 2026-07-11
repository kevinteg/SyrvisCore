"""Tests for the shared structured config reader (config_reader.read_config)."""

from syrviscore.config_reader import RedactedConfig, is_secret_key, read_config


def _write_env(tmp_path, body):
    env = tmp_path / ".env"
    env.write_text(body)
    return env


def test_is_secret_key():
    assert is_secret_key("CLOUDFLARE_TUNNEL_TOKEN")
    assert is_secret_key("PORTAINER_ADMIN_PASSWORD")
    assert is_secret_key("GITHUB_TOKEN")
    assert is_secret_key("OIDC_CLIENT_SECRET")
    assert is_secret_key("SSH_PRIVATE_KEY")
    assert not is_secret_key("DOMAIN")
    assert not is_secret_key("NETWORK_SUBNET")
    assert not is_secret_key("CLOUDFLARE_ACCESS_AUD")


def test_secrets_are_masked_but_plain_values_are_not(tmp_path):
    env = _write_env(
        tmp_path,
        "# comment\n"
        "DOMAIN=example.com\n"
        "NETWORK_SUBNET=192.168.0.0/24\n"
        "CLOUDFLARE_TUNNEL_TOKEN=super-secret-value\n"
        "PORTAINER_ADMIN_PASSWORD=hunter2hunter2\n"
        "GITHUB_TOKEN=ghp_abcdef\n",
    )
    cfg = read_config(env_path=env)
    assert isinstance(cfg, RedactedConfig)
    assert cfg.domain == "example.com"
    assert cfg.values["DOMAIN"] == "example.com"
    assert cfg.values["NETWORK_SUBNET"] == "192.168.0.0/24"
    assert cfg.values["CLOUDFLARE_TUNNEL_TOKEN"] == "****"
    assert cfg.values["PORTAINER_ADMIN_PASSWORD"] == "****"
    assert cfg.values["GITHUB_TOKEN"] == "****"
    # The raw secret must never appear anywhere in the redacted view.
    assert "super-secret-value" not in str(cfg.to_dict())


def test_empty_secret_stays_empty_not_masked(tmp_path):
    env = _write_env(tmp_path, "CLOUDFLARE_TUNNEL_TOKEN=\nDOMAIN=example.com\n")
    cfg = read_config(env_path=env)
    # An unset secret shows as empty so the UI can say "exists but not configured".
    assert cfg.values["CLOUDFLARE_TUNNEL_TOKEN"] == ""


def test_redact_false_returns_raw(tmp_path):
    env = _write_env(tmp_path, "CLOUDFLARE_TUNNEL_TOKEN=raw-token\n")
    cfg = read_config(env_path=env, redact=False)
    assert cfg.values["CLOUDFLARE_TUNNEL_TOKEN"] == "raw-token"


def test_enabled_components_detection(tmp_path):
    env = _write_env(
        tmp_path,
        "CLOUDFLARE_TUNNEL_TOKEN=tok\n"
        "CLOUDFLARE_API_TOKEN=\n"
        "SYNOLOGY_DSM_ENABLED=true\n"
        "SYNOLOGY_PHOTOS_ENABLED=false\n",
    )
    cfg = read_config(env_path=env)
    assert cfg.enabled_components["cloudflared"] is True
    assert cfg.enabled_components["cloudflare_ddns"] is False
    assert cfg.enabled_components["synology_dsm"] is True
    assert cfg.enabled_components["synology_photos"] is False


def test_missing_env_file_yields_empty(tmp_path):
    cfg = read_config(env_path=tmp_path / "does-not-exist.env")
    assert cfg.values == {}
    assert cfg.domain == ""
    assert cfg.enabled_components["cloudflared"] is False
