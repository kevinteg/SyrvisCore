"""The installed manifest must stay readable by the operator.

Regression for: a reconcile runs as root, so a manifest carrying inline env that
dump_definition writes 0600 lands root:root and locks the operator out of
`service list` ("Failed to load service definition"). _write_manifest gives it
the config-tree group + 0640 (0644 without inline env).
"""
from pathlib import Path

from syrviscore.service_manager import ServiceManager
from syrviscore.service_schema import load_service_definition


def _decl(home: Path, name: str, env: bool) -> Path:
    d = home / "config" / "services.d"
    d.mkdir(parents=True, exist_ok=True)
    body = ("name: {0}\nversion: \"0.1.0\"\nimage: nginx:1\ncontainer_name: {0}\n"
            "traefik:\n  enabled: false\n").format(name)
    if env:
        body += "environment:\n  - FOO=bar\n"
    p = d / "{}.yaml".format(name)
    p.write_text(body)
    return p


def _write(home: Path, name: str, env: bool):
    svc = load_service_definition(_decl(home, name, env))
    svc_path = home / "services" / name
    svc_path.mkdir(parents=True, exist_ok=True)
    ServiceManager(syrvis_home=home)._write_manifest(svc, svc_path)
    return svc_path / "syrvis-service.yaml"


def test_manifest_with_env_is_group_readable_not_0600(tmp_path):
    home = tmp_path / "syrviscore"
    m = _write(home, "withenv", env=True)
    assert m.stat().st_mode & 0o777 == 0o640          # NOT 0600 — operator can read
    # group inherited from config/services.d (the operator's shared group)
    assert m.stat().st_gid == (home / "config" / "services.d").stat().st_gid


def test_manifest_without_env_is_world_readable(tmp_path):
    home = tmp_path / "syrviscore"
    m = _write(home, "noenv", env=False)
    assert m.stat().st_mode & 0o777 == 0o644
