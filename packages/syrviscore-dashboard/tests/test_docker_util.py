"""Scoping rules for which containers the dashboard may touch."""

import pytest

from syrviscore_dashboard import docker_util


def test_is_managed_rules():
    assert docker_util.is_managed("traefik", "")  # core service
    assert docker_util.is_managed("portainer", "")
    assert docker_util.is_managed("syrviscore-dashboard", "")  # the dashboard itself
    assert docker_util.is_managed("gollum", "syrvis-gollum")  # L2 project prefix
    assert not docker_util.is_managed("random", "some-other-project")
    assert not docker_util.is_managed("random", "")


def test_invalid_name_rejected():
    with pytest.raises(docker_util.InvalidName):
        docker_util.get_managed_container("bad name!")


def test_docker_unavailable_surfaces():
    # no daemon in tests → DockerUnavailable (a valid name gets past the regex)
    with pytest.raises(docker_util.DockerUnavailable):
        docker_util.get_managed_container("traefik")
