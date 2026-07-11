"""Tests for the docker compose command resolver (v2 plugin vs v1 standalone)."""

import subprocess

import pytest

from syrviscore import compose_cmd


@pytest.fixture(autouse=True)
def _clear_cache():
    compose_cmd.reset_cache()
    yield
    compose_cmd.reset_cache()


def _fake_run(available):
    """subprocess.run stub: exit 0 only for `<available> version`."""

    def run(cmd, **kwargs):
        base = cmd[:-1]  # drop the trailing "version"
        rc = 0 if base == available else 1
        return subprocess.CompletedProcess(cmd, rc, b"", b"")

    return run


def test_prefers_v2_plugin(monkeypatch):
    monkeypatch.setattr(subprocess, "run", _fake_run(["docker", "compose"]))
    assert compose_cmd.resolve_compose_cmd() == ["docker", "compose"]


def test_falls_back_to_v1_standalone(monkeypatch):
    monkeypatch.setattr(subprocess, "run", _fake_run(["docker-compose"]))
    assert compose_cmd.resolve_compose_cmd() == ["docker-compose"]


def test_defaults_to_v2_when_neither_present(monkeypatch):
    def boom(cmd, **kwargs):
        raise FileNotFoundError("docker")

    monkeypatch.setattr(subprocess, "run", boom)
    assert compose_cmd.resolve_compose_cmd() == ["docker", "compose"]


def test_result_is_cached(monkeypatch):
    calls = {"n": 0}

    def run(cmd, **kwargs):
        calls["n"] += 1
        base = cmd[:-1]
        return subprocess.CompletedProcess(cmd, 0 if base == ["docker-compose"] else 1, b"", b"")

    monkeypatch.setattr(subprocess, "run", run)
    first = compose_cmd.resolve_compose_cmd()
    n_after_first = calls["n"]
    second = compose_cmd.resolve_compose_cmd()
    assert first == second == ["docker-compose"]
    assert calls["n"] == n_after_first  # cached; no re-probe
