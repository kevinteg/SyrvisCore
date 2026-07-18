"""ensure_config_tree_readable makes the config tree + service manifests + core
compose operator-readable (docker group) WITHOUT ever touching config/.env, which
carries secrets and must stay 0600. Regression for the operator-read gap that
locked syrvis-operator out of service_list/verify (design/04 §7)."""
import os
from pathlib import Path

from syrviscore import privileged_ops, remediation


def _tree(home: Path):
    (home / "config" / "services.d").mkdir(parents=True)
    (home / "services" / "foo").mkdir(parents=True)
    compose = home / "config" / "docker-compose.yaml"
    compose.write_text("services: {}\n")
    compose.chmod(0o600)
    env = home / "config" / ".env"
    env.write_text("SECRET=shh\n")
    env.chmod(0o600)
    manifest = home / "services" / "foo" / "syrvis-service.yaml"
    manifest.write_text("name: foo\n")
    manifest.chmod(0o600)
    return compose, env, manifest


def test_config_tree_made_group_readable_env_untouched(tmp_path, monkeypatch):
    home = tmp_path / "syrviscore"
    compose, env, manifest = _tree(home)
    own_gid = os.getgid()
    # pretend the test user's own gid is the docker gid so os.chown(-1, gid) works
    monkeypatch.setattr(privileged_ops, "get_docker_group_info", lambda: (True, own_gid))

    ok, msg = privileged_ops.ensure_config_tree_readable(home)
    assert ok, msg

    for p in (compose, manifest):  # now group-readable by the docker gid
        st = p.stat()
        assert st.st_gid == own_gid
        assert st.st_mode & 0o040, "{} not group-readable".format(p.name)

    # .env is NEVER touched — still 0600, no group read
    assert env.stat().st_mode & 0o777 == 0o600
    assert not (env.stat().st_mode & 0o040)


def test_remediation_dispatch_wired(tmp_path, monkeypatch):
    """config_tree_perms must route to ensure_config_tree_readable, not fall to
    the "no fix wired up" default (the H3-style dispatch-drift regression)."""
    seen = {}
    monkeypatch.setattr(
        privileged_ops, "ensure_config_tree_readable",
        lambda d: (seen.setdefault("dir", d), (True, "ok"))[1],
    )
    ok, _ = remediation.apply_fix("config_tree_perms", tmp_path)
    assert ok and seen.get("dir") == tmp_path
