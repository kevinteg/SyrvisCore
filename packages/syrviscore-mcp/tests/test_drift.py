"""
Drift guards (G17, G18).

G17: the MCP's copied CLI regexes must match the source-of-truth regexes in the
     syrviscore/syrviscore-manager packages (read from source, not imported).
G18: the committed sudoers + shim must equal what deploy/gen.py produces from
     the command registry.
"""

import re
from pathlib import Path

from syrviscore_mcp import _cli_regexes

REPO = Path(__file__).resolve().parents[3]  # .../SyrvisCore
DEPLOY = Path(__file__).resolve().parents[1] / "deploy"


def _extract(pattern_var, source_file):
    text = (REPO / source_file).read_text()
    m = re.search(pattern_var + r'\s*=\s*re\.compile\(r"([^"]+)"\)', text)
    assert m, f"could not find {pattern_var} in {source_file}"
    return m.group(1)


class TestRegexDrift:
    def test_version_re_matches_source(self):
        src = _extract(
            "VERSION_RE",
            "packages/syrviscore-manager/src/syrviscore_manager/paths.py",
        )
        assert _cli_regexes.VERSION_RE.pattern == src

    def test_name_re_matches_source(self):
        src = _extract("NAME_RE", "packages/syrviscore/src/syrviscore/service_schema.py")
        assert _cli_regexes.NAME_RE.pattern == src

    def test_reserved_names_match_source(self):
        text = (REPO / "packages/syrviscore/src/syrviscore/service_schema.py").read_text()
        m = re.search(r"RESERVED_NAMES\s*=\s*frozenset\((\{[^}]+\})\)", text)
        assert m
        source_set = eval(m.group(1))  # noqa: S307 - trusted local source
        assert _cli_regexes.RESERVED_NAMES == frozenset(source_set)


class TestDeployDrift:
    def test_sudoers_matches_generator(self):
        from syrviscore_mcp.deploy import gen

        committed = (DEPLOY / "sudoers.d" / "syrviscore-mcp").read_text()
        assert committed == gen.render_sudoers()

    def test_shim_matches_generator(self):
        from syrviscore_mcp.deploy import gen

        committed = (DEPLOY / "ssh" / "syrvis-mcp-shim").read_text()
        assert committed == gen.render_shim()
