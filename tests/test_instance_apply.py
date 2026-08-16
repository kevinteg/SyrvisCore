"""syrvis apply — the syrvis-instance/v1 core-tier bundle (schema + apply + CLI).

Mirrors test_deploy_bundle.py: schema strictness first (this is root parsing
attacker-controlled input), then apply semantics against a tmp SYRVIS_HOME,
then the CLI boundary with elevation stubbed. Secret VALUES must never appear
in reports or CLI output — several tests assert exactly that.
"""

import json
import stat

import pytest
import yaml
from click.testing import CliRunner

from syrviscore import stack as stack_mod
from syrviscore.instance_bundle import (
    INSTANCE_API_VERSION,
    InstanceBundle,
    InstanceBundleError,
    apply_instance_bundle,
)

from conftest import stamp_install_root


def decl(name="web", **over):
    d = {"name": name, "version": "1.0.0", "image": "nginx:1.25.3"}
    d.update(over)
    return d


def bundle_doc(**sections):
    doc = {"apiVersion": INSTANCE_API_VERSION}
    doc.update(sections)
    return doc


def make_env(**over):
    env = {"DOMAIN": "example.com", "TRAEFIK_IP": "192.168.1.100"}
    env.update(over)
    return env


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------


class TestParse:
    def test_not_a_mapping(self):
        with pytest.raises(InstanceBundleError, match="mapping"):
            InstanceBundle.from_dict(["x"])

    def test_missing_api_version(self):
        with pytest.raises(InstanceBundleError, match="apiVersion"):
            InstanceBundle.from_dict({"env": make_env()})

    def test_wrong_api_version(self):
        with pytest.raises(InstanceBundleError, match="apiVersion"):
            InstanceBundle.from_dict({"apiVersion": "syrvis-instance/v9", "env": make_env()})

    def test_unknown_key_rejected(self):
        with pytest.raises(InstanceBundleError, match="unknown bundle keys"):
            InstanceBundle.from_dict(bundle_doc(env=make_env(), extra={}))

    def test_empty_bundle_rejected(self):
        with pytest.raises(InstanceBundleError, match="declares nothing"):
            InstanceBundle.from_dict(bundle_doc())

    # --- env ---

    def test_env_must_be_mapping(self):
        with pytest.raises(InstanceBundleError, match="'env'"):
            InstanceBundle.from_dict(bundle_doc(env=["A=1"]))

    @pytest.mark.parametrize("key", ["lower", "1BAD", "SP ACE", "DASH-Y", ""])
    def test_env_bad_key_rejected(self, key):
        with pytest.raises(InstanceBundleError, match="invalid env key"):
            InstanceBundle.from_dict(bundle_doc(env={key: "v", "DOMAIN": "example.com"}))

    def test_env_non_string_value_rejected(self):
        with pytest.raises(InstanceBundleError, match="must be a string"):
            InstanceBundle.from_dict(bundle_doc(env=make_env(PORT=80)))

    def test_env_newline_value_rejected_and_not_echoed(self):
        with pytest.raises(InstanceBundleError) as exc:
            InstanceBundle.from_dict(bundle_doc(env=make_env(TOKEN="secret\nINJECTED=1")))
        assert "INJECTED" not in str(exc.value)  # value never echoed

    def test_env_requires_domain(self):
        with pytest.raises(InstanceBundleError, match="DOMAIN"):
            InstanceBundle.from_dict(bundle_doc(env={"ACME_EMAIL": "a@example.com"}))

    def test_env_size_cap(self):
        big = {"DOMAIN": "example.com", "BLOB": "x" * 70000}
        with pytest.raises(InstanceBundleError, match="too large"):
            InstanceBundle.from_dict(bundle_doc(env=big))

    # --- stack ---

    def test_stack_unknown_service_rejected(self):
        with pytest.raises(InstanceBundleError, match="unknown core service"):
            InstanceBundle.from_dict(bundle_doc(stack={"services": {"nginx": {"enabled": True}}}))

    def test_stack_primordial_cannot_be_disabled(self):
        with pytest.raises(InstanceBundleError, match="primordial"):
            InstanceBundle.from_dict(
                bundle_doc(stack={"services": {"traefik": {"enabled": False}}})
            )

    def test_stack_enabled_must_be_bool(self):
        with pytest.raises(InstanceBundleError, match="boolean"):
            InstanceBundle.from_dict(
                bundle_doc(stack={"services": {"cloudflared": {"enabled": "yes"}}})
            )

    def test_stack_wrong_version_rejected(self):
        with pytest.raises(InstanceBundleError, match="stack version"):
            InstanceBundle.from_dict(
                bundle_doc(stack={"version": 99, "services": {"cloudflared": {"enabled": True}}})
            )

    # --- declarations ---

    def test_declaration_runs_trust_boundary(self):
        with pytest.raises(InstanceBundleError, match="invalid declaration 'web'"):
            InstanceBundle.from_dict(bundle_doc(declarations={"web": decl(image="nginx:latest")}))

    def test_declaration_unknown_key_rejected_strict(self):
        with pytest.raises(InstanceBundleError, match="invalid declaration"):
            InstanceBundle.from_dict(bundle_doc(declarations={"web": decl(privileged=True)}))

    def test_declaration_name_mismatch(self):
        with pytest.raises(InstanceBundleError, match="must match"):
            InstanceBundle.from_dict(bundle_doc(declarations={"other": decl(name="web")}))

    def test_valid_bundle_parses(self):
        b = InstanceBundle.from_dict(
            bundle_doc(
                env=make_env(),
                stack={"services": {"cloudflared": {"enabled": True}}},
                declarations={"web": decl()},
            )
        )
        assert b.env["DOMAIN"] == "example.com"
        assert b.stack["services"]["cloudflared"]["enabled"] is True
        assert b.has_declarations and "web" in b.declarations


# ---------------------------------------------------------------------------
# Apply — env
# ---------------------------------------------------------------------------


class TestApplyEnv:
    def _apply(self, home, env, **kw):
        b = InstanceBundle.from_dict(bundle_doc(env=env))
        return apply_instance_bundle(b, home, **kw)

    def test_create(self, tmp_path):
        report = self._apply(tmp_path, make_env())
        assert report["env"]["action"] == "create"
        env_path = tmp_path / "config" / ".env"
        assert stat.S_IMODE(env_path.stat().st_mode) == 0o600
        text = env_path.read_text()
        assert "DOMAIN=example.com" in text
        assert "SYRVIS_HOME={}".format(tmp_path) in text  # auto-filled

    def test_syrvis_home_mismatch_rejected(self, tmp_path):
        with pytest.raises(InstanceBundleError, match="SYRVIS_HOME"):
            self._apply(tmp_path, make_env(SYRVIS_HOME="/volume9/elsewhere"))

    def test_update_reports_key_names_only(self, tmp_path):
        (tmp_path / "config").mkdir(parents=True)
        (tmp_path / "config" / ".env").write_text("DOMAIN=old.example\nGONE=1\n")
        report = self._apply(tmp_path, make_env())
        assert report["env"]["action"] == "update"
        assert "DOMAIN" in report["env"]["changed"]
        assert "GONE" in report["env"]["removed"]
        assert "TRAEFIK_IP" in report["env"]["added"]

    def test_idempotent_second_apply_unchanged(self, tmp_path):
        self._apply(tmp_path, make_env())
        report = self._apply(tmp_path, make_env())
        assert report["env"]["action"] == "unchanged"

    def test_secret_change_needs_flag(self, tmp_path):
        (tmp_path / "config").mkdir(parents=True)
        (tmp_path / "config" / ".env").write_text("DOMAIN=example.com\nX_TOKEN=oldvalue\n")
        with pytest.raises(InstanceBundleError) as exc:
            self._apply(tmp_path, make_env(X_TOKEN="newvalue"))
        assert "allow-secret-change" in str(exc.value)
        assert "oldvalue" not in str(exc.value) and "newvalue" not in str(exc.value)

    def test_secret_change_with_flag(self, tmp_path):
        (tmp_path / "config").mkdir(parents=True)
        (tmp_path / "config" / ".env").write_text("DOMAIN=example.com\nX_TOKEN=oldvalue\n")
        report = self._apply(tmp_path, make_env(X_TOKEN="newvalue"), allow_secret_change=True)
        assert "X_TOKEN" in report["env"]["changed"]
        assert "newvalue" in (tmp_path / "config" / ".env").read_text()

    def test_secret_removal_needs_flag(self, tmp_path):
        (tmp_path / "config").mkdir(parents=True)
        (tmp_path / "config" / ".env").write_text("DOMAIN=example.com\nX_TOKEN=oldvalue\n")
        with pytest.raises(InstanceBundleError, match="allow-secret-change"):
            self._apply(tmp_path, make_env())

    def test_new_secret_needs_no_flag(self, tmp_path):
        (tmp_path / "config").mkdir(parents=True)
        (tmp_path / "config" / ".env").write_text("DOMAIN=example.com\nX_TOKEN=\n")
        report = self._apply(tmp_path, make_env(X_TOKEN="firstvalue"))
        assert "X_TOKEN" in report["env"]["changed"]

    def test_report_never_contains_values(self, tmp_path):
        report = self._apply(tmp_path, make_env(X_TOKEN="supersecretvalue"))
        assert "supersecretvalue" not in json.dumps(report)

    def test_dry_run_writes_nothing(self, tmp_path):
        report = self._apply(tmp_path, make_env(), dry_run=True)
        assert report["dry_run"] is True
        assert not (tmp_path / "config" / ".env").exists()

    def test_dry_run_previews_secret_change_without_flag(self, tmp_path):
        """A plan writes nothing and reports key names only, so a rotation must be
        previewable over the seam (no --dry-run --allow-secret-change argv exists)."""
        (tmp_path / "config").mkdir(parents=True)
        (tmp_path / "config" / ".env").write_text("DOMAIN=example.com\nX_TOKEN=oldvalue\n")
        report = self._apply(tmp_path, make_env(X_TOKEN="newvalue"), dry_run=True)
        assert "X_TOKEN" in report["env"]["changed"]
        assert "newvalue" not in json.dumps(report)  # values never in the report
        assert not (tmp_path / "config" / ".env").read_text().count("newvalue")  # unwritten

    def test_whitespace_value_is_idempotent(self, tmp_path):
        """A value with surrounding whitespace is stripped to match read-back, so
        re-applying the same bundle reports 'unchanged' (not an eternal 'update')."""
        self._apply(tmp_path, make_env(NOTE="  spaced  "))
        written = (tmp_path / "config" / ".env").read_text()
        assert "NOTE=spaced" in written
        report = self._apply(tmp_path, make_env(NOTE="  spaced  "))
        assert report["env"]["action"] == "unchanged"


# ---------------------------------------------------------------------------
# Apply — stack + declarations
# ---------------------------------------------------------------------------


class TestApplyStackDeclarations:
    def test_stack_written_and_loadable(self, tmp_path):
        b = InstanceBundle.from_dict(
            bundle_doc(stack={"services": {"cloudflared": {"enabled": True}}})
        )
        report = apply_instance_bundle(b, tmp_path)
        assert report["stack"]["action"] == "create"
        assert report["stack"]["enabled_changes"]["cloudflared"] is True
        path = tmp_path / "config" / "stack.yaml"
        assert stat.S_IMODE(path.stat().st_mode) == 0o644
        loaded = stack_mod.from_dict(yaml.safe_load(path.read_text()))
        assert loaded.is_enabled("cloudflared") and loaded.is_enabled("traefik")

    def test_stack_unchanged_second_apply(self, tmp_path):
        b = InstanceBundle.from_dict(
            bundle_doc(stack={"services": {"cloudflared": {"enabled": True}}})
        )
        apply_instance_bundle(b, tmp_path)
        report = apply_instance_bundle(b, tmp_path)
        assert report["stack"]["action"] == "unchanged"

    def test_declarations_replace_set(self, tmp_path):
        d = tmp_path / "config" / "services.d"
        d.mkdir(parents=True)
        (d / "stale.yaml").write_text("name: stale\n")
        b = InstanceBundle.from_dict(bundle_doc(declarations={"web": decl()}))
        report = apply_instance_bundle(b, tmp_path)
        assert report["declarations"]["written"] == ["web"]
        assert report["declarations"]["removed"] == ["stale"]
        assert (d / "web.yaml").exists() and not (d / "stale.yaml").exists()
        # the written declaration round-trips through the strict schema
        data = yaml.safe_load((d / "web.yaml").read_text())
        assert data["name"] == "web" and data["image"] == "nginx:1.25.3"

    def test_declaration_with_env_written_0600(self, tmp_path):
        b = InstanceBundle.from_dict(bundle_doc(declarations={"web": decl(environment=["A=1"])}))
        apply_instance_bundle(b, tmp_path)
        path = tmp_path / "config" / "services.d" / "web.yaml"
        assert stat.S_IMODE(path.stat().st_mode) == 0o600

    def test_declarations_unchanged_second_apply(self, tmp_path):
        b = InstanceBundle.from_dict(bundle_doc(declarations={"web": decl()}))
        apply_instance_bundle(b, tmp_path)
        report = apply_instance_bundle(b, tmp_path)
        assert report["declarations"]["unchanged"] == ["web"]
        assert report["declarations"]["written"] == []

    def test_empty_declarations_removes_all(self, tmp_path):
        b = InstanceBundle.from_dict(bundle_doc(declarations={"web": decl()}))
        apply_instance_bundle(b, tmp_path)
        b2 = InstanceBundle.from_dict(bundle_doc(env=make_env(), declarations={}))
        report = apply_instance_bundle(b2, tmp_path)
        assert report["declarations"]["removed"] == ["web"]

    def test_absent_declarations_untouched(self, tmp_path):
        b = InstanceBundle.from_dict(bundle_doc(declarations={"web": decl()}))
        apply_instance_bundle(b, tmp_path)
        b2 = InstanceBundle.from_dict(bundle_doc(env=make_env()))
        report = apply_instance_bundle(b2, tmp_path)
        assert report["declarations"] is None
        assert (tmp_path / "config" / "services.d" / "web.yaml").exists()

    def test_dry_run_full_bundle_writes_nothing(self, tmp_path):
        b = InstanceBundle.from_dict(
            bundle_doc(
                env=make_env(),
                stack={"services": {"dashboard": {"enabled": True}}},
                declarations={"web": decl()},
            )
        )
        report = apply_instance_bundle(b, tmp_path, dry_run=True)
        assert report["dry_run"] is True
        assert not (tmp_path / "config").exists()


# ---------------------------------------------------------------------------
# CLI — syrvis apply (elevation stubbed)
# ---------------------------------------------------------------------------


class TestApplyCli:
    def _run(self, monkeypatch, tmp_path, argv, stdin):
        import syrviscore.privilege as privilege
        from syrviscore.cli import cli

        monkeypatch.setattr(privilege, "ensure_elevated", lambda *a, **k: None)
        monkeypatch.setenv("SYRVIS_HOME", str(tmp_path))
        stamp_install_root(tmp_path)
        return CliRunner().invoke(cli, argv, input=stdin)

    def test_registered(self):
        from syrviscore.cli import cli

        assert "apply" in cli.commands

    def test_happy_path_json(self, monkeypatch, tmp_path):
        doc = json.dumps(bundle_doc(env=make_env(), declarations={"web": decl()}))
        r = self._run(monkeypatch, tmp_path, ["apply", "--json"], doc)
        assert r.exit_code == 0, r.output
        report = json.loads(r.output)
        assert report["env"]["action"] == "create"
        assert report["declarations"]["written"] == ["web"]
        assert (tmp_path / "config" / ".env").exists()

    def test_dry_run_writes_nothing(self, monkeypatch, tmp_path):
        doc = json.dumps(bundle_doc(env=make_env()))
        r = self._run(monkeypatch, tmp_path, ["apply", "--dry-run", "--json"], doc)
        assert r.exit_code == 0, r.output
        assert json.loads(r.output)["dry_run"] is True
        assert not (tmp_path / "config" / ".env").exists()

    def test_secret_guard_json_error_envelope(self, monkeypatch, tmp_path):
        (tmp_path / "config").mkdir(parents=True)
        (tmp_path / "config" / ".env").write_text("DOMAIN=example.com\nX_TOKEN=oldvalue\n")
        doc = json.dumps(bundle_doc(env=make_env(X_TOKEN="newvalue")))
        r = self._run(monkeypatch, tmp_path, ["apply", "--json"], doc)
        assert r.exit_code != 0
        err = json.loads(r.output)
        assert "allow-secret-change" in err["error"]
        assert "oldvalue" not in r.output and "newvalue" not in r.output

    def test_allow_secret_change_flag(self, monkeypatch, tmp_path):
        (tmp_path / "config").mkdir(parents=True)
        (tmp_path / "config" / ".env").write_text("DOMAIN=example.com\nX_TOKEN=oldvalue\n")
        doc = json.dumps(bundle_doc(env=make_env(X_TOKEN="newvalue")))
        r = self._run(monkeypatch, tmp_path, ["apply", "--allow-secret-change", "--json"], doc)
        assert r.exit_code == 0, r.output

    def test_invalid_json_rejected(self, monkeypatch, tmp_path):
        r = self._run(monkeypatch, tmp_path, ["apply"], "{not json")
        assert r.exit_code != 0
        assert "not valid JSON" in r.output

    def test_empty_stdin_rejected(self, monkeypatch, tmp_path):
        r = self._run(monkeypatch, tmp_path, ["apply"], "")
        assert r.exit_code != 0
        assert "no bundle" in r.output


class TestDeclarationRemovalFailure:
    def test_unlinkable_declaration_surfaces(self, tmp_path):
        """A removal that can't happen (a dir shadows the file) must fail the
        apply, not report a false convergence."""
        d = tmp_path / "config" / "services.d"
        d.mkdir(parents=True)
        # a DIRECTORY named stale.yaml — unlink() raises IsADirectoryError (OSError)
        (d / "stale.yaml").mkdir()
        b = InstanceBundle.from_dict(bundle_doc(declarations={"web": decl()}))
        with pytest.raises(InstanceBundleError, match="could not remove declaration"):
            apply_instance_bundle(b, tmp_path)


class TestExport:
    def _seed(self, home):
        (home / "config" / "services.d").mkdir(parents=True)
        (home / "config" / ".env").write_text(
            "DOMAIN=example.com\nTRAEFIK_IP=192.168.1.100\nX_TOKEN=supersecret\n"
        )
        from syrviscore import stack as stack_mod

        st = stack_mod.default_stack()
        st.services["cloudflared"].enabled = True
        (home / "config" / "stack.yaml").write_text(st.to_yaml())
        (home / "config" / "services.d" / "web.yaml").write_text(yaml.safe_dump(decl(name="web")))

    def test_redacts_secrets_by_default(self, tmp_path):
        from syrviscore.instance_bundle import export_instance

        self._seed(tmp_path)
        bundle = export_instance(tmp_path)
        assert bundle["apiVersion"] == "syrvis-instance/v1"
        assert bundle["env"]["DOMAIN"] == "example.com"
        assert bundle["env"]["X_TOKEN"] == "****"  # secret redacted
        assert "supersecret" not in json.dumps(bundle)
        assert bundle["stack"]["services"]["cloudflared"]["enabled"] is True
        assert bundle["declarations"]["web"]["image"] == "nginx:1.25.3"

    def test_reveal_secrets(self, tmp_path):
        from syrviscore.instance_bundle import export_instance

        self._seed(tmp_path)
        bundle = export_instance(tmp_path, reveal_secrets=True)
        assert bundle["env"]["X_TOKEN"] == "supersecret"

    def test_export_roundtrips_through_apply(self, tmp_path):
        """A revealed export re-applies cleanly (structural round-trip)."""
        from syrviscore.instance_bundle import (
            InstanceBundle,
            apply_instance_bundle,
            export_instance,
        )

        self._seed(tmp_path)
        exported = export_instance(tmp_path, reveal_secrets=True)
        # a fresh home; applying the exported bundle reproduces the config
        dest = tmp_path / "dest"
        exported["env"]["SYRVIS_HOME"] = str(dest)
        report = apply_instance_bundle(InstanceBundle.from_dict(exported), dest)
        assert report["declarations"]["written"] == ["web"]
        assert (dest / "config" / ".env").exists()
        assert (dest / "config" / "services.d" / "web.yaml").exists()

    def test_cli_export_yaml_default(self, tmp_path, monkeypatch):
        from click.testing import CliRunner

        from syrviscore.cli import cli

        self._seed(tmp_path)
        monkeypatch.setenv("SYRVIS_HOME", str(tmp_path))
        stamp_install_root(tmp_path)
        r = CliRunner().invoke(cli, ["export"])
        assert r.exit_code == 0, r.output
        doc = yaml.safe_load(r.output)
        assert doc["apiVersion"] == "syrvis-instance/v1"
        assert doc["env"]["X_TOKEN"] == "****"
        assert "supersecret" not in r.output


class TestExportRedaction:
    def test_declaration_inline_env_secret_redacted(self, tmp_path):
        from syrviscore.instance_bundle import export_instance

        d = tmp_path / "config" / "services.d"
        d.mkdir(parents=True)
        (d / "web.yaml").write_text(
            yaml.safe_dump(
                decl(name="web", environment=["LOG_LEVEL=info", "API_TOKEN=supersecret"])
            )
        )
        bundle = export_instance(tmp_path)
        env = bundle["declarations"]["web"]["environment"]
        assert "LOG_LEVEL=info" in env  # non-secret kept
        assert "API_TOKEN=****" in env  # secret value masked
        assert "supersecret" not in json.dumps(bundle)

    def test_dns_env_credential_var_redacted(self, tmp_path):
        """A DNS-01 provider credential var (named in TRAEFIK_ACME_DNS_ENV) is
        redacted even though its name lacks a TOKEN/SECRET/KEY marker."""
        from syrviscore.instance_bundle import export_instance

        (tmp_path / "config").mkdir(parents=True)
        (tmp_path / "config" / ".env").write_text(
            "DOMAIN=example.com\nTRAEFIK_ACME_DNS_ENV=DESEC_AUTH\nDESEC_AUTH=leakme\n"
        )
        bundle = export_instance(tmp_path)
        assert bundle["env"]["DESEC_AUTH"] == "****"
        assert "leakme" not in json.dumps(bundle)

    def test_unparseable_declaration_raises(self, tmp_path):
        from syrviscore.instance_bundle import InstanceBundleError, export_instance

        d = tmp_path / "config" / "services.d"
        d.mkdir(parents=True)
        (d / "broken.yaml").write_text("{not: valid: yaml:")
        with pytest.raises(InstanceBundleError, match="cannot export declaration 'broken'"):
            export_instance(tmp_path)
