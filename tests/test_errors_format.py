"""
Tests for the service package's typed error model (syrviscore.errors), the
CLI error boundary (syrviscore.cli.handle_errors), and the shared table
formatting helpers (syrviscore._format).
"""

import click
import pytest
from click.testing import CliRunner

from syrviscore import _format
from syrviscore.cli import handle_errors
from syrviscore.docker_manager import DockerConnectionError, DockerError
from syrviscore.errors import SyrvisError
from syrviscore.paths import SyrvisHomeError
from syrviscore.privileged_ops import PrivilegedOpsError
from syrviscore.service_schema import ServiceValidationError
from syrviscore.stack import StackError


class TestErrorTaxonomy:
    def test_stable_codes(self):
        assert SyrvisError.code == "error"
        assert DockerConnectionError.code == "docker_unreachable"
        assert DockerError.code == "docker_error"
        assert SyrvisHomeError.code == "home_not_found"
        assert StackError.code == "stack_invalid"
        assert ServiceValidationError.code == "service_invalid"
        assert PrivilegedOpsError.code == "privileged_op_failed"

    def test_all_subclass_syrvis_error(self):
        for exc in (
            DockerConnectionError,
            DockerError,
            SyrvisHomeError,
            StackError,
            ServiceValidationError,
            PrivilegedOpsError,
        ):
            assert issubclass(exc, SyrvisError)

    def test_to_dict_envelope(self):
        err = StackError("unknown service 'nope'")
        assert err.to_dict() == {"error": "unknown service 'nope'", "code": "stack_invalid"}

    def test_default_exit_code(self):
        assert SyrvisError("boom").exit_code == 1

    def test_service_validation_error_still_a_valueerror(self):
        # Existing `except ValueError` call sites must keep catching it.
        with pytest.raises(ValueError):
            raise ServiceValidationError("bad manifest")


class TestHandleErrors:
    def _cmd(self, exc):
        @click.command()
        @handle_errors
        def boom():
            raise exc

        return boom

    def test_syrvis_error_rendered_once(self):
        result = CliRunner().invoke(self._cmd(SyrvisError("it broke")))
        assert result.exit_code == 1
        assert "Error: it broke" in result.output
        assert "Aborted!" not in result.output  # no redundant click.Abort line

    def test_unexpected_error_rendered(self):
        result = CliRunner().invoke(self._cmd(RuntimeError("surprise")))
        assert result.exit_code == 1
        assert "Error: surprise" in result.output

    def test_click_abort_propagates(self):
        result = CliRunner().invoke(self._cmd(click.Abort()))
        assert result.exit_code == 1
        assert "Aborted!" in result.output
        assert "Error:" not in result.output

    def test_click_usage_error_propagates(self):
        result = CliRunner().invoke(self._cmd(click.UsageError("bad usage")))
        assert result.exit_code == 2  # click's usage-error exit code, untouched

    def test_system_exit_propagates(self):
        result = CliRunner().invoke(self._cmd(SystemExit(3)))
        assert result.exit_code == 3
        assert "Error:" not in result.output

    def test_exit_code_honored(self):
        class Fatal(SyrvisError):
            exit_code = 7

        result = CliRunner().invoke(self._cmd(Fatal("fatal")))
        assert result.exit_code == 7


class TestStatusGlyph:
    def test_ok_states(self):
        assert _format.status_glyph("running") == "[+]"
        assert _format.status_glyph("enabled") == "[+]"
        assert _format.status_glyph(True) == "[+]"

    def test_off_states(self):
        assert _format.status_glyph("stopped") == "[-]"
        assert _format.status_glyph("exited") == "[-]"
        assert _format.status_glyph("not running") == "[-]"
        assert _format.status_glyph(False) == "[-]"

    def test_unknown_state(self):
        assert _format.status_glyph("weird") == "[?]"
        assert _format.status_glyph("") == "[?]"


class TestFormatRow:
    def test_header_and_row_share_widths(self):
        widths = (10, 5, 0)
        header = _format.format_row(list(zip(("NAME", "STATE", "URL"), widths)))
        row = _format.format_row(list(zip(("[+] app", "up", "http://x"), widths)))
        # Both rows place column 2 at the same offset.
        assert header.index("STATE") == row.index("up") == 11
        assert header.index("URL") == row.index("http://x") == 17

    def test_last_column_not_padded(self):
        assert _format.format_row([("a", 3), ("b", 0)]) == "a   b"

    def test_no_trailing_whitespace(self):
        line = _format.format_row([("x", 10), ("", 10)])
        assert line == line.rstrip()
