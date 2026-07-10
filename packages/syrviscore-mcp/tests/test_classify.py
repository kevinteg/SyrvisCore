"""Result classification into the typed error taxonomy (§5.4)."""

import pytest

from syrviscore_mcp import remote
from syrviscore_mcp.errors import (
    AuthError,
    CliError,
    ConfigError,
    HostKeyError,
    NetworkError,
    PrivilegeError,
    ProtocolError,
)


def R(rc, out="", err=""):
    return remote.RunResult(argv=["ssh"], returncode=rc, stdout=out, stderr=err)


def test_json_success():
    assert remote.classify(R(0, '{"a": 1}'), expect_json=True) == {"a": 1}


def test_verify_unhealthy_rc1_still_returns_dict():
    # verify emits valid JSON at rc==1 when unhealthy — must be honored
    out = remote.classify(R(1, '{"healthy": false}'), expect_json=True)
    assert out == {"healthy": False}


def test_non_json_when_expected_is_protocol_error():
    with pytest.raises(ProtocolError):
        remote.classify(R(0, "not json"), expect_json=True)


def test_host_key():
    with pytest.raises(HostKeyError):
        remote.classify(R(255, err="Host key verification failed."), expect_json=True)


def test_auth():
    with pytest.raises(AuthError):
        remote.classify(R(255, err="Permission denied (publickey)."), expect_json=True)


def test_network():
    with pytest.raises(NetworkError):
        remote.classify(
            R(255, err="ssh: connect to host ... Connection timed out"), expect_json=True
        )


def test_binary_missing():
    with pytest.raises(ConfigError):
        remote.classify(R(127, err="command not found"), expect_json=True)


def test_sudo_password_required():
    with pytest.raises(PrivilegeError):
        remote.classify(R(1, err="sudo: a password is required"), expect_json=False)


def test_sudo_not_allowed():
    with pytest.raises(PrivilegeError):
        remote.classify(
            R(1, err="Sorry, user operator is not allowed to execute ... sudoers"),
            expect_json=False,
        )


def test_non_json_command_ok():
    out = remote.classify(R(0, "Started"), expect_json=False)
    assert out["ok"] is True and "Started" in out["detail"]


def test_non_json_command_failure_is_cli_error():
    with pytest.raises(CliError):
        remote.classify(R(1, err="boom"), expect_json=False)
