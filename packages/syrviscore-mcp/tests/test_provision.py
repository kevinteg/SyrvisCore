"""The generated provisioning script is valid sh, parameterized, and injection-safe."""

import subprocess

import pytest

from syrviscore_mcp.deploy import gen

PUBKEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAISAMPLE syrvis-mcp"


def _render(**cfg_over):
    cfg = gen.DeployConfig(**cfg_over)
    return gen.render_provision(cfg, PUBKEY, from_cidr="192.168.1.0/24")


def _sh_n(script: str) -> subprocess.CompletedProcess:
    return subprocess.run(["sh", "-n", "/dev/stdin"], input=script, text=True, capture_output=True)


def test_default_script_is_valid_sh():
    r = _sh_n(_render())
    assert r.returncode == 0, r.stderr


def test_custom_home_is_valid_and_reparameterized():
    script = _render(syrvis_home="/volume2/syrviscore", operator="mcp-op")
    assert _sh_n(script).returncode == 0
    assert "/volume2/syrviscore/bin/syrvis" in script
    assert 'OPERATOR="mcp-op"' in script


def test_bakes_in_key_and_source_restriction():
    script = _render()
    assert PUBKEY in script
    assert 'from="192.168.1.0/24"' in script
    assert 'command="/usr/local/bin/syrvis-mcp-shim"' in script


def test_captures_original_before_change():
    script = _render()
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


def test_sudoers_installed_atomically():
    script = _render()
    # staged in the sudoers.d dir with a dotted name sudo ignores, then renamed
    assert ".syrviscore-mcp.tmp" in script
    assert "mv -f '$TMP_SUDOERS' '$SUDOERS_PATH'" in script
    # capture the original before the rename
    assert script.index('capture_original "$SUDOERS_PATH"') < script.index("mv -f '$TMP_SUDOERS'")
    # visudo is used only if present (DSM has none) — never a hard requirement
    assert "command -v visudo" in script


def test_authorized_keys_is_additive():
    script = _render()
    # preserves other keys (grep -vF), never truncates with '> "$AUTH"'
    assert "grep -vF" in script
    assert '> "$AUTH"' not in script


def test_dsm_native_tooling():
    script = _render()
    # DSM has no getent/visudo; synouser/synogroup live in /usr/syno/sbin.
    # getent must not be INVOKED (a comment mentioning it is fine).
    assert "getent passwd" not in script
    assert "getent group" not in script
    assert "/usr/syno/sbin" in script
    assert "awk -F: -v u=" in script  # home dir from /etc/passwd
    assert 'grep -q "^docker:" /etc/group' in script  # group from /etc/group
    # visudo and getent are NOT in the required-tools preflight
    assert "for t in synouser synogroup install cp id awk chmod chown mktemp" in script


def test_operator_login_shell_set_to_sh():
    script = _render()
    # DSM's synouser creates users as /sbin/nologin, which cannot run the shim.
    assert "set operator login shell" in script
    assert '$7="/bin/sh"' in script
    # idempotent: skip when already /bin/sh
    assert '[ "$CUR_SHELL" = "/bin/sh" ]' in script
    # atomic swap: temp in /etc (same fs), then rename over /etc/passwd
    assert 'mv -f "$PW_NEW" /etc/passwd' in script
    # a record-count guard means it refuses to install a mangled passwd
    assert "refusing to install /etc/passwd" in script


def test_shell_rollback_is_surgical_not_full_overwrite():
    script = _render()
    # rollback restores only the one shell field via _setshell, never cp/mv of a
    # whole /etc/passwd backup (which could clobber unrelated later changes).
    assert "_setshell()" in script
    assert "setshell) printf" in script
    # the manifest entry that drives it
    assert "printf 'setshell %s %s\\n'" in script
    # we never capture_original the whole passwd file
    assert 'capture_original "/etc/passwd"' not in script
    assert 'capture_original "$PW' not in script


def test_dry_run_tolerates_missing_operator_home():
    # In --dry-run the account isn't created, so /etc/passwd has no home entry.
    # The preview must not abort there; it assumes the DSM default home instead.
    script = _render()
    assert '[ "$DRYRUN" = 1 ]' in script
    assert "/var/services/homes/$OPERATOR" in script
    # a real run still fails loudly with actionable DSM guidance
    assert "Enable user home service" in script


def test_docker_group_ensured_and_verified():
    script = _render()
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
