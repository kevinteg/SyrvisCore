"""Byte-exact argv construction per command (G1/G7/G8) and no-shell-string (G1)."""

import pytest

from syrviscore_mcp import remote
from syrviscore_mcp.commands import get_command
from syrviscore_mcp.errors import ValidationError

from .conftest import make_config

CTL = "/var/packages/syrviscore/target/venv/bin/syrvisctl"
WRAP = "/volume1/syrviscore/bin/syrvis"
HOME = "/volume1/syrviscore"


@pytest.fixture
def cfg():
    return make_config()


class TestArgvExact:
    def test_status(self, cfg):
        toks = remote.build_remote_tokens(cfg, get_command("status"), {})
        assert toks == [WRAP, "status", "--json"]

    def test_verify_smoke(self, cfg):
        toks = remote.build_remote_tokens(cfg, get_command("verify_smoke"), {})
        assert toks == [WRAP, "verify", "--smoke", "--json"]

    def test_start_has_sudo(self, cfg):
        toks = remote.build_remote_tokens(cfg, get_command("start"), {})
        assert toks == ["sudo", "-n", WRAP, "start"]

    def test_service_stop_positional_after_dashdash(self, cfg):
        toks = remote.build_remote_tokens(cfg, get_command("service_stop"), {"name": "gollum"})
        assert toks == ["sudo", "-n", WRAP, "service", "stop", "--", "gollum"]

    def test_service_add(self):
        cfg = make_config(git_url_allowed_hosts=["github.com"])
        toks = remote.build_remote_tokens(
            cfg, get_command("service_add"), {"git_url": "https://github.com/u/r.git"}
        )
        assert toks == [
            "sudo",
            "-n",
            WRAP,
            "service",
            "add",
            "--",
            "https://github.com/u/r.git",
        ]

    def test_install_with_version(self, cfg):
        toks = remote.build_remote_tokens(cfg, get_command("install"), {"version": "0.2.0"})
        assert toks == ["sudo", "-n", CTL, "install", "-y", "--path", HOME, "--", "0.2.0"]

    def test_install_without_version(self, cfg):
        toks = remote.build_remote_tokens(cfg, get_command("install"), {"version": None})
        assert toks == ["sudo", "-n", CTL, "install", "-y", "--path", HOME]

    def test_activate(self, cfg):
        toks = remote.build_remote_tokens(cfg, get_command("activate"), {"version": "0.2.0"})
        assert toks == ["sudo", "-n", CTL, "activate", "--", "0.2.0"]

    def test_cleanup_flag_value(self, cfg):
        toks = remote.build_remote_tokens(cfg, get_command("cleanup"), {"keep": 3})
        assert toks == ["sudo", "-n", CTL, "cleanup", "--keep", "3", "-y"]

    def test_service_remove(self, cfg):
        toks = remote.build_remote_tokens(cfg, get_command("service_remove"), {"name": "blog"})
        assert toks == ["sudo", "-n", WRAP, "service", "remove", "-y", "--", "blog"]

    def test_versions_list_no_sudo_no_path(self, cfg):
        toks = remote.build_remote_tokens(cfg, get_command("versions_list"), {})
        assert toks == [CTL, "list", "--json"]

    def test_logs_flag_value_and_positional(self, cfg):
        toks = remote.build_remote_tokens(
            cfg, get_command("logs"), {"service": "gollum", "tail": 50}
        )
        assert toks == [WRAP, "logs", "-n", "50", "--", "gollum"]

    def test_no_sudo_when_use_sudo_false(self):
        cfg = make_config(use_sudo=False)
        toks = remote.build_remote_tokens(cfg, get_command("start"), {})
        assert toks == [WRAP, "start"]

    def test_reconcile_plan(self, cfg):
        toks = remote.build_remote_tokens(cfg, get_command("reconcile_plan"), {})
        assert toks == ["sudo", "-n", WRAP, "reconcile", "--dry-run", "--json"]

    def test_reconcile(self, cfg):
        toks = remote.build_remote_tokens(cfg, get_command("reconcile"), {})
        assert toks == ["sudo", "-n", WRAP, "reconcile", "--json", "-y"]

    def test_reconcile_prune(self, cfg):
        toks = remote.build_remote_tokens(cfg, get_command("reconcile_prune"), {"prune": "stop"})
        assert toks == ["sudo", "-n", WRAP, "reconcile", "--json", "-y", "--prune", "stop"]

    def test_service_declare(self, cfg):
        cfg = make_config(image_allowed_registries=["ghcr.io"])
        toks = remote.build_remote_tokens(
            cfg,
            get_command("service_declare"),
            {
                "name": "cyberquill",
                "image": "ghcr.io/acme/cyberquill:1.4.0",
                "subdomain": "cyberquill",
                "exposure": "tunnel",
                "port": 8080,
                "enabled": "true",
                "critical": "false",
            },
        )
        assert toks == [
            "sudo",
            "-n",
            WRAP,
            "service",
            "declare",
            "--image",
            "ghcr.io/acme/cyberquill:1.4.0",
            "--subdomain",
            "cyberquill",
            "--exposure",
            "tunnel",
            "--port",
            "8080",
            "--enabled",
            "true",
            "--critical",
            "false",
            "--json",
            "--",
            "cyberquill",
        ]

    def test_service_adopt(self, cfg):
        toks = remote.build_remote_tokens(cfg, get_command("service_adopt"), {"name": "gollum"})
        assert toks == ["sudo", "-n", WRAP, "service", "adopt", "--json", "--", "gollum"]


class TestInjectionBlockedAtArgv:
    @pytest.mark.parametrize("bad", ["0.0.0; reboot", "$(id)", "-y", "../etc", "a b"])
    def test_bad_version_never_builds(self, cfg, bad):
        with pytest.raises(ValidationError):
            remote.build_remote_tokens(cfg, get_command("activate"), {"version": bad})

    @pytest.mark.parametrize("bad", ["--purge", "; rm", "traefik"])
    def test_bad_name_never_builds(self, cfg, bad):
        with pytest.raises(ValidationError):
            remote.build_remote_tokens(cfg, get_command("service_stop"), {"name": bad})

    @pytest.mark.parametrize("bad", ["everything", "stop;id", "-y", "Purge", "stop remove"])
    def test_bad_prune_policy_never_builds(self, cfg, bad):
        with pytest.raises(ValidationError):
            remote.build_remote_tokens(cfg, get_command("reconcile_prune"), {"prune": bad})

    @pytest.mark.parametrize("bad", ["True", "1", "yes", "true;id", ""])
    def test_bad_bool_flag_never_builds(self, bad):
        cfg = make_config(image_allowed_registries=["ghcr.io"])
        args = {
            "name": "cq",
            "image": "ghcr.io/a/b:1.0",
            "subdomain": "cq",
            "exposure": "internal",
            "port": 80,
            "enabled": bad,
            "critical": "false",
        }
        with pytest.raises(ValidationError):
            remote.build_remote_tokens(cfg, get_command("service_declare"), args)


class TestNoShellString:
    def test_source_has_no_shell_true_or_string_ssh(self):
        """G1: remote.py must never invoke a shell or f-string an ssh command."""
        import pathlib

        src = pathlib.Path(remote.__file__).read_text()
        assert "shell=True" not in src
        # ssh command is a list; the only join is the shlex-quoted remote string
        assert "shlex.quote" in src

    def test_ssh_argv_quotes_each_token(self, cfg):
        toks = remote.build_remote_tokens(cfg, get_command("service_stop"), {"name": "gollum"})
        argv = remote.build_ssh_argv(cfg, toks)
        assert argv[0] == "ssh"
        assert "-T" in argv and "BatchMode=yes" in argv
        # the remote command is a single trailing argument
        assert argv[-1].endswith("gollum")
