"""Container-image update checking: ref parsing, version compare, registry query."""

import json

import pytest

from syrviscore import image_updates as iu


class TestParseImageRef:
    def test_bare_hub_official(self):
        r = iu.parse_image_ref("traefik:v3.6.5")
        assert (r.registry, r.repository, r.tag, r.digest) == (
            "docker.io",
            "library/traefik",
            "v3.6.5",
            "",
        )
        assert r.api_host == "registry-1.docker.io"

    def test_hub_org_repo(self):
        r = iu.parse_image_ref("portainer/portainer-ce:2.33.6-alpine")
        assert r.registry == "docker.io"
        assert r.repository == "portainer/portainer-ce"
        assert r.tag == "2.33.6-alpine"

    def test_registry_qualified(self):
        r = iu.parse_image_ref("ghcr.io/kevinteg/syrviscore-dashboard:0.1.5")
        assert r.registry == "ghcr.io"
        assert r.repository == "kevinteg/syrviscore-dashboard"
        assert r.tag == "0.1.5"
        assert r.api_host == "ghcr.io"

    def test_registry_with_port(self):
        r = iu.parse_image_ref("registry.local:5000/team/app:1.2.3")
        assert r.registry == "registry.local:5000"
        assert r.repository == "team/app"
        assert r.tag == "1.2.3"

    def test_digest_pin(self):
        r = iu.parse_image_ref("traefik@sha256:" + "a" * 64)
        assert r.tag == ""
        assert r.digest == "sha256:" + "a" * 64
        assert r.repository == "library/traefik"

    def test_tag_and_digest(self):
        r = iu.parse_image_ref("ghcr.io/o/n:1.0.0@sha256:" + "b" * 64)
        assert r.tag == "1.0.0" and r.digest == "sha256:" + "b" * 64

    def test_empty_rejected(self):
        with pytest.raises(ValueError):
            iu.parse_image_ref("  ")


class TestVersionCompare:
    def test_finds_newer_same_flavor(self):
        newer = iu.find_newer_tags("v3.6.5", ["v3.6.4", "v3.6.5", "v3.6.6", "v3.7.1", "v3.10.0"])
        assert newer == ["v3.6.6", "v3.7.1", "v3.10.0"]  # numeric, not lexicographic

    def test_respects_alpine_suffix(self):
        # a plain-suffix current must not jump to -alpine, and vice versa
        newer = iu.find_newer_tags(
            "2.33.6-alpine", ["2.33.7-alpine", "2.34.0", "2.34.0-alpine", "2.33.6-alpine"]
        )
        assert newer == ["2.33.7-alpine", "2.34.0-alpine"]

    def test_never_suggests_prerelease_over_stable(self):
        newer = iu.find_newer_tags("2.33.6", ["2.34.0-rc1", "2.34.0", "latest"])
        assert newer == ["2.34.0"]

    def test_v_prefix_flavor_isolated(self):
        newer = iu.find_newer_tags("v1.2.0", ["1.3.0", "v1.3.0"])  # v-prefix must match
        assert newer == ["v1.3.0"]

    def test_calver(self):
        newer = iu.find_newer_tags("2026.7.1", ["2026.7.0", "2026.7.2", "2026.8.0"])
        assert newer == ["2026.7.2", "2026.8.0"]

    def test_uncomparable_current_returns_empty(self):
        assert iu.find_newer_tags("latest", ["1.0.0", "2.0.0"]) == []

    def test_shorter_tuple_padding(self):
        # 3.6 vs 3.6.1 — pad and compare
        assert iu.find_newer_tags("3.6", ["3.6.1", "3.6"]) == ["3.6.1"]
        assert iu.find_newer_tags("3.6.1", ["3.6"]) == []


class FakeResp:
    def __init__(self, status=200, body=None, headers=None):
        self.status_code = status
        self._body = body or {}
        self.headers = headers or {}

    def json(self):
        return self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise AssertionError("status {}".format(self.status_code))


class FakeSession:
    """Scripts registry responses by URL substring for list_tags/check_image."""

    def __init__(self, script):
        self.script = script
        self.calls = []

    def get(self, url, params=None, headers=None, timeout=None):
        self.calls.append((url, params, headers))
        for needle, resp in self.script:
            if needle in url:
                return resp
        return FakeResp(404)


class TestListTagsAndCheck:
    def test_anonymous_401_then_token_then_tags(self, monkeypatch):
        ref = iu.parse_image_ref("ghcr.io/o/n:1.0.0")
        session = FakeSession(
            [
                ("token", FakeResp(200, {"token": "T"})),
                (
                    "/v2/o/n/tags/list",
                    None,  # placeholder; handled below
                ),
            ]
        )

        # First call to tags/list → 401 with challenge; second (with token) → 200.
        state = {"n": 0}

        def get(url, params=None, headers=None, timeout=None):
            session.calls.append((url, params, headers))
            if "tags/list" in url:
                state["n"] += 1
                if state["n"] == 1:
                    return FakeResp(
                        401,
                        headers={
                            "WWW-Authenticate": 'Bearer realm="https://ghcr.io/token",'
                            'service="ghcr.io",scope="repository:o/n:pull"'
                        },
                    )
                assert headers.get("Authorization") == "Bearer T"
                return FakeResp(200, {"tags": ["1.0.0", "1.1.0", "1.2.0"]})
            if "ghcr.io/token" in url:
                assert params.get("scope") == "repository:o/n:pull"
                return FakeResp(200, {"token": "T"})
            return FakeResp(404)

        session.get = get
        tags = iu.list_tags(ref, session=session)
        assert tags == ["1.0.0", "1.1.0", "1.2.0"]

    def test_pagination_follows_link(self):
        ref = iu.parse_image_ref("ghcr.io/o/n:1.0.0")

        def get(url, params=None, headers=None, timeout=None):
            if "last=" in url or (params and False):
                return FakeResp(200, {"tags": ["3.0.0"]})
            if "tags/list" in url and "last=" not in url:
                return FakeResp(
                    200,
                    {"tags": ["1.0.0", "2.0.0"]},
                    headers={"Link": '</v2/o/n/tags/list?last=2.0.0&n=100>; rel="next"'},
                )
            return FakeResp(404)

        session = FakeSession([])
        session.get = get
        tags = iu.list_tags(ref, session=session)
        assert tags == ["1.0.0", "2.0.0", "3.0.0"]

    def test_check_image_reports_update(self):
        session = FakeSession(
            [("tags/list", FakeResp(200, {"tags": ["v3.6.5", "v3.6.6", "v3.7.0"]}))]
        )
        r = iu.check_image("traefik:v3.6.5", session=session)
        assert r["update_available"] is True
        assert r["latest"] == "v3.7.0"
        assert r["newer"] == ["v3.6.6", "v3.7.0"]
        assert r["error"] is None

    def test_check_image_up_to_date(self):
        session = FakeSession([("tags/list", FakeResp(200, {"tags": ["v3.6.5", "v3.6.4"]}))])
        r = iu.check_image("traefik:v3.6.5", session=session)
        assert r["update_available"] is False
        assert r["latest"] == "v3.6.5"

    def test_check_image_digest_pin(self):
        r = iu.check_image("traefik@sha256:" + "a" * 64)
        assert r["update_available"] is False
        assert "digest" in r["error"]

    def test_check_image_registry_404(self):
        session = FakeSession([("tags/list", FakeResp(404))])
        r = iu.check_image("ghcr.io/o/missing:1.0.0", session=session)
        assert r["update_available"] is False
        assert "not found" in r["error"]


class TestCollectAndCache:
    def test_check_updates_caches(self, tmp_path, monkeypatch):
        # No install context beyond home; collect returns [] but the report caches.
        monkeypatch.setattr(iu, "collect_pinned_images", lambda home=None: [])
        report = iu.check_updates(home=tmp_path, now=1000.0)
        assert report["count"] == 0 and report["cached"] is False
        cache = tmp_path / "data" / ".image-updates-cache.json"
        assert cache.is_file()
        # a second call within TTL returns the cache (no recompute)
        again = iu.check_updates(home=tmp_path, now=1000.0 + 60)
        assert again["cached"] is True

    def test_refresh_bypasses_cache(self, tmp_path, monkeypatch):
        calls = {"n": 0}

        def fake_collect(home=None):
            calls["n"] += 1
            return []

        monkeypatch.setattr(iu, "collect_pinned_images", fake_collect)
        iu.check_updates(home=tmp_path, now=1000.0)
        iu.check_updates(home=tmp_path, now=1000.0 + 60, refresh=True)
        assert calls["n"] == 2  # refresh recomputed

    def test_read_cached(self, tmp_path):
        cache = tmp_path / "data" / ".image-updates-cache.json"
        cache.parent.mkdir(parents=True)
        cache.write_text(json.dumps({"count": 3, "update_count": 1, "images": []}))
        got = iu.read_cached(home=tmp_path)
        assert got["count"] == 3 and got["cached"] is True

    def test_read_cached_absent(self, tmp_path):
        assert iu.read_cached(home=tmp_path) is None

    def test_collect_dedups_and_kinds(self, tmp_path, monkeypatch):
        """core + L2 images collected, de-duplicated, tagged with kind."""
        from syrviscore import stack as stack_mod

        # a stack with dashboard enabled so it's collected
        st = stack_mod.default_stack()
        st.services["dashboard"].enabled = True
        monkeypatch.setattr(stack_mod, "load_stack", lambda: st)

        from syrviscore.compose import ComposeGenerator

        monkeypatch.setattr(
            ComposeGenerator,
            "load_config",
            lambda self: {
                "docker_images": {
                    "traefik": {"full_image": "traefik:v3.6.5"},
                    "portainer": {"full_image": "portainer/portainer-ce:2.33.6"},
                    "cloudflared": {"full_image": "cloudflare/cloudflared:2026.7.1"},
                    "dashboard": {"full_image": "ghcr.io/x/dash:0.1.5"},
                }
            },
        )
        images = iu.collect_pinned_images(tmp_path)
        by_name = {i["name"]: i for i in images}
        assert by_name["traefik"]["kind"] == "core"
        assert by_name["dashboard"]["image"] == "ghcr.io/x/dash:0.1.5"
        # cloudflared is optional + disabled in the default stack → not collected
        assert "cloudflared" not in by_name


class TestUpdatesCli:
    def _run(self, monkeypatch, tmp_path, argv, report):
        from click.testing import CliRunner

        from syrviscore import image_updates
        from syrviscore.cli import cli

        monkeypatch.setenv("SYRVIS_HOME", str(tmp_path))
        monkeypatch.setattr(image_updates, "check_updates", lambda **kw: report)
        return CliRunner().invoke(cli, argv)

    def test_registered(self):
        from syrviscore.cli import cli

        assert "updates" in cli.commands

    def test_json_output(self, monkeypatch, tmp_path):
        report = {"count": 1, "update_count": 1, "images": [{"name": "traefik"}], "cached": False}
        r = self._run(monkeypatch, tmp_path, ["updates", "--json"], report)
        assert r.exit_code == 0, r.output
        assert json.loads(r.output)["update_count"] == 1

    def test_human_output_shows_arrow(self, monkeypatch, tmp_path):
        report = {
            "count": 2,
            "update_count": 1,
            "cached": False,
            "images": [
                {
                    "name": "traefik",
                    "current": "v3.6.5",
                    "latest": "v3.7.0",
                    "newer": ["v3.7.0"],
                    "update_available": True,
                    "error": None,
                },
                {"name": "web", "current": "1.25.3", "update_available": False, "error": None},
            ],
        }
        r = self._run(monkeypatch, tmp_path, ["updates"], report)
        assert r.exit_code == 0, r.output
        assert "traefik" in r.output and "v3.6.5" in r.output and "v3.7.0" in r.output
        assert "up to date" in r.output

    def test_refresh_flag_forwarded(self, monkeypatch, tmp_path):
        seen = {}
        from click.testing import CliRunner

        from syrviscore import image_updates
        from syrviscore.cli import cli

        monkeypatch.setenv("SYRVIS_HOME", str(tmp_path))
        monkeypatch.setattr(
            image_updates,
            "check_updates",
            lambda **kw: seen.update(kw) or {"images": [], "count": 0, "update_count": 0},
        )
        CliRunner().invoke(cli, ["updates", "--refresh"])
        assert seen.get("refresh") is True
