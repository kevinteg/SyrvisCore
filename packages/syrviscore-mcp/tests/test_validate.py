"""Injection boundary tests (G2–G6). Malicious args must raise before any SSH."""

import pytest

from syrviscore_mcp import validate
from syrviscore_mcp.errors import ValidationError

INJECTION_CORPUS = [
    "0.0.0; reboot",
    "$(id)",
    "`id`",
    "x | cat /etc/passwd",
    "x & sleep 1",
    "../../etc/passwd",
    "-y",
    "--purge",
    "--upload-pack=/bin/sh",
    "a b",
    "a\tb",
    "a\nb",
    "\x00null",
    "x>out",
    "x<in",
    "$HOME",
    "-",
    "",
]


class TestVersion:
    @pytest.mark.parametrize("v", ["0.1.0", "v1.2.3", "10.20.30"])
    def test_valid(self, v):
        assert validate.validate_version(v)

    @pytest.mark.parametrize("v", INJECTION_CORPUS + ["0.1", "0.1.0-rc1", "1.2.3.4"])
    def test_invalid_rejected(self, v):
        with pytest.raises(ValidationError):
            validate.validate_version(v)


class TestName:
    @pytest.mark.parametrize("n", ["gollum", "home-assistant", "rag_db", "svc1"])
    def test_valid(self, n):
        assert validate.validate_name(n) == n

    @pytest.mark.parametrize("n", INJECTION_CORPUS + ["UPPER", ".hidden", "a" * 65])
    def test_invalid_rejected(self, n):
        with pytest.raises(ValidationError):
            validate.validate_name(n)

    @pytest.mark.parametrize("n", ["traefik", "portainer", "cloudflared", "proxy"])
    def test_reserved_rejected(self, n):
        with pytest.raises(ValidationError):
            validate.validate_name(n)


class TestGitUrl:
    @pytest.mark.parametrize(
        "u",
        [
            "https://github.com/user/repo.git",
            "https://github.com/user/repo",
            "git@github.com:user/repo.git",
            "ssh://git@github.com/user/repo.git",
        ],
    )
    def test_valid(self, u):
        assert validate.validate_git_url(u) == u

    @pytest.mark.parametrize(
        "u",
        [
            "file:///etc/passwd",
            "http://insecure/repo.git",
            "ext::sh -c id",
            "-oProxyCommand=id",
            "--upload-pack=/bin/sh",
            "https://h/r; reboot",
            "git@host:path`id`",
            "ssh://evil/`id`",
        ],
    )
    def test_dangerous_rejected(self, u):
        with pytest.raises(ValidationError):
            validate.validate_git_url(u)

    def test_host_allowlist(self):
        validate.validate_git_url("https://github.com/u/r", allowed_hosts=["github.com"])
        with pytest.raises(ValidationError):
            validate.validate_git_url("https://evil.com/u/r", allowed_hosts=["github.com"])


class TestInts:
    def test_tail_bounds(self):
        assert validate.validate_tail(100) == 100
        for bad in (0, -1, 10001, True, "5"):
            with pytest.raises(ValidationError):
                validate.validate_tail(bad)

    def test_keep_bounds(self):
        assert validate.validate_keep(2) == 2
        for bad in (-1, 51, True, "2"):
            with pytest.raises(ValidationError):
                validate.validate_keep(bad)
