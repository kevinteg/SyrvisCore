"""The syrvis wrapper's discovery fallback (incident 2026-08-16, P2).

The absolute install path is baked into four independent artifacts with zero
fallback — the wrapper, the sudoers policy, the shim allowlist and the rc.d hook
— three of them on the rootfs, all of which outlived the rename while pointing at
a path that no longer existed. The wrapper's failure message then actively
misdirected: "No service version installed. Run 'syrvisctl install'" is advice
that would install a SECOND tree beside the intact one, which the repair runbook
has to explicitly forbid.

The baked path stays the fast path. What is tested here is what happens when it
misses.
"""

import os
import subprocess

import pytest

from syrviscore_manager.paths import create_syrvis_wrapper


def _wrapper(home):
    home.mkdir(parents=True, exist_ok=True)
    return create_syrvis_wrapper(home)


def _fake_install(root, version="0.5.9"):
    """A tree that looks installed: manifest + current -> versions/<v>."""
    root.mkdir(parents=True, exist_ok=True)
    (root / ".syrviscore-manifest.json").write_text('{"schema_version": 3}')
    venv_bin = root / "versions" / version / "cli" / "venv" / "bin"
    venv_bin.mkdir(parents=True)
    syrvis = venv_bin / "syrvis"
    syrvis.write_text('#!/bin/sh\necho "RAN-FROM=$SYRVIS_HOME"\n')
    syrvis.chmod(0o755)
    (root / "current").symlink_to("versions/{}".format(version))
    return root


def _run(wrapper, tmp_path):
    """Run the wrapper with its volume globs repointed into ``tmp_path``."""
    text = wrapper.read_text().replace("/volume[0-9]*", "{}/volume[0-9]*".format(tmp_path))
    patched = tmp_path / "syrvis-wrapper"
    patched.write_text(text)
    patched.chmod(0o755)
    return subprocess.run(["sh", str(patched), "--version"], capture_output=True, text=True)


@pytest.mark.skipif(os.name != "posix", reason="POSIX shell required")
class TestWrapperFallback:
    def test_baked_path_is_used_when_it_resolves(self, tmp_path):
        home = _fake_install(tmp_path / "volume4" / "syrviscore")
        result = _run(_wrapper(home), tmp_path)
        assert "RAN-FROM={}".format(home) in result.stdout
        assert "WARNING" not in result.stderr

    def test_renamed_install_is_found_and_used_with_a_loud_warning(self, tmp_path):
        baked = tmp_path / "volume4" / "syrviscore"
        wrapper = _wrapper(baked)
        # DSM renames the root out from under the (already-written) wrapper
        renamed = _fake_install(tmp_path / "volume4" / "syrviscore_1")

        result = _run(wrapper, tmp_path)

        assert result.returncode == 0
        assert "RAN-FROM={}".format(renamed) in result.stdout
        assert "WARNING" in result.stderr
        assert str(renamed) in result.stderr
        # SYRVIS_HOME must follow the discovery, not the baked path
        assert result.stdout.strip() != "RAN-FROM={}".format(baked)

    def test_two_candidates_refuse_rather_than_guess(self, tmp_path):
        wrapper = _wrapper(tmp_path / "volume4" / "syrviscore")
        _fake_install(tmp_path / "volume4" / "syrviscore_1")
        _fake_install(tmp_path / "volume6" / "syrviscore")

        result = _run(wrapper, tmp_path)

        assert result.returncode == 1
        assert "refusing to guess" in result.stderr

    def test_nothing_found_points_at_the_rename_not_at_a_reinstall(self, tmp_path):
        wrapper = _wrapper(tmp_path / "volume4" / "syrviscore")

        result = _run(wrapper, tmp_path)

        assert result.returncode == 1
        assert "RENAMED path" in result.stderr
        assert "ls -d /volume*/syrviscore*" in result.stderr
        assert "would create a SECOND tree" in result.stderr
        # the old advice must be gone: it is the destructive one
        assert "Run 'syrvisctl install'" not in result.stderr

    def test_is_valid_posix_shell(self, tmp_path):
        wrapper = _wrapper(tmp_path / "syrviscore")
        assert subprocess.call(["sh", "-n", str(wrapper)]) == 0
