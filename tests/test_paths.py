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
