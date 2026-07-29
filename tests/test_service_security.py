"""
Security tests for the Layer 2 service trust boundary.

A syrvis-service.yaml is attacker-controlled input from a third-party git
repo. These tests pin the guarantees from the Phase 3 audit fixes:
- C1: a service name cannot traverse out of its directories
- C2: volumes cannot mount arbitrary host paths or the docker socket
- unknown keys / unpinned images / privileged options are rejected
"""

import pytest

from syrviscore.service_schema import (
    ServiceDefinition,
    ServiceValidationError,
    validate_service_name,
)


class TestServiceNameValidation:
    @pytest.mark.parametrize(
        "name",
        [
            "../../../../usr/local/etc/rc.d/S99evil",
            "..",
            "foo/bar",
            "foo/../bar",
            "/etc/passwd",
            "UPPER",
            "has space",
            "trailing/",
            "",
            ".hidden",
            "a" * 65,
        ],
    )
    def test_malicious_names_rejected(self, name):
        with pytest.raises(ServiceValidationError):
            validate_service_name(name)

    @pytest.mark.parametrize("name", ["gollum", "home-assistant", "rag_db", "svc1"])
    def test_valid_names_accepted(self, name):
        assert validate_service_name(name) == name

    @pytest.mark.parametrize("name", ["traefik", "portainer", "cloudflared", "proxy"])
    def test_reserved_core_names_rejected(self, name):
        with pytest.raises(ServiceValidationError):
            validate_service_name(name)


def base_service(**overrides):
    data = {"name": "svc", "version": "1.0.0", "image": "nginx:1.27.0"}
    data.update(overrides)
    return data


class TestServiceDefinitionSecurity:
    def test_traversal_name_rejected_at_parse(self):
        with pytest.raises(ServiceValidationError):
            ServiceDefinition.from_dict(base_service(name="../../evil"))

    def test_container_name_traversal_rejected(self):
        with pytest.raises(ServiceValidationError):
            ServiceDefinition.from_dict(base_service(container_name="../../evil"))

    @pytest.mark.parametrize(
        "volume",
        [
            "/:/host:rw",
            "/etc:/etc:rw",
            "/var/run/docker.sock:/var/run/docker.sock:ro",
            "../../../etc:/etc:rw",
            "../escape:/data:rw",
            "$HOME/x:/data:rw",
            "data:/container:xw",  # bad mode
            "onlyonefield",
        ],
    )
    def test_dangerous_volumes_rejected(self, volume):
        with pytest.raises(ServiceValidationError):
            ServiceDefinition.from_dict(base_service(volumes=[volume]))

    @pytest.mark.parametrize(
        "volume",
        ["wiki:/wiki:rw", "subdir/data:/var/lib/app:ro", "conf:/etc/app"],
    )
    def test_safe_relative_volumes_accepted(self, volume):
        svc = ServiceDefinition.from_dict(base_service(volumes=[volume]))
        assert svc.volumes == [volume]

    def test_docker_sock_rejected_any_form(self):
        with pytest.raises(ServiceValidationError):
            ServiceDefinition.from_dict(base_service(volumes=["/var/run/docker.sock:/sock"]))

    def test_unknown_keys_rejected(self):
        # privileged/cap_add/network_mode etc. would arrive as unknown keys
        with pytest.raises(ServiceValidationError):
            ServiceDefinition.from_dict(base_service(privileged=True))
        with pytest.raises(ServiceValidationError):
            ServiceDefinition.from_dict(base_service(cap_add=["SYS_ADMIN"]))

    def test_depends_on_rejected_as_unsupported(self):
        # Each service is its own compose project, so depends_on can never work;
        # it must fail loudly at parse time, not silently no-op at run time.
        with pytest.raises(ServiceValidationError, match="depends_on is not supported"):
            ServiceDefinition.from_dict(base_service(depends_on=["db"]))
        # An empty/absent depends_on remains valid.
        assert ServiceDefinition.from_dict(base_service(depends_on=[])).depends_on == []

    @pytest.mark.parametrize("image", ["nginx", "nginx:latest", "nginx:", "has space:1.0"])
    def test_unpinned_or_latest_image_rejected(self, image):
        with pytest.raises(ServiceValidationError):
            ServiceDefinition.from_dict(base_service(image=image))

    def test_digest_pinned_image_accepted(self):
        digest = "nginx@sha256:" + "a" * 64
        svc = ServiceDefinition.from_dict(base_service(image=digest))
        assert svc.image == digest

    def test_bad_restart_policy_rejected(self):
        with pytest.raises(ServiceValidationError):
            ServiceDefinition.from_dict(base_service(restart="always-ish"))

    def test_bad_env_entry_rejected(self):
        with pytest.raises(ServiceValidationError):
            ServiceDefinition.from_dict(base_service(environment=["not-an-assignment"]))
        with pytest.raises(ServiceValidationError):
            ServiceDefinition.from_dict(base_service(environment=["1BAD=value"]))

    def test_bad_subdomain_rejected(self):
        with pytest.raises(ServiceValidationError):
            ServiceDefinition.from_dict(
                base_service(traefik={"enabled": True, "subdomain": "not a domain", "port": 80})
            )

    def test_bad_port_rejected(self):
        with pytest.raises(ServiceValidationError):
            ServiceDefinition.from_dict(
                base_service(traefik={"enabled": True, "subdomain": "wiki", "port": 99999})
            )


class TestComposeGenerationContainment:
    def _manager(self, tmp_path):
        from syrviscore.service_manager import ServiceManager

        return ServiceManager(syrvis_home=tmp_path)

    def test_compose_paths_stay_contained(self, tmp_path):
        mgr = self._manager(tmp_path)
        p = mgr._service_paths("gollum")
        assert p["service"] == tmp_path / "services" / "gollum"
        assert p["compose"] == tmp_path / "compose" / "gollum.yaml"

    def test_service_paths_reject_bad_name(self, tmp_path):
        mgr = self._manager(tmp_path)
        with pytest.raises(ServiceValidationError):
            mgr._service_paths("../../evil")

    def test_generated_compose_resolves_volumes_under_data(self, tmp_path):
        mgr = self._manager(tmp_path)
        mgr._ensure_directories()
        svc = ServiceDefinition.from_dict(base_service(name="gollum", volumes=["wiki:/wiki:rw"]))
        compose_path = mgr._generate_compose_file(svc)

        import yaml

        compose = yaml.safe_load(compose_path.read_text())
        vols = compose["services"]["gollum"]["volumes"]
        expected = str((tmp_path / "data" / "gollum" / "wiki").resolve())
        assert vols == [f"{expected}:/wiki:rw"]
        # The bind-mount SOURCE must be pre-created: DSM's Docker refuses to
        # auto-create it, so `up` fails ("Bind mount failed: ... does not exist")
        # if the dir is missing. Regression guard for the bug that took a
        # volume-declaring service offline on a reconcile replace.
        import stat as _stat

        wiki_dir = tmp_path / "data" / "gollum" / "wiki"
        assert wiki_dir.is_dir()
        # ...and it must be writable by the container's (non-root) UID, or a
        # root-owned dir shadows the image volume -> the app can't write ->
        # crash-loop. rw volumes are made 0777 (see _ensure_volume_dir).
        assert _stat.S_IMODE(wiki_dir.stat().st_mode) == 0o777
        # no-new-privileges is always set for Layer 2 services
        assert compose["services"]["gollum"]["security_opt"] == ["no-new-privileges:true"]
        # no deprecated top-level version key
        assert "version" not in compose

    def test_readonly_volume_dir_is_not_world_writable(self, tmp_path):
        mgr = self._manager(tmp_path)
        mgr._ensure_directories()
        svc = ServiceDefinition.from_dict(base_service(name="ro", volumes=["conf:/etc/app:ro"]))
        mgr._generate_compose_file(svc)
        import stat as _stat

        conf_dir = tmp_path / "data" / "ro" / "conf"
        assert conf_dir.is_dir()
        # a read-only mount needs no write bit granted
        assert _stat.S_IMODE(conf_dir.stat().st_mode) != 0o777

    def test_project_name_isolated_per_service(self, tmp_path):
        mgr = self._manager(tmp_path)
        assert mgr._project_name("gollum") == "syrvis-gollum"

    def test_generated_compose_emits_command(self, tmp_path):
        mgr = self._manager(tmp_path)
        mgr._ensure_directories()
        argv = [
            "--promscrape.config=/etc/vmagent/scrape.yml",
            "--remoteWrite.url=http://victoria-metrics:8428/api/v1/write",
        ]
        svc = ServiceDefinition.from_dict(base_service(name="vmagent", command=argv))
        compose_path = mgr._generate_compose_file(svc)

        import yaml

        compose = yaml.safe_load(compose_path.read_text())
        # emitted verbatim as an exec-form list (never coerced to a shell string)
        assert compose["services"]["vmagent"]["command"] == argv
        # still fully confined
        assert compose["services"]["vmagent"]["security_opt"] == ["no-new-privileges:true"]

    def test_generated_compose_omits_absent_command(self, tmp_path):
        mgr = self._manager(tmp_path)
        mgr._ensure_directories()
        svc = ServiceDefinition.from_dict(base_service(name="nocmd"))
        compose_path = mgr._generate_compose_file(svc)

        import yaml

        compose = yaml.safe_load(compose_path.read_text())
        assert "command" not in compose["services"]["nocmd"]

    def test_command_cannot_inject_sibling_compose_keys(self, tmp_path):
        # The strongest guarantee: a command element carrying YAML-structural
        # payload (newlines, a leading '- ', colons) must land as a single quoted
        # scalar list element, NEVER as a sibling key in the service's compose
        # mapping. PyYAML quoting enforces this — assert it directly so a future
        # emit change (e.g. a hand-rolled writer) can't silently smuggle a
        # privileged: true / cap_add sibling in through the argv.
        mgr = self._manager(tmp_path)
        mgr._ensure_directories()
        payload = [
            "--config=/etc/x.yml",
            "--flag=1\nprivileged: true",  # newline → would-be sibling key
            "- cap_add:\n  - SYS_ADMIN",  # leading '- ' → would-be list item/key
            "value: with: colons",  # colons must not create a mapping
        ]
        svc = ServiceDefinition.from_dict(base_service(name="vmagent", command=payload))
        compose_path = mgr._generate_compose_file(svc)

        import yaml

        svc_dict = yaml.safe_load(compose_path.read_text())["services"]["vmagent"]
        # command survives byte-for-byte as an exec-form list
        assert svc_dict["command"] == payload
        # nothing leaked out as a sibling compose key
        forbidden = {
            "privileged",
            "cap_add",
            "devices",
            "network_mode",
            "entrypoint",
            "user",
            "pid",
            "ipc",
            "cgroup_parent",
        }
        assert set(svc_dict) & forbidden == set()
        # only the keys we intentionally emit are present
        assert set(svc_dict) <= {
            "image",
            "container_name",
            "restart",
            "networks",
            "security_opt",
            "command",
        }

    def test_command_control_chars_accepted_and_inert(self, tmp_path):
        # Control chars inside an arg are legitimate literals — a shell never sees
        # them (exec form). Validation accepts them; tab/newline round-trip through
        # the compose emit intact and never split the argument.
        argv = ["--x=a\tb", "--y=c\nd", "--z=e\rf", "--n=g\x00h"]
        assert ServiceDefinition.from_dict(base_service(command=argv)).command == argv
        mgr = self._manager(tmp_path)
        mgr._ensure_directories()
        # NUL omitted here: the concern in THIS assertion is the PyYAML emit
        # round-trip, and NUL's YAML representation is not the property under test.
        emit_argv = ["--x=a\tb", "--y=c\nd"]
        svc = ServiceDefinition.from_dict(base_service(name="cc", command=emit_argv))

        import yaml

        compose = yaml.safe_load(mgr._generate_compose_file(svc).read_text())
        assert compose["services"]["cc"]["command"] == emit_argv


class TestElevationPreservesHome:
    def test_self_elevate_forwards_syrvis_home(self, monkeypatch):
        import syrviscore.privilege as privilege

        monkeypatch.setenv("SYRVIS_HOME", "/volume1/syrviscore")
        monkeypatch.setattr(privilege.shutil, "which", lambda _: "/usr/bin/sudo")

        captured = {}

        def fake_execv(path, args):
            captured["path"] = path
            captured["args"] = args

        monkeypatch.setattr(privilege.os, "execv", fake_execv)
        # click.echo is harmless; run it
        privilege.self_elevate("need root")

        assert captured["path"] == "/usr/bin/sudo"
        assert "SYRVIS_HOME=/volume1/syrviscore" in captured["args"]
        # SYRVIS_HOME must appear before the interpreter so sudo treats it as env
        home_idx = captured["args"].index("SYRVIS_HOME=/volume1/syrviscore")
        assert home_idx == 1


class TestSchemaV2Fields:
    """healthcheck / env_file / resources — audited, strictly sub-validated."""

    def test_healthcheck_valid(self):
        svc = ServiceDefinition.from_dict(
            base_service(
                healthcheck={
                    "test": ["CMD", "curl", "-f", "http://localhost:8080/healthz"],
                    "interval": "30s",
                    "timeout": "5s",
                    "retries": 3,
                }
            )
        )
        assert svc.healthcheck["retries"] == 3

    @pytest.mark.parametrize(
        "hc",
        [
            {"test": "curl localhost"},  # not a list
            {"test": ["SHELL", "x"]},  # bad first token
            {"test": ["CMD", "x"], "interval": "30 seconds"},  # bad duration
            {"test": ["CMD", "x"], "retries": 0},  # out of range
            {"test": ["CMD", "x"], "disable": True},  # unknown key
        ],
    )
    def test_healthcheck_invalid_rejected(self, hc):
        with pytest.raises(ServiceValidationError):
            ServiceDefinition.from_dict(base_service(healthcheck=hc))

    def test_env_file_relative_only(self):
        svc = ServiceDefinition.from_dict(base_service(env_file="secrets.env"))
        assert svc.env_file == "secrets.env"
        for bad in ("/etc/passwd", "../outside.env"):
            with pytest.raises(ServiceValidationError):
                ServiceDefinition.from_dict(base_service(env_file=bad))

    def test_resources_valid_and_invalid(self):
        svc = ServiceDefinition.from_dict(base_service(resources={"cpus": "1.5", "memory": "512m"}))
        assert svc.resources == {"cpus": "1.5", "memory": "512m"}
        for bad in ({"cpus": "lots"}, {"memory": "512q"}, {"gpu": 1}, {}):
            with pytest.raises(ServiceValidationError):
                ServiceDefinition.from_dict(base_service(resources=bad))

    def test_command_valid_argv_accepted(self):
        argv = [
            "--promscrape.config=/etc/vmagent/scrape.yml",
            "--remoteWrite.url=http://victoria-metrics:8428/api/v1/write",
        ]
        svc = ServiceDefinition.from_dict(base_service(command=argv))
        assert svc.command == argv
        # absent command defaults to an empty list (use image's default CMD)
        assert ServiceDefinition.from_dict(base_service()).command == []

    def test_command_shell_metachars_are_inert_not_rejected(self):
        # The exec form (a LIST, never a shell string) is WHY these are safe: a
        # metacharacter inside an argv element is passed literally to the
        # entrypoint and never seen by a shell, so ';', '|', '&' cannot chain a
        # second command. We therefore accept them verbatim (a PromQL/relabel
        # match like {job=~"a|b"} is a legitimate flag value) rather than
        # performing security-theater rejection. Only '$' (real compose-time
        # ${VAR} interpolation) is banned — see test_command_invalid_rejected.
        argv = ["--promscrape.config=/etc/vmagent/scrape.yml;echo pwned", '--match={job=~"a|b"}']
        svc = ServiceDefinition.from_dict(base_service(command=argv))
        assert svc.command == argv  # preserved exactly, no splitting, no execution

    @pytest.mark.parametrize(
        "cmd",
        [
            "--flag=value",  # bare-string shell form is refused (exec form only)
            [],  # empty list is meaningless — reject like resources={}
            ["--url=${SECRET}"],  # '$' interpolation is not permitted
            ["--url=$SECRET"],  # bare '$' too
            ["ok", ""],  # empty entry
            ["ok", 42],  # non-string entry
            ["ok", None],  # non-string entry
        ],
    )
    def test_command_invalid_rejected(self, cmd):
        with pytest.raises(ServiceValidationError):
            ServiceDefinition.from_dict(base_service(command=cmd))

    def test_v2_fields_round_trip_to_dict(self):
        data = base_service(
            healthcheck={"test": ["CMD", "true"]},
            command=["--foo=bar", "--baz"],
            env_file="secrets.env",
            resources={"memory": "256m"},
        )
        svc = ServiceDefinition.from_dict(data)
        out = svc.to_dict()
        assert out["healthcheck"] == {"test": ["CMD", "true"]}
        assert out["command"] == ["--foo=bar", "--baz"]
        assert out["env_file"] == "secrets.env"
        assert out["resources"] == {"memory": "256m"}
        # and the round-tripped dict re-validates
        ServiceDefinition.from_dict(out)
        # an empty command is NOT serialized (keeps installed manifests clean)
        assert "command" not in ServiceDefinition.from_dict(base_service()).to_dict()


class TestCommandReconcileAndConverge:
    """command must survive real persistence, drive drift, and stay off the shorthand."""

    def test_command_change_triggers_reconcile(self):
        # The reconcile diff compares services_d._content_dict(current, declared).
        # If command were dropped from that projection, a command appearing or
        # changing would silently NOT redeploy. Pin both directions.
        from syrviscore import services_d

        no_cmd = ServiceDefinition.from_dict(base_service(name="vmagent"))
        cmd_a = ServiceDefinition.from_dict(base_service(name="vmagent", command=["--a=1"]))
        cmd_b = ServiceDefinition.from_dict(base_service(name="vmagent", command=["--a=2"]))
        cmd_a2 = ServiceDefinition.from_dict(base_service(name="vmagent", command=["--a=1"]))

        assert services_d._content_dict(no_cmd) != services_d._content_dict(cmd_a)  # appear
        assert services_d._content_dict(cmd_a) != services_d._content_dict(cmd_b)  # change
        assert services_d._content_dict(cmd_a) == services_d._content_dict(cmd_a2)  # no churn

    def test_command_survives_installed_manifest_writer(self, tmp_path):
        # dump_definition(..., include_orchestration=False) is the actual
        # installed-manifest path (_write_manifest). Prove command round-trips
        # through the real writer + loader, not just to_dict/from_dict.
        from syrviscore.service_schema import dump_definition

        argv = [
            "--promscrape.config=/etc/vmagent/scrape.yml",
            "--remoteWrite.url=http://victoria-metrics:8428/api/v1/write",
        ]
        svc = ServiceDefinition.from_dict(base_service(name="vmagent", command=argv))
        manifest = tmp_path / "syrvis-service.yaml"
        dump_definition(svc, manifest, include_orchestration=False)
        assert ServiceDefinition.from_yaml(manifest).command == argv

    def test_converge_shorthand_rejects_command(self):
        # The image-first desired-doc shorthand (ALLOWED_SERVICE_KEYS) intentionally
        # omits command (as it does volumes/healthcheck/resources). A command there
        # must fail LOUDLY, never silently drop.
        from syrviscore import converge

        with pytest.raises(converge.ConvergeError, match="unknown key"):
            converge.validate_desired(
                {
                    "version": 1,
                    "services": {
                        "vmagent": {
                            "image": "victoriametrics/vmagent:v1.147.0",
                            "command": ["--a=1"],
                        }
                    },
                }
            )


class TestInfraTier:
    """design/22 — the privileged infra tier: an enumerated READ-ONLY host-mount
    allowlist, gated by AUTHORSHIP (only an operator services.d/deploy declaration
    may set tier: infra — never a git/image/catalog service)."""

    def test_non_infra_rejects_any_host_mount(self):
        for vol in (
            "/proc:/host/proc:ro",
            "/var/run/docker.sock:/var/run/docker.sock:ro",
            "/:/rootfs:ro",
        ):
            with pytest.raises(ServiceValidationError):
                ServiceDefinition.from_dict(base_service(volumes=[vol]))

    def test_infra_accepts_allowlisted_ro_host_mounts(self):
        svc = ServiceDefinition.from_dict(
            base_service(
                tier="infra",
                volumes=[
                    "/proc:/host/proc:ro",
                    "/sys:/host/sys:ro",
                    "/:/rootfs:ro",
                    "/var/run/docker.sock:/var/run/docker.sock:ro",
                    "data:/data:rw",
                ],
            )
        )
        assert svc.tier == "infra"
        assert len(svc.volumes) == 5  # host mounts + a normal named volume

    def test_infra_host_mount_must_be_readonly(self):
        for vol in (
            "/proc:/host/proc:rw",
            "/:/rootfs",
            "/var/run/docker.sock:/var/run/docker.sock:rw",
        ):
            with pytest.raises(ServiceValidationError, match="read-only"):
                ServiceDefinition.from_dict(base_service(tier="infra", volumes=[vol]))

    def test_infra_non_allowlisted_host_path_still_refused(self):
        # only /proc,/sys,/,docker.sock — NOT /etc, a volume, a look-alike sock, or '..'
        for vol in (
            "/etc:/host/etc:ro",
            "/volume4:/data:ro",
            "/var/run/x.sock:/s:ro",
            "/proc/../etc:/x:ro",
        ):
            with pytest.raises(ServiceValidationError):
                ServiceDefinition.from_dict(base_service(tier="infra", volumes=[vol]))

    def test_bad_tier_rejected(self):
        with pytest.raises(ServiceValidationError, match="tier"):
            ServiceDefinition.from_dict(base_service(tier="root"))

    def test_tier_round_trips_and_default_omitted(self):
        out = ServiceDefinition.from_dict(base_service(tier="infra")).to_dict()
        assert out["tier"] == "infra" and ServiceDefinition.from_dict(out).tier == "infra"
        assert "tier" not in ServiceDefinition.from_dict(base_service()).to_dict()

    def _mgr(self, tmp_path):
        import os
        from syrviscore.service_manager import ServiceManager

        os.environ.setdefault("DOMAIN", "example.com")
        m = ServiceManager(syrvis_home=tmp_path)
        m._ensure_directories()
        m._start_service = lambda n, cp: (True, "started")
        m._reload_traefik = lambda: None
        return m

    def test_authorship_gate_allows_operator_and_emits_host_mounts(self, tmp_path):
        import yaml

        mgr = self._mgr(tmp_path)
        svc = ServiceDefinition.from_dict(
            base_service(
                name="node-exporter", tier="infra", volumes=["/proc:/host/proc:ro", "/:/rootfs:ro"]
            )
        )
        # install_declaration sets source_url="services.d:node-exporter" -> operator -> allowed
        ok, msg = mgr.install_declaration(svc, start=False)
        assert ok, msg
        compose = yaml.safe_load((tmp_path / "compose" / "node-exporter.yaml").read_text())
        vols = compose["services"]["node-exporter"]["volumes"]
        # emitted as ABSOLUTE host paths (not resolved under data/<svc>/), read-only
        assert "/proc:/host/proc:ro" in vols and "/:/rootfs:ro" in vols

    def test_authorship_gate_rejects_git_source(self, tmp_path):
        mgr = self._mgr(tmp_path)
        svc = ServiceDefinition.from_dict(
            base_service(name="evil", tier="infra", volumes=["/proc:/host/proc:ro"])
        )
        svc.source_url = "https://github.com/attacker/evil.git"  # a repo, NOT services.d:
        sp = mgr.services_dir / "evil"
        sp.mkdir(parents=True, exist_ok=True)
        ok, msg = mgr._install_from_definition(svc, sp, start=False)
        assert not ok and "infra" in msg.lower()

    def test_deploy_bundle_update_path_handles_infra(self, tmp_path):
        # design/22 N1: deploy_bundle's UPDATE path regenerates compose WITHOUT
        # routing through _install_from_definition's gate. A bundle is
        # operator-authored (seam-delivered) so infra is PERMITTED — but the gate
        # must be EXPLICIT, not incidental. Fresh-install benign, then update to
        # infra; both succeed and the update emits the allowlisted host mounts.
        import yaml
        from syrviscore.bundle import DeployBundle

        mgr = self._mgr(tmp_path)
        benign = ServiceDefinition.from_dict(
            base_service(name="node-exporter", image="prom/node-exporter:v1.12.1")
        )
        ok, msg = mgr.deploy_bundle(DeployBundle(service=benign))  # fresh
        assert ok, msg
        infra = ServiceDefinition.from_dict(
            base_service(
                name="node-exporter",
                image="prom/node-exporter:v1.12.1",
                tier="infra",
                volumes=["/proc:/host/proc:ro", "/:/rootfs:ro"],
            )
        )
        ok, msg = mgr.deploy_bundle(DeployBundle(service=infra))  # update
        assert ok, msg
        assert infra.source_url == "deploy:node-exporter"  # marked operator-authored
        vols = yaml.safe_load((tmp_path / "compose" / "node-exporter.yaml").read_text())[
            "services"
        ]["node-exporter"]["volumes"]
        assert "/proc:/host/proc:ro" in vols and "/:/rootfs:ro" in vols

    def test_compose_emit_is_defense_in_depth(self, tmp_path):
        # Even if the schema were bypassed (volumes mutated AFTER validation), the
        # emit must never produce a non-allowlisted or writable host bind.
        import yaml

        mgr = self._mgr(tmp_path)
        svc = ServiceDefinition.from_dict(
            base_service(name="ne", tier="infra", volumes=["/proc:/host/proc:ro"])
        )
        svc.volumes.append("/etc:/host/etc:ro")  # inject non-allowlisted host path
        with pytest.raises(ServiceValidationError, match="escapes the service data directory"):
            mgr._generate_compose_file(svc)
        # a :rw allowlisted mount is forced back to :ro by the emit
        svc2 = ServiceDefinition.from_dict(
            base_service(name="ne2", tier="infra", volumes=["/proc:/host/proc:ro"])
        )
        svc2.volumes[:] = ["/:/rootfs:rw"]
        vols = yaml.safe_load(mgr._generate_compose_file(svc2).read_text())["services"]["ne2"][
            "volumes"
        ]
        assert vols == ["/:/rootfs:ro"]


# ---------------------------------------------------------------------------
# design/26 — the app/location model: a service's home on a declared volume
# ---------------------------------------------------------------------------


def _v2_mgr(tmp_path, monkeypatch, mounted=True, start_ok=True):
    """A ServiceManager with the /volumeN plumbing faked onto tmp_path.

    ``resolve_volume_root`` maps /volumeN -> tmp_path/volumes/volumeN (the
    same seam DSM-sim uses) and ``is_mounted_volume`` is stubbed — both are
    module-level in syrviscore.paths precisely so tests can do this.
    """
    import os

    from syrviscore import paths as paths_mod
    from syrviscore.service_manager import ServiceManager

    os.environ.setdefault("DOMAIN", "example.com")
    monkeypatch.setattr(
        paths_mod,
        "resolve_volume_root",
        lambda loc: tmp_path / "volumes" / str(loc).lstrip("/"),
    )
    monkeypatch.setattr(paths_mod, "is_mounted_volume", lambda loc: mounted)
    m = ServiceManager(syrvis_home=tmp_path / "home")
    m._ensure_directories()
    m._start_service = lambda n, cp: (start_ok, "started" if start_ok else "boom")
    m._reload_traefik = lambda: None
    return m


def _resolved_home(tmp_path, name, volume="volume6"):
    return tmp_path / "volumes" / volume / "syrviscore" / "apps" / name


class TestLocationSchema:
    """(i)+(iii): parse-time validation is regex-ONLY; round-trips everywhere."""

    def test_valid_location_parses_and_round_trips(self):
        svc = ServiceDefinition.from_dict(base_service(location="/volume6"))
        assert svc.location == "/volume6"
        out = svc.to_dict()
        assert out["location"] == "/volume6"
        assert ServiceDefinition.from_dict(out).location == "/volume6"

    def test_default_location_omitted_from_dict(self):
        assert "location" not in ServiceDefinition.from_dict(base_service()).to_dict()

    @pytest.mark.parametrize(
        "loc",
        [
            "/etc",
            "/volume6/../volume1",
            "/volume6/sub",
            "volume6",
            "/volume6/",
            "/Volume6",
            " /volume6",
            "/volume6 ",
            "/volume",
            "~/volume6",
            6,
            ["/volume6"],
        ],
    )
    def test_bad_location_rejected_at_parse(self, loc):
        with pytest.raises(ServiceValidationError, match="location"):
            ServiceDefinition.from_dict(base_service(location=loc))

    def test_no_mount_check_at_parse(self, monkeypatch):
        # Laptop-side declaration validation must keep working: parsing NEVER
        # touches the filesystem for `location`.
        from syrviscore import paths as paths_mod

        def boom(loc):  # pragma: no cover - must not be called
            raise AssertionError("parse must not mount-check")

        monkeypatch.setattr(paths_mod, "is_mounted_volume", boom)
        assert ServiceDefinition.from_dict(base_service(location="/volume9")).location == "/volume9"

    def test_location_survives_materialized_manifest_writer(self, tmp_path):
        # dump_definition(include_orchestration=False) is the ONE writer behind
        # installed manifests — location must survive it (lifecycle ops read it
        # back by name) while enabled/critical are stripped.
        from syrviscore.service_schema import dump_definition

        svc = ServiceDefinition.from_dict(
            base_service(location="/volume6", enabled=False, critical=True)
        )
        out = tmp_path / "syrvis-service.yaml"
        dump_definition(svc, out, include_orchestration=False)
        import yaml

        data = yaml.safe_load(out.read_text())
        assert data["location"] == "/volume6"
        assert "enabled" not in data and "critical" not in data
        assert ServiceDefinition.from_dict(data).location == "/volume6"

    def test_location_is_content_for_reconcile_diff(self):
        # A location change must classify as REPLACE (content), never be
        # ignored as orchestration.
        from syrviscore.services_d import _content_dict

        a = ServiceDefinition.from_dict(base_service())
        b = ServiceDefinition.from_dict(base_service(location="/volume6"))
        assert _content_dict(a) != _content_dict(b)


class TestLocationAuthorshipGate:
    """(ii): only an operator-authored services.d/deploy declaration may set
    a location — a git/image/catalog manifest is refused at install."""

    @pytest.mark.parametrize(
        "source_url",
        [
            "https://github.com/attacker/evil.git",  # service add (git repo)
            "ghcr.io/attacker/evil:1.0.0",  # service run (image-first)
            "catalog:evil",  # catalog template
            None,  # unknown provenance
        ],
    )
    def test_non_operator_location_rejected(self, tmp_path, monkeypatch, source_url):
        mgr = _v2_mgr(tmp_path, monkeypatch)
        svc = ServiceDefinition.from_dict(base_service(name="evil", location="/volume6"))
        svc.source_url = source_url
        sp = mgr.services_dir / "evil"
        sp.mkdir(parents=True, exist_ok=True)
        ok, msg = mgr._install_from_definition(svc, sp, start=False)
        assert not ok
        assert "location" in msg and "operator-authored" in msg

    def test_services_d_declaration_accepted(self, tmp_path, monkeypatch):
        mgr = _v2_mgr(tmp_path, monkeypatch)
        svc = ServiceDefinition.from_dict(base_service(name="pg", location="/volume6"))
        ok, msg = mgr.install_declaration(svc, start=False)
        assert ok, msg
        assert svc.source_url == "services.d:pg"

    def test_unmounted_location_refused_even_for_operator(self, tmp_path, monkeypatch):
        mgr = _v2_mgr(tmp_path, monkeypatch, mounted=False)
        svc = ServiceDefinition.from_dict(base_service(name="pg", location="/volume6"))
        ok, msg = mgr.install_declaration(svc, start=False)
        assert not ok and "not a mounted volume" in msg
        # nothing materialized at the (unmounted) location
        assert not (tmp_path / "volumes").exists()


class TestUpdateGate:
    """The design/22 gap fix: a git-pull update may not CHANGE tier or
    location (both fields, one code path), and the refused pull is reverted."""

    def _git_service(self, mgr, name, manifest):
        import yaml

        sp = mgr.services_dir / name
        (sp / ".git").mkdir(parents=True)
        (sp / "syrvis-service.yaml").write_text(yaml.safe_dump(manifest))
        return sp

    def _fake_git(
        self,
        monkeypatch,
        sp,
        pulled_manifest,
        calls,
        reset_rc=0,
        head_ok=True,
        reset_restores=True,
    ):
        import yaml

        from syrviscore import service_manager as sm_mod

        manifest_path = sp / "syrvis-service.yaml"
        old_text = manifest_path.read_text()

        def fake_run(cmd, **kwargs):
            calls.append(list(cmd))

            class R:
                returncode = 0
                stdout = "aaaa1111\n"
                stderr = ""

            if "rev-parse" in cmd and not head_ok:
                R.returncode = 1
                R.stdout = ""
                R.stderr = "fatal: not a git repository"
            if "pull" in cmd:
                # every pull re-lands the (possibly hostile) repo manifest
                manifest_path.write_text(yaml.safe_dump(pulled_manifest))
            if "reset" in cmd:
                R.returncode = reset_rc
                if reset_rc == 0 and reset_restores:
                    manifest_path.write_text(old_text)
                else:
                    R.stderr = "fatal: unable to write new index file"
            return R()

        monkeypatch.setattr(sm_mod.subprocess, "run", fake_run)

    @pytest.mark.parametrize(
        "field,value",
        [("tier", "infra"), ("location", "/volume6")],
    )
    def test_update_refuses_privileged_field_change(self, tmp_path, monkeypatch, field, value):
        import yaml

        mgr = _v2_mgr(tmp_path, monkeypatch)
        old = base_service(name="app")
        sp = self._git_service(mgr, "app", old)
        pulled = base_service(name="app", version="2.0.0")
        pulled[field] = value
        if field == "tier":
            pulled["volumes"] = ["/proc:/host/proc:ro"]
        calls = []
        self._fake_git(monkeypatch, sp, pulled, calls)

        ok, msg = mgr.update("app")
        assert not ok
        assert field in msg and "operator-authored" in msg
        assert "pull reverted" in msg  # both revert layers succeeded
        # the pull was reverted: the on-disk manifest no longer carries the
        # refused field (name-only path lookups would otherwise honor it)
        data = yaml.safe_load((sp / "syrvis-service.yaml").read_text())
        assert field not in data
        assert any("reset" in c for c in calls)

    def test_update_with_unchanged_fields_proceeds(self, tmp_path, monkeypatch):
        mgr = _v2_mgr(tmp_path, monkeypatch)
        old = base_service(name="app")
        sp = self._git_service(mgr, "app", old)
        calls = []
        self._fake_git(monkeypatch, sp, dict(old), calls)
        ok, msg = mgr.update("app")
        assert ok, msg
        assert not any("reset" in c for c in calls)

    # -- adversarial review #3/#6: revert-failure variants + two-step bypass --

    @pytest.mark.parametrize(
        "variant_kwargs",
        [
            {"reset_rc": 1, "reset_restores": False},  # A: git reset fails
            {"head_ok": False},  # B: no pre-pull HEAD -> no reset attempted
        ],
        ids=["reset-fails", "rev-parse-empty"],
    )
    @pytest.mark.parametrize("field,value", [("tier", "infra"), ("location", "/volume6")])
    def test_refusal_restores_manifest_even_when_git_revert_fails(
        self, tmp_path, monkeypatch, field, value, variant_kwargs
    ):
        import yaml

        mgr = _v2_mgr(tmp_path, monkeypatch)
        old = base_service(name="app")
        sp = self._git_service(mgr, "app", old)
        pulled = base_service(name="app", version="2.0.0")
        pulled[field] = value
        if field == "tier":
            pulled["volumes"] = ["/proc:/host/proc:ro"]
        calls = []
        self._fake_git(monkeypatch, sp, pulled, calls, **variant_kwargs)

        ok, msg = mgr.update("app")
        assert not ok and field in msg
        # honest message: the git revert did NOT run/succeed
        assert "pull reverted" not in msg
        assert "REVERT FAILED" in msg
        # the load-bearing restore: the manifest came back from the in-memory
        # last-authorized copy, so the on-disk baseline never drifted
        data = yaml.safe_load((sp / "syrvis-service.yaml").read_text())
        assert field not in data

    @pytest.mark.parametrize("field,value", [("tier", "infra"), ("location", "/volume6")])
    def test_two_step_update_cannot_launder_the_change(
        self, tmp_path, monkeypatch, field, value
    ):
        # probe_two_step.py: with the revert silently failing, update #2 used
        # to load the polluted manifest as its baseline, see "no change", and
        # pass the gate — regenerating compose + services.d with the smuggled
        # field. The manifest re-materialization kills the laundering.
        import yaml

        mgr = _v2_mgr(tmp_path, monkeypatch)
        old = base_service(name="app")
        sp = self._git_service(mgr, "app", old)
        pulled = base_service(name="app", version="2.0.0")
        pulled[field] = value
        if field == "tier":
            pulled["volumes"] = ["/proc:/host/proc:ro"]
        calls = []
        self._fake_git(monkeypatch, sp, pulled, calls, reset_rc=1, reset_restores=False)

        ok1, msg1 = mgr.update("app")
        ok2, msg2 = mgr.update("app")
        assert not ok1 and not ok2
        assert field in msg2 and "operator-authored" in msg2  # gate held BOTH times
        # nothing downstream ever carried the smuggled field
        data = yaml.safe_load((sp / "syrvis-service.yaml").read_text())
        assert field not in data
        assert not (mgr.compose_dir / "app.yaml").exists()
        decl = mgr.syrvis_home / "config" / "services.d" / "app.yaml"
        assert not decl.exists()


class TestAppHomeLayout:
    """(iv): the v2 standard layout, slot modes, and containment."""

    def _pg(self, name="pg"):
        return ServiceDefinition.from_dict(
            base_service(
                name=name,
                location="/volume6",
                volumes=["pgdata:/var/lib/postgresql/data"],
                env_file="secrets.env",
            )
        )

    def test_install_materializes_standard_layout(self, tmp_path, monkeypatch):
        import os
        import stat as stat_mod

        import yaml

        mgr = _v2_mgr(tmp_path, monkeypatch)
        ok, msg = mgr.install_declaration(self._pg(), start=False)
        assert ok, msg
        home = _resolved_home(tmp_path, "pg")
        modes = {
            "data": 0o777,
            "config": 0o755,
            "secrets": 0o700,
            "logs": 0o777,
        }
        for slot, mode in modes.items():
            path = home / slot
            assert path.is_dir(), slot
            assert stat_mod.S_IMODE(path.stat().st_mode) == mode, slot
        # compose: the bind source lives under home/data, not SYRVIS_HOME
        compose = yaml.safe_load((mgr.compose_dir / "pg.yaml").read_text())
        svc = compose["services"]["pg"]
        expected_src = os.path.realpath(str(home / "data" / "pgdata"))
        assert svc["volumes"] == ["{}:/var/lib/postgresql/data:rw".format(expected_src)]
        # env_file: materialized 0600 under home/secrets
        env_path = home / "secrets" / "secrets.env"
        assert svc["env_file"] == [str(env_path.resolve())] or svc["env_file"] == [
            os.path.realpath(str(env_path))
        ]
        assert stat_mod.S_IMODE(env_path.stat().st_mode) == 0o600
        # the LEGACY data dir was never created
        assert not (mgr.data_dir / "pg").exists()
        # manifest + dual-written declaration both carry the location
        manifest = yaml.safe_load((mgr.services_dir / "pg" / "syrvis-service.yaml").read_text())
        assert manifest["location"] == "/volume6"
        decl = yaml.safe_load(
            (mgr.syrvis_home / "config" / "services.d" / "pg.yaml").read_text()
        )
        assert decl["location"] == "/volume6"

    def test_name_only_resolution_reads_manifest(self, tmp_path, monkeypatch):
        mgr = _v2_mgr(tmp_path, monkeypatch)
        assert mgr.install_declaration(self._pg(), start=False)[0]
        home = _resolved_home(tmp_path, "pg")
        p = mgr._service_paths("pg")
        assert p["home"] == home
        assert p["data"] == home / "data"
        assert p["config"] == home / "config"
        assert p["secrets"] == home / "secrets"
        assert p["logs"] == home / "logs"
        # control plane stays central
        assert p["service"] == mgr.services_dir / "pg"
        assert p["compose"] == mgr.compose_dir / "pg.yaml"

    def test_symlinked_app_home_refused(self, tmp_path, monkeypatch):
        mgr = _v2_mgr(tmp_path, monkeypatch)
        apps = _resolved_home(tmp_path, "pg").parent
        apps.mkdir(parents=True)
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        (apps / "pg").symlink_to(elsewhere)
        ok, msg = mgr.install_declaration(self._pg(), start=False)
        assert not ok and "escapes" in msg

    def test_symlinked_slot_refused(self, tmp_path, monkeypatch):
        mgr = _v2_mgr(tmp_path, monkeypatch)
        home = _resolved_home(tmp_path, "pg")
        home.mkdir(parents=True)
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        (home / "data").symlink_to(elsewhere)
        ok, msg = mgr.install_declaration(self._pg(), start=False)
        assert not ok and "escapes" in msg

    def test_tampered_manifest_location_fails_closed(self, tmp_path, monkeypatch):
        # A hand-tampered manifest with a non-/volumeN location must refuse the
        # operation (never silently fall back to a legacy path a purge would
        # then resolve wrong).
        import yaml

        mgr = _v2_mgr(tmp_path, monkeypatch)
        svc = ServiceDefinition.from_dict(base_service(name="web"))
        assert mgr.install_declaration(svc, start=False)[0]
        manifest = mgr.services_dir / "web" / "syrvis-service.yaml"
        data = yaml.safe_load(manifest.read_text())
        data["location"] = "/etc"
        manifest.write_text(yaml.safe_dump(data))
        ok, msg = mgr.remove("web")
        assert not ok and "invalid location" in msg
        ok, msg = mgr.stop("web")
        assert not ok and "invalid location" in msg

    def test_write_secret_targets_home_secrets(self, tmp_path, monkeypatch):
        mgr = _v2_mgr(tmp_path, monkeypatch)
        assert mgr.install_declaration(self._pg(), start=False)[0]
        ok, msg = mgr.write_secret("pg", "POSTGRES_PASSWORD=hunter2\n")
        assert ok, msg
        target = _resolved_home(tmp_path, "pg") / "secrets" / "secrets.env"
        assert target.read_text() == "POSTGRES_PASSWORD=hunter2\n"

    def test_remove_purge_resolves_the_app_home(self, tmp_path, monkeypatch):
        mgr = _v2_mgr(tmp_path, monkeypatch)
        assert mgr.install_declaration(self._pg(), start=False)[0]
        home = _resolved_home(tmp_path, "pg")
        (home / "data" / "pgdata").mkdir(parents=True, exist_ok=True)
        (home / "data" / "pgdata" / "PG_VERSION").write_text("16")
        ok, msg = mgr.remove("pg", purge=True)
        assert ok, msg
        # the WHOLE home is gone (self-contained unit), never a stale
        # SYRVIS_HOME/data path
        assert not home.exists()

    def test_remove_without_purge_keeps_home(self, tmp_path, monkeypatch):
        mgr = _v2_mgr(tmp_path, monkeypatch)
        assert mgr.install_declaration(self._pg(), start=False)[0]
        home = _resolved_home(tmp_path, "pg")
        (home / "data" / "keep").write_text("x")
        ok, _ = mgr.remove("pg", purge=False)
        assert ok
        assert (home / "data" / "keep").exists()

    def test_list_rows_carry_location(self, tmp_path, monkeypatch):
        mgr = _v2_mgr(tmp_path, monkeypatch)
        assert mgr.install_declaration(self._pg(), start=False)[0]
        legacy = ServiceDefinition.from_dict(base_service(name="web"))
        assert mgr.install_declaration(legacy, start=False)[0]
        rows = {r["name"]: r for r in mgr.list()}
        assert rows["pg"]["location"] == "/volume6"
        assert rows["web"]["location"] == ""

    def test_deployment_record_carries_location(self, tmp_path, monkeypatch):
        # (viii) — additive v1-schema field at the record choke point.
        import json

        mgr = _v2_mgr(tmp_path, monkeypatch)
        assert mgr.install_declaration(self._pg(), start=False)[0]
        record = json.loads(
            (mgr.syrvis_home / "data" / "deployments" / "pg" / "0001.json").read_text()
        )
        assert record["location"] == "/volume6"
        assert record["manifest"]["location"] == "/volume6"
        legacy = ServiceDefinition.from_dict(base_service(name="web"))
        assert mgr.install_declaration(legacy, start=False)[0]
        record = json.loads(
            (mgr.syrvis_home / "data" / "deployments" / "web" / "0001.json").read_text()
        )
        assert record["location"] == ""


class TestLegacyLayoutGolden:
    """(vii): a location-less service is byte-for-byte today's layout across
    every path site — the 19 running services must not move."""

    def test_legacy_paths_unchanged(self, tmp_path, monkeypatch):
        import os

        import yaml

        mgr = _v2_mgr(tmp_path, monkeypatch)  # v2 plumbing present but unused
        svc = ServiceDefinition.from_dict(
            base_service(
                name="web",
                volumes=["cfg:/etc/web:ro", "state:/var/lib/web"],
                env_file="secrets.env",
            )
        )
        ok, msg = mgr.install_declaration(svc, start=False)
        assert ok, msg
        home_root = mgr.syrvis_home

        # _service_paths: exactly today's three keys, today's values
        p = mgr._service_paths("web")
        assert set(p) == {"service", "data", "compose"}
        assert p["service"] == home_root / "services" / "web"
        assert p["data"] == home_root / "data" / "web"
        assert p["compose"] == home_root / "compose" / "web.yaml"

        # compose emit: volumes + env_file under data/<name>
        data_root = os.path.realpath(str(home_root / "data" / "web"))
        compose = yaml.safe_load((mgr.compose_dir / "web.yaml").read_text())
        emitted = compose["services"]["web"]
        assert emitted["volumes"] == [
            "{}/cfg:/etc/web:ro".format(data_root),
            "{}/state:/var/lib/web:rw".format(data_root),
        ]
        assert emitted["env_file"] == ["{}/secrets.env".format(data_root)]

        # write_secret targets data/<name>/secrets.env
        ok, msg = mgr.write_secret("web", "K=v\n")
        assert ok, msg
        assert (home_root / "data" / "web" / "secrets.env").read_text() == "K=v\n"

        # _place_config targets data/<name>/<dest>
        ok, msg = mgr._place_config("web", "config/app.yml", "a: 1\n")
        assert ok, msg
        assert (home_root / "data" / "web" / "config" / "app.yml").read_text() == "a: 1\n"

        # no app-home tree was ever created
        assert not (tmp_path / "volumes").exists()


class TestV2SlotMapping:
    """design/26 (owner decision): the v2 standard layout is SEMANTIC — a
    relative volume source named exactly `config` mounts home/config, exactly
    `logs` mounts home/logs, `secrets` is refused (env_file is the secrets
    mechanism), and everything else stays under home/data. Legacy services are
    completely unchanged."""

    def _svc(self, volumes, name="app", location="/volume6"):
        data = base_service(name=name, volumes=volumes)
        if location:
            data["location"] = location
        return ServiceDefinition.from_dict(data)

    def test_config_volume_mounts_the_placed_config_tree(self, tmp_path, monkeypatch):
        import os

        import yaml

        mgr = _v2_mgr(tmp_path, monkeypatch)
        svc = self._svc(["config:/etc/app:ro"])
        ok, msg = mgr.install_declaration(svc, start=False)
        assert ok, msg
        home = _resolved_home(tmp_path, "app")
        compose = yaml.safe_load((mgr.compose_dir / "app.yaml").read_text())
        src, dest, mode = compose["services"]["app"]["volumes"][0].rsplit(":", 2)
        assert src == os.path.realpath(str(home / "config"))
        assert (dest, mode) == ("/etc/app", "ro")
        # what _place_config writes is exactly what the mount serves
        ok, msg = mgr._place_config("app", "app.yml", "key: 1\n")
        assert ok, msg
        from pathlib import Path

        assert (Path(src) / "app.yml").read_text() == "key: 1\n"
        # the slot keeps its 0755 convention (never the 0777 data treatment)
        import stat as stat_mod

        assert stat_mod.S_IMODE((home / "config").stat().st_mode) == 0o755

    def test_logs_volume_mounts_home_logs(self, tmp_path, monkeypatch):
        import os
        import stat as stat_mod

        import yaml

        mgr = _v2_mgr(tmp_path, monkeypatch)
        svc = self._svc(["logs:/var/log/app"])
        assert mgr.install_declaration(svc, start=False)[0]
        home = _resolved_home(tmp_path, "app")
        compose = yaml.safe_load((mgr.compose_dir / "app.yaml").read_text())
        assert compose["services"]["app"]["volumes"] == [
            "{}:/var/log/app:rw".format(os.path.realpath(str(home / "logs")))
        ]
        assert stat_mod.S_IMODE((home / "logs").stat().st_mode) == 0o777

    def test_other_sources_stay_under_home_data(self, tmp_path, monkeypatch):
        import os

        import yaml

        mgr = _v2_mgr(tmp_path, monkeypatch)
        # incl. near-miss names: only the EXACT slot names map
        svc = self._svc(["pgdata:/var/lib/pg", "config/extra:/etc/extra:ro"])
        assert mgr.install_declaration(svc, start=False)[0]
        home = _resolved_home(tmp_path, "app")
        data_root = os.path.realpath(str(home / "data"))
        compose = yaml.safe_load((mgr.compose_dir / "app.yaml").read_text())
        assert compose["services"]["app"]["volumes"] == [
            "{}/pgdata:/var/lib/pg:rw".format(data_root),
            "{}/config/extra:/etc/extra:ro".format(data_root),
        ]

    def test_secrets_volume_refused_for_v2(self, tmp_path, monkeypatch):
        mgr = _v2_mgr(tmp_path, monkeypatch)
        svc = self._svc(["secrets:/run/secrets:ro"])
        ok, msg = mgr.install_declaration(svc, start=False)
        assert not ok and "secrets" in msg and "env_file" in msg
        # direct emit raises the typed error
        svc2 = self._svc(["secrets:/run/secrets:ro"], name="app2")
        (mgr.services_dir / "app2").mkdir(parents=True, exist_ok=True)
        mgr._write_manifest(svc2, mgr.services_dir / "app2")
        with pytest.raises(ServiceValidationError, match="secrets"):
            mgr._generate_compose_file(svc2)

    def test_slot_names_are_plain_data_dirs_for_legacy(self, tmp_path, monkeypatch):
        # (iii)+(iv): a location-less service treats config/logs/secrets as
        # ordinary relative sources under data/<svc> — byte-identical to today.
        import os

        import yaml

        mgr = _v2_mgr(tmp_path, monkeypatch)
        svc = self._svc(
            ["config:/etc/app:ro", "logs:/var/log/app", "secrets:/run/secrets:ro"],
            name="web",
            location="",
        )
        ok, msg = mgr.install_declaration(svc, start=False)
        assert ok, msg
        data_root = os.path.realpath(str(mgr.syrvis_home / "data" / "web"))
        compose = yaml.safe_load((mgr.compose_dir / "web.yaml").read_text())
        assert compose["services"]["web"]["volumes"] == [
            "{}/config:/etc/app:ro".format(data_root),
            "{}/logs:/var/log/app:rw".format(data_root),
            "{}/secrets:/run/secrets:ro".format(data_root),
        ]
        assert not (tmp_path / "volumes").exists()


# ---------------------------------------------------------------------------
# design/26 adversarial-review regression tests (probe scripts s3_*/s4_*)
# ---------------------------------------------------------------------------


class TestGateRefusalDataSafety:
    """Review release-blocker #1: an authorship-gate refusal must NEVER
    destroy pre-existing data — neither the legacy data/<name> a
    remove-without-purge kept, nor a pre-populated v2 home."""

    def _refused_install(self, mgr, svc):
        svc.source_url = "https://github.com/evil/repo.git"
        service_path = mgr.services_dir / svc.name
        service_path.mkdir(parents=True, exist_ok=True)
        mgr._write_manifest(svc, service_path)
        return mgr._install_from_definition(svc, service_path, start=True)

    def test_tier_gate_refusal_keeps_legacy_data(self, tmp_path, monkeypatch):
        mgr = _v2_mgr(tmp_path, monkeypatch)
        kept = mgr.data_dir / "foo"
        kept.mkdir(parents=True)
        (kept / "db.sqlite").write_text("precious kept data")
        svc = ServiceDefinition.from_dict(base_service(name="foo", tier="infra"))
        ok, msg = self._refused_install(mgr, svc)
        assert not ok and "infra" in msg
        assert (kept / "db.sqlite").exists()  # the release-blocking bug
        assert not (mgr.services_dir / "foo").exists()  # refusal still cleans up

    def test_tier_gate_refusal_keeps_v2_home(self, tmp_path, monkeypatch):
        mgr = _v2_mgr(tmp_path, monkeypatch)
        home = _resolved_home(tmp_path, "foo")
        (home / "data" / "pgdata").mkdir(parents=True)
        (home / "data" / "pgdata" / "PG_VERSION").write_text("16")
        svc = ServiceDefinition.from_dict(
            base_service(name="foo", tier="infra", location="/volume6")
        )
        ok, msg = self._refused_install(mgr, svc)
        assert not ok and "infra" in msg
        assert (home / "data" / "pgdata" / "PG_VERSION").exists()

    def test_location_gate_refusal_keeps_v2_home(self, tmp_path, monkeypatch):
        mgr = _v2_mgr(tmp_path, monkeypatch)
        home = _resolved_home(tmp_path, "bar")
        (home / "data").mkdir(parents=True)
        (home / "data" / "keep").write_text("x")
        svc = ServiceDefinition.from_dict(base_service(name="bar", location="/volume6"))
        ok, msg = self._refused_install(mgr, svc)
        assert not ok and "location" in msg
        assert (home / "data" / "keep").exists()
        assert not (mgr.services_dir / "bar").exists()


class TestUnmountedLocationStart:
    """Review release-blocker #2: the mount check is not install-time-only.
    A start (boot resume / reconcile) while the declared volume is unmounted
    must refuse and create NOTHING — never re-materialize an empty home on
    the bare mountpoint for a DB to initdb into."""

    def test_start_refuses_and_creates_nothing(self, tmp_path, monkeypatch):
        import shutil

        from syrviscore import paths as paths_mod

        mgr = _v2_mgr(tmp_path, monkeypatch)
        svc = ServiceDefinition.from_dict(
            base_service(
                name="pg",
                location="/volume6",
                volumes=["pgdata:/var/lib/postgresql/data"],
            )
        )
        assert mgr.install_declaration(svc, start=False)[0]
        # the volume disappears (failed NVMe mount): the tree under it is gone
        shutil.rmtree(tmp_path / "volumes")
        monkeypatch.setattr(paths_mod, "is_mounted_volume", lambda loc: False)
        compose_calls = []
        mgr._compose = lambda name, cp, *args, timeout: (
            compose_calls.append(args[0]) or (True, "")
        )

        ok, msg = mgr.start("pg", fire_hooks=False)
        assert not ok
        assert "not a mounted volume" in msg
        assert not (tmp_path / "volumes").exists()  # nothing re-materialized
        assert compose_calls == []  # up was never attempted

    def test_install_paths_still_gate(self, tmp_path, monkeypatch):
        # install-time behavior unchanged: gate message, nothing created
        mgr = _v2_mgr(tmp_path, monkeypatch, mounted=False)
        svc = ServiceDefinition.from_dict(base_service(name="pg", location="/volume6"))
        ok, msg = mgr.install_declaration(svc, start=False)
        assert not ok and "not a mounted volume" in msg


class TestCorruptManifestFailsClosed:
    """Review #4: a corrupt central manifest must fail CLOSED — a v2 app must
    never silently resolve to the legacy path (a purge would rmtree the wrong
    tree while leaking the real home). Only a genuinely ABSENT manifest means
    legacy."""

    def test_remove_purge_refuses_on_corrupt_manifest(self, tmp_path, monkeypatch):
        mgr = _v2_mgr(tmp_path, monkeypatch)
        svc = ServiceDefinition.from_dict(base_service(name="pg", location="/volume6"))
        assert mgr.install_declaration(svc, start=False)[0]
        home = _resolved_home(tmp_path, "pg")
        (home / "data" / "pgdata").mkdir(parents=True, exist_ok=True)
        (home / "data" / "pgdata" / "PG_VERSION").write_text("16")
        # a legacy-path orphan that a fail-open resolution would purge instead
        orphan = mgr.data_dir / "pg"
        orphan.mkdir(parents=True)
        (orphan / "dump.sql").write_text("legacy-path bytes")

        (mgr.services_dir / "pg" / "syrvis-service.yaml").write_text("{[:::not yaml")

        ok, msg = mgr.remove("pg", purge=True)
        assert not ok and "unreadable" in msg
        assert (home / "data" / "pgdata" / "PG_VERSION").exists()  # home leaked, not lost
        assert (orphan / "dump.sql").exists()  # wrong tree NOT purged

        ok, msg = mgr.stop("pg")
        assert not ok and "unreadable" in msg

    def test_absent_manifest_still_means_legacy(self, tmp_path, monkeypatch):
        # a service dir with no manifest at all keeps today's behavior
        mgr = _v2_mgr(tmp_path, monkeypatch)
        assert mgr._manifest_location("ghost") == ""


class TestAdoptionWholeHome:
    """Review #6: the adoption predicate is the WHOLE home — a home
    pre-staged with only config/ (no data yet) must survive a failed fresh
    install's rollback."""

    def test_config_only_home_survives_failed_fresh_install(self, tmp_path, monkeypatch):
        mgr = _v2_mgr(tmp_path, monkeypatch, start_ok=False)
        home = _resolved_home(tmp_path, "pg")
        (home / "config").mkdir(parents=True)
        (home / "config" / "precious.conf").write_text("staged-by-operator\n")
        svc = ServiceDefinition.from_dict(base_service(name="pg", location="/volume6"))
        ok, msg = mgr.install_declaration(svc, start=True)
        assert not ok
        assert (home / "config" / "precious.conf").exists()

    def test_untouched_home_still_rolled_back(self, tmp_path, monkeypatch):
        # counter-edge: a home this call created from nothing is still dropped
        mgr = _v2_mgr(tmp_path, monkeypatch, start_ok=False)
        svc = ServiceDefinition.from_dict(base_service(name="pg", location="/volume6"))
        ok, _ = mgr.install_declaration(svc, start=True)
        assert not ok
        assert not _resolved_home(tmp_path, "pg").exists()


class TestUmaskIndependentPerms:
    """Review #8: layout perms must not depend on the caller's umask — under
    a hardened root shell (umask 077) a non-root container UID must still be
    able to traverse to its data dir."""

    def test_home_and_ancestors_traversable_under_umask_077(self, tmp_path, monkeypatch):
        import os
        import stat as stat_mod

        old_umask = os.umask(0o077)
        try:
            mgr = _v2_mgr(tmp_path, monkeypatch)
            svc = ServiceDefinition.from_dict(
                base_service(
                    name="pg",
                    location="/volume6",
                    volumes=["pgdata:/var/lib/postgresql/data"],
                    env_file="secrets.env",
                )
            )
            ok, msg = mgr.install_declaration(svc, start=False)
            assert ok, msg
        finally:
            os.umask(old_umask)
        home = _resolved_home(tmp_path, "pg")

        def mode(p):
            return stat_mod.S_IMODE(p.stat().st_mode)

        # created ancestors: <vol>/syrviscore, <vol>/syrviscore/apps, home
        assert mode(home.parent.parent) == 0o755
        assert mode(home.parent) == 0o755
        assert mode(home) == 0o755
        assert mode(home / "data") == 0o777
        assert mode(home / "config") == 0o755
        assert mode(home / "secrets") == 0o700
        assert mode(home / "logs") == 0o777
