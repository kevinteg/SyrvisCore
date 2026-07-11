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
    HOSTS = ["github.com"]

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
        assert validate.validate_git_url(u, self.HOSTS) == u

    def test_empty_allowlist_fails_closed(self):
        # no allowlist -> service_add disabled (fail closed), never allow-any
        with pytest.raises(ValidationError):
            validate.validate_git_url("https://github.com/u/r", [])
        with pytest.raises(ValidationError):
            validate.validate_git_url("https://github.com/u/r", None)

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
        with pytest.raises(ValidationError):
            validate.validate_git_url("git@evil.example:u/r", allowed_hosts=["github.com"])


class TestUnicodeDigits:
    def test_unicode_version_rejected(self):
        # homoglyph digits must not pass (re.ASCII)
        with pytest.raises(ValidationError):
            validate.validate_version("१.२.३")


class TestPrunePolicy:
    @pytest.mark.parametrize("p", ["stop", "remove", "purge"])
    def test_valid(self, p):
        assert validate.validate_prune_policy(p) == p

    @pytest.mark.parametrize(
        "p", INJECTION_CORPUS + ["Stop", "REMOVE", "everything", "purge ", "stop;id"]
    )
    def test_invalid_rejected(self, p):
        with pytest.raises(ValidationError):
            validate.validate_prune_policy(p)


class TestBoolFlag:
    @pytest.mark.parametrize("b", ["true", "false"])
    def test_valid(self, b):
        assert validate.validate_bool_flag(b) == b

    @pytest.mark.parametrize(
        "b", INJECTION_CORPUS + ["True", "FALSE", "1", "0", "yes", "no", "true "]
    )
    def test_invalid_rejected(self, b):
        with pytest.raises(ValidationError):
            validate.validate_bool_flag(b)

    @pytest.mark.parametrize("b", [True, False, 1, None])
    def test_non_string_rejected(self, b):
        # the MCP tool renders Python bools lowercase BEFORE this boundary;
        # the slot validator itself only ever accepts the two strings
        with pytest.raises(ValidationError):
            validate.validate_bool_flag(b)


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
