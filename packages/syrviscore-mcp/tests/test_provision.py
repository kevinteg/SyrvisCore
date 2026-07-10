"""The generated provisioning script is valid sh, parameterized, and injection-safe."""

import subprocess

import pytest

from syrviscore_mcp.deploy import gen

PUBKEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAISAMPLE syrvis-mcp"


def _render(**cfg_over):
    cfg = gen.DeployConfig(**cfg_over)
    return gen.render_provision(cfg, PUBKEY, from_cidr="192.168.8.0/24")


def _sh_n(script: str) -> subprocess.CompletedProcess:
    return subprocess.run(["sh", "-n", "/dev/stdin"], input=script, text=True, capture_output=True)


def test_default_script_is_valid_sh():
    r = _sh_n(_render())
    assert r.returncode == 0, r.stderr


def test_custom_home_is_valid_and_reparameterized():
    script = _render(syrvis_home="/volume4/syrviscore", operator="mcp-op")
    assert _sh_n(script).returncode == 0
    assert "/volume4/syrviscore/bin/syrvis" in script
    assert 'OPERATOR="mcp-op"' in script


def test_bakes_in_key_and_source_restriction():
    script = _render()
    assert PUBKEY in script
    assert 'from="192.168.8.0/24"' in script
    assert 'command="/usr/local/bin/syrvis-mcp-shim"' in script


def test_backs_up_and_validates_before_install():
    script = _render()
    # visudo validation must appear before the sudoers install
    assert script.index("visudo -cf") < script.index("install -m 0440")
    # the true pre-install state of each target is captured before it's changed
    assert 'capture_original "$SUDOERS_PATH"' in script
    assert 'capture_original "$SHIM_PATH"' in script
    assert 'capture_original "$AUTH"' in script


def test_generates_rollback_and_points_at_it():
    script = _render()
    assert "write_rollback" in script
    assert "sudo sh $ROLLBACK" in script
    # the old broken 'cp -a $BACKUP_DIR/* /' rollback is gone
    assert "cp -a $BACKUP_DIR/* /" not in script


def test_landed_sudoers_revalidated():
    script = _render()
    # after install, the landed file is re-validated and removed if bad
    assert script.count("visudo -cf") >= 2
    assert "removed to keep sudo working" in script


def test_authorized_keys_is_additive():
    script = _render()
    # preserves other keys (grep -vF), never truncates with '> "$AUTH"'
    assert "grep -vF" in script
    assert '> "$AUTH"' not in script


def test_docker_group_ensured_and_verified():
    script = _render()
    assert "getent group docker" in script
    assert "synogroup --add docker" in script
    assert "--memberadd docker" in script


def test_requires_root_and_dry_run_supported():
    script = _render()
    assert '[ "$(id -u)" = "0" ]' in script
    assert "--dry-run" in script
    # abort trap reports partial state
    assert "ABORTED during" in script


@pytest.mark.parametrize("bad", ["key\nrm -rf /", "key' ; rm -rf /", ""])
def test_pubkey_injection_rejected(bad):
    with pytest.raises(ValueError):
        gen.render_provision(gen.DeployConfig(), bad)


def test_pubkey_with_single_quote_cannot_break_authline():
    # a single quote in the key would break the AUTHLINE='...' quoting; the
    # generated script must remain valid sh regardless (or the key is rejected).
    tricky = "ssh-ed25519 AAAA'inject syrvis-mcp"
    try:
        script = gen.render_provision(gen.DeployConfig(), tricky)
    except ValueError:
        return  # rejected outright is acceptable
    assert _sh_n(script).returncode == 0
