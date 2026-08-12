"""
Tests for the deployment bundle (design/21): the syrvis-bundle schema, the
atomic ``ServiceManager.deploy_bundle`` apply, and the ``syrvis deploy`` CLI.

A bundle is attacker-controlled input that root turns into filesystem writes, so
the schema tests pin the trust boundary (unknown keys, traversal dests, env_file
collisions, secrets-without-env_file, unpinned images) and the apply tests pin
the invariants: config 0644 / secret 0600, start LAST, atomic rollback, targeted
(never touches another service), and no secret value in any message.
"""

import json
import os
import stat

import pytest
import yaml
from click.testing import CliRunner

from syrviscore.bundle import (
    BUNDLE_API_VERSION,
    BundleValidationError,
    DeployBundle,
)


def base_manifest(**overrides):
    m = {"name": "snmp-exporter", "version": "v0.30.1", "image": "prom/snmp-exporter:v0.30.1"}
    m.update(overrides)
    return m


def base_bundle(**overrides):
    doc = {"service": base_manifest()}
    doc.update(overrides)
    return doc


# ---------------------------------------------------------------------------
# Schema — DeployBundle.from_dict (the trust boundary)
# ---------------------------------------------------------------------------


class TestBundleSchema:
    def test_valid_full_bundle(self):
        b = DeployBundle.from_dict(
            {
                "apiVersion": BUNDLE_API_VERSION,
                "service": base_manifest(
                    env_file="secrets.env",
                    volumes=["config:/etc/snmp_exporter:ro"],
                    command=["--config.file=/etc/snmp_exporter/snmp.yml"],
                ),
                "configs": [{"dest": "config/snmp.yml", "content": "auths: {}\n"}],
                "secrets": {"SNMP_V3_USER": "snmp-monitor", "SNMP_V3_AUTH_PASS": "pw"},
            }
        )
        assert b.name == "snmp-exporter"
        assert [c.dest for c in b.configs] == ["config/snmp.yml"]
        assert b.secrets["SNMP_V3_USER"] == "snmp-monitor"

    def test_minimal_bundle_manifest_only(self):
        b = DeployBundle.from_dict(base_bundle())
        assert b.configs == [] and b.secrets == {}

    def test_default_apiversion_accepted(self):
        # apiVersion may be omitted (defaults to the current one)
        assert DeployBundle.from_dict(base_bundle()).name == "snmp-exporter"

    @pytest.mark.parametrize(
        "mutate",
        [
            lambda d: d.update(evil=1),  # unknown top-level key
            lambda d: d.update(apiVersion="syrvis-bundle/v2"),  # unknown version
            lambda d: d.pop("service"),  # missing manifest
            lambda d: d.update(configs="notalist"),
            lambda d: d.update(secrets=["notamap"]),
        ],
    )
    def test_structural_rejections(self, mutate):
        d = base_bundle()
        mutate(d)
        with pytest.raises(BundleValidationError):
            DeployBundle.from_dict(d)

    @pytest.mark.parametrize("dest", ["../escape.yml", "/etc/passwd", "a/../../b"])
    def test_config_dest_traversal_rejected(self, dest):
        with pytest.raises(BundleValidationError):
            DeployBundle.from_dict(base_bundle(configs=[{"dest": dest, "content": "x"}]))

    def test_config_dest_cannot_be_env_file(self):
        with pytest.raises(BundleValidationError, match="env_file"):
            DeployBundle.from_dict(
                base_bundle(
                    service=base_manifest(env_file="secrets.env"),
                    configs=[{"dest": "secrets.env", "content": "x"}],
                )
            )

    def test_duplicate_config_dest_rejected(self):
        with pytest.raises(BundleValidationError, match="duplicate"):
            DeployBundle.from_dict(
                base_bundle(
                    configs=[
                        {"dest": "config/a", "content": "1"},
                        {"dest": "config/a", "content": "2"},
                    ]
                )
            )

    def test_config_content_must_be_string(self):
        with pytest.raises(BundleValidationError):
            DeployBundle.from_dict(base_bundle(configs=[{"dest": "config/a", "content": 42}]))

    def test_config_unknown_key_rejected(self):
        with pytest.raises(BundleValidationError):
            DeployBundle.from_dict(
                base_bundle(configs=[{"dest": "config/a", "content": "x", "mode": "0777"}])
            )

    def test_config_secret_flag(self):
        b = DeployBundle.from_dict(
            base_bundle(configs=[{"dest": "config/a", "content": "x", "secret": True}])
        )
        assert b.configs[0].secret is True
        # default is non-secret
        b2 = DeployBundle.from_dict(base_bundle(configs=[{"dest": "config/a", "content": "x"}]))
        assert b2.configs[0].secret is False
        # must be a bool
        with pytest.raises(BundleValidationError, match="boolean"):
            DeployBundle.from_dict(
                base_bundle(configs=[{"dest": "config/a", "content": "x", "secret": "yes"}])
            )

    @pytest.mark.parametrize("key", ["1BAD", "has space", "with-dash", "", "FOO\n", "FOO\nBAR"])
    def test_bad_secret_env_key_rejected(self, key):
        # incl. trailing/embedded newline — a $-anchored .match() would let "FOO\n"
        # through (fullmatch closes it), corrupting the line-oriented env_file.
        with pytest.raises(BundleValidationError):
            DeployBundle.from_dict(
                base_bundle(service=base_manifest(env_file="secrets.env"), secrets={key: "v"})
            )

    @pytest.mark.parametrize("value", ["a\nADMIN=1", "a\r\nX=2", "line\nbreak"])
    def test_secret_value_with_newline_rejected(self, value):
        # A newline in a VALUE would inject extra KEY=VALUE lines into the env_file.
        with pytest.raises(BundleValidationError, match="newline"):
            DeployBundle.from_dict(
                base_bundle(service=base_manifest(env_file="secrets.env"), secrets={"OK": value})
            )

    @pytest.mark.parametrize("name", ["svc\n", "sv\nc", "SVC", "../evil"])
    def test_service_name_with_newline_or_bad_charset_rejected(self, name):
        # fullmatch: "svc\n" must not become a data/<name>/ dir + declaration.
        with pytest.raises(BundleValidationError):
            DeployBundle.from_dict(base_bundle(service=base_manifest(name=name)))

    def test_secrets_require_env_file(self):
        with pytest.raises(BundleValidationError, match="env_file"):
            DeployBundle.from_dict(base_bundle(secrets={"A": "b"}))  # no env_file declared

    def test_unpinned_image_rejected_uniformly(self):
        # the inner ServiceDefinition error surfaces as ONE BundleValidationError
        with pytest.raises(BundleValidationError, match="latest"):
            DeployBundle.from_dict(base_bundle(service=base_manifest(image="prom/x:latest")))

    def test_oversize_config_rejected(self):
        big = "x" * (65536 + 1)
        with pytest.raises(BundleValidationError, match="too large"):
            DeployBundle.from_dict(base_bundle(configs=[{"dest": "config/a", "content": big}]))


# ---------------------------------------------------------------------------
# Apply — ServiceManager.deploy_bundle (docker start stubbed)
# ---------------------------------------------------------------------------


def _manager(tmp_path, start_ok=True):
    from syrviscore.service_manager import ServiceManager

    os.environ.setdefault("DOMAIN", "example.com")
    mgr = ServiceManager(syrvis_home=tmp_path)
    mgr._ensure_directories()
    mgr._reload_traefik = lambda: None
    mgr._start_service = lambda name, cp: (start_ok, "started" if start_ok else "boom")
    return mgr


def _snmp_bundle():
    return DeployBundle.from_dict(
        {
            "service": base_manifest(
                env_file="secrets.env",
                volumes=["config:/etc/snmp_exporter:ro"],
                command=[
                    "--config.file=/etc/snmp_exporter/snmp.yml",
                    "--config.expand-environment-variables",
                ],
                networks=["proxy"],
            ),
            "configs": [
                {
                    "dest": "config/snmp.yml",
                    "content": "auths:\n  synology_v3:\n    username: ${SNMP_V3_USER}\n",
                }
            ],
            "secrets": {
                "SNMP_V3_USER": "snmp-monitor",
                "SNMP_V3_AUTH_PASS": "a",
                "SNMP_V3_PRIV_PASS": "p",
            },
        }
    )


class TestDeployBundleApply:
    def test_fresh_install_writes_everything(self, tmp_path):
        mgr = _manager(tmp_path)
        ok, msg = mgr.deploy_bundle(_snmp_bundle())
        assert ok, msg
        assert "installed" in msg

        # declaration
        assert (tmp_path / "config" / "services.d" / "snmp-exporter.yaml").exists()
        # config: 0644, content preserved (placeholders intact — not resolved here)
        cfg = tmp_path / "data" / "snmp-exporter" / "config" / "snmp.yml"
        assert stat.S_IMODE(cfg.stat().st_mode) == 0o644
        assert "${SNMP_V3_USER}" in cfg.read_text()
        # env_file: 0600, all three secrets
        env = tmp_path / "data" / "snmp-exporter" / "secrets.env"
        assert stat.S_IMODE(env.stat().st_mode) == 0o600
        assert env.read_text().count("=") == 3
        # compose carries command + env_file
        compose = yaml.safe_load((tmp_path / "compose" / "snmp-exporter.yaml").read_text())
        svc = compose["services"]["snmp-exporter"]
        assert svc["command"][-1] == "--config.expand-environment-variables"
        assert svc["env_file"]

    def test_update_is_idempotent_and_preserves_data(self, tmp_path):
        mgr = _manager(tmp_path)
        assert mgr.deploy_bundle(_snmp_bundle())[0]
        # drop a marker in the data dir; an UPDATE must not wipe it
        marker = tmp_path / "data" / "snmp-exporter" / "keepme"
        marker.write_text("x")
        ok, msg = mgr.deploy_bundle(_snmp_bundle())
        assert ok and "updated" in msg
        assert marker.exists()

    def test_failed_start_rolls_back_fresh_install(self, tmp_path):
        mgr = _manager(tmp_path, start_ok=False)
        ok, msg = mgr.deploy_bundle(_snmp_bundle())
        assert not ok and "failed" in msg
        # fresh install rollback removes the service + its just-created data dir
        assert not (tmp_path / "services" / "snmp-exporter").exists()
        assert not (tmp_path / "data" / "snmp-exporter").exists()
        # ...but the declared intent remains for a retry (written outside rollback)
        assert (tmp_path / "config" / "services.d" / "snmp-exporter.yaml").exists()

    def test_failed_update_blast_radius_is_documented_behavior(self, tmp_path):
        # design/21: a FAILED update takes the service DOWN, keeps data, and leaves
        # the declaration at the NEW (failed) manifest — NOT blue/green. Pin it so
        # the documented limitation is intentional + regression-guarded.
        mgr = _manager(tmp_path)
        assert mgr.deploy_bundle(_snmp_bundle())[0]  # fresh install ok
        (tmp_path / "data" / "snmp-exporter" / "keepme").write_text("x")
        mgr._start_service = lambda name, cp: (False, "boom")  # the update will fail at start
        changed = DeployBundle.from_dict(
            {
                "service": base_manifest(
                    version="v0.30.2",
                    env_file="secrets.env",
                    volumes=["config:/etc/snmp_exporter:ro"],
                    networks=["proxy"],
                ),
                "configs": [{"dest": "config/snmp.yml", "content": "changed\n"}],
                "secrets": {
                    "SNMP_V3_USER": "u",
                    "SNMP_V3_AUTH_PASS": "a",
                    "SNMP_V3_PRIV_PASS": "p",
                },
            }
        )
        ok, msg = mgr.deploy_bundle(changed)
        assert not ok and "failed" in msg
        # torn down: compose + service dir gone; data preserved; declaration KEPT
        # and now holds the new (failed) version — recovery is a re-deploy.
        assert not (tmp_path / "compose" / "snmp-exporter.yaml").exists()
        assert (tmp_path / "data" / "snmp-exporter" / "keepme").exists()
        decl = tmp_path / "config" / "services.d" / "snmp-exporter.yaml"
        assert decl.exists() and "v0.30.2" in decl.read_text()

    def test_config_carrying_update_restarts_to_apply_change(self, tmp_path):
        # `up -d` won't recreate an unchanged compose, so a changed bind-mounted
        # config/secret would keep serving old content — deploy_bundle restarts the
        # container on a config/secret-carrying UPDATE to re-read it.
        mgr = _manager(tmp_path)
        assert mgr.deploy_bundle(_snmp_bundle())[0]  # fresh install
        calls = []
        mgr._compose = lambda name, cp, *a, **k: (calls.append(a) or (True, ""))
        assert mgr.deploy_bundle(_snmp_bundle())[0]  # update (has config + secrets)
        assert any("restart" in a for a in calls), f"expected a restart; calls={calls}"

    def test_configless_update_does_not_restart(self, tmp_path):
        mgr = _manager(tmp_path)
        b = DeployBundle.from_dict(
            base_bundle(service=base_manifest(name="vm", networks=["proxy"]))
        )
        assert mgr.deploy_bundle(b)[0]  # fresh
        calls = []
        mgr._compose = lambda name, cp, *a, **k: (calls.append(a) or (True, ""))
        assert mgr.deploy_bundle(b)[0]  # update, NO configs/secrets
        assert not any("restart" in a for a in calls), "config-less update must not restart"

    def test_no_secret_value_in_return_message(self, tmp_path):
        mgr = _manager(tmp_path)
        ok, msg = mgr.deploy_bundle(_snmp_bundle())
        assert ok
        assert "snmp-monitor" not in msg and "secret(s)" in msg

    def test_place_config_refuses_to_downgrade_a_0600_file(self, tmp_path):
        mgr = _manager(tmp_path)
        assert mgr.deploy_bundle(_snmp_bundle())[0]
        # simulate a pre-existing 0600 secret at a config path
        secret_path = tmp_path / "data" / "snmp-exporter" / "config" / "hush"
        secret_path.write_text("top")
        os.chmod(secret_path, 0o600)
        ok, msg = mgr._place_config("snmp-exporter", "config/hush", "world-readable")
        assert not ok and "secret" in msg.lower()
        assert stat.S_IMODE(secret_path.stat().st_mode) == 0o600  # untouched

    def test_place_config_confined_to_data_dir(self, tmp_path):
        mgr = _manager(tmp_path)
        assert mgr.deploy_bundle(_snmp_bundle())[0]
        ok, msg = mgr._place_config("snmp-exporter", "../../escape", "x")
        assert not ok

    def test_secret_config_written_0600(self, tmp_path):
        mgr = _manager(tmp_path)
        b = DeployBundle.from_dict(
            {
                "service": base_manifest(
                    name="svc",
                    env_file="secrets.env",
                    volumes=["config:/etc/x:ro"],
                    networks=["proxy"],
                ),
                "configs": [
                    {"dest": "config/public.yml", "content": "a"},
                    {"dest": "config/token.scfg", "content": "topic secret-slug", "secret": True},
                ],
                "secrets": {"K": "v"},
            }
        )
        assert mgr.deploy_bundle(b)[0]
        pub = tmp_path / "data" / "svc" / "config" / "public.yml"
        sec = tmp_path / "data" / "svc" / "config" / "token.scfg"
        assert stat.S_IMODE(pub.stat().st_mode) == 0o644  # non-secret config
        assert stat.S_IMODE(sec.stat().st_mode) == 0o600  # secret config
        # a secret RE-write over the existing 0600 is fine (not a downgrade)
        ok, _ = mgr._place_config("svc", "config/token.scfg", "topic new", secret=True)
        assert ok and stat.S_IMODE(sec.stat().st_mode) == 0o600
        # but a NON-secret write over that 0600 is refused (downgrade guard)
        ok2, msg2 = mgr._place_config("svc", "config/token.scfg", "x", secret=False)
        assert not ok2 and "secret" in msg2.lower()


# ---------------------------------------------------------------------------
# CLI — syrvis deploy (elevation stubbed; no docker)
# ---------------------------------------------------------------------------


class TestDeployCli:
    def _run(self, monkeypatch, tmp_path, argv, stdin, deploy_impl=None):
        import syrviscore.privilege as privilege
        from syrviscore import service_manager
        from syrviscore.cli import cli

        monkeypatch.setattr(privilege, "ensure_elevated", lambda *a, **k: None)
        monkeypatch.setenv("SYRVIS_HOME", str(tmp_path))
        if deploy_impl is not None:
            monkeypatch.setattr(service_manager.ServiceManager, "deploy_bundle", deploy_impl)
        return CliRunner().invoke(cli, argv, input=stdin)

    def test_registered(self):
        from syrviscore.cli import cli

        assert "deploy" in cli.commands

    def test_happy_path_calls_manager(self, monkeypatch, tmp_path):
        seen = {}

        def fake_deploy(self, bundle):
            seen["name"] = bundle.name
            return True, "deployed snmp-exporter (installed; 1 config(s), 3 secret(s))"

        bundle = json.dumps(
            {
                "service": base_manifest(env_file="secrets.env"),
                "configs": [{"dest": "config/snmp.yml", "content": "x"}],
                "secrets": {"SNMP_V3_USER": "snmp-monitor"},
            }
        )
        r = self._run(monkeypatch, tmp_path, ["deploy", "--", "snmp-exporter"], bundle, fake_deploy)
        assert r.exit_code == 0, r.output
        assert seen["name"] == "snmp-exporter"
        assert "snmp-monitor" not in r.output  # secret never echoed

    def test_name_mismatch_rejected(self, monkeypatch, tmp_path):
        bundle = json.dumps({"service": base_manifest(name="other")})
        r = self._run(monkeypatch, tmp_path, ["deploy", "--", "snmp-exporter"], bundle)
        assert r.exit_code != 0
        assert "does not match" in r.output

    def test_invalid_json_rejected(self, monkeypatch, tmp_path):
        r = self._run(monkeypatch, tmp_path, ["deploy", "--", "snmp-exporter"], "{not json")
        assert r.exit_code != 0
        assert "not valid JSON" in r.output

    def test_empty_stdin_rejected(self, monkeypatch, tmp_path):
        r = self._run(monkeypatch, tmp_path, ["deploy", "--", "snmp-exporter"], "")
        assert r.exit_code != 0


# ---------------------------------------------------------------------------
# design/26 — app location over the deploy verb + the pre-pull companion fix
# ---------------------------------------------------------------------------


def _v2_env(tmp_path, monkeypatch, mounted=True):
    """Fake the /volumeN plumbing onto tmp_path (see test_service_security)."""
    from syrviscore import paths as paths_mod

    monkeypatch.setattr(
        paths_mod,
        "resolve_volume_root",
        lambda loc: tmp_path / "volumes" / str(loc).lstrip("/"),
    )
    monkeypatch.setattr(paths_mod, "is_mounted_volume", lambda loc: mounted)
    return tmp_path / "volumes" / "volume6" / "syrviscore" / "apps"


def _pg_bundle(**service_overrides):
    manifest = base_manifest(
        name="pg",
        image="postgres:16.4",
        location="/volume6",
        env_file="secrets.env",
        volumes=["pgdata:/var/lib/postgresql/data"],
        networks=["proxy"],
    )
    manifest.update(service_overrides)
    return DeployBundle.from_dict(
        {
            "service": manifest,
            "configs": [{"dest": "tuning.conf", "content": "shared_buffers=1GB\n"}],
            "secrets": {"POSTGRES_PASSWORD": "hunter2"},
        }
    )


class TestDeployBundleLocation:
    def test_fresh_v2_deploy_lands_in_app_home(self, tmp_path, monkeypatch):
        apps = _v2_env(tmp_path / "vol", monkeypatch)
        mgr = _manager(tmp_path)
        ok, msg = mgr.deploy_bundle(_pg_bundle())
        assert ok, msg
        home = apps / "pg"
        # config -> home/config (config-root-relative dest), secret -> home/secrets
        cfg = home / "config" / "tuning.conf"
        assert cfg.read_text() == "shared_buffers=1GB\n"
        assert stat.S_IMODE(cfg.stat().st_mode) == 0o644
        env = home / "secrets" / "secrets.env"
        assert "POSTGRES_PASSWORD=hunter2" in env.read_text()
        assert stat.S_IMODE(env.stat().st_mode) == 0o600
        # compose bind source under home/data
        compose = yaml.safe_load((tmp_path / "compose" / "pg.yaml").read_text())
        src = compose["services"]["pg"]["volumes"][0].split(":")[0]
        assert src == os.path.realpath(str(home / "data" / "pgdata"))
        # legacy data dir untouched
        assert not (tmp_path / "data" / "pg").exists()

    def test_update_refuses_location_change_with_data(self, tmp_path, monkeypatch):
        _v2_env(tmp_path / "vol", monkeypatch)
        mgr = _manager(tmp_path)
        assert mgr.deploy_bundle(_snmp_bundle())[0]  # legacy install
        (tmp_path / "data" / "snmp-exporter" / "state.db").write_text("data")
        relocated = DeployBundle.from_dict(
            {
                "service": base_manifest(
                    location="/volume6",
                    env_file="secrets.env",
                    networks=["proxy"],
                ),
                "secrets": {"SNMP_V3_USER": "u"},
            }
        )
        ok, msg = mgr.deploy_bundle(relocated)
        assert not ok
        assert "app-move" in msg and "wiki/runbooks/app-move.md" in msg
        # nothing was torn down or rewritten: the installed manifest is
        # still location-less and the data survived
        manifest = yaml.safe_load(
            (tmp_path / "services" / "snmp-exporter" / "syrvis-service.yaml").read_text()
        )
        assert "location" not in manifest
        assert (tmp_path / "data" / "snmp-exporter" / "state.db").exists()

    def test_update_location_change_proceeds_after_old_root_cleared(self, tmp_path, monkeypatch):
        # The documented app-move bypass: clear the old data root, then the
        # re-declare/deploy with the new location goes through.
        import shutil

        apps = _v2_env(tmp_path / "vol", monkeypatch)
        mgr = _manager(tmp_path)
        assert mgr.deploy_bundle(_snmp_bundle())[0]
        (tmp_path / "data" / "snmp-exporter" / "state.db").write_text("data")
        relocated = DeployBundle.from_dict(
            {
                "service": base_manifest(
                    location="/volume6",
                    env_file="secrets.env",
                    networks=["proxy"],
                ),
                "secrets": {"SNMP_V3_USER": "u"},
            }
        )
        assert not mgr.deploy_bundle(relocated)[0]  # refused with data
        shutil.rmtree(tmp_path / "data" / "snmp-exporter")  # operator clears it
        ok, msg = mgr.deploy_bundle(relocated)
        assert ok, msg
        manifest = yaml.safe_load(
            (tmp_path / "services" / "snmp-exporter" / "syrvis-service.yaml").read_text()
        )
        assert manifest["location"] == "/volume6"
        assert (apps / "snmp-exporter" / "secrets" / "secrets.env").exists()

    def test_prepopulated_home_is_silently_adopted(self, tmp_path, monkeypatch):
        # The END of the app-move procedure: the new home already holds the
        # copied data — install must adopt it (exist_ok, never wipe, no flag)
        # and the compose must point at it.
        apps = _v2_env(tmp_path / "vol", monkeypatch)
        sentinel = apps / "pg" / "data" / "pgdata" / "PG_VERSION"
        sentinel.parent.mkdir(parents=True)
        sentinel.write_text("16")
        mgr = _manager(tmp_path)
        ok, msg = mgr.deploy_bundle(_pg_bundle())
        assert ok, msg
        assert sentinel.read_text() == "16"  # survived, byte for byte
        compose = yaml.safe_load((tmp_path / "compose" / "pg.yaml").read_text())
        src = compose["services"]["pg"]["volumes"][0].split(":")[0]
        assert src == os.path.realpath(str(sentinel.parent))

    def test_adopted_data_survives_failed_fresh_install(self, tmp_path, monkeypatch):
        # A FRESH install normally drops its just-created data on rollback —
        # but ADOPTED data predates the call and must survive the failure.
        apps = _v2_env(tmp_path / "vol", monkeypatch)
        sentinel = apps / "pg" / "data" / "pgdata" / "PG_VERSION"
        sentinel.parent.mkdir(parents=True)
        sentinel.write_text("16")
        mgr = _manager(tmp_path, start_ok=False)
        ok, msg = mgr.deploy_bundle(_pg_bundle())
        assert not ok
        assert sentinel.exists()

    def test_fresh_v2_without_adoption_rolls_back_home(self, tmp_path, monkeypatch):
        apps = _v2_env(tmp_path / "vol", monkeypatch)
        mgr = _manager(tmp_path, start_ok=False)
        ok, _ = mgr.deploy_bundle(_pg_bundle())
        assert not ok
        # empty home was created by this call — rollback drops it entirely
        assert not (apps / "pg").exists()


class TestPrePull:
    """design/26 companion fix: best-effort compose pull before the timed up."""

    def _mgr(self, tmp_path):
        from syrviscore.service_manager import ServiceManager

        return ServiceManager(syrvis_home=tmp_path)

    def test_pull_runs_before_up_with_900s_budget(self, tmp_path):
        mgr = self._mgr(tmp_path)
        calls = []
        mgr._compose = lambda name, cp, *args, timeout: (
            calls.append((args, timeout)) or (True, "")
        )
        ok, msg = mgr._start_service("svc", tmp_path / "c.yaml")
        assert ok and msg == "Started"
        assert calls == [(("pull",), 900), (("up", "-d"), 120)]

    def test_pull_failure_is_not_fatal(self, tmp_path):
        mgr = self._mgr(tmp_path)
        calls = []

        def compose(name, cp, *args, timeout):
            calls.append(args)
            if args[0] == "pull":
                return False, "registry unreachable"
            return True, ""

        mgr._compose = compose
        ok, msg = mgr._start_service("svc", tmp_path / "c.yaml")
        assert ok and msg == "Started"  # up was still attempted and won
        assert calls == [("pull",), ("up", "-d")]

    def test_up_failure_still_fails(self, tmp_path):
        mgr = self._mgr(tmp_path)
        mgr._compose = lambda name, cp, *args, timeout: (
            (True, "") if args[0] == "pull" else (False, "boom")
        )
        ok, msg = mgr._start_service("svc", tmp_path / "c.yaml")
        assert not ok and msg == "boom"


class TestV2SlotMappingDeploy:
    """End-to-end over the deploy verb: a located app that mounts `config`
    reads exactly what the bundle's configs placed (design/26 slot mapping)."""

    def test_config_volume_and_bundle_config_share_one_tree(self, tmp_path, monkeypatch):
        apps = _v2_env(tmp_path / "vol", monkeypatch)
        mgr = _manager(tmp_path)
        bundle = DeployBundle.from_dict(
            {
                "service": base_manifest(
                    name="exporter",
                    location="/volume6",
                    env_file="secrets.env",
                    volumes=["config:/etc/exporter:ro"],
                    networks=["proxy"],
                ),
                "configs": [{"dest": "exporter.yml", "content": "modules: {}\n"}],
                "secrets": {"TOKEN": "t"},
            }
        )
        ok, msg = mgr.deploy_bundle(bundle)
        assert ok, msg
        home = apps / "exporter"
        compose = yaml.safe_load((tmp_path / "compose" / "exporter.yaml").read_text())
        src = compose["services"]["exporter"]["volumes"][0].rsplit(":", 2)[0]
        assert src == os.path.realpath(str(home / "config"))
        # the mount source contains the placed config — one tree, no divergence
        assert (home / "config" / "exporter.yml").read_text() == "modules: {}\n"


class TestPrePullLocalShortCircuit:
    """Adversarial review #7: routine starts must not touch the registry —
    the pre-pull runs ONLY when the pinned image is genuinely missing
    locally (the huge-first-pull case the 900s budget exists for)."""

    def _mgr_with_compose(self, tmp_path, image="postgres:16.4"):
        from syrviscore.service_manager import ServiceManager

        mgr = ServiceManager(syrvis_home=tmp_path)
        compose_path = tmp_path / "c.yaml"
        compose_path.write_text(yaml.safe_dump({"services": {"svc": {"image": image}}}))
        calls = []
        mgr._compose = lambda name, cp, *args, timeout: (
            calls.append((args, timeout)) or (True, "")
        )
        return mgr, compose_path, calls

    def test_image_present_skips_the_pull(self, tmp_path):
        mgr, compose_path, calls = self._mgr_with_compose(tmp_path)
        mgr._image_present_locally = lambda image: True
        ok, msg = mgr._start_service("svc", compose_path)
        assert ok and msg == "Started"
        assert calls == [(("up", "-d"), 120)]  # no pull issued at all

    def test_image_absent_pulls_with_900s_budget(self, tmp_path):
        mgr, compose_path, calls = self._mgr_with_compose(tmp_path)
        mgr._image_present_locally = lambda image: False
        seen = []
        mgr._image_present_locally = lambda image: (seen.append(image), False)[1]
        ok, msg = mgr._start_service("svc", compose_path)
        assert ok and msg == "Started"
        assert seen == ["postgres:16.4"]  # checked the COMPOSE-pinned ref
        assert calls == [(("pull",), 900), (("up", "-d"), 120)]

    def test_undetermined_image_errs_toward_pulling(self, tmp_path):
        # no compose file at all -> cannot prove the image is local -> pull
        from syrviscore.service_manager import ServiceManager

        mgr = ServiceManager(syrvis_home=tmp_path)
        calls = []
        mgr._compose = lambda name, cp, *args, timeout: (
            calls.append((args, timeout)) or (True, "")
        )
        mgr._image_present_locally = lambda image: (_ for _ in ()).throw(
            AssertionError("must not be called without a pinned image")
        )
        ok, _ = mgr._start_service("svc", tmp_path / "missing.yaml")
        assert ok
        assert calls == [(("pull",), 900), (("up", "-d"), 120)]


class TestBundleAdoptionWholeHome:
    """Adversarial review #6, deploy-verb site: a config-only pre-staged home
    survives a failed fresh deploy's rollback."""

    def test_config_only_home_survives_failed_fresh_deploy(self, tmp_path, monkeypatch):
        apps = _v2_env(tmp_path / "vol", monkeypatch)
        staged = apps / "pg" / "config" / "precious.conf"
        staged.parent.mkdir(parents=True)
        staged.write_text("staged-by-operator\n")
        mgr = _manager(tmp_path, start_ok=False)
        ok, _ = mgr.deploy_bundle(_pg_bundle())
        assert not ok
        assert staged.exists()
