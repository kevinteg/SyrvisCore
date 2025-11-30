"""
Tests for path management module.
"""

import os

import pytest

from syrviscore.paths import (
    SyrvisHomeError,
    get_config_path,
    get_core_path,
    get_docker_compose_path,
    get_syrvis_home,
    set_syrvis_home,
    unset_syrvis_home,
    validate_docker_compose_exists,
)


@pytest.fixture
def temp_syrvis_home(tmp_path):
    """Create temporary SYRVIS_HOME directory."""
    syrvis_dir = tmp_path / "syrviscore"
    syrvis_dir.mkdir()
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
        """Test error when SYRVIS_HOME not set."""
        unset_syrvis_home()
        with pytest.raises(SyrvisHomeError, match="SYRVIS_HOME environment variable not set"):
            get_syrvis_home()

    def test_get_syrvis_home_does_not_exist(self):
        """Test error when SYRVIS_HOME doesn't exist."""
        set_syrvis_home("/nonexistent/path")
        with pytest.raises(SyrvisHomeError, match="SYRVIS_HOME directory does not exist"):
            get_syrvis_home()

    def test_get_syrvis_home_not_directory(self, tmp_path):
        """Test error when SYRVIS_HOME is a file, not directory."""
        file_path = tmp_path / "file.txt"
        file_path.write_text("test")
        set_syrvis_home(str(file_path))
        with pytest.raises(SyrvisHomeError, match="SYRVIS_HOME is not a directory"):
            get_syrvis_home()


class TestGetDockerComposePath:
    """Test get_docker_compose_path function."""

    def test_get_docker_compose_path(self, temp_syrvis_home):
        """Test getting docker-compose.yaml path."""
        set_syrvis_home(str(temp_syrvis_home))
        result = get_docker_compose_path()
        expected = temp_syrvis_home / "docker-compose.yaml"
        assert result == expected

    def test_get_docker_compose_path_no_syrvis_home(self):
        """Test error when SYRVIS_HOME not set."""
        unset_syrvis_home()
        with pytest.raises(SyrvisHomeError):
            get_docker_compose_path()


class TestGetConfigPath:
    """Test get_config_path function."""

    def test_get_config_path(self, temp_syrvis_home):
        """Test getting config.yaml path."""
        set_syrvis_home(str(temp_syrvis_home))
        result = get_config_path()
        expected = temp_syrvis_home / "build" / "config.yaml"
        assert result == expected

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
        compose_file = temp_syrvis_home / "docker-compose.yaml"
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
