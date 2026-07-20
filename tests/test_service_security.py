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
            "privileged", "cap_add", "devices", "network_mode",
            "entrypoint", "user", "pid", "ipc", "cgroup_parent",
        }
        assert set(svc_dict) & forbidden == set()
        # only the keys we intentionally emit are present
        assert set(svc_dict) <= {
            "image", "container_name", "restart", "networks", "security_opt", "command",
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
        for vol in ("/proc:/host/proc:ro", "/var/run/docker.sock:/var/run/docker.sock:ro",
                    "/:/rootfs:ro"):
            with pytest.raises(ServiceValidationError):
                ServiceDefinition.from_dict(base_service(volumes=[vol]))

    def test_infra_accepts_allowlisted_ro_host_mounts(self):
        svc = ServiceDefinition.from_dict(base_service(
            tier="infra",
            volumes=["/proc:/host/proc:ro", "/sys:/host/sys:ro", "/:/rootfs:ro",
                     "/var/run/docker.sock:/var/run/docker.sock:ro", "data:/data:rw"]))
        assert svc.tier == "infra"
        assert len(svc.volumes) == 5  # host mounts + a normal named volume

    def test_infra_host_mount_must_be_readonly(self):
        for vol in ("/proc:/host/proc:rw", "/:/rootfs", "/var/run/docker.sock:/var/run/docker.sock:rw"):
            with pytest.raises(ServiceValidationError, match="read-only"):
                ServiceDefinition.from_dict(base_service(tier="infra", volumes=[vol]))

    def test_infra_non_allowlisted_host_path_still_refused(self):
        # only /proc,/sys,/,docker.sock — NOT /etc, a volume, a look-alike sock, or '..'
        for vol in ("/etc:/host/etc:ro", "/volume4:/data:ro", "/var/run/x.sock:/s:ro",
                    "/proc/../etc:/x:ro"):
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
        svc = ServiceDefinition.from_dict(base_service(
            name="node-exporter", tier="infra",
            volumes=["/proc:/host/proc:ro", "/:/rootfs:ro"]))
        # install_declaration sets source_url="services.d:node-exporter" -> operator -> allowed
        ok, msg = mgr.install_declaration(svc, start=False)
        assert ok, msg
        compose = yaml.safe_load((tmp_path / "compose" / "node-exporter.yaml").read_text())
        vols = compose["services"]["node-exporter"]["volumes"]
        # emitted as ABSOLUTE host paths (not resolved under data/<svc>/), read-only
        assert "/proc:/host/proc:ro" in vols and "/:/rootfs:ro" in vols

    def test_authorship_gate_rejects_git_source(self, tmp_path):
        mgr = self._mgr(tmp_path)
        svc = ServiceDefinition.from_dict(base_service(
            name="evil", tier="infra", volumes=["/proc:/host/proc:ro"]))
        svc.source_url = "https://github.com/attacker/evil.git"  # a repo, NOT services.d:
        sp = mgr.services_dir / "evil"
        sp.mkdir(parents=True, exist_ok=True)
        ok, msg = mgr._install_from_definition(svc, sp, start=False)
        assert not ok and "infra" in msg.lower()
