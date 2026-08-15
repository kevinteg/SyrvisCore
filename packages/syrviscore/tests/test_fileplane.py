"""Tests for file-plane share volumes (fileplane=) and raw port publishes.

The fileplane feature is the sanctioned bridge between two worlds the schema
otherwise keeps apart: service declarations (which may never name an absolute
host path) and operator-declared NAS file shares (shares.d). These tests pin
the whole contract: YAML-mapping normalization, canonical-string validation,
registry resolution, the resting-share rw sanction, the never-mkdir rule, and
the ports escape hatch for non-HTTP data planes.
"""

import pytest

from syrviscore.service_schema import (
    ServiceDefinition,
    ServiceValidationError,
    _validate_ports,
    _validate_volume,
)
from syrviscore.shares_registry import (
    SharesRegistryError,
    load_shares,
    resolve_fileplane,
)

PINNED_IMAGE = "docker.io/library/alpine:3.20@sha256:" + "a" * 64


def _svc(**over):
    base = {
        "name": "romm",
        "version": "1.0.0",
        "image": PINNED_IMAGE,
    }
    base.update(over)
    return ServiceDefinition.from_dict(base)


# ---------------------------------------------------------------------------
# Schema: normalization + validation
# ---------------------------------------------------------------------------


def test_mapping_form_normalizes_to_canonical_string():
    svc = _svc(
        volumes=[{"fileplane": {"share": "gaming", "subpath": "Emulation/ROMS", "mount": "/roms"}}]
    )
    assert svc.volumes == ["fileplane=gaming/Emulation/ROMS:/roms:ro"]


def test_mode_defaults_ro_and_rw_is_expressible():
    svc = _svc(volumes=[{"fileplane": {"share": "gaming", "mount": "/roms", "mode": "rw"}}])
    assert svc.volumes == ["fileplane=gaming:/roms:rw"]


def test_canonical_string_round_trips_through_from_dict():
    svc = _svc(volumes=["fileplane=gaming/Emulation/ROMS:/roms:ro"])
    assert svc.volumes == ["fileplane=gaming/Emulation/ROMS:/roms:ro"]
    assert svc.to_dict()["volumes"] == ["fileplane=gaming/Emulation/ROMS:/roms:ro"]


@pytest.mark.parametrize(
    "spec",
    [
        {"share": "Bad_Share", "mount": "/x"},  # uppercase/underscore id
        {"share": "gaming", "subpath": "../escape", "mount": "/x"},  # traversal
        {"share": "gaming", "subpath": "/abs", "mount": "/x"},  # absolute subpath
        {"share": "gaming", "mount": "relative"},  # container path not absolute
        {"share": "gaming", "mount": "/x", "mode": "z"},  # bad mode
        {"share": "gaming", "mount": "/x", "bogus": 1},  # unknown key
    ],
)
def test_bad_fileplane_mappings_are_refused(spec):
    with pytest.raises(ServiceValidationError):
        _svc(volumes=[{"fileplane": spec}])


def test_absolute_host_paths_still_refused_for_ordinary_volumes():
    with pytest.raises(ServiceValidationError):
        _validate_volume("/volume5/Gaming:/roms:ro")


def test_fileplane_prefix_cannot_smuggle_env_expansion():
    with pytest.raises(ServiceValidationError):
        _validate_volume("fileplane=gaming/$HOME:/roms:ro")


# ---------------------------------------------------------------------------
# Shares registry
# ---------------------------------------------------------------------------


def _write_share(home, name, body):
    d = home / "shares.d"
    d.mkdir(parents=True, exist_ok=True)
    (d / (name + ".yaml")).write_text(body)


def test_load_and_resolve(tmp_path):
    _write_share(
        tmp_path,
        "gaming",
        "share_name: Gaming\nvolume: /volume5\nclass: resting\nwriters: [syncthing]\n",
    )
    reg = load_shares(tmp_path)
    assert set(reg) == {"gaming"}
    host = resolve_fileplane(reg, "romm", "gaming", "Emulation/ROMS", "ro")
    assert host == "/volume5/Gaming/Emulation/ROMS"


def test_registry_extra_keys_ignored(tmp_path):
    _write_share(
        tmp_path,
        "gaming",
        "share_name: Gaming\nvolume: /volume5\nacl:\n  kevin: rw\nsensitivity: low\n",
    )
    assert load_shares(tmp_path)["gaming"].root == "/volume5/Gaming"


def test_undeclared_share_refused(tmp_path):
    with pytest.raises(SharesRegistryError) as exc:
        resolve_fileplane(load_shares(tmp_path), "romm", "gaming", "", "ro")
    assert "undeclared share" in str(exc.value)


def test_rw_on_resting_share_requires_writers_sanction(tmp_path):
    _write_share(
        tmp_path,
        "gaming",
        "share_name: Gaming\nvolume: /volume5\nclass: resting\nwriters: [syncthing]\n",
    )
    reg = load_shares(tmp_path)
    # sanctioned writer: allowed
    assert resolve_fileplane(reg, "syncthing", "gaming", "installs", "rw")
    # anyone else: refused
    with pytest.raises(SharesRegistryError) as exc:
        resolve_fileplane(reg, "romm", "gaming", "installs", "rw")
    assert "writers" in str(exc.value)


def test_malformed_declaration_raises_not_skips(tmp_path):
    _write_share(tmp_path, "broken", "share_name: [not, a, string]\nvolume: /volume5\n")
    with pytest.raises(SharesRegistryError):
        load_shares(tmp_path)


# ---------------------------------------------------------------------------
# Render (service_manager compose generation)
# ---------------------------------------------------------------------------


def _manager_with_share(tmp_path, share_body):
    from syrviscore.service_manager import ServiceManager

    home = tmp_path / "syrviscore"
    for sub in ("services", "compose", "data", "config/services.d"):
        (home / sub).mkdir(parents=True)
    _write_share(home, "gaming", share_body)
    return ServiceManager(syrvis_home=home), home


def test_render_resolves_declared_share_ro(tmp_path):
    mgr, home = _manager_with_share(tmp_path, "share_name: Gaming\nvolume: {}\n".format(tmp_path))
    roms = tmp_path / "Gaming" / "Emulation" / "ROMS"
    roms.mkdir(parents=True)
    svc = _svc(volumes=["fileplane=gaming/Emulation/ROMS:/roms:ro"])
    compose_path = mgr._generate_compose_file(svc)
    text = compose_path.read_text()
    assert "{}:/roms:ro".format(roms) in text


def test_render_refuses_missing_fileplane_dir(tmp_path):
    mgr, home = _manager_with_share(tmp_path, "share_name: Gaming\nvolume: {}\n".format(tmp_path))
    svc = _svc(volumes=["fileplane=gaming/Emulation/ROMS:/roms:ro"])
    with pytest.raises(SharesRegistryError) as exc:
        mgr._generate_compose_file(svc)
    assert "does not exist" in str(exc.value)


def test_render_never_creates_or_chmods_fileplane_dirs(tmp_path):
    mgr, home = _manager_with_share(tmp_path, "share_name: Gaming\nvolume: {}\n".format(tmp_path))
    svc = _svc(volumes=["fileplane=gaming/Emulation/ROMS:/roms:ro"])
    try:
        mgr._generate_compose_file(svc)
    except SharesRegistryError:
        pass
    assert not (tmp_path / "Gaming").exists()


# ---------------------------------------------------------------------------
# Ports
# ---------------------------------------------------------------------------


def test_ports_validate_and_normalize():
    assert _validate_ports(["22000:22000/tcp", "21027:21027/udp", "8384:8384"]) == [
        "22000:22000/tcp",
        "21027:21027/udp",
        "8384:8384/tcp",
    ]


@pytest.mark.parametrize("entry", ["22000", "0:80", "80:70000", "a:b", "80:80/sctp", ""])
def test_bad_ports_refused(entry):
    with pytest.raises(ServiceValidationError):
        _validate_ports([entry])


def test_ports_render_into_compose(tmp_path):
    mgr, home = _manager_with_share(tmp_path, "share_name: Gaming\nvolume: /volume5\n")
    svc = _svc(ports=["22000:22000/tcp", "22000:22000/udp"])
    compose_path = mgr._generate_compose_file(svc)
    text = compose_path.read_text()
    assert "22000:22000/tcp" in text and "22000:22000/udp" in text


def test_ports_round_trip():
    svc = _svc(ports=["22000:22000"])
    assert svc.ports == ["22000:22000/tcp"]
    assert svc.to_dict()["ports"] == ["22000:22000/tcp"]
