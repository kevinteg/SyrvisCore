"""Tests for setup's .env generation idempotency helpers.

Regression guard: a `syrvis setup` re-run must not blank operator-set secrets
that the interactive prompts don't manage (DNS-01 / DDNS / tunnel tokens, OIDC
secret, dashboard session secret).
"""

from syrviscore.setup import _parse_env_file, _preserve_existing_values


def test_parse_env_file_reads_key_values(tmp_path):
    p = tmp_path / ".env"
    p.write_text(
        "# a comment\n"
        "DOMAIN=example.com\n"
        "\n"
        "CLOUDFLARE_DNS_API_TOKEN=secret-token\n"
        "MALFORMED_LINE_NO_EQUALS\n"
    )
    values = _parse_env_file(p)
    assert values["DOMAIN"] == "example.com"
    assert values["CLOUDFLARE_DNS_API_TOKEN"] == "secret-token"
    assert "MALFORMED_LINE_NO_EQUALS" not in values


def test_parse_env_file_missing_returns_empty(tmp_path):
    assert _parse_env_file(tmp_path / "nope.env") == {}


def test_preserve_restores_blanked_secrets():
    generated = (
        "DOMAIN=example.com\n"
        "CLOUDFLARE_DNS_API_TOKEN=\n"  # blanked by a re-run
        "OIDC_CLIENT_SECRET=\n"
        "DASHBOARD_SUBDOMAIN=dash\n"  # non-empty default — must be left alone
    )
    existing = {
        "CLOUDFLARE_DNS_API_TOKEN": "kept-dns-token",
        "OIDC_CLIENT_SECRET": "kept-oidc-secret",
        "DASHBOARD_SUBDOMAIN": "old-sub",
    }
    result = _preserve_existing_values(generated, existing)
    assert "CLOUDFLARE_DNS_API_TOKEN=kept-dns-token" in result
    assert "OIDC_CLIENT_SECRET=kept-oidc-secret" in result
    # A non-empty generated value wins over the prior one (prompt-managed keys).
    assert "DASHBOARD_SUBDOMAIN=dash" in result
    assert "old-sub" not in result


def test_preserve_noop_without_existing():
    generated = "DOMAIN=example.com\nCLOUDFLARE_DNS_API_TOKEN=\n"
    assert _preserve_existing_values(generated, {}) == generated


def test_preserve_leaves_comments_and_populated_lines():
    generated = "# header\nDOMAIN=example.com\nTOKEN=\n"
    existing = {"DOMAIN": "should-not-win", "TOKEN": "restored"}
    result = _preserve_existing_values(generated, existing)
    assert "# header" in result
    assert "DOMAIN=example.com" in result  # populated line untouched
    assert "TOKEN=restored" in result
