"""
Tests for Traefik configuration generation.
"""

import os
from unittest.mock import patch

import yaml

from syrviscore.traefik_config import (
    generate_traefik_dynamic_config,
    generate_traefik_static_config,
)


class TestStaticConfig:
    """Test Traefik static configuration generation."""

    def test_generate_static_config_returns_string(self):
        """Test that static config generation returns a string."""
        config = generate_traefik_static_config()
        assert isinstance(config, str)
        assert len(config) > 0

    def test_static_config_is_valid_yaml(self):
        """Test that generated static config is valid YAML."""
        config = generate_traefik_static_config()
        parsed = yaml.safe_load(config)
        assert isinstance(parsed, dict)

    def test_static_config_has_required_sections(self):
        """Test that static config contains all required sections."""
        config = generate_traefik_static_config()
        parsed = yaml.safe_load(config)

        # Check required sections exist
        assert "api" in parsed
        assert "entryPoints" in parsed
        assert "providers" in parsed
        assert "log" in parsed
        assert "accessLog" in parsed
        assert "certificatesResolvers" in parsed

    def test_static_config_entry_points(self):
        """Test entry points configuration."""
        config = generate_traefik_static_config()
        parsed = yaml.safe_load(config)

        # Check web and websecure entry points
        assert "web" in parsed["entryPoints"]
        assert parsed["entryPoints"]["web"]["address"] == ":80"
        assert "websecure" in parsed["entryPoints"]
        assert parsed["entryPoints"]["websecure"]["address"] == ":443"

    def test_static_config_providers(self):
        """Test providers configuration."""
        config = generate_traefik_static_config()
        parsed = yaml.safe_load(config)

        # Check Docker provider
        assert "docker" in parsed["providers"]
        assert parsed["providers"]["docker"]["exposedByDefault"] is False
        assert parsed["providers"]["docker"]["network"] == "proxy"

        # Check file provider
        assert "file" in parsed["providers"]
        assert parsed["providers"]["file"]["directory"] == "/config"
        assert parsed["providers"]["file"]["watch"] is True

    def test_static_config_uses_acme_email_env(self):
        """Test that ACME_EMAIL environment variable is used."""
        with patch.dict(os.environ, {"ACME_EMAIL": "test@example.com"}):
            config = generate_traefik_static_config()
            parsed = yaml.safe_load(config)

            acme_email = parsed["certificatesResolvers"]["letsencrypt"]["acme"]["email"]
            assert acme_email == "test@example.com"

    def test_static_config_default_acme_email(self):
        """Test default ACME email when env var not set."""
        with patch.dict(os.environ, {}, clear=True):
            config = generate_traefik_static_config()
            parsed = yaml.safe_load(config)

            acme_email = parsed["certificatesResolvers"]["letsencrypt"]["acme"]["email"]
            assert acme_email == "admin@example.com"

    def test_static_config_lets_encrypt(self):
        """Test Let's Encrypt configuration."""
        config = generate_traefik_static_config()
        parsed = yaml.safe_load(config)

        letsencrypt = parsed["certificatesResolvers"]["letsencrypt"]["acme"]
        assert letsencrypt["storage"] == "/acme.json"
        assert "httpChallenge" in letsencrypt
        assert letsencrypt["httpChallenge"]["entryPoint"] == "web"


class TestDynamicConfig:
    """Test Traefik dynamic configuration generation."""

    def test_generate_dynamic_config_returns_string(self):
        """Test that dynamic config generation returns a string."""
        config = generate_traefik_dynamic_config()
        assert isinstance(config, str)
        assert len(config) > 0

    def test_dynamic_config_is_valid_yaml(self):
        """Test that generated dynamic config is valid YAML."""
        config = generate_traefik_dynamic_config()
        parsed = yaml.safe_load(config)
        assert isinstance(parsed, dict)

    def test_dynamic_config_has_http_section(self):
        """Test that dynamic config has HTTP section."""
        config = generate_traefik_dynamic_config()
        parsed = yaml.safe_load(config)

        assert "http" in parsed
        assert "routers" in parsed["http"]
        assert "services" in parsed["http"]
        assert "middlewares" in parsed["http"]

    def test_dynamic_config_dashboard_router(self):
        """Test dashboard router configuration."""
        with patch.dict(os.environ, {"DOMAIN": "mydomain.com"}):
            config = generate_traefik_dynamic_config()
            parsed = yaml.safe_load(config)

            dashboard = parsed["http"]["routers"]["dashboard"]
            assert dashboard["rule"] == "Host(`traefik.mydomain.com`)"
            assert dashboard["service"] == "api@internal"
            assert "websecure" in dashboard["entryPoints"]

    def test_dynamic_config_uses_domain_env(self):
        """Test that DOMAIN environment variable is used."""
        with patch.dict(os.environ, {"DOMAIN": "test.local"}):
            config = generate_traefik_dynamic_config()
            assert "traefik.test.local" in config

    def test_dynamic_config_default_domain(self):
        """Test default domain when env var not set."""
        with patch.dict(os.environ, {}, clear=True):
            config = generate_traefik_dynamic_config()
            parsed = yaml.safe_load(config)

            dashboard_rule = parsed["http"]["routers"]["dashboard"]["rule"]
            assert "example.com" in dashboard_rule


class TestConfigIntegration:
    """Test configuration integration."""

    def test_both_configs_can_be_generated(self):
        """Test that both static and dynamic configs can be generated together."""
        static = generate_traefik_static_config()
        dynamic = generate_traefik_dynamic_config()

        assert static is not None
        assert dynamic is not None

        # Both should be valid YAML
        yaml.safe_load(static)
        yaml.safe_load(dynamic)

    def test_configs_use_consistent_paths(self):
        """Test that configs reference consistent paths."""
        static = generate_traefik_static_config()
        static_parsed = yaml.safe_load(static)

        # Static config says to watch /config directory
        assert static_parsed["providers"]["file"]["directory"] == "/config"

        # Static config stores certs in /acme.json
        assert (
            static_parsed["certificatesResolvers"]["letsencrypt"]["acme"]["storage"] == "/acme.json"
        )

        # Logs in /logs
        assert static_parsed["log"]["filePath"] == "/logs/traefik.log"
        assert static_parsed["accessLog"]["filePath"] == "/logs/access.log"
