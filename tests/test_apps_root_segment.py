"""The CONFIGURABLE apps-root segment (``SYRVIS_APPS_ROOT_NAME``), 0.5.14.

Owner ruling 2026-08-16: the DSM share ``syrviscore`` on /volume5 is renamed to
``syrviscore-apps`` to retire the cold-boot share-collision class that decapitated
the homebase. That rename is only possible because the platform stops hardcoding
the middle segment of a located app's home — ``<location>/syrviscore/apps/<name>``
becomes ``<location>/<SYRVIS_APPS_ROOT_NAME>/apps/<name>``.

Four properties are pinned here:

  * the value flows exactly like every other instance-config value (process env,
    then ``$SYRVIS_HOME/config/.env``), defaults to the historical name, and
    REFUSES a value that is not a single safe path segment;
  * every app-home derivation — the manager's ``_app_home``, the compose bind
    sources, the deployment record's config checksums — follows it;
  * the rootfs boot reclaim guard watches the configured name too (a rename of
    the apps roots is guarded from the moment it is configured, not after the
    next release);
  * ``syrvis verify`` FAILS loudly while the configured segment and the trees on
    disk disagree — the window-day safety net, because "absent" reads as CREATE
    to a declarative engine and that is how empty trees get started over real
    databases.
"""

import os
import subprocess
from pathlib import Path

import pytest
import yaml

from syrviscore import paths, privileged_ops, validators
from syrviscore.service_manager import ServiceManager
from syrviscore.service_schema import ServiceDefinition

from conftest import stamp_install_root

SEGMENT = "syrviscore-apps"


def _write_env(home: Path, **values) -> Path:
    """Write a minimal ``config/.env`` — the real read path, not a monkeypatch."""
    home = Path(home)
    (home / "config").mkdir(parents=True, exist_ok=True)
    env = home / "config" / ".env"
    env.write_text("".join("{}={}\n".format(k, v) for k, v in values.items()))
    return env


# ---------------------------------------------------------------------------
# The config value itself
# ---------------------------------------------------------------------------


class TestAppsRootNameConfig:
    def test_defaults_to_the_package_name_when_nothing_is_set(self, tmp_path, monkeypatch):
        monkeypatch.delenv(paths.APPS_ROOT_NAME_ENV, raising=False)
        home = stamp_install_root(tmp_path / "syrviscore")
        assert paths.get_apps_root_name(home) == paths.PACKAGE_NAME

    def test_reads_the_instance_env_file(self, tmp_path, monkeypatch):
        monkeypatch.delenv(paths.APPS_ROOT_NAME_ENV, raising=False)
        home = stamp_install_root(tmp_path / "syrviscore")
        _write_env(home, DOMAIN="example.com", SYRVIS_APPS_ROOT_NAME=SEGMENT)
        assert paths.get_apps_root_name(home) == SEGMENT

    def test_process_env_wins_over_the_file(self, tmp_path, monkeypatch):
        home = stamp_install_root(tmp_path / "syrviscore")
        _write_env(home, SYRVIS_APPS_ROOT_NAME=SEGMENT)
        monkeypatch.setenv(paths.APPS_ROOT_NAME_ENV, "from-env")
        assert paths.get_apps_root_name(home) == "from-env"

    def test_blank_means_default_not_empty(self, tmp_path, monkeypatch):
        """setup.py emits the key blank so re-runs preserve it — blank is UNSET."""
        home = stamp_install_root(tmp_path / "syrviscore")
        _write_env(home, SYRVIS_APPS_ROOT_NAME="")
        monkeypatch.setenv(paths.APPS_ROOT_NAME_ENV, "")
        assert paths.get_apps_root_name(home) == paths.PACKAGE_NAME

    def test_quoted_values_are_unwrapped(self, tmp_path, monkeypatch):
        monkeypatch.delenv(paths.APPS_ROOT_NAME_ENV, raising=False)
        home = stamp_install_root(tmp_path / "syrviscore")
        (home / "config").mkdir(parents=True, exist_ok=True)
        (home / "config" / ".env").write_text('SYRVIS_APPS_ROOT_NAME="{}"\n'.format(SEGMENT))
        assert paths.get_apps_root_name(home) == SEGMENT

    def test_an_unresolvable_home_falls_back_instead_of_raising(self, monkeypatch):
        monkeypatch.delenv(paths.APPS_ROOT_NAME_ENV, raising=False)
        monkeypatch.setattr(
            paths, "get_syrvis_home", lambda: (_ for _ in ()).throw(paths.SyrvisHomeError("nope"))
        )
        assert paths.get_apps_root_name() == paths.PACKAGE_NAME

    @pytest.mark.parametrize(
        "bad",
        [
            "with/slash",
            "..",
            ".",
            "../escape",
            ".hidden",
            "has space",
            "syrviscore_1",  # DSM's own collision suffix — never a CONFIGURED name
            "apps_12",
        ],
    )
    def test_an_unsafe_segment_raises_rather_than_falling_back(self, tmp_path, monkeypatch, bad):
        """Silently deriving a DIFFERENT tree than configured is the failure mode
        that starts empty databases over real ones. Fail closed, by name."""
        monkeypatch.setenv(paths.APPS_ROOT_NAME_ENV, bad)
        with pytest.raises(paths.AppsRootNameError):
            paths.get_apps_root_name(tmp_path)


# ---------------------------------------------------------------------------
# Derivation: app home, compose binds, deployment records
# ---------------------------------------------------------------------------


def _v2_mgr(tmp_path, monkeypatch, segment=SEGMENT):
    """A ServiceManager whose instance config names ``segment`` as the apps root."""
    monkeypatch.delenv(paths.APPS_ROOT_NAME_ENV, raising=False)
    os.environ.setdefault("DOMAIN", "example.com")
    home = stamp_install_root(tmp_path / "home")
    if segment is not None:
        _write_env(home, DOMAIN="example.com", SYRVIS_APPS_ROOT_NAME=segment)
    monkeypatch.setattr(
        paths,
        "resolve_volume_root",
        lambda loc: tmp_path / "volumes" / str(loc).lstrip("/"),
    )
    monkeypatch.setattr(paths, "is_mounted_volume", lambda loc: True)
    mgr = ServiceManager(syrvis_home=home)
    mgr._ensure_directories()
    mgr._start_service = lambda n, cp: (True, "started")
    mgr._reload_traefik = lambda: None
    return mgr


class TestDerivation:
    def test_app_home_uses_the_configured_segment(self, tmp_path, monkeypatch):
        mgr = _v2_mgr(tmp_path, monkeypatch)
        svc = ServiceDefinition.from_dict(
            {"name": "pg", "version": "1.0.0", "image": "postgres:16.4", "location": "/volume6"}
        )
        assert mgr._app_home(svc) == tmp_path / "volumes" / "volume6" / SEGMENT / "apps" / "pg"

    def test_default_segment_reproduces_the_historical_path(self, tmp_path, monkeypatch):
        mgr = _v2_mgr(tmp_path, monkeypatch, segment=None)
        svc = ServiceDefinition.from_dict(
            {"name": "pg", "version": "1.0.0", "image": "postgres:16.4", "location": "/volume6"}
        )
        assert mgr._app_home(svc) == (
            tmp_path / "volumes" / "volume6" / "syrviscore" / "apps" / "pg"
        )

    def test_install_materializes_the_tree_under_the_configured_segment(
        self, tmp_path, monkeypatch
    ):
        mgr = _v2_mgr(tmp_path, monkeypatch)
        svc = ServiceDefinition.from_dict(
            {
                "name": "pg",
                "version": "1.0.0",
                "image": "postgres:16.4",
                "location": "/volume6",
                "volumes": ["pgdata:/var/lib/postgresql/data"],
                "env_file": "secrets.env",
            }
        )
        ok, msg = mgr.install_declaration(svc, start=False)
        assert ok, msg

        home = tmp_path / "volumes" / "volume6" / SEGMENT / "apps" / "pg"
        for slot in ("data", "config", "secrets", "logs"):
            assert (home / slot).is_dir()
        # and NOTHING was created under the historical name
        assert not (tmp_path / "volumes" / "volume6" / "syrviscore").exists()

    def test_compose_bind_sources_follow_the_segment(self, tmp_path, monkeypatch):
        mgr = _v2_mgr(tmp_path, monkeypatch)
        svc = ServiceDefinition.from_dict(
            {
                "name": "pg",
                "version": "1.0.0",
                "image": "postgres:16.4",
                "location": "/volume6",
                "volumes": ["pgdata:/var/lib/postgresql/data"],
                "env_file": "secrets.env",
            }
        )
        ok, msg = mgr.install_declaration(svc, start=False)
        assert ok, msg

        compose = yaml.safe_load((tmp_path / "home" / "compose" / "pg.yaml").read_text())
        spec = compose["services"]["pg"]
        expected = str(tmp_path / "volumes" / "volume6" / SEGMENT / "apps" / "pg")
        assert any(v.startswith(expected + "/data") for v in spec["volumes"]), spec["volumes"]
        assert spec["env_file"] == [expected + "/secrets/secrets.env"]

    def test_deployment_config_checksums_read_the_segmented_home(self, tmp_path, monkeypatch):
        from syrviscore import deployments

        mgr = _v2_mgr(tmp_path, monkeypatch)
        svc = ServiceDefinition.from_dict(
            {
                "name": "app",
                "version": "1.0.0",
                "image": "nginx:1.27.0",
                "location": "/volume6",
                "config_templates": [{"source": "t.conf", "dest": "app.conf"}],
            }
        )
        config_dir = tmp_path / "volumes" / "volume6" / SEGMENT / "apps" / "app" / "config"
        config_dir.mkdir(parents=True)
        (config_dir / "app.conf").write_text("key = value\n")

        sums = deployments._config_checksums(mgr.syrvis_home, svc)

        assert list(sums) == ["app.conf"]
        assert sums["app.conf"].startswith("sha256:")


# ---------------------------------------------------------------------------
# The rootfs boot reclaim guard
# ---------------------------------------------------------------------------


class TestBootGuardWatchesTheSegment:
    def test_render_bakes_both_watched_names(self, tmp_path):
        content = privileged_ops.render_boot_script(tmp_path / "install", SEGMENT)
        start = content.split("start)", 1)[1].split("stop)", 1)[0]
        assert 'APPS_ROOT_NAME="{}"'.format(SEGMENT) in start
        assert "syrviscore_[0-9]*" in start  # the install root, never configurable
        assert '"$APPS_ROOT_NAME"_[0-9]*' in start

    def test_contract_marker_was_bumped_for_the_new_capability(self, tmp_path):
        assert privileged_ops.BOOT_HOOK_CONTRACT >= 3
        assert "# boot-hook-contract: {}".format(
            privileged_ops.BOOT_HOOK_CONTRACT
        ) in privileged_ops.render_boot_script(tmp_path / "install", SEGMENT)

    def test_render_stays_valid_posix_shell_with_a_hyphenated_segment(self, tmp_path):
        script = tmp_path / "S99.sh"
        script.write_text(privileged_ops.render_boot_script(tmp_path / "install", SEGMENT))
        assert subprocess.call(["sh", "-n", str(script)]) == 0

    def test_the_boot_env_cache_carries_the_segment(self):
        body = privileged_ops.render_boot_env_cache("https://ntfy.example/t", SEGMENT)
        assert "SYRVIS_APPS_ROOT_NAME='{}'".format(SEGMENT) in body

    def test_ensure_writes_the_segment_into_the_rootfs_cache(self, tmp_path, monkeypatch):
        install = stamp_install_root(tmp_path / "install")
        _write_env(install, NTFY_URL="https://ntfy.example/t", SYRVIS_APPS_ROOT_NAME=SEGMENT)
        monkeypatch.delenv(paths.APPS_ROOT_NAME_ENV, raising=False)
        monkeypatch.setattr(privileged_ops, "BOOT_ENV_PATH", tmp_path / "boot.env")
        monkeypatch.setattr(privileged_ops, "BOOT_SCRIPT_PATH", tmp_path / "S99.sh")

        ok, msg = privileged_ops.DsmOperations().ensure_boot_script(install)

        assert ok, msg
        assert "SYRVIS_APPS_ROOT_NAME='{}'".format(SEGMENT) in (tmp_path / "boot.env").read_text()
        assert 'APPS_ROOT_NAME="{}"'.format(SEGMENT) in (tmp_path / "S99.sh").read_text()

    def test_cache_is_written_even_with_no_ntfy_url(self, tmp_path, monkeypatch):
        """The segment alone is worth caching: it is what lets the MANAGER
        classify a renamed apps root from the rootfs, with no resolvable home."""
        install = stamp_install_root(tmp_path / "install")
        _write_env(install, SYRVIS_APPS_ROOT_NAME=SEGMENT)
        monkeypatch.delenv(paths.APPS_ROOT_NAME_ENV, raising=False)
        monkeypatch.setattr(privileged_ops, "BOOT_ENV_PATH", tmp_path / "boot.env")
        monkeypatch.setattr(privileged_ops, "BOOT_SCRIPT_PATH", tmp_path / "S99.sh")

        ok, msg = privileged_ops.DsmOperations().ensure_boot_script(install)

        assert ok and "no NTFY_URL" in msg
        assert "SYRVIS_APPS_ROOT_NAME='{}'".format(SEGMENT) in (tmp_path / "boot.env").read_text()

    def test_the_validator_renders_with_the_same_segment_as_the_writer(self, tmp_path, monkeypatch):
        """Otherwise every verify on a renamed instance reports permanent drift."""
        install = stamp_install_root(tmp_path / "install")
        _write_env(install, SYRVIS_APPS_ROOT_NAME=SEGMENT)
        monkeypatch.delenv(paths.APPS_ROOT_NAME_ENV, raising=False)
        monkeypatch.setattr(validators.privileged_ops, "BOOT_SCRIPT_PATH", tmp_path / "S99.sh")
        deployed = tmp_path / "S99.sh"
        deployed.write_text(privileged_ops.render_boot_script(install, SEGMENT))

        assert privileged_ops.read_apps_root_name(install) == SEGMENT
        # what the WRITER would produce now equals what is deployed
        assert (
            privileged_ops.render_boot_script(install, privileged_ops.read_apps_root_name(install))
            == deployed.read_text()
        )

    def test_read_apps_root_name_never_raises_on_a_bad_value(self, tmp_path, monkeypatch):
        install = stamp_install_root(tmp_path / "install")
        _write_env(install, SYRVIS_APPS_ROOT_NAME="not/a/segment")
        monkeypatch.delenv(paths.APPS_ROOT_NAME_ENV, raising=False)
        assert privileged_ops.read_apps_root_name(install) == paths.PACKAGE_NAME


def _runnable_hook(tmp_path, install_dir, segment):
    """The shipped hook repointed at a fake /volume tree, minus the seam heal.

    Mirrors ``test_boot_reclaim_guard._runnable_hook`` — two surgical
    substitutions so what runs IS the shipped reclaim guard.
    """
    text = privileged_ops.render_boot_script(Path(install_dir), segment)
    text = text.replace("/volume[0-9]*", "{}/volume[0-9]*".format(tmp_path))
    text = text.replace(str(privileged_ops.BOOT_ENV_PATH), str(tmp_path / "boot.env"))
    head, _, rest = text.partition("        for SEAM_USER in")
    _, _, tail = rest.partition("        done\n")
    script = tmp_path / "S99-runnable.sh"
    script.write_text(head + tail)
    script.chmod(0o755)
    return script


def _renamed_apps_root(tmp_path, volume, name):
    root = tmp_path / volume / name
    (root / "apps" / "immich-db" / "data").mkdir(parents=True)
    (root / "apps" / "immich-db" / "data" / "pg").write_text("cluster")
    return root


@pytest.mark.skipif(os.name != "posix", reason="POSIX shell required")
class TestReclaimGuardExecutionWithASegment:
    def _run(self, script, tmp_path):
        return subprocess.run(
            ["sh", str(script), "start"], capture_output=True, text=True, cwd=str(tmp_path)
        )

    def test_reclaims_a_renamed_apps_root_under_the_configured_name(self, tmp_path):
        renamed = _renamed_apps_root(tmp_path, "volume6", SEGMENT + "_1")
        script = _runnable_hook(tmp_path, tmp_path / "volume4" / "syrviscore", SEGMENT)

        result = self._run(script, tmp_path)

        assert not renamed.exists()
        assert (tmp_path / "volume6" / SEGMENT / "apps" / "immich-db" / "data" / "pg").exists()
        assert "reclaimed" in result.stdout

    def test_still_reclaims_the_install_root_name(self, tmp_path):
        """The install root is NOT configurable; guarding the segment must not
        have displaced it."""
        renamed = tmp_path / "volume4" / "syrviscore_1"
        renamed.mkdir(parents=True)
        (renamed / ".syrviscore-manifest.json").write_text('{"schema_version": 3}')
        (renamed / "payload").write_text("real data")
        script = _runnable_hook(tmp_path, tmp_path / "volume4" / "syrviscore", SEGMENT)

        self._run(script, tmp_path)

        assert (tmp_path / "volume4" / "syrviscore" / "payload").read_text() == "real data"

    def test_the_rootfs_cache_overrides_a_stale_baked_segment(self, tmp_path):
        """A hook rendered before a segment change still guards the current one:
        the cache is refreshed by the same ensure that writes the hook."""
        renamed = _renamed_apps_root(tmp_path, "volume6", "newname_1")
        (tmp_path / "boot.env").write_text("SYRVIS_APPS_ROOT_NAME='newname'\n")
        script = _runnable_hook(tmp_path, tmp_path / "volume4" / "syrviscore", SEGMENT)

        self._run(script, tmp_path)

        assert not renamed.exists()
        assert (tmp_path / "volume6" / "newname" / "apps" / "immich-db").is_dir()

    def test_a_default_install_never_acts_on_the_same_root_twice(self, tmp_path):
        """Both globs are the SAME word when the segment is the default; the
        REFUSING page must not be sent twice."""
        renamed = tmp_path / "volume5" / "syrviscore_1"
        renamed.mkdir(parents=True)
        (renamed / ".syrviscore-manifest.json").write_text("{}")
        impostor = tmp_path / "volume5" / "syrviscore"
        impostor.mkdir(parents=True)
        (impostor / "someone-elses-file").write_text("share content")
        script = _runnable_hook(tmp_path, impostor, paths.PACKAGE_NAME)

        result = self._run(script, tmp_path)

        assert result.stdout.count("REFUSING") == 1
        assert renamed.exists()


# ---------------------------------------------------------------------------
# The migration safety net: `syrvis verify`
# ---------------------------------------------------------------------------


@pytest.fixture
def sim(tmp_path, monkeypatch):
    """A fake DSM volume layout under DSM_SIM_ROOT, with an install on /volume4."""
    monkeypatch.setenv("DSM_SIM_ACTIVE", "1")
    monkeypatch.setenv("DSM_SIM_ROOT", str(tmp_path))
    monkeypatch.delenv(paths.APPS_ROOT_NAME_ENV, raising=False)
    for n in (4, 5, 6):
        (tmp_path / "volume{}".format(n)).mkdir()
    home = stamp_install_root(tmp_path / "volume4" / "syrviscore")
    monkeypatch.setenv("SYRVIS_HOME", str(home))
    return tmp_path


def _install_manifest(home: Path, name: str, location: str) -> None:
    """Materialize the CENTRAL manifest the platform indexes installs by."""
    d = home / "services" / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "syrvis-service.yaml").write_text(
        yaml.safe_dump(
            {"name": name, "version": "1.0.0", "image": "nginx:1.27.0", "location": location}
        )
    )


class TestVerifyAppsRoot:
    def test_missing_segment_tree_fails_and_names_the_mv(self, sim):
        home = sim / "volume4" / "syrviscore"
        _write_env(home, SYRVIS_APPS_ROOT_NAME=SEGMENT)
        _install_manifest(home, "immich-db", "/volume5")
        # the tree is still under the OLD name — the window the ruling opens
        (sim / "volume5" / "syrviscore" / "apps" / "immich-db").mkdir(parents=True)

        result = validators.InstallationValidator().check_apps_root()

        assert result.passed is False
        assert SEGMENT in result.message
        assert str(sim / "volume5" / SEGMENT) in result.message
        assert (
            "sudo mv {} {}".format(sim / "volume5" / "syrviscore", sim / "volume5" / SEGMENT)
            in result.details
        )
        assert "reconcile" in result.details  # do NOT converge into this
        assert result.fixable is False

    def test_a_renamed_sibling_is_offered_as_the_mv_source(self, sim):
        home = sim / "volume4" / "syrviscore"
        _write_env(home, SYRVIS_APPS_ROOT_NAME=SEGMENT)
        _install_manifest(home, "app", "/volume6")
        renamed = sim / "volume6" / (SEGMENT + "_1")
        (renamed / "apps").mkdir(parents=True)

        result = validators.InstallationValidator().check_apps_root()

        assert result.passed is False
        assert "sudo mv {} {}".format(renamed, sim / "volume6" / SEGMENT) in result.details

    def test_passes_once_the_tree_is_moved(self, sim):
        home = sim / "volume4" / "syrviscore"
        _write_env(home, SYRVIS_APPS_ROOT_NAME=SEGMENT)
        _install_manifest(home, "immich-db", "/volume5")
        (sim / "volume5" / SEGMENT / "apps" / "immich-db").mkdir(parents=True)

        result = validators.InstallationValidator().check_apps_root()

        assert result.passed is True
        assert SEGMENT in result.message

    def test_default_install_with_located_services_is_untouched(self, sim):
        home = sim / "volume4" / "syrviscore"
        _install_manifest(home, "immich-db", "/volume5")
        (sim / "volume5" / "syrviscore" / "apps" / "immich-db").mkdir(parents=True)

        result = validators.InstallationValidator().check_apps_root()

        assert result.passed is True
        assert paths.PACKAGE_NAME in result.message

    def test_no_located_services_never_fires(self, sim):
        home = sim / "volume4" / "syrviscore"
        _write_env(home, SYRVIS_APPS_ROOT_NAME=SEGMENT)
        _install_manifest(home, "legacy", "")  # a legacy install has no location

        result = validators.InstallationValidator().check_apps_root()

        assert result.passed is True
        assert "no located services" in result.message

    def test_an_unreadable_env_reports_unknown_rather_than_going_red(self, sim):
        """config/.env is 0600 BY DESIGN. A caller that cannot read it would
        resolve the default segment and then 'discover' a missing tree — the
        0.5.13 permanent-red failure mode. Say 'unknown' instead."""
        if os.geteuid() == 0:
            pytest.skip("root can read anything")
        home = sim / "volume4" / "syrviscore"
        _write_env(home, SYRVIS_APPS_ROOT_NAME=SEGMENT)
        _install_manifest(home, "immich-db", "/volume5")
        (home / "config" / ".env").chmod(0o000)
        try:
            result = validators.InstallationValidator().check_apps_root()
        finally:
            (home / "config" / ".env").chmod(0o600)

        assert result.passed is True
        assert "segment unknown" in result.message

    def test_an_unsafe_segment_is_reported_here_by_name(self, sim, monkeypatch):
        monkeypatch.setenv(paths.APPS_ROOT_NAME_ENV, "../escape")
        result = validators.InstallationValidator().check_apps_root()
        assert result.passed is False
        assert paths.APPS_ROOT_NAME_ENV in result.message

    def test_wired_into_the_installation_report_and_therefore_the_smoke_tier(self, sim):
        home = sim / "volume4" / "syrviscore"
        _write_env(home, SYRVIS_APPS_ROOT_NAME=SEGMENT)
        _install_manifest(home, "immich-db", "/volume5")

        report = validators.validate_installation()

        by_name = {c.name: c for c in report.checks}
        assert "Apps root" in by_name
        assert by_name["Apps root"].passed is False
        assert report.passed is False
