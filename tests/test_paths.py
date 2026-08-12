"""
Tests for path management module.
"""

import os
import json

import pytest

from syrviscore.paths import (
    SyrvisHomeError,
    get_config_path,
    get_core_path,
    get_docker_compose_path,
    get_syrvis_home,
    get_config_dir,
    get_data_dir,
    get_versions_dir,
    get_active_version,
    list_installed_versions,
    set_syrvis_home,
    unset_syrvis_home,
    validate_docker_compose_exists,
    ensure_directory_structure,
    create_manifest,
    get_manifest,
    save_manifest,
    MANIFEST_SCHEMA_VERSION,
)


@pytest.fixture
def temp_syrvis_home(tmp_path):
    """Create temporary SYRVIS_HOME directory with proper structure."""
    syrvis_dir = tmp_path / "syrviscore"
    syrvis_dir.mkdir()

    # Create basic structure
    (syrvis_dir / "versions").mkdir()
    (syrvis_dir / "config").mkdir()
    (syrvis_dir / "data").mkdir()

    # Create a version directory
    version_dir = syrvis_dir / "versions" / "0.0.1"
    version_dir.mkdir()
    (version_dir / "cli").mkdir()
    (version_dir / "build").mkdir()

    # Create current symlink
    current = syrvis_dir / "current"
    current.symlink_to("versions/0.0.1")

    # Create manifest
    manifest = create_manifest("0.0.1", syrvis_dir)
    (syrvis_dir / ".syrviscore-manifest.json").write_text(json.dumps(manifest, indent=2))

    return syrvis_dir


@pytest.fixture(autouse=True)
def cleanup_env():
    """Clean up environment variables after each test."""
    original_value = os.environ.get("SYRVIS_HOME")
    yield
    # Restore original value
    if original_value:
        os.environ["SYRVIS_HOME"] = original_value
    elif "SYRVIS_HOME" in os.environ:
        del os.environ["SYRVIS_HOME"]


class TestGetSyrvisHome:
    """Test get_syrvis_home function."""

    def test_get_syrvis_home_success(self, temp_syrvis_home):
        """Test getting valid SYRVIS_HOME."""
        set_syrvis_home(str(temp_syrvis_home))
        result = get_syrvis_home()
        assert result == temp_syrvis_home

    def test_get_syrvis_home_not_set(self):
        """Test error when SYRVIS_HOME not set and no installation found."""
        unset_syrvis_home()
        with pytest.raises(SyrvisHomeError, match="Cannot find SyrvisCore installation"):
            get_syrvis_home()

    def test_get_syrvis_home_does_not_exist(self):
        """Test that nonexistent path is skipped (env var strategy)."""
        set_syrvis_home("/nonexistent/path")
        # This now tries fallback strategies, so it raises different error
        with pytest.raises(SyrvisHomeError, match="Cannot find SyrvisCore installation"):
            get_syrvis_home()

    def test_get_syrvis_home_not_directory(self, tmp_path):
        """Test that file path is skipped (env var strategy)."""
        file_path = tmp_path / "file.txt"
        file_path.write_text("test")
        set_syrvis_home(str(file_path))
        with pytest.raises(SyrvisHomeError, match="Cannot find SyrvisCore installation"):
            get_syrvis_home()


class TestIsInstallRoot:
    """_is_install_root: a candidate must SELF-IDENTIFY, not just carry a marker.

    Guards the design/26 hardening — per-service app homes now materialize
    `<location>/syrviscore/apps/` on secondary volumes, so a stray/mis-scoped
    manifest under a location root must never masquerade as the install root.
    """

    def _mk(self, tmp_path, payload):
        root = tmp_path / "syrviscore"
        root.mkdir()
        (root / ".syrviscore-manifest.json").write_text(json.dumps(payload))
        return root

    def test_self_identifying_root_accepted(self, tmp_path):
        from syrviscore.paths import _is_install_root

        root = self._mk(tmp_path, {"schema_version": 1, "versions": {}, "install_path": ""})
        (root / ".syrviscore-manifest.json").write_text(
            json.dumps({"schema_version": 1, "versions": {}, "install_path": str(root)})
        )
        assert _is_install_root(root) is True

    def test_stray_copy_rejected(self, tmp_path):
        from syrviscore.paths import _is_install_root

        # install_path points at a DIFFERENT root (a copied/restored manifest)
        root = self._mk(tmp_path, {"schema_version": 1, "install_path": "/volume4/syrviscore"})
        assert _is_install_root(root) is False

    def test_bare_marker_rejected(self, tmp_path):
        from syrviscore.paths import _is_install_root

        root = self._mk(tmp_path, {"apps": ["onyx"]})
        assert _is_install_root(root) is False

    def test_legacy_manifest_without_install_path_accepted(self, tmp_path):
        from syrviscore.paths import _is_install_root

        root = self._mk(tmp_path, {"schema_version": 1, "versions": {}})
        assert _is_install_root(root) is True

    def test_corrupt_manifest_rejected(self, tmp_path):
        from syrviscore.paths import _is_install_root

        root = tmp_path / "syrviscore"
        root.mkdir()
        (root / ".syrviscore-manifest.json").write_text("{not json")
        assert _is_install_root(root) is False


class TestGetDockerComposePath:
    """Test get_docker_compose_path function."""

    def test_get_docker_compose_path(self, temp_syrvis_home):
        """Test getting docker-compose.yaml path (now in config/)."""
        set_syrvis_home(str(temp_syrvis_home))
        result = get_docker_compose_path()
        # Now in config subdirectory
        expected = temp_syrvis_home / "config" / "docker-compose.yaml"
        assert result == expected

    def test_get_docker_compose_path_no_syrvis_home(self):
        """Test error when SYRVIS_HOME not set."""
        unset_syrvis_home()
        with pytest.raises(SyrvisHomeError):
            get_docker_compose_path()


class TestGetConfigPath:
    """Test get_config_path function."""

    def test_get_config_path(self, temp_syrvis_home):
        """Test getting build config.yaml path (now version-specific)."""
        set_syrvis_home(str(temp_syrvis_home))
        result = get_config_path()
        # Now in current version's build directory
        expected = temp_syrvis_home / "current" / "build" / "config.yaml"
        # Resolve symlink for comparison
        assert result.resolve() == expected.resolve()

    def test_get_config_path_no_syrvis_home(self):
        """Test error when SYRVIS_HOME not set."""
        unset_syrvis_home()
        with pytest.raises(SyrvisHomeError):
            get_config_path()


class TestGetCorePath:
    """Test get_core_path function."""

    def test_get_core_path(self, temp_syrvis_home):
        """Test getting core data path."""
        set_syrvis_home(str(temp_syrvis_home))
        result = get_core_path()
        expected = temp_syrvis_home / "data"
        assert result == expected

    def test_get_core_path_no_syrvis_home(self):
        """Test error when SYRVIS_HOME not set."""
        unset_syrvis_home()
        with pytest.raises(SyrvisHomeError):
            get_core_path()


class TestValidateDockerComposeExists:
    """Test validate_docker_compose_exists function."""

    def test_validate_docker_compose_exists_success(self, temp_syrvis_home):
        """Test validation when file exists."""
        set_syrvis_home(str(temp_syrvis_home))
        # Create in config subdirectory
        config_dir = temp_syrvis_home / "config"
        compose_file = config_dir / "docker-compose.yaml"
        compose_file.write_text("version: '3.8'")

        # Should not raise
        validate_docker_compose_exists()

    def test_validate_docker_compose_missing(self, temp_syrvis_home):
        """Test error when file doesn't exist."""
        set_syrvis_home(str(temp_syrvis_home))

        with pytest.raises(FileNotFoundError, match="docker-compose.yaml not found"):
            validate_docker_compose_exists()

    def test_validate_docker_compose_no_syrvis_home(self):
        """Test error when SYRVIS_HOME not set."""
        unset_syrvis_home()
        with pytest.raises(SyrvisHomeError):
            validate_docker_compose_exists()


class TestVersionedPaths:
    """Test versioned directory structure functions."""

    def test_get_versions_dir(self, temp_syrvis_home):
        """Test getting versions directory."""
        set_syrvis_home(str(temp_syrvis_home))
        result = get_versions_dir()
        expected = temp_syrvis_home / "versions"
        assert result == expected

    def test_get_config_dir(self, temp_syrvis_home):
        """Test getting config directory."""
        set_syrvis_home(str(temp_syrvis_home))
        result = get_config_dir()
        expected = temp_syrvis_home / "config"
        assert result == expected

    def test_get_data_dir(self, temp_syrvis_home):
        """Test getting data directory."""
        set_syrvis_home(str(temp_syrvis_home))
        result = get_data_dir()
        expected = temp_syrvis_home / "data"
        assert result == expected

    def test_get_active_version(self, temp_syrvis_home):
        """Test getting active version from manifest."""
        set_syrvis_home(str(temp_syrvis_home))
        result = get_active_version()
        assert result == "0.0.1"

    def test_list_installed_versions(self, temp_syrvis_home):
        """Test listing installed versions."""
        set_syrvis_home(str(temp_syrvis_home))

        # Add another version
        (temp_syrvis_home / "versions" / "0.0.2").mkdir()

        result = list_installed_versions()
        assert "0.0.1" in result
        assert "0.0.2" in result


class TestManifest:
    """Test manifest functions."""

    def test_create_manifest(self, tmp_path):
        """Test creating a manifest."""
        manifest = create_manifest("1.0.0", tmp_path)
        assert manifest["schema_version"] == MANIFEST_SCHEMA_VERSION
        assert manifest["active_version"] == "1.0.0"
        assert manifest["setup_complete"] is False
        assert "1.0.0" in manifest["versions"]

    def test_get_manifest(self, temp_syrvis_home):
        """Test reading manifest."""
        set_syrvis_home(str(temp_syrvis_home))
        manifest = get_manifest()
        assert manifest["active_version"] == "0.0.1"

    def test_save_manifest(self, temp_syrvis_home):
        """Test saving manifest."""
        set_syrvis_home(str(temp_syrvis_home))
        manifest = get_manifest()
        manifest["setup_complete"] = True
        save_manifest(manifest)

        # Re-read and verify
        updated = get_manifest()
        assert updated["setup_complete"] is True


class TestDirectoryStructure:
    """Test directory structure creation."""

    def test_ensure_directory_structure(self, tmp_path):
        """Test creating full directory structure."""
        install_path = tmp_path / "syrviscore"
        ensure_directory_structure(install_path, "1.0.0")

        # Check root directories
        assert (install_path / "versions").is_dir()
        assert (install_path / "config").is_dir()
        assert (install_path / "data").is_dir()
        assert (install_path / "data" / "traefik").is_dir()
        assert (install_path / "data" / "portainer").is_dir()

        # Check version directories
        version_dir = install_path / "versions" / "1.0.0"
        assert version_dir.is_dir()
        assert (version_dir / "cli").is_dir()
        assert (version_dir / "build").is_dir()


class TestSetUnsetSyrvisHome:
    """Test helper functions for setting/unsetting SYRVIS_HOME."""

    def test_set_syrvis_home(self):
        """Test setting SYRVIS_HOME."""
        test_path = "/test/path"
        set_syrvis_home(test_path)
        assert os.environ.get("SYRVIS_HOME") == test_path

    def test_unset_syrvis_home_when_set(self):
        """Test unsetting SYRVIS_HOME when it's set."""
        os.environ["SYRVIS_HOME"] = "/test/path"
        unset_syrvis_home()
        assert "SYRVIS_HOME" not in os.environ

    def test_unset_syrvis_home_when_not_set(self):
        """Test unsetting SYRVIS_HOME when it's not set."""
        if "SYRVIS_HOME" in os.environ:
            del os.environ["SYRVIS_HOME"]
        # Should not raise
        unset_syrvis_home()
        assert "SYRVIS_HOME" not in os.environ


class TestSyrvisHomeError:
    """Test SyrvisHomeError exception."""

    def test_exception_inheritance(self):
        """Test that SyrvisHomeError is an Exception."""
        error = SyrvisHomeError("test message")
        assert isinstance(error, Exception)
        assert str(error) == "test message"


class TestIsMountedVolume:
    """design/26: the install-time half of `location:` validation.

    Parse-time is regex-only; this is the on-box check that the declared
    /volumeN is a real, mounted DSM volume — a symlink or a bare directory
    (an UNMOUNTED /volumeN is exactly that) must never pass outside sim mode.
    """

    def _no_sim(self, monkeypatch):
        monkeypatch.delenv("DSM_SIM_ACTIVE", raising=False)
        monkeypatch.delenv("DSM_SIM_ROOT", raising=False)

    def test_nonexistent_path_rejected(self, monkeypatch):
        from syrviscore.paths import is_mounted_volume

        self._no_sim(monkeypatch)
        assert is_mounted_volume("/volume987654") is False

    def test_plain_directory_is_not_a_mount(self, monkeypatch, tmp_path):
        from syrviscore import paths as paths_mod

        self._no_sim(monkeypatch)
        vol = tmp_path / "volume6"
        vol.mkdir()
        monkeypatch.setattr(paths_mod, "resolve_volume_root", lambda loc: vol)
        assert paths_mod.is_mounted_volume("/volume6") is False

    def test_symlink_to_directory_rejected(self, monkeypatch, tmp_path):
        # os.path.ismount returns False for a symlink — a symlinked /volumeN
        # (the class of trick the old nvme-flip workaround used) never passes.
        from syrviscore import paths as paths_mod

        self._no_sim(monkeypatch)
        real = tmp_path / "real"
        real.mkdir()
        link = tmp_path / "volume7"
        link.symlink_to(real)
        monkeypatch.setattr(paths_mod, "resolve_volume_root", lambda loc: link)
        assert paths_mod.is_mounted_volume("/volume7") is False

    def test_real_mountpoint_accepted(self, monkeypatch):
        # "/" is a mountpoint on every platform the suite runs on.
        from syrviscore import paths as paths_mod

        self._no_sim(monkeypatch)
        monkeypatch.setattr(
            paths_mod, "resolve_volume_root", lambda loc: __import__("pathlib").Path("/")
        )
        assert paths_mod.is_mounted_volume("/volume1") is True

    def test_sim_mode_accepts_existing_dir_under_sim_root(self, monkeypatch, tmp_path):
        from syrviscore.paths import is_mounted_volume, resolve_volume_root

        monkeypatch.setenv("DSM_SIM_ACTIVE", "1")
        monkeypatch.setenv("DSM_SIM_ROOT", str(tmp_path))
        (tmp_path / "volume6").mkdir()
        assert resolve_volume_root("/volume6") == tmp_path / "volume6"
        assert is_mounted_volume("/volume6") is True
        assert is_mounted_volume("/volume9") is False  # absent even under sim

    def test_sim_mode_still_rejects_symlinked_volume(self, monkeypatch, tmp_path):
        # Adversarial review #9: sim mode waives only the mountpoint
        # requirement — a SYMLINKED volume root stays rejected in every mode.
        from syrviscore.paths import is_mounted_volume

        monkeypatch.setenv("DSM_SIM_ACTIVE", "1")
        monkeypatch.setenv("DSM_SIM_ROOT", str(tmp_path))
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        (tmp_path / "volume9").symlink_to(elsewhere)
        (tmp_path / "volume6").mkdir()
        assert is_mounted_volume("/volume9") is False  # symlink: refused
        assert is_mounted_volume("/volume6") is True  # plain dir: sim-accepted
