"""The little Markdown structure the vault writers need, as pure text
functions: headings outside code fences, and one level-2 section by name.

No I/O and no policy. `obsidian_links` uses it for `## Related`;
`obsidian_logs` for `## Entries` and `## Development Log`. What a section is
FOR, and what may be written into it, is theirs to decide, not this module's.
"""

from __future__ import annotations

import re

_HEADING = re.compile(r"(#{1,6})[ \t]+(.*?)[ \t#]*\Z")
_FENCE = re.compile(r"[ \t]{0,3}(`{3,}|~{3,})")


def line_ending(text: str) -> str:
    return "\r\n" if "\r\n" in text else "\n"


def _scan(lines: list[str]) -> tuple[list[tuple[int, int, str]], bool]:
    out, fence = [], None
    for i, line in enumerate(lines):
        bare = line.rstrip("\r\n")
        f = _FENCE.match(bare)
        if f:
            mark = f.group(1)[0]
            if fence is None:
                fence = mark
            elif fence == mark:
                fence = None
            continue
        if fence is not None:
            continue
        h = _HEADING.match(bare)
        if h:
            out.append((i, len(h.group(1)), h.group(2).strip()))
    return out, fence is not None


def headings(lines: list[str]) -> list[tuple[int, int, str]]:
    """(line index, level, text) of every heading outside a code fence."""
    return _scan(lines)[0]


def fence_left_open(lines: list[str]) -> bool:
    """Whether a code fence is still open after the last line: text that
    would swallow every heading written after it."""
    return _scan(lines)[1]


def section_span(lines: list[str], title: str) -> tuple[int, int] | None | bool:
    """(first line after the heading, end) of the one `## <title>` section,
    matched without regard to case; None if there is none; False if there is
    more than one, which is the user's structure to sort out, not ours."""
    heads = headings(lines)
    want = title.casefold()
    found = [i for i, lvl, t in heads if lvl == 2 and t.casefold() == want]
    if len(found) > 1:
        return False
    if not found:
        return None
    start = found[0]
    end = len(lines)
    for i, lvl, _t in heads:
        if i > start and lvl <= 2:
            end = i
            break
    return start + 1, end
