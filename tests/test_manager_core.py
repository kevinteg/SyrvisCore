"""
Hermetic tests for the syrviscore-manager library core.

No network, no real venv/pip: the venv backend is replaced with a fake that
writes marker files, so install/activate/rollback/backup/restore run against
real filesystem state in tmp_path in milliseconds.
"""

import io
import json
import stat
import tarfile

import pytest

from syrviscore_manager import backup, downloader, manifest, paths, version_manager
from syrviscore_manager.errors import (
    ActiveVersionError,
    HomeNotFoundError,
    InstallError,
    IntegrityError,
    InvalidVersionError,
    LockError,
    RestoreError,
    VersionNotFoundError,
)
from syrviscore_manager.locking import hold_lock


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    """Isolate from ambient installation discovery (and other tests' leaks)."""
    monkeypatch.delenv("SYRVIS_HOME", raising=False)
    monkeypatch.delenv("SYNOPKG_PKGDEST", raising=False)
    monkeypatch.delenv("DSM_SIM_ACTIVE", raising=False)
    monkeypatch.delenv("DSM_SIM_ROOT", raising=False)


@pytest.fixture
def home(tmp_path):
    return tmp_path / "syrviscore"


@pytest.fixture
def fake_venv_backend(monkeypatch):
    """Replace venv creation and pip install with fast filesystem fakes."""

    def _create_venv(venv_path):
        (venv_path / "bin").mkdir(parents=True, exist_ok=True)
        (venv_path / "bin" / "pip").write_text("#!/bin/sh\n")

    def _pip_install_wheel(venv_path, wheel_path):
        # Real pip bakes the venv's absolute path into script shebangs;
        # embed it here so the staging->final relocation fixup is exercised.
        (venv_path / "bin" / "syrvis").write_text(
            "#!/bin/sh\n# venv: {}\necho fake-syrvis\n".format(venv_path)
        )

    monkeypatch.setattr(version_manager, "_create_venv", _create_venv)
    monkeypatch.setattr(version_manager, "_pip_install_wheel", _pip_install_wheel)


def make_wheel(tmp_path, version="0.1.0"):
    wheel = tmp_path / "syrviscore-{}-py3-none-any.whl".format(version)
    wheel.write_bytes(b"fake wheel contents for " + version.encode())
    return wheel


def install(home, tmp_path, version="0.1.0", activate=True):
    wheel = make_wheel(tmp_path, version)
    return version_manager.install_from_wheel(home, wheel, activate=activate)


# =============================================================================
# Version validation and path containment
# =============================================================================


class TestVersionValidation:
    def test_valid_versions(self):
        assert paths.validate_version("0.1.0") == "0.1.0"
        assert paths.validate_version("v1.2.3") == "1.2.3"
        assert paths.validate_version("10.20.30") == "10.20.30"

    @pytest.mark.parametrize(
        "bad",
        ["../..", "0.1", "0.1.0-rc1", "", "vv1.2.3", "1.2.3.4", "a.b.c", "0.1.0/x", ".."],
    )
    def test_invalid_versions_rejected(self, bad):
        with pytest.raises(InvalidVersionError):
            paths.validate_version(bad)

    def test_version_dir_rejects_traversal(self, home):
        with pytest.raises(InvalidVersionError):
            paths.version_dir(home, "../..")

    def test_parse_version_strict(self):
        assert downloader.parse_version("v0.2.0") == (0, 2, 0)
        with pytest.raises(InvalidVersionError):
            downloader.parse_version("0.2.0-rc1")

    def test_compare_versions_numeric(self):
        assert downloader.compare_versions("0.10.0", "0.2.0") == 1
        assert downloader.compare_versions("0.2.0", "0.2.0") == 0
        assert downloader.compare_versions("0.1.9", "0.2.0") == -1


# =============================================================================
# Home resolution
# =============================================================================


class TestResolveHome:
    def test_no_installation_raises(self, home):
        with pytest.raises(HomeNotFoundError):
            paths.resolve_home()

    def test_env_var_must_be_installation(self, home, monkeypatch):
        home.mkdir(parents=True)
        monkeypatch.setenv("SYRVIS_HOME", str(home))
        with pytest.raises(HomeNotFoundError):
            paths.resolve_home()

    def test_env_var_with_manifest_resolves(self, home, monkeypatch):
        home.mkdir(parents=True)
        manifest.ensure_manifest(home)
        monkeypatch.setenv("SYRVIS_HOME", str(home))
        assert paths.resolve_home() == home

    def test_explicit_beats_env(self, home, tmp_path, monkeypatch):
        other = tmp_path / "other"
        for p in (home, other):
            p.mkdir(parents=True)
            manifest.ensure_manifest(p)
        monkeypatch.setenv("SYRVIS_HOME", str(other))
        assert paths.resolve_home(explicit=home) == home

    def test_create_makes_explicit_path(self, home):
        assert paths.resolve_home(explicit=home, create=True) == home
        assert home.exists()


# =============================================================================
# Install / activate / uninstall
# =============================================================================


class TestInstall:
    def test_install_from_wheel_end_to_end(self, home, tmp_path, fake_venv_backend):
        result = install(home, tmp_path, "0.1.0")
        assert result["version"] == "0.1.0"

        vdir = home / "versions" / "0.1.0"
        marker = vdir / "cli" / "venv" / "bin" / "syrvis"
        assert marker.exists()
        # The staging path must have been rewritten to the final location
        content = marker.read_text()
        assert ".staging-" not in content
        assert str(vdir) in content
        assert list((vdir / "wheel").glob("*.whl"))
        assert paths.active_version(home) == "0.1.0"
        assert (home / "bin" / "syrvis").exists()
        assert (home / "syrvis.profile").exists()
        assert manifest.get_version_info(home, "0.1.0") is not None

    def test_wheel_filename_version_inference(self, tmp_path, home):
        bad = tmp_path / "notawheel-1.0.whl"
        bad.write_bytes(b"x")
        with pytest.raises(InstallError):
            version_manager.install_from_wheel(home, bad)

    def test_duplicate_install_requires_force(self, home, tmp_path, fake_venv_backend):
        install(home, tmp_path, "0.1.0")
        wheel = make_wheel(tmp_path, "0.1.0")
        with pytest.raises(InstallError):
            version_manager.install_version(home, "0.1.0", wheel)
        # force succeeds
        version_manager.install_version(home, "0.1.0", wheel, force=True)

    def test_failed_install_leaves_no_trace(self, home, tmp_path, fake_venv_backend, monkeypatch):
        def boom(venv_path, wheel_path):
            raise InstallError("pip exploded")

        monkeypatch.setattr(version_manager, "_pip_install_wheel", boom)
        wheel = make_wheel(tmp_path, "0.3.0")
        with pytest.raises(InstallError):
            version_manager.install_version(home, "0.3.0", wheel)

        assert not (home / "versions" / "0.3.0").exists()
        assert not list((home / "versions").glob(".staging-*"))

    def test_failed_reinstall_preserves_existing_version(
        self, home, tmp_path, fake_venv_backend, monkeypatch
    ):
        install(home, tmp_path, "0.1.0")
        marker = home / "versions" / "0.1.0" / "cli" / "venv" / "bin" / "syrvis"
        original = marker.read_text()

        def boom(venv_path, wheel_path):
            raise InstallError("pip exploded")

        monkeypatch.setattr(version_manager, "_pip_install_wheel", boom)
        wheel = make_wheel(tmp_path, "0.1.0")
        with pytest.raises(InstallError):
            version_manager.install_version(home, "0.1.0", wheel, force=True)

        # The existing version was never touched
        assert marker.read_text() == original
        assert paths.active_version(home) == "0.1.0"


class TestActivate:
    def test_switch_between_versions(self, home, tmp_path, fake_venv_backend):
        install(home, tmp_path, "0.1.0")
        install(home, tmp_path, "0.2.0")
        assert paths.active_version(home) == "0.2.0"

        version_manager.activate_version(home, "0.1.0")
        assert paths.active_version(home) == "0.1.0"
        assert manifest.get_active_version(home) == "0.1.0"

        history = manifest.get_update_history(home)
        assert history[-1]["type"] == "rollback"

    def test_activate_missing_version(self, home, tmp_path, fake_venv_backend):
        install(home, tmp_path, "0.1.0")
        with pytest.raises(VersionNotFoundError):
            version_manager.activate_version(home, "9.9.9")

    def test_activate_incomplete_version(self, home, tmp_path, fake_venv_backend):
        install(home, tmp_path, "0.1.0")
        broken = home / "versions" / "0.2.0"
        broken.mkdir(parents=True)
        with pytest.raises(VersionNotFoundError):
            version_manager.activate_version(home, "0.2.0")

    def test_current_as_real_directory_refused(self, home, tmp_path, fake_venv_backend):
        install(home, tmp_path, "0.1.0", activate=False)
        (home / "current").mkdir()
        with pytest.raises(HomeNotFoundError):
            version_manager.activate_version(home, "0.1.0")


class TestUninstall:
    def test_uninstall_active_refused(self, home, tmp_path, fake_venv_backend):
        install(home, tmp_path, "0.1.0")
        with pytest.raises(ActiveVersionError):
            version_manager.uninstall_version(home, "0.1.0")

    def test_uninstall_missing_version(self, home, tmp_path, fake_venv_backend):
        install(home, tmp_path, "0.1.0")
        with pytest.raises(VersionNotFoundError):
            version_manager.uninstall_version(home, "0.5.0")

    def test_uninstall_removes_dir_and_manifest_entry(self, home, tmp_path, fake_venv_backend):
        install(home, tmp_path, "0.1.0")
        install(home, tmp_path, "0.2.0")
        version_manager.uninstall_version(home, "0.1.0")
        assert not (home / "versions" / "0.1.0").exists()
        assert manifest.get_version_info(home, "0.1.0") is None


class TestCleanup:
    def test_keeps_active_and_newest(self, home, tmp_path, fake_venv_backend):
        for v in ("0.1.0", "0.2.0", "0.3.0", "0.4.0"):
            install(home, tmp_path, v)
        version_manager.activate_version(home, "0.2.0")

        to_remove = version_manager.cleanup_old_versions(home, keep=2, dry_run=True)
        # active (0.2.0) + newest other (0.4.0) are kept
        assert to_remove == ["0.3.0", "0.1.0"]

        removed = version_manager.cleanup_old_versions(home, keep=2)
        assert removed == ["0.3.0", "0.1.0"]
        assert paths.list_installed_versions(home) == ["0.4.0", "0.2.0"]


class TestLocking:
    def test_concurrent_mutation_refused(self, home):
        home.mkdir(parents=True)
        with hold_lock(home):
            with pytest.raises(LockError):
                with hold_lock(home):
                    pass


# =============================================================================
# Manifest
# =============================================================================


class TestManifest:
    def test_atomic_save_produces_valid_json(self, home):
        home.mkdir(parents=True)
        m = manifest.ensure_manifest(home)
        m["setup_complete"] = True
        manifest.save_manifest(home, m)
        assert json.loads(paths.manifest_path(home).read_text())["setup_complete"] is True
        # No temp files left behind
        assert not list(home.glob(".manifest-*"))

    def test_symlink_is_source_of_truth(self, home, tmp_path, fake_venv_backend):
        install(home, tmp_path, "0.1.0")
        # Corrupt the manifest's idea of active; symlink must win
        m = manifest.get_manifest(home)
        m["active_version"] = "9.9.9"
        manifest.save_manifest(home, m)
        assert manifest.get_active_version(home) == "0.1.0"


# =============================================================================
# Downloader integrity helpers
# =============================================================================


class TestDownloaderHelpers:
    def test_find_wheel_asset_skips_manager_wheels(self):
        release = {
            "assets": [
                {"name": "syrviscore_manager-0.1.0-py3-none-any.whl"},
                {"name": "syrviscore-0.2.0-py3-none-any.whl"},
            ]
        }
        assert downloader.find_wheel_asset(release)["name"] == ("syrviscore-0.2.0-py3-none-any.whl")

    def test_find_checksums_asset(self):
        release = {"assets": [{"name": "SHA256SUMS"}]}
        assert downloader.find_checksums_asset(release)["name"] == "SHA256SUMS"
        assert downloader.find_checksums_asset({"assets": []}) is None

    def test_checksum_verification(self, tmp_path):
        f = tmp_path / "syrviscore-0.2.0-py3-none-any.whl"
        f.write_bytes(b"hello")
        digest = downloader.sha256_file(f)

        sums = downloader.parse_sha256sums("# comment\n{}  {}\nabc\n".format(digest, f.name))
        downloader.verify_asset_checksum(f, sums)  # no raise

        with pytest.raises(IntegrityError):
            downloader.verify_asset_checksum(f, {f.name: "0" * 64})

        with pytest.raises(IntegrityError):
            downloader.verify_asset_checksum(f, {})


# =============================================================================
# Backup and restore
# =============================================================================


def _populate_config(home):
    (home / "config").mkdir(parents=True, exist_ok=True)
    (home / "config" / ".env").write_text("SECRET=1\n")
    traefik = home / "data" / "traefik"
    traefik.mkdir(parents=True, exist_ok=True)
    acme = traefik / "acme.json"
    acme.write_text("{}")
    acme.chmod(0o600)


class TestBackupRestore:
    def test_backup_archive_is_private(self, home, tmp_path, fake_venv_backend):
        install(home, tmp_path, "0.1.0")
        _populate_config(home)

        backup_path = backup.create_backup(home)
        mode = stat.S_IMODE(backup_path.stat().st_mode)
        assert mode == 0o600

        listed = backup.list_backups(home)
        assert listed[0]["version"] == "0.1.0"
        assert listed[0]["reason"] == "manual"

    def test_restore_roundtrip(self, home, tmp_path, fake_venv_backend):
        install(home, tmp_path, "0.1.0")
        _populate_config(home)
        backup_path = backup.create_backup(home)

        target = tmp_path / "restored"
        metadata = backup.restore_from_backup(backup_path, target)

        assert metadata["version"] == "0.1.0"
        assert (target / "config" / ".env").read_text() == "SECRET=1\n"
        acme = target / "data" / "traefik" / "acme.json"
        assert stat.S_IMODE(acme.stat().st_mode) == 0o600
        # venv was rebuilt from the cached wheel and activated
        assert (target / "versions" / "0.1.0" / "cli" / "venv" / "bin" / "syrvis").exists()
        assert paths.active_version(target) == "0.1.0"
        assert (target / "bin" / "syrvis").exists()

    def test_backup_captures_and_restores_layer2_state(self, home, tmp_path, fake_venv_backend):
        """Layer-2 services (definitions, compose, per-service data) must survive a
        backup/restore round-trip and be listed in the metadata inventory — a
        bare-metal rebuild otherwise silently loses every user-installed service."""
        install(home, tmp_path, "0.1.0")
        _populate_config(home)

        # A user-installed Layer-2 service: definition + generated compose + data.
        (home / "services" / "wiki").mkdir(parents=True)
        (home / "services" / "wiki" / "syrvis-service.yaml").write_text(
            "name: wiki\nimage: ghcr.io/o/wiki:1.0.0\n"
        )
        (home / "compose").mkdir(parents=True)
        (home / "compose" / "wiki.yaml").write_text("services: {wiki: {}}\n")
        (home / "data" / "wiki").mkdir(parents=True)
        (home / "data" / "wiki" / "state.db").write_text("rows")

        backup_path = backup.create_backup(home)
        metadata = backup.read_backup_metadata(backup_path)
        assert metadata["layer2_services"] == ["wiki"]

        target = tmp_path / "restored"
        restored_meta = backup.restore_from_backup(backup_path, target)
        assert restored_meta["layer2_services"] == ["wiki"]
        assert (target / "services" / "wiki" / "syrvis-service.yaml").exists()
        assert (target / "compose" / "wiki.yaml").exists()
        assert (target / "data" / "wiki" / "state.db").read_text() == "rows"

    def test_restore_rejects_path_traversal(self, home, tmp_path):
        evil_tar = tmp_path / "0.1.0.tar.gz"
        with tarfile.open(str(evil_tar), "w:gz") as tar:
            meta = json.dumps(
                {"backup_version": 1, "version": "0.1.0", "syrvis_home": "/x"}
            ).encode()
            info = tarfile.TarInfo("backup-metadata.json")
            info.size = len(meta)
            tar.addfile(info, io.BytesIO(meta))

            payload = b"pwned"
            info = tarfile.TarInfo("config/../../evil.txt")
            info.size = len(payload)
            tar.addfile(info, io.BytesIO(payload))

        target = tmp_path / "victim" / "home"
        with pytest.raises(RestoreError):
            backup.restore_from_backup(evil_tar, target)

        assert not (tmp_path / "evil.txt").exists()
        assert not (tmp_path / "victim" / "evil.txt").exists()

    def test_restore_without_wheel_or_venv_fails_loudly(self, home, tmp_path):
        evil_tar = tmp_path / "0.1.0.tar.gz"
        with tarfile.open(str(evil_tar), "w:gz") as tar:
            meta = json.dumps(
                {"backup_version": 1, "version": "0.1.0", "syrvis_home": "/x"}
            ).encode()
            info = tarfile.TarInfo("backup-metadata.json")
            info.size = len(meta)
            tar.addfile(info, io.BytesIO(meta))

        target = tmp_path / "restored"
        with pytest.raises(RestoreError):
            backup.restore_from_backup(evil_tar, target)
        # And crucially: no current symlink was created
        assert not (target / "current").exists()

    def test_get_backup_for_rollback_prefers_base(self, home, tmp_path, fake_venv_backend):
        install(home, tmp_path, "0.1.0")
        _populate_config(home)
        base = backup.create_backup(home)
        suffixed = backup.create_backup(home, suffix=1)
        assert backup.get_backup_for_rollback(home, "0.1.0") == base

        base.unlink()
        assert backup.get_backup_for_rollback(home, "0.1.0") == suffixed

    def test_rollback_creates_safety_backup(self, home, tmp_path, fake_venv_backend):
        install(home, tmp_path, "0.1.0")
        _populate_config(home)
        backup.create_backup(home)  # backup of 0.1.0

        install(home, tmp_path, "0.2.0")
        assert manifest.get_active_version(home) == "0.2.0"

        version_manager.rollback_to_backup(home, "0.1.0")

        assert paths.active_version(home) == "0.1.0"
        # A pre-rollback safety backup of 0.2.0 exists
        reasons = {(b["version"], b["reason"]) for b in backup.list_backups(home)}
        assert ("0.2.0", "pre-rollback") in reasons

    def test_cleanup_old_backups(self, home, tmp_path, fake_venv_backend):
        for v in ("0.1.0", "0.2.0", "0.3.0", "0.4.0"):
            install(home, tmp_path, v)
            _populate_config(home)
            backup.create_backup(home, version=v)

        to_delete = backup.cleanup_old_backups(home, keep_versions=2, dry_run=True)
        assert sorted(p.name for p in to_delete) == ["0.1.0.tar.gz", "0.2.0.tar.gz"]

        backup.cleanup_old_backups(home, keep_versions=2)
        remaining = sorted(b["filename"] for b in backup.list_backups(home))
        assert remaining == ["0.3.0.tar.gz", "0.4.0.tar.gz"]


class TestManagerCompatibilityGate:
    """activate refuses a service that declares a newer MIN_MANAGER_VERSION."""

    def test_activate_refused_when_manager_too_old(
        self, home, tmp_path, fake_venv_backend, monkeypatch
    ):
        from syrviscore_manager.errors import CompatibilityError

        install(home, tmp_path, "0.2.0", activate=False)
        monkeypatch.setattr(version_manager, "probe_min_manager_version", lambda h, v: "99.0.0")
        with pytest.raises(CompatibilityError, match="requires manager >= 99.0.0"):
            version_manager.activate_version(home, "0.2.0")
        # the refused activation changed nothing
        assert paths.active_version(home) != "0.2.0"

    def test_activate_allowed_when_manager_new_enough(
        self, home, tmp_path, fake_venv_backend, monkeypatch
    ):
        install(home, tmp_path, "0.2.0", activate=False)
        monkeypatch.setattr(version_manager, "probe_min_manager_version", lambda h, v: "0.1.0")
        version_manager.activate_version(home, "0.2.0")
        assert paths.active_version(home) == "0.2.0"

    def test_no_declared_constraint_is_backward_compatible(self, home, tmp_path, fake_venv_backend):
        # The fake venv has no python, so the probe yields None -> no gate.
        install(home, tmp_path, "0.2.0", activate=True)
        assert paths.active_version(home) == "0.2.0"

    def test_malformed_declaration_never_blocks(
        self, home, tmp_path, fake_venv_backend, monkeypatch
    ):
        install(home, tmp_path, "0.2.0", activate=False)
        monkeypatch.setattr(
            version_manager, "probe_min_manager_version", lambda h, v: "not-a-version"
        )
        version_manager.activate_version(home, "0.2.0")
        assert paths.active_version(home) == "0.2.0"


class TestBackupIntegrity:
    """Per-file digests + sidecar + staged verify-then-move extraction."""

    def test_backup_records_digests_and_sidecar(self, home, tmp_path, fake_venv_backend):
        install(home, tmp_path, "0.1.0")
        _populate_config(home)
        backup_path = backup.create_backup(home)

        metadata = backup.read_backup_metadata(backup_path)
        digests = metadata.get("file_digests") or {}
        assert "config/.env" in digests
        assert all(len(d) == 64 for d in digests.values())

        sidecar = backup.sidecar_path(backup_path)
        assert sidecar.exists()
        recorded = sidecar.read_text().split()[0]
        assert recorded == backup._sha256_file(backup_path)

    def test_restore_refuses_tampered_member_and_leaves_target_untouched(
        self, home, tmp_path, fake_venv_backend
    ):
        install(home, tmp_path, "0.1.0")
        _populate_config(home)
        backup_path = backup.create_backup(home)

        # Repack the archive with one member's content changed but the original
        # metadata (digests) intact — simulating corruption/tampering.
        tampered = tmp_path / "tampered.tar.gz"
        with tarfile.open(str(backup_path), "r:gz") as src_tar:
            with tarfile.open(str(tampered), "w:gz") as dst_tar:
                for member in src_tar.getmembers():
                    payload = src_tar.extractfile(member)
                    data = payload.read() if payload else b""
                    if member.name == "config/.env":
                        data = b"EVIL=1\n"
                        member.size = len(data)
                    dst_tar.addfile(member, io.BytesIO(data))

        target = tmp_path / "victim"
        with pytest.raises(RestoreError, match="does not match its recorded digest"):
            backup.restore_from_backup(tampered, target)

        # Verified-before-moved: nothing was written into the target.
        assert not (target / "config" / ".env").exists()
        assert not (target / ".restore-staging").exists()

    def test_restore_refuses_bad_sidecar(self, home, tmp_path, fake_venv_backend):
        install(home, tmp_path, "0.1.0")
        _populate_config(home)
        backup_path = backup.create_backup(home)
        backup.sidecar_path(backup_path).write_text("0" * 64 + "  " + backup_path.name + "\n")

        with pytest.raises(RestoreError, match="sidecar"):
            backup.restore_from_backup(backup_path, tmp_path / "victim2")

    def test_restore_clamps_env_to_0600(self, home, tmp_path, fake_venv_backend):
        install(home, tmp_path, "0.1.0")
        _populate_config(home)
        backup_path = backup.create_backup(home)

        target = tmp_path / "restored-env"
        backup.restore_from_backup(backup_path, target)
        assert stat.S_IMODE((target / "config" / ".env").stat().st_mode) == 0o600
