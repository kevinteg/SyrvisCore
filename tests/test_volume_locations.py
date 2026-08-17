"""design/37 §4 Phase 1 — `volume_locations:`, per-NAMED-VOLUME placement.

The gap this closes (design/37 §3): the platform could place a SERVICE
(`location:`) but not a VOLUME, and `immich-server` needs exactly the split that
model cannot express — `upload` (bulk) and `thumbs`/`encoded-video` (hot) in ONE
container, which is also Immich's own sanctioned nested-bind pattern.
design/64 D7 adds the second consumer: `location: /volume6` for the app home
plus `volume_locations: {upload: /volume5}` for the bulk tree.

The feature is mechanical, not semantic: it places named volumes and knows
nothing about what they hold (semantics stay in `data.d`). Every refusal below
mirrors an existing `location:` behavior.
"""

import pytest
import yaml

from syrviscore import paths, services_d
from syrviscore.service_manager import ServiceManager
from syrviscore.service_schema import ServiceDefinition, ServiceValidationError

from conftest import stamp_install_root


@pytest.fixture
def sim(tmp_path, monkeypatch):
    """A fake DSM volume layout: install on /volume4, data volumes 5 and 6."""
    monkeypatch.setenv("DSM_SIM_ACTIVE", "1")
    monkeypatch.setenv("DSM_SIM_ROOT", str(tmp_path))
    monkeypatch.delenv(paths.APPS_ROOT_NAME_ENV, raising=False)
    for n in (4, 5, 6):
        (tmp_path / "volume{}".format(n)).mkdir()
    home = stamp_install_root(tmp_path / "volume4" / "syrviscore")
    monkeypatch.setenv("SYRVIS_HOME", str(home))
    monkeypatch.setenv("DOMAIN", "example.com")
    return tmp_path


@pytest.fixture
def home(sim):
    return sim / "volume4" / "syrviscore"


def _manager(home):
    return ServiceManager(syrvis_home=home)


#: The design/37 §4 declaration, verbatim in shape.
IMMICH = {
    "name": "immich-server",
    "version": "1",
    "image": "ghcr.io/immich-app/immich-server:v1.0.0",
    "location": "/volume5",
    "volumes": [
        "upload:/usr/src/app/upload:rw",
        "thumbs:/usr/src/app/upload/thumbs:rw",
        "encoded-video:/usr/src/app/upload/encoded-video:rw",
    ],
    "volume_locations": {"thumbs": "/volume6", "encoded-video": "/volume6"},
}


def _immich(**overrides):
    doc = dict(IMMICH)
    doc.update(overrides)
    return ServiceDefinition.from_dict(doc)


# ---------------------------------------------------------------------------
# §4 point 2 — validation
# ---------------------------------------------------------------------------


class TestValidation:
    def test_the_design37_immich_declaration_parses(self):
        svc = _immich()
        assert svc.volume_locations == {"encoded-video": "/volume6", "thumbs": "/volume6"}
        assert svc.location == "/volume5"

    def test_it_round_trips_through_to_dict(self):
        svc = _immich()
        assert ServiceDefinition.from_dict(svc.to_dict()).volume_locations == svc.volume_locations

    def test_absent_is_omitted_entirely(self):
        assert (
            "volume_locations"
            not in ServiceDefinition.from_dict(
                {"name": "a", "version": "1", "image": "a/b:1"}
            ).to_dict()
        )

    def test_an_unknown_key_is_a_parse_error(self):
        # Fail closed: a typo must not silently leave the data on the slow volume.
        with pytest.raises(ServiceValidationError, match="does not name a declared volume"):
            _immich(volume_locations={"thumbnails": "/volume6"})

    @pytest.mark.parametrize(
        "value", ["/volume6/sub", "volume6", "/etc", "/volume6/", "", "/volume6/../volume1", 6]
    )
    def test_the_value_must_be_a_bare_volume_root(self, value):
        with pytest.raises(ServiceValidationError):
            _immich(volume_locations={"thumbs": value})

    def test_the_semantic_slots_are_never_overridable(self):
        with pytest.raises(ServiceValidationError, match="semantic config/logs/secrets"):
            ServiceDefinition.from_dict(
                {
                    "name": "a",
                    "version": "1",
                    "image": "a/b:1",
                    "location": "/volume5",
                    "volumes": ["config:/etc/a", "d:/d"],
                    "volume_locations": {"config": "/volume6"},
                }
            )

    def test_an_infra_host_mount_is_never_overridable(self):
        with pytest.raises(ServiceValidationError, match="infra-tier host mounts"):
            ServiceDefinition.from_dict(
                {
                    "name": "node-exporter",
                    "version": "1",
                    "image": "a/b:1",
                    "location": "/volume5",
                    "tier": "infra",
                    "volumes": ["/proc:/host/proc:ro", "d:/d"],
                    "volume_locations": {"/proc": "/volume6"},
                }
            )

    def test_a_fileplane_bind_is_not_in_the_key_space(self):
        with pytest.raises(ServiceValidationError, match="does not name a declared volume"):
            ServiceDefinition.from_dict(
                {
                    "name": "romm",
                    "version": "1",
                    "image": "a/b:1",
                    "location": "/volume5",
                    "volumes": [
                        {"fileplane": {"share": "gaming", "subpath": "ROMS", "mount": "/roms"}},
                        "d:/d",
                    ],
                    "volume_locations": {"gaming": "/volume6"},
                }
            )

    def test_overlapping_keys_are_refused(self):
        with pytest.raises(ServiceValidationError, match="overlap"):
            ServiceDefinition.from_dict(
                {
                    "name": "a",
                    "version": "1",
                    "image": "a/b:1",
                    "location": "/volume5",
                    "volumes": ["up:/u", "up/th:/u/th"],
                    "volume_locations": {"up": "/volume6", "up/th": "/volume6"},
                }
            )

    @pytest.mark.parametrize("key", ["../evil", "/abs", ""])
    def test_absolute_and_escaping_keys_are_refused(self, key):
        with pytest.raises(ServiceValidationError):
            _immich(volume_locations={key: "/volume6"})

    def test_it_requires_a_location(self):
        # Per-volume placement materializes the v2 tree; a legacy app must adopt
        # a home first rather than sprout half a v2 layout on another volume.
        with pytest.raises(ServiceValidationError, match="requires location"):
            ServiceDefinition.from_dict(
                {
                    "name": "a",
                    "version": "1",
                    "image": "a/b:1",
                    "volumes": ["d:/d"],
                    "volume_locations": {"d": "/volume6"},
                }
            )

    def test_a_non_mapping_is_refused(self):
        with pytest.raises(ServiceValidationError, match="must be a mapping"):
            _immich(volume_locations=["thumbs"])


# ---------------------------------------------------------------------------
# §4 points 1, 3 and 5 — materialization, containment, compose output
# ---------------------------------------------------------------------------


class TestComposeGeneration:
    def _binds(self, home, service):
        sm = _manager(home)
        ok, msg = sm.install_declaration(service, start=False)
        assert ok, msg
        compose = yaml.safe_load((home / "compose" / "{}.yaml".format(service.name)).read_text())
        return {
            entry.split(":")[1]: entry.split(":")[0]
            for entry in compose["services"][service.name]["volumes"]
        }

    def test_the_immich_split_binds_to_two_volumes(self, home, sim):
        binds = self._binds(home, _immich())
        apps = "{}/apps/immich-server".format(paths.PACKAGE_NAME)
        # The bulk tree stays on the app's own home volume...
        assert binds["/usr/src/app/upload"] == str(sim / "volume5" / apps / "data" / "upload")
        # ...and the two hot derivative dirs land on /volume6.
        assert binds["/usr/src/app/upload/thumbs"] == str(
            sim / "volume6" / apps / "data" / "thumbs"
        )
        assert binds["/usr/src/app/upload/encoded-video"] == str(
            sim / "volume6" / apps / "data" / "encoded-video"
        )

    def test_the_app_home_itself_does_not_move(self, home, sim):
        sm = _manager(home)
        assert sm.install_declaration(_immich(), start=False)[0]
        apps = "{}/apps/immich-server".format(paths.PACKAGE_NAME)
        # config/secrets/logs and the manifest all stay on /volume5.
        for slot in ("config", "secrets", "logs"):
            assert (sim / "volume5" / apps / slot).is_dir()
            assert not (sim / "volume6" / apps / slot).exists()

    def test_the_override_directories_are_pre_created(self, home, sim):
        sm = _manager(home)
        assert sm.install_declaration(_immich(), start=False)[0]
        apps = "{}/apps/immich-server".format(paths.PACKAGE_NAME)
        # DSM's docker refuses to auto-create a bind-mount source.
        assert (sim / "volume6" / apps / "data" / "thumbs").is_dir()
        assert (sim / "volume6" / apps / "data" / "encoded-video").is_dir()

    def test_absent_overrides_are_byte_for_byte_todays_compose(self, home):
        sm = _manager(home)
        plain = ServiceDefinition.from_dict(
            {
                "name": "plain",
                "version": "1",
                "image": "a/b:1",
                "location": "/volume5",
                "volumes": ["data:/data"],
            }
        )
        assert sm.install_declaration(plain, start=False)[0]
        text = (home / "compose" / "plain.yaml").read_text()
        assert "volume_locations" not in text

    def test_the_generated_compose_names_each_override(self, home):
        # §4 point 5: drift inspection must stay legible — a bind pointing at
        # /volume6 in a service homed on /volume5 must not read as a mystery.
        sm = _manager(home)
        assert sm.install_declaration(_immich(), start=False)[0]
        text = (home / "compose" / "immich-server.yaml").read_text()
        assert "# volume_locations: thumbs -> /volume6" in text
        assert "# volume_locations: encoded-video -> /volume6" in text
        # ...and it is still valid YAML.
        assert yaml.safe_load(text)["services"]["immich-server"]["image"]

    def test_nested_container_targets_are_emitted_as_declared(self, home):
        # Docker mounts nested targets correctly regardless of list order (the
        # engine sorts by destination) — this is Immich's documented pattern.
        binds = self._binds(home, _immich())
        assert "/usr/src/app/upload" in binds
        assert "/usr/src/app/upload/thumbs" in binds

    def test_an_unmounted_override_volume_refuses_before_any_mkdir(self, home, sim):
        # design/26 release-blocker #2, scoped to one volume: materializing an
        # empty tree on a bare mountpoint presents as data loss when the real
        # volume mounts over it later.
        sm = _manager(home)
        svc = _immich(volume_locations={"thumbs": "/volume9"})  # never created
        ok, msg = sm.install_declaration(svc, start=False)
        assert not ok
        assert "volume_locations['thumbs'] = '/volume9' is not a mounted volume" in msg
        assert not (sim / "volume9").exists()

    def test_the_refusal_names_the_volume_not_just_the_service(self, home):
        sm = _manager(home)
        ok, msg = sm.install_declaration(
            _immich(volume_locations={"encoded-video": "/volume9"}), start=False
        )
        assert not ok and "encoded-video" in msg

    def test_a_symlinked_override_root_is_refused(self, home, sim):
        (sim / "volume7").symlink_to(sim / "volume6")
        sm = _manager(home)
        ok, msg = sm.install_declaration(
            _immich(volume_locations={"thumbs": "/volume7"}), start=False
        )
        assert not ok and "not a mounted volume" in msg


class TestServicePaths:
    def test_override_roots_are_derived_and_containment_asserted(self, home, sim):
        sm = _manager(home)
        svc = _immich()
        p = sm._service_paths("immich-server", svc)
        roots = sm._override_roots(p)
        apps = "{}/apps/immich-server".format(paths.PACKAGE_NAME)
        assert roots["thumbs"] == sim / "volume6" / apps / "data" / "thumbs"
        # The four standard slots are untouched by the override.
        assert p["config"] == sim / "volume5" / apps / "config"

    def test_a_symlinked_override_home_cannot_redirect(self, home, sim):
        sm = _manager(home)
        apps_base = sim / "volume6" / paths.PACKAGE_NAME / "apps"
        apps_base.mkdir(parents=True)
        elsewhere = sim / "elsewhere"
        elsewhere.mkdir()
        (apps_base / "immich-server").symlink_to(elsewhere)
        with pytest.raises(ServiceValidationError, match="escapes"):
            sm._service_paths("immich-server", _immich())

    def test_name_only_callers_read_the_overrides_from_the_manifest(self, home, sim):
        # A lifecycle op holding only a NAME still has to find every tree the
        # app owns, or a purge silently orphans one.
        sm = _manager(home)
        assert sm.install_declaration(_immich(), start=False)[0]
        roots = sm._override_roots(sm._service_paths("immich-server"))
        assert set(roots) == {"thumbs", "encoded-video"}

    def test_a_service_without_overrides_has_none(self, home):
        sm = _manager(home)
        p = sm._service_paths(
            "plain",
            ServiceDefinition.from_dict({"name": "plain", "version": "1", "image": "a/b:1"}),
        )
        assert sm._override_roots(p) == {}


# ---------------------------------------------------------------------------
# §4 point 4 — the per-volume change refusal
# ---------------------------------------------------------------------------


class TestChangeRefusal:
    def _installed(self, home):
        sm = _manager(home)
        assert sm.install_declaration(_immich(), start=False)[0]
        return sm

    def test_no_change_proceeds(self, home):
        sm = self._installed(home)
        assert (
            sm._volume_location_change_refusal("immich-server", _immich().volume_locations) is None
        )

    def test_changing_an_override_with_data_present_is_refused(self, home, sim):
        sm = self._installed(home)
        apps = "{}/apps/immich-server".format(paths.PACKAGE_NAME)
        (sim / "volume6" / apps / "data" / "thumbs" / "a.jpg").write_text("x")
        refusal = sm._volume_location_change_refusal(
            "immich-server", {"thumbs": "/volume4", "encoded-video": "/volume6"}
        )
        assert refusal and "thumbs" in refusal
        assert "app-move.md" in refusal

    def test_an_empty_current_dir_proceeds(self, home):
        # The documented single-volume app-move bypass: stop -> copy -> clear
        # the old dir -> re-declare -> deploy.
        sm = self._installed(home)
        assert (
            sm._volume_location_change_refusal(
                "immich-server", {"thumbs": "/volume4", "encoded-video": "/volume6"}
            )
            is None
        )

    def test_ADDING_an_override_over_populated_data_is_refused(self, home, sim):
        # The nested-bind subtlety (§6): bytes written BEFORE the override
        # existed are SHADOWED, not merged — they must be moved first.
        sm = _manager(home)
        plain = ServiceDefinition.from_dict(
            {
                "name": "immich-server",
                "version": "1",
                "image": "a/b:1",
                "location": "/volume5",
                "volumes": ["thumbs:/t"],
            }
        )
        assert sm.install_declaration(plain, start=False)[0]
        apps = "{}/apps/immich-server".format(paths.PACKAGE_NAME)
        (sim / "volume5" / apps / "data" / "thumbs" / "a.jpg").write_text("x")
        refusal = sm._volume_location_change_refusal("immich-server", {"thumbs": "/volume6"})
        assert refusal and "thumbs" in refusal

    def test_REMOVING_an_override_with_data_is_refused(self, home, sim):
        sm = self._installed(home)
        apps = "{}/apps/immich-server".format(paths.PACKAGE_NAME)
        (sim / "volume6" / apps / "data" / "encoded-video" / "a.mp4").write_text("x")
        assert sm._volume_location_change_refusal("immich-server", {"thumbs": "/volume6"})

    def test_the_reconcile_replace_path_consults_it(self, home, sim):
        sm = self._installed(home)
        apps = "{}/apps/immich-server".format(paths.PACKAGE_NAME)
        (sim / "volume6" / apps / "data" / "thumbs" / "a.jpg").write_text("x")
        moved = _immich(volume_locations={"thumbs": "/volume4", "encoded-video": "/volume6"})
        plan = {
            "actions": [
                {
                    "kind": "replace",
                    "name": "immich-server",
                    "critical": False,
                    "destructive": False,
                }
            ]
        }
        results = services_d.apply_reconcile_plan(sm, {"immich-server": moved}, plan)
        assert results[0]["ok"] is False
        assert "thumbs" in results[0]["message"]
        # ...and nothing was torn down.
        assert (sim / "volume6" / apps / "data" / "thumbs" / "a.jpg").exists()


class TestPurge:
    def test_purge_removes_the_override_trees_too(self, home, sim):
        # Leaving them behind is the silent-orphan class: unreferenced bytes on
        # another volume that nothing names.
        sm = _manager(home)
        assert sm.install_declaration(_immich(), start=False)[0]
        apps = "{}/apps/immich-server".format(paths.PACKAGE_NAME)
        (sim / "volume6" / apps / "data" / "thumbs" / "a.jpg").write_text("x")
        ok, msg = sm.remove("immich-server", purge=True)
        assert ok, msg
        assert not (sim / "volume6" / apps / "data" / "thumbs").exists()
        assert not (sim / "volume5" / apps).exists()
        assert "thumbs" in msg and "encoded-video" in msg

    def test_a_non_purge_removal_keeps_them(self, home, sim):
        sm = _manager(home)
        assert sm.install_declaration(_immich(), start=False)[0]
        apps = "{}/apps/immich-server".format(paths.PACKAGE_NAME)
        (sim / "volume6" / apps / "data" / "thumbs" / "a.jpg").write_text("x")
        assert sm.remove("immich-server", purge=False)[0]
        assert (sim / "volume6" / apps / "data" / "thumbs" / "a.jpg").exists()


class TestDesign64D7:
    """The second named consumer: home on the fast volume, bulk tree on the slow one."""

    def test_the_home_and_the_bulk_tree_can_diverge(self, home, sim):
        svc = ServiceDefinition.from_dict(
            {
                "name": "immich-server",
                "version": "1",
                "image": "a/b:1",
                "location": "/volume6",
                "volumes": ["upload:/usr/src/app/upload:rw"],
                "volume_locations": {"upload": "/volume5"},
            }
        )
        sm = _manager(home)
        assert sm.install_declaration(svc, start=False)[0]
        apps = "{}/apps/immich-server".format(paths.PACKAGE_NAME)
        compose = yaml.safe_load((home / "compose" / "immich-server.yaml").read_text())
        bind = compose["services"]["immich-server"]["volumes"][0]
        assert bind.startswith(str(sim / "volume5" / apps / "data" / "upload"))
        # The app home — config slot, secrets slot, logs — rides /volume6.
        assert (sim / "volume6" / apps / "config").is_dir()
