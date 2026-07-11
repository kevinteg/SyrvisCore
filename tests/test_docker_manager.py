"""
Tests for Docker manager module.
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import Mock, patch

import pytest
from docker.errors import DockerException

from syrviscore import compose_cmd
from syrviscore.docker_manager import DockerConnectionError, DockerError, DockerManager
from syrviscore.paths import set_syrvis_home


@pytest.fixture(autouse=True)
def _pin_compose_cmd():
    """Pin the compose resolver to v1 so compose tests don't probe subprocess
    (their mocks assert an exact call count) and match the 'docker-compose' argv."""
    compose_cmd.reset_cache()
    compose_cmd._cached = ["docker-compose"]
    yield
    compose_cmd.reset_cache()


@pytest.fixture
def temp_syrvis_home_with_compose(tmp_path):
    """Create temp SYRVIS_HOME with docker-compose.yaml in versioned structure."""
    syrvis_dir = tmp_path / "syrviscore"
    syrvis_dir.mkdir()

    # Create versioned structure
    (syrvis_dir / "versions" / "0.0.1" / "cli").mkdir(parents=True)
    (syrvis_dir / "versions" / "0.0.1" / "build").mkdir(parents=True)
    (syrvis_dir / "current").symlink_to("versions/0.0.1")
    (syrvis_dir / "data" / "traefik" / "config").mkdir(parents=True)

    # docker-compose.yaml now in config/ directory
    config_dir = syrvis_dir / "config"
    config_dir.mkdir()
    compose_file = config_dir / "docker-compose.yaml"
    compose_file.write_text("version: '3.8'\nservices:\n  traefik: {}")

    # Create manifest
    import json

    manifest = {
        "schema_version": 2,
        "active_version": "0.0.1",
        "install_path": str(syrvis_dir),
        "setup_complete": False,
    }
    (syrvis_dir / ".syrviscore-manifest.json").write_text(json.dumps(manifest))

    set_syrvis_home(str(syrvis_dir))
    return syrvis_dir


@pytest.fixture
def mock_docker_client():
    """Mock Docker client."""
    with patch("syrviscore.docker_manager.docker.from_env") as mock:
        client = Mock()
        client.ping.return_value = True
        mock.return_value = client
        yield client


class TestDockerManagerInit:
    """Test Docker Manager initialization."""

    def test_init_success(self, mock_docker_client):
        """Test successful initialization."""
        manager = DockerManager()
        assert manager.client == mock_docker_client
        mock_docker_client.ping.assert_called_once()

    def test_init_docker_not_running(self):
        """Test initialization when Docker not running."""
        with patch("syrviscore.docker_manager.docker.from_env") as mock:
            mock.side_effect = DockerException("Cannot connect")
            with pytest.raises(DockerConnectionError, match="Cannot connect to Docker daemon"):
                DockerManager()


class TestGetCoreContainers:
    """Test getting core containers."""

    def test_get_core_containers_success(self, mock_docker_client):
        """Test getting containers successfully."""
        container1 = Mock()
        container2 = Mock()
        mock_docker_client.containers.list.return_value = [container1, container2]

        manager = DockerManager()
        containers = manager.get_core_containers()

        assert len(containers) == 2
        assert containers == [container1, container2]
        mock_docker_client.containers.list.assert_called_once_with(
            all=True,
            filters={"label": "com.docker.compose.project=syrviscore"},
        )

    def test_get_core_containers_docker_error(self, mock_docker_client):
        """Test error when Docker fails."""
        mock_docker_client.containers.list.side_effect = DockerException("Connection failed")

        manager = DockerManager()
        with pytest.raises(DockerConnectionError, match="Failed to list containers"):
            manager.get_core_containers()


class TestStartStopRestart:
    """Test start, stop, restart operations."""

    def test_start_core_services(self, mock_docker_client, temp_syrvis_home_with_compose):
        """Test starting services."""
        with patch("syrviscore.docker_manager.subprocess.run") as mock_run:
            mock_run.return_value = Mock(returncode=0)

            manager = DockerManager()
            manager.start_core_services()

            mock_run.assert_called_once()
            args = mock_run.call_args[0][0]
            assert "docker-compose" in args
            assert "up" in args
            assert "-d" in args

    def test_stop_core_services(self, mock_docker_client, temp_syrvis_home_with_compose):
        """Test stopping services."""
        with patch("syrviscore.docker_manager.subprocess.run") as mock_run:
            mock_run.return_value = Mock(returncode=0)

            manager = DockerManager()
            manager.stop_core_services()

            mock_run.assert_called_once()
            args = mock_run.call_args[0][0]
            assert "stop" in args

    def test_restart_core_services(self, mock_docker_client, temp_syrvis_home_with_compose):
        """Restart force-recreates so both static-config AND compose-spec changes apply."""
        with patch("syrviscore.docker_manager.subprocess.run") as mock_run:
            mock_run.return_value = Mock(returncode=0)

            manager = DockerManager()
            manager.restart_core_services()

            mock_run.assert_called_once()
            args = mock_run.call_args[0][0]
            assert "up" in args
            assert "--force-recreate" in args


class TestGetContainerStatus:
    """Test getting container status."""

    def test_get_container_status(self, mock_docker_client):
        """Test getting status of containers."""
        # Mock container
        container = Mock()
        container.name = "traefik"
        container.status = "running"
        container.labels = {"com.docker.compose.service": "traefik"}
        container.image.tags = ["traefik:v3.0.0"]

        # Mock created time (2 hours ago)
        created_time = datetime.now(timezone.utc) - timedelta(hours=2)
        container.attrs = {"Created": created_time.isoformat()}

        mock_docker_client.containers.list.return_value = [container]

        manager = DockerManager()
        status = manager.get_container_status()

        assert "traefik" in status
        assert status["traefik"]["name"] == "traefik"
        assert status["traefik"]["status"] == "running"
        assert status["traefik"]["image"] == "traefik:v3.0.0"
        assert "hour" in status["traefik"]["uptime"]

    def test_get_container_status_empty(self, mock_docker_client):
        """Test status with no containers."""
        mock_docker_client.containers.list.return_value = []

        manager = DockerManager()
        status = manager.get_container_status()

        assert status == {}


class TestGetContainerLogs:
    """Test getting container logs."""

    def test_get_logs_single_service(self, mock_docker_client):
        """Test getting logs for single service."""
        container = Mock()
        container.name = "traefik"
        container.labels = {"com.docker.compose.service": "traefik"}
        container.logs.return_value = b"2024-01-01 Test log\n"

        mock_docker_client.containers.list.return_value = [container]

        manager = DockerManager()
        logs = manager.get_container_logs(service="traefik", follow=False)

        assert "traefik" in logs
        assert "Test log" in logs

    def test_get_logs_all_services(self, mock_docker_client):
        """Test getting logs for all services."""
        container1 = Mock()
        container1.labels = {"com.docker.compose.service": "traefik"}
        container1.logs.return_value = b"Traefik log\n"

        container2 = Mock()
        container2.labels = {"com.docker.compose.service": "portainer"}
        container2.logs.return_value = b"Portainer log\n"

        mock_docker_client.containers.list.return_value = [container1, container2]

        manager = DockerManager()
        logs = manager.get_container_logs(follow=False)

        assert "traefik" in logs
        assert "portainer" in logs

    def test_get_logs_service_not_found(self, mock_docker_client):
        """Test error when service not found."""
        mock_docker_client.containers.list.return_value = []

        manager = DockerManager()
        with pytest.raises(ValueError, match="Service 'nonexistent' not found"):
            manager.get_container_logs(service="nonexistent")


class TestFormatUptime:
    """Test uptime formatting."""

    def test_format_uptime_seconds(self):
        """Test formatting seconds."""
        assert DockerManager._format_uptime(30) == "30 seconds"

    def test_format_uptime_minutes(self):
        """Test formatting minutes."""
        assert DockerManager._format_uptime(120) == "2 minutes"
        assert DockerManager._format_uptime(60) == "1 minute"

    def test_format_uptime_hours(self):
        """Test formatting hours."""
        assert DockerManager._format_uptime(7200) == "2 hours"
        assert DockerManager._format_uptime(3600) == "1 hour"

    def test_format_uptime_days(self):
        """Test formatting days."""
        assert DockerManager._format_uptime(172800) == "2 days"
        assert DockerManager._format_uptime(86400) == "1 day"


class TestCreateTraefikFiles:
    """Test creating required Traefik files."""

    def test_create_traefik_files(self, mock_docker_client, temp_syrvis_home_with_compose):
        """Test that required Traefik files are created with content."""
        manager = DockerManager()
        manager._create_traefik_files()

        traefik_data = temp_syrvis_home_with_compose / "data" / "traefik"

        # Check acme.json exists with correct permissions (empty file)
        acme_file = traefik_data / "acme.json"
        assert acme_file.exists()
        assert acme_file.is_file()
        assert oct(acme_file.stat().st_mode)[-3:] == "600"

        # Check traefik.yml exists with correct permissions and has content
        config_file = traefik_data / "traefik.yml"
        assert config_file.exists()
        assert config_file.is_file()
        assert oct(config_file.stat().st_mode)[-3:] == "644"
        content = config_file.read_text()
        assert "# Traefik Static Configuration" in content
        assert "entryPoints:" in content

        # Check config directory exists
        config_dir = traefik_data / "config"
        assert config_dir.exists()
        assert config_dir.is_dir()

        # Check dynamic config exists with content
        dynamic_file = config_dir / "dynamic.yml"
        assert dynamic_file.exists()
        assert dynamic_file.is_file()
        assert oct(dynamic_file.stat().st_mode)[-3:] == "644"
        dynamic_content = dynamic_file.read_text()
        assert "# Traefik Dynamic Configuration" in dynamic_content
        assert "http:" in dynamic_content

    def test_create_traefik_files_idempotent(
        self, mock_docker_client, temp_syrvis_home_with_compose
    ):
        """Test that creating files multiple times is safe and idempotent."""
        manager = DockerManager()

        # Create files first time
        manager._create_traefik_files()

        traefik_data = temp_syrvis_home_with_compose / "data" / "traefik"
        acme_file = traefik_data / "acme.json"
        traefik_yml = traefik_data / "traefik.yml"

        # Verify initial files exist
        assert acme_file.exists()
        assert traefik_yml.exists()

        # Get initial traefik.yml content
        initial_content = traefik_yml.read_text()

        # Create files second time (should be safe)
        manager._create_traefik_files()

        # Should still exist with correct permissions
        assert acme_file.exists()
        assert oct(acme_file.stat().st_mode)[-3:] == "600"

        # traefik.yml should be updated (content same in this case)
        assert traefik_yml.exists()
        assert oct(traefik_yml.stat().st_mode)[-3:] == "644"
        updated_content = traefik_yml.read_text()
        assert updated_content == initial_content

    def test_acme_json_not_overwritten(self, mock_docker_client, temp_syrvis_home_with_compose):
        """Test that acme.json is NOT overwritten if it already exists."""
        manager = DockerManager()

        traefik_data = temp_syrvis_home_with_compose / "data" / "traefik"
        traefik_data.mkdir(parents=True, exist_ok=True)

        # Create acme.json with certificate data
        acme_file = traefik_data / "acme.json"
        cert_data = '{"certificates": "important data"}'
        acme_file.write_text(cert_data)
        acme_file.chmod(0o600)

        # Call _create_traefik_files() - should NOT overwrite acme.json
        manager._create_traefik_files()

        # Verify acme.json still has original content
        assert acme_file.read_text() == cert_data

    def test_remove_disabled_core_containers(self, temp_syrvis_home_with_compose):
        """A disabled optional core service's container is stopped+removed on apply;
        enabled and primordial services are never touched."""
        from syrviscore.docker_manager import remove_disabled_core_containers

        # stack.yaml: dashboard disabled, cloudflared enabled.
        (temp_syrvis_home_with_compose / "config" / "stack.yaml").write_text(
            "version: 1\n"
            "services:\n"
            "  traefik: {enabled: true}\n"
            "  portainer: {enabled: true}\n"
            "  cloudflared: {enabled: true}\n"
            "  dashboard: {enabled: false}\n"
            "  cloudflare_ddns: {enabled: false}\n"
        )

        containers = {
            "syrviscore-dashboard": Mock(),
            "cloudflared": Mock(),
            "traefik": Mock(),
        }

        class _NotFound(Exception):
            pass

        def get_container(name):
            if name in containers:
                return containers[name]
            raise _NotFound(name)

        with patch("docker.from_env") as mock_env:
            mock_env.return_value.containers.get.side_effect = get_container
            removed = remove_disabled_core_containers()

        # Only the disabled-but-present dashboard was removed; ddns had no container.
        assert removed == ["syrviscore-dashboard"]
        containers["syrviscore-dashboard"].stop.assert_called_once()
        containers["syrviscore-dashboard"].remove.assert_called_once()
        containers["cloudflared"].stop.assert_not_called()
        containers["traefik"].stop.assert_not_called()

    def test_write_traefik_config_files_reports_static_change(self, temp_syrvis_home_with_compose):
        """write_traefik_config_files() must flag when the STATIC config changed.

        Regression guard for the ping-404 class of bug: static config is only read
        by Traefik at process start, so callers rely on this boolean to know when a
        restart is required. First write (no prior file) -> True; an identical
        rewrite -> False; a rewrite over stale content -> True.
        """
        from syrviscore.docker_manager import write_traefik_config_files

        # First write: no traefik.yml existed -> change signalled.
        assert write_traefik_config_files() is True
        # Identical rewrite: content matches on disk -> no change.
        assert write_traefik_config_files() is False

        # Simulate an upgrade that adds a line (e.g. `ping: {}`) to the static file.
        traefik_yml = temp_syrvis_home_with_compose / "data" / "traefik" / "traefik.yml"
        traefik_yml.write_text("stale: config\n")
        assert write_traefik_config_files() is True

    def test_noop_regeneration_preserves_static_mtime(self, temp_syrvis_home_with_compose):
        """An unchanged static config must NOT be rewritten: the stale-static
        drift check is mtime-vs-StartedAt, so a no-op regen that bumped the
        mtime would raise a false stale_static_config flag (seen live)."""
        import os

        from syrviscore.docker_manager import write_traefik_config_files

        write_traefik_config_files()
        traefik_yml = temp_syrvis_home_with_compose / "data" / "traefik" / "traefik.yml"
        # Backdate the file, then regenerate with identical content.
        old_epoch = traefik_yml.stat().st_mtime - 3600
        os.utime(str(traefik_yml), (old_epoch, old_epoch))

        assert write_traefik_config_files() is False
        assert traefik_yml.stat().st_mtime == old_epoch  # untouched

    def test_start_core_services_restarts_traefik_on_static_change(
        self, mock_docker_client, temp_syrvis_home_with_compose
    ):
        """A static-config change during `syrvis start` must trigger a Traefik restart.

        `docker compose up -d` does not recreate a container for a bind-mounted file
        edit, so start_core_services() restarts Traefik itself when the static config
        changed. (First start has no prior traefik.yml, so a change is always seen.)
        """
        manager = DockerManager()
        with patch("syrviscore.docker_manager.restart_traefik_if_running") as mock_restart, patch(
            "syrviscore.docker_manager.DockerManager._run_compose_command"
        ), patch("syrviscore.docker_manager.DockerManager._ensure_macvlan_shim"):
            manager.start_core_services()
            mock_restart.assert_called_once()

    def test_config_files_are_overwritten(self, mock_docker_client, temp_syrvis_home_with_compose):
        """Test that traefik.yml and dynamic.yml ARE overwritten to allow updates."""
        manager = DockerManager()

        traefik_data = temp_syrvis_home_with_compose / "data" / "traefik"
        traefik_data.mkdir(parents=True, exist_ok=True)
        config_dir = traefik_data / "config"
        config_dir.mkdir(exist_ok=True)

        # Create old config files with different content
        traefik_yml = traefik_data / "traefik.yml"
        dynamic_yml = config_dir / "dynamic.yml"
        traefik_yml.write_text("old static config")
        dynamic_yml.write_text("old dynamic config")

        # Call _create_traefik_files() - should overwrite with new content
        manager._create_traefik_files()

        # Verify files were overwritten with new content
        assert "old static config" not in traefik_yml.read_text()
        assert "# Traefik Static Configuration" in traefik_yml.read_text()
        assert "old dynamic config" not in dynamic_yml.read_text()
        assert "# Traefik Dynamic Configuration" in dynamic_yml.read_text()

    def test_start_creates_traefik_files(self, mock_docker_client, temp_syrvis_home_with_compose):
        """Test that start_core_services creates Traefik files."""
        with patch("syrviscore.docker_manager.subprocess.run") as mock_run:
            mock_run.return_value = Mock(returncode=0, stderr="", stdout="")

            manager = DockerManager()
            manager.start_core_services()

            # Verify files were created
            traefik_data = temp_syrvis_home_with_compose / "data" / "traefik"
            assert (traefik_data / "acme.json").exists()
            assert (traefik_data / "traefik.yml").exists()
            assert (traefik_data / "config").exists()
            assert (traefik_data / "config" / "dynamic.yml").exists()

    def test_restart_creates_traefik_files(self, mock_docker_client, temp_syrvis_home_with_compose):
        """Test that restart_core_services creates/updates Traefik files."""
        with patch("syrviscore.docker_manager.subprocess.run") as mock_run:
            mock_run.return_value = Mock(returncode=0, stderr="", stdout="")

            manager = DockerManager()
            manager.restart_core_services()

            # Verify files were created
            traefik_data = temp_syrvis_home_with_compose / "data" / "traefik"
            assert (traefik_data / "acme.json").exists()
            assert (traefik_data / "traefik.yml").exists()
            assert (traefik_data / "config" / "dynamic.yml").exists()

    def test_config_is_directory_not_file(self, mock_docker_client, temp_syrvis_home_with_compose):
        """Test that config is created as a DIRECTORY, not a file."""
        manager = DockerManager()
        manager._create_traefik_files()

        traefik_data = temp_syrvis_home_with_compose / "data" / "traefik"
        config_path = traefik_data / "config"

        # Verify config exists and is a directory
        assert config_path.exists(), "config should exist"
        assert config_path.is_dir(), "config must be a DIRECTORY, not a file"
        assert not config_path.is_file(), "config must NOT be a file"

        # Verify dynamic.yml exists inside the directory
        dynamic_yml = config_path / "dynamic.yml"
        assert dynamic_yml.exists(), "dynamic.yml should exist inside config directory"
        assert dynamic_yml.is_file(), "dynamic.yml should be a file"


class TestDockerErrorHandling:
    """Test Docker error handling and output capture."""

    def test_run_compose_command_captures_stderr(
        self, mock_docker_client, temp_syrvis_home_with_compose
    ):
        """Test that docker-compose stderr is captured in error."""
        with patch("syrviscore.docker_manager.subprocess.run") as mock_run:
            mock_run.return_value = Mock(
                returncode=1, stderr="Error: network not found\ndetailed error message", stdout=""
            )

            manager = DockerManager()
            with pytest.raises(DockerError) as exc_info:
                manager.start_core_services()

            # Error should contain the actual docker-compose error
            assert "network not found" in str(exc_info.value)
            assert "detailed error message" in str(exc_info.value)

    def test_run_compose_command_captures_stdout_if_no_stderr(
        self, mock_docker_client, temp_syrvis_home_with_compose
    ):
        """Test that stdout is used if stderr is empty."""
        with patch("syrviscore.docker_manager.subprocess.run") as mock_run:
            mock_run.return_value = Mock(returncode=1, stderr="", stdout="Error in stdout message")

            manager = DockerManager()
            with pytest.raises(DockerError) as exc_info:
                manager.stop_core_services()

            # Error should contain stdout if stderr empty
            assert "Error in stdout message" in str(exc_info.value)

    def test_run_compose_command_includes_command_in_error(
        self, mock_docker_client, temp_syrvis_home_with_compose
    ):
        """Test that error message includes the command that failed."""
        with patch("syrviscore.docker_manager.subprocess.run") as mock_run:
            mock_run.return_value = Mock(returncode=1, stderr="Some error", stdout="")

            manager = DockerManager()
            with pytest.raises(DockerError) as exc_info:
                manager.restart_core_services()

            # Error should mention which command failed (restart = up --force-recreate)
            assert "--force-recreate" in str(exc_info.value)
            assert "docker-compose" in str(exc_info.value)
