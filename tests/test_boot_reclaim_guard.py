"""The rootfs boot hook after incident 2026-08-16 (cold-boot share collision).

DSM renamed ``/volume4/syrviscore`` to ``syrviscore_1`` at a cold boot because a
shared folder of the same name existed on another volume. Every control-plane
path is absolute into that tree, so ONE rename took out the platform wrapper, the
operator seam, and both self-healers at once — and the rc.d hook that DID survive
was a three-line trampoline into the renamed tree with no ``else``, no log and no
alarm. The failure alarm was stored inside the thing it was supposed to alarm
about.

These tests pin the three properties that fix requires:

  * the seam shell heal is INLINED (it needs nothing but ``/etc/passwd``),
  * a collision-renamed platform root is reclaimed — and a NON-EMPTY impostor
    is refused rather than clobbered,
  * a missing startup script logs and pages instead of silently doing nothing.

The reclaim guard is exercised by actually RUNNING the rendered shell against a
fake volume tree; assertions alone would not prove a shell script works.
"""

import os
import subprocess
from pathlib import Path

import pytest

from syrviscore import privileged_ops


def _render(install_dir):
    return privileged_ops.render_boot_script(Path(install_dir))


class TestRenderedContract:
    def test_seam_heal_is_inlined_not_trampolined(self, tmp_path):
        content = _render(tmp_path / "install")
        # the heal itself, verbatim from the startup script, and BEFORE the
        # trampoline: it must run even when the tree is unreachable.
        assert "syrvis-operator syrvis-reader" in content
        assert "/sbin/nologin" in content and "/bin/sh" in content
        start_case = content.split("start)", 1)[1].split("stop)", 1)[0]
        trampoline = 'if [ -x "{}"'.format(tmp_path / "install" / "bin" / "syrvis-startup.sh")
        assert start_case.index("SEAM_USER") < start_case.index(trampoline)

    def test_reclaim_guard_precedes_the_trampoline(self, tmp_path):
        content = _render(tmp_path / "install")
        start_case = content.split("start)", 1)[1].split("stop)", 1)[0]
        assert "syrviscore_[0-9]*" in start_case
        trampoline = 'if [ -x "{}"'.format(tmp_path / "install" / "bin" / "syrvis-startup.sh")
        assert start_case.index("syrviscore_[0-9]*") < start_case.index(trampoline)

    def test_missing_startup_script_has_an_else_branch_that_alarms(self, tmp_path):
        content = _render(tmp_path / "install")
        start_case = content.split("start)", 1)[1].split("stop)", 1)[0]
        assert "else" in start_case
        assert "boot_alarm" in start_case

    def test_alarm_reads_the_rootfs_cache_not_the_tree(self, tmp_path):
        install = tmp_path / "install"
        content = _render(install)
        assert str(privileged_ops.BOOT_ENV_PATH) in content
        # the NTFY_URL must never be sourced from inside the install tree
        assert '. "{}/config/.env"'.format(install) not in content

    def test_carries_the_contract_marker_the_manager_reads(self, tmp_path):
        content = _render(tmp_path / "install")
        assert "# boot-hook-contract: {}".format(privileged_ops.BOOT_HOOK_CONTRACT) in content

    def test_reclaim_guard_is_gated_on_synocheckshare(self, tmp_path):
        # opc:F6 — the agent's heal waits for DSM's share-reconcile pass to
        # finish; the rootfs belt (the one that runs when the agent is dead)
        # must not be the one permitted to race the renamer.
        start_case = _render(tmp_path / "install").split("start)", 1)[1].split("stop)", 1)[0]
        assert "synocheckshare.service" in start_case
        assert start_case.index("synocheckshare.service") < start_case.index("syrviscore_[0-9]*")

    def test_the_gate_is_bounded_and_never_blocks_the_boot(self, tmp_path):
        start_case = _render(tmp_path / "install").split("start)", 1)[1].split("stop)", 1)[0]
        # a bound, a log-and-proceed on expiry, and a no-systemctl escape
        assert "SYNOCHECKSHARE_WAIT={}".format(privileged_ops.SYNOCHECKSHARE_WAIT_S) in start_case
        assert "proceeding with the reclaim guard anyway" in start_case
        assert "systemctl unavailable" in start_case
        # ...and no `exit`/`return` anywhere in the gate: it can only fall through
        gate = start_case.split("SYNOCHECKSHARE_UNIT=", 1)[1].split("RECLAIM GUARD", 1)[0]
        assert "exit " not in gate and "return" not in gate

    def test_the_gate_does_not_bump_the_contract(self, tmp_path):
        # A hook without the gate still heals, reclaims and alarms — marking
        # every deployed hook STALE for a race-narrowing tweak would page for
        # nothing. Content drift still triggers the ordinary rewrite.
        assert privileged_ops.BOOT_HOOK_CONTRACT == 3

    def test_still_carries_the_design28_stop_case(self, tmp_path):
        # the reclaim work must not have displaced the graceful-shutdown flush
        assert "shutdown --reason reboot" in _render(tmp_path / "install")

    def test_is_valid_posix_shell(self, tmp_path):
        script = tmp_path / "S99.sh"
        script.write_text(_render(tmp_path / "install"))
        assert subprocess.call(["sh", "-n", str(script)]) == 0


# ---------------------------------------------------------------------------
# Executable behaviour: run the real rendered guard against a fake volume tree
# ---------------------------------------------------------------------------


def _runnable_hook(tmp_path, install_dir):
    """The rendered hook, repointed at a fake /volume tree, minus the seam heal.

    Two surgical substitutions and nothing else, so what runs IS the shipped
    reclaim guard:

      * the ``/volume[0-9]*`` glob becomes ``<tmp>/volume[0-9]*``;
      * the ``/etc/passwd`` heal loop is dropped. It is asserted on above, and
        executing it here would (a) sed the developer's real /etc/passwd and
        (b) fail on BSD sed, whose ``-i`` takes a mandatory argument.
    """
    text = privileged_ops.render_boot_script(Path(install_dir))
    text = text.replace("/volume[0-9]*", "{}/volume[0-9]*".format(tmp_path))
    head, _, rest = text.partition("        for SEAM_USER in")
    _, _, tail = rest.partition("        done\n")
    script = tmp_path / "S99-runnable.sh"
    script.write_text(head + tail)
    script.chmod(0o755)
    return script


def _volume(tmp_path, n):
    vol = tmp_path / "volume{}".format(n)
    vol.mkdir(parents=True, exist_ok=True)
    return vol


def _install_root(path, manifest=True):
    path.mkdir(parents=True, exist_ok=True)
    if manifest:
        (path / ".syrviscore-manifest.json").write_text('{"schema_version": 3}')
    (path / "payload").write_text("real data")
    return path


def _run(script, tmp_path):
    return subprocess.run(
        ["sh", str(script), "start"],
        capture_output=True,
        text=True,
        cwd=str(tmp_path),
    )


@pytest.mark.skipif(os.name != "posix", reason="POSIX shell required")
class TestReclaimGuardExecution:
    def test_renames_a_collision_sibling_back(self, tmp_path):
        vol = _volume(tmp_path, 4)
        renamed = _install_root(vol / "syrviscore_1")
        script = _runnable_hook(tmp_path, vol / "syrviscore")

        result = _run(script, tmp_path)

        assert result.returncode == 0
        assert not renamed.exists()
        assert (vol / "syrviscore" / "payload").read_text() == "real data"
        assert "reclaimed" in result.stdout

    def test_reclaims_an_app_home_root_with_no_manifest(self, tmp_path):
        # /volume6 in the incident: app homes, no install manifest. Recognised
        # by its apps/ tree — the half of the rename that produced wave 2.
        vol = _volume(tmp_path, 6)
        renamed = vol / "syrviscore_1"
        (renamed / "apps" / "immich-db" / "data").mkdir(parents=True)
        (renamed / "apps" / "immich-db" / "data" / "pg").write_text("cluster")
        script = _runnable_hook(tmp_path, vol / "syrviscore")

        result = _run(script, tmp_path)

        assert not renamed.exists()
        assert (vol / "syrviscore" / "apps" / "immich-db" / "data" / "pg").exists()
        assert "reclaimed" in result.stdout

    def test_absorbs_an_empty_scaffold_left_by_a_reconcile(self, tmp_path):
        # exactly wave 2: resume scaffolded an empty <vol>/syrviscore beside the
        # renamed real one. An EMPTY target is not an obstacle.
        vol = _volume(tmp_path, 6)
        _install_root(vol / "syrviscore_1")
        (vol / "syrviscore").mkdir()
        script = _runnable_hook(tmp_path, vol / "syrviscore")

        _run(script, tmp_path)

        assert (vol / "syrviscore" / "payload").read_text() == "real data"

    def test_refuses_a_non_empty_impostor_and_touches_neither(self, tmp_path):
        vol = _volume(tmp_path, 5)
        renamed = _install_root(vol / "syrviscore_1")
        impostor = vol / "syrviscore"
        impostor.mkdir()
        (impostor / "someone-elses-file").write_text("share content")
        script = _runnable_hook(tmp_path, vol / "syrviscore")

        result = _run(script, tmp_path)

        assert renamed.exists() and (renamed / "payload").exists()
        assert (impostor / "someone-elses-file").read_text() == "share content"
        assert "REFUSING" in result.stdout

    def test_ignores_an_unrelated_directory_with_the_prefix(self, tmp_path):
        vol = _volume(tmp_path, 4)
        stray = vol / "syrviscore_1"  # no manifest, no apps/ -> not ours
        stray.mkdir(parents=True)
        (stray / "random").write_text("x")
        script = _runnable_hook(tmp_path, vol / "syrviscore")

        _run(script, tmp_path)

        assert stray.exists()
        assert not (vol / "syrviscore").exists()

    def test_missing_startup_script_logs_loudly(self, tmp_path):
        _volume(tmp_path, 4)
        script = _runnable_hook(tmp_path, tmp_path / "volume4" / "syrviscore")

        result = _run(script, tmp_path)

        assert result.returncode == 0  # a boot hook never fails the boot
        assert "will NOT auto-resume" in result.stdout

    def test_runs_the_startup_script_when_present(self, tmp_path):
        install = _volume(tmp_path, 4) / "syrviscore"
        (install / "bin").mkdir(parents=True)
        startup = install / "bin" / "syrvis-startup.sh"
        startup.write_text("#!/bin/sh\necho STARTUP-RAN\n")
        startup.chmod(0o755)
        script = _runnable_hook(tmp_path, install)

        result = _run(script, tmp_path)

        assert "STARTUP-RAN" in result.stdout


# ---------------------------------------------------------------------------
# The rootfs boot-env cache
# ---------------------------------------------------------------------------


class TestBootEnvCache:
    def test_rendered_cache_is_sourceable_and_carries_the_url(self, tmp_path):
        body = privileged_ops.render_boot_env_cache("https://ntfy.example/topic")
        env = tmp_path / "boot.env"
        env.write_text(body)
        out = subprocess.run(
            ["sh", "-c", '. "{}"; printf "%s" "$NTFY_URL"'.format(env)],
            capture_output=True,
            text=True,
        )
        assert out.stdout == "https://ntfy.example/topic"

    def test_read_env_value_handles_quotes_export_and_absence(self, tmp_path):
        env = tmp_path / ".env"
        env.write_text('OTHER=1\nexport NTFY_URL="https://n/x"\n')
        assert privileged_ops.read_env_value(env, "NTFY_URL") == "https://n/x"
        assert privileged_ops.read_env_value(env, "NOPE") == ""
        assert privileged_ops.read_env_value(tmp_path / "absent", "NTFY_URL") == ""

    def test_ensure_boot_script_writes_the_cache_beside_the_hook(self, tmp_path, monkeypatch):
        install = tmp_path / "install"
        (install / "config").mkdir(parents=True)
        (install / "config" / ".env").write_text("NTFY_URL=https://ntfy.example/homebase\n")
        monkeypatch.setattr(privileged_ops, "BOOT_ENV_PATH", tmp_path / "rootfs-boot.env")
        # keep the S99 write inside tmp too
        monkeypatch.setattr(privileged_ops, "BOOT_SCRIPT_PATH", tmp_path / "S99syrviscore.sh")

        ok, msg = privileged_ops.DsmOperations().ensure_boot_script(install)

        assert ok, msg
        cache = (tmp_path / "rootfs-boot.env").read_text()
        assert "https://ntfy.example/homebase" in cache
        assert (tmp_path / "rootfs-boot.env").stat().st_mode & 0o777 == 0o600

    def test_absent_ntfy_url_never_fails_the_boot_hook_install(self, tmp_path, monkeypatch):
        install = tmp_path / "install"
        (install / "config").mkdir(parents=True)
        monkeypatch.setattr(privileged_ops, "BOOT_ENV_PATH", tmp_path / "rootfs-boot.env")
        monkeypatch.setattr(privileged_ops, "BOOT_SCRIPT_PATH", tmp_path / "S99syrviscore.sh")

        ok, msg = privileged_ops.DsmOperations().ensure_boot_script(install)

        assert ok
        assert "no NTFY_URL" in msg
        assert (tmp_path / "S99syrviscore.sh").exists()


# ---------------------------------------------------------------------------
# SPK start-stop-status
# ---------------------------------------------------------------------------

SSS = Path(__file__).resolve().parents[1] / "spk" / "scripts" / "start-stop-status"


@pytest.mark.skipif(os.name != "posix", reason="POSIX shell required")
class TestSpkStatus:
    def test_status_exits_nonzero_when_no_install_root_is_found(self, tmp_path):
        """A failed home scan is an ERROR, not 'nothing installed yet'.

        DSM's own package status stayed GREEN through a fully decapitated
        install, because this branch exit 0'd.
        """
        pkgdest = tmp_path / "target"
        (pkgdest / "venv" / "bin").mkdir(parents=True)
        (pkgdest / "venv" / "bin" / "syrvisctl").write_text("#!/bin/sh\n")

        result = subprocess.run(
            ["sh", str(SSS), "status"],
            capture_output=True,
            text=True,
            env=dict(os.environ, SYNOPKG_PKGDEST=str(pkgdest)),
        )

        assert result.returncode == 1
        combined = result.stdout + result.stderr
        assert "no SyrvisCore install root found" in combined
        assert "syrviscore*" in combined  # points at the rename hypothesis

    def test_is_valid_posix_shell(self):
        assert subprocess.call(["sh", "-n", str(SSS)]) == 0
