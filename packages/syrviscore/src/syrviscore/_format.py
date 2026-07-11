"""
Shared CLI output formatting (stdlib-only, Python 3.8-clean).

One glyph convention for every table the ``syrvis`` CLI prints — plain ASCII,
the safest choice for DSM/ssh terminals:

    [+]  running / enabled / ok
    [-]  stopped / disabled
    [?]  unknown

and a tiny fixed-width row helper so header and data rows share ONE width
spec instead of hand-rolled f-strings that drift apart.

Usage (see ``cli.py`` for real call sites)::

    widths = [15, 12, 0]  # 0 = last column, never padded
    click.echo(format_row(list(zip(["Service", "Status", "Uptime"], widths))))
    click.echo(format_row([(f"{status_glyph(state)} {name}", 15), (state, 12), (uptime, 0)]))
"""

from typing import List, Tuple

GLYPH_OK = "[+]"
GLYPH_OFF = "[-]"
GLYPH_UNKNOWN = "[?]"

_OK_STATES = frozenset({"running", "enabled", "ok", "up", "healthy", "active"})
_OFF_STATES = frozenset(
    {
        "stopped",
        "disabled",
        "exited",
        "created",
        "paused",
        "dead",
        "restarting",
        "removing",
        "not running",
    }
)


def status_glyph(state) -> str:
    """Map a state (string or bool) to the shared ASCII glyph.

    ``[+]`` for running/enabled-ish states, ``[-]`` for stopped/disabled-ish
    states, ``[?]`` for anything unrecognized.
    """
    if isinstance(state, bool):
        return GLYPH_OK if state else GLYPH_OFF
    normalized = str(state).strip().lower()
    if normalized in _OK_STATES:
        return GLYPH_OK
    if normalized in _OFF_STATES:
        return GLYPH_OFF
    return GLYPH_UNKNOWN


def format_row(columns: List[Tuple[str, int]]) -> str:
    """Format one table row from ``(text, width)`` cells.

    Each cell is left-justified to ``width``; a width of 0 (conventionally the
    last column) is emitted as-is. Trailing whitespace is stripped so padded
    rows never end in invisible spaces. Use the SAME width list for the header
    row and every data row to keep columns in sync.
    """
    parts = []
    for text, width in columns:
        cell = str(text)
        parts.append(cell.ljust(width) if width > 0 else cell)
    return " ".join(parts).rstrip()
