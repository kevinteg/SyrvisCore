"""Platform-curated profiles (`syrvis profile enable monitoring`).

Every bundled catalog template must resolve through the full schema (that is
what pins the profile's images), and enabling must be strictly additive: an
existing declaration or config file is never overwritten — the deployment owns
anything it customized.
"""

import pytest
import yaml
from click.testing import CliRunner

from syrviscore import catalog, profiles
from syrviscore.services_d import declaration_path


class TestCatalogTemplates:
    def test_every_bundled_template_resolves(self, monkeypatch):
        """A template that fails the schema ships a broken profile — fail here."""
        monkeypatch.delenv("SYRVIS_HOME", raising=False)
        for entry in catalog.list_templates():
            assert "error" not in entry, entry

    def test_infra_members_carry_tier(self):
        assert catalog.resolve("node-exporter").tier == "infra"
        assert catalog.resolve("docker-socket-proxy").tier == "infra"
        assert catalog.resolve("victoria-metrics").tier == ""

    def test_monitoring_members_all_exist_in_catalog(self):
        for name in profiles.PROFILES["monitoring"]["services"]:
            assert catalog.resolve(name).name == name


class TestEnableProfile:
    def test_unknown_profile(self, tmp_path):
        with pytest.raises(profiles.ProfileError, match="unknown profile"):
            profiles.enable_profile("nope", tmp_path)

    def test_enable_writes_declarations_and_configs(self, tmp_path):
        report = profiles.enable_profile("monitoring", tmp_path)
        assert set(report["declared"]) == set(profiles.PROFILES["monitoring"]["services"])
        # declarations round-trip through the strict schema (incl. tier: infra)
        decl = yaml.safe_load(declaration_path(tmp_path, "node-exporter").read_text())
        assert decl["tier"] == "infra"
        assert decl["image"].startswith("prom/node-exporter:")
        # seeded configs land under data/<svc>/
        scrape = tmp_path / "data" / "vmagent" / "config" / "scrape.yml"
        assert scrape.exists() and "node-exporter:9100" in scrape.read_text()
        assert (tmp_path / "data" / "alertmanager" / "config" / "alertmanager.yml").exists()

    def test_enable_never_overwrites(self, tmp_path):
        d = declaration_path(tmp_path, "vmagent")
        d.parent.mkdir(parents=True)
        d.write_text("name: vmagent\ncustom: true\n")  # deployment-owned
        cfg = tmp_path / "data" / "vmagent" / "config" / "scrape.yml"
        cfg.parent.mkdir(parents=True)
        cfg.write_text("# customized\n")
        report = profiles.enable_profile("monitoring", tmp_path)
        assert "vmagent" in report["existing_declarations_kept"]
        assert "vmagent" not in report["declared"]
        assert d.read_text() == "name: vmagent\ncustom: true\n"
        assert cfg.read_text() == "# customized\n"
        assert "vmagent/config/scrape.yml" in report["configs_kept"]

    def test_dry_run_writes_nothing(self, tmp_path):
        report = profiles.enable_profile("monitoring", tmp_path, dry_run=True)
        assert report["dry_run"] is True
        assert report["declared"]  # would declare everything
        assert not (tmp_path / "config").exists()
        assert not (tmp_path / "data").exists()

    def test_idempotent_second_enable(self, tmp_path):
        profiles.enable_profile("monitoring", tmp_path)
        report = profiles.enable_profile("monitoring", tmp_path)
        assert report["declared"] == []
        assert set(report["existing_declarations_kept"]) == set(
            profiles.PROFILES["monitoring"]["services"]
        )
        assert report["configs_written"] == []


class TestCli:
    def test_profile_list_json(self, tmp_path, monkeypatch):
        import json

        from syrviscore.cli import cli

        monkeypatch.setenv("SYRVIS_HOME", str(tmp_path))
        r = CliRunner().invoke(cli, ["profile", "list", "--json"])
        assert r.exit_code == 0, r.output
        names = [p["name"] for p in json.loads(r.output)["profiles"]]
        assert "monitoring" in names

    def test_profile_enable_json(self, tmp_path, monkeypatch):
        import json

        import syrviscore.privilege as privilege
        from syrviscore.cli import cli

        monkeypatch.setattr(privilege, "ensure_elevated", lambda *a, **k: None)
        monkeypatch.setenv("SYRVIS_HOME", str(tmp_path))
        r = CliRunner().invoke(cli, ["profile", "enable", "monitoring", "--json"])
        assert r.exit_code == 0, r.output
        report = json.loads(r.output)
        assert "vmagent" in report["declared"]
        assert (tmp_path / "config" / "services.d" / "vmagent.yaml").exists()
