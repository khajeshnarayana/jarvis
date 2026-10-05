"""Chronological notes in the user's Obsidian vault: one daily note per local
calendar day, and one development log per project per day.

    05 Journal/YYYY-MM-DD.md
    01 Projects/<Project>/Logs/YYYY-MM-DD.md

These are the USER's notes, written only when the user asks for something to
be logged. They are not JARVIS's own journal (`write_journal`, his handover
between conversations) and nothing here reads or writes JARVIS's memory.

Each note has one H1 and one section this module owns — `## Entries` in a
daily note, `## Development Log` in a project log — holding one `###` entry
per event:

    # 2026-10-05

    ## Entries

    ### 14:32 — Obsidian Integration
    Implemented related-note backlinking.

Entries are only ever added, at the end of that section. Everything else in
the note — frontmatter, the user's own sections before or after it, earlier
entries — is left byte for byte as it was. A note with two such sections is
refused rather than guessed at.

The trust boundary is `obsidian_organizer`'s. The path is built from the
brain's arguments alone: a fixed folder, a project folder resolved exactly as
a save resolves one, and a filename that is a validated calendar date. What
an existing log says can decide only two things — whether this exact entry is
already in it, and where its section ends — never the path, the project, the
entry written, or any other action. No time or date is ever taken from note
text.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date as Date, datetime

import obsidian_markdown as md
import obsidian_organizer as org
import obsidian_vault as ov

DAILY = "daily"
PROJECT = "project"
KINDS = (DAILY, PROJECT)

JOURNAL = "05 Journal"
LOGS = "Logs"
SECTIONS = {DAILY: "Entries", PROJECT: "Development Log"}

# Strict: four ASCII digits, two, two. `fromisoformat` alone would also take
# "20261005" and, on recent Pythons, other shapes; the date is a filename, so
# exactly one spelling of it is allowed.
_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", re.ASCII)
# What makes a `###` heading inside a managed section an entry: one this
# module wrote, or one the user typed the same way.
_ENTRY = re.compile(r"(?:[0-9]{1,2}:[0-9]{2}|Entry)(?:\s|\Z)", re.ASCII)
UNDATED = "Entry"      # the heading of an entry for an earlier day: no clock


def now() -> datetime:
    """The local time on this Mac. Tests replace it."""
    return datetime.now().astimezone()


@dataclass(frozen=True)
class LogResult:
    action: str          # "created" | "appended" | "unchanged"
    kind: str            # one of KINDS
    project: str | None  # the project folder's name, for a project log
    date: str            # YYYY-MM-DD
    path: str            # vault-relative, with `.md`


def resolve_date(value, today: Date) -> Date:
    """`value` as a calendar date — `YYYY-MM-DD`, today or earlier — or
    today when it is not given. Anything else is refused, never repaired."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return today
    if not isinstance(value, str) or not _DATE.fullmatch(value.strip()):
        raise ov.VaultError("A date must be written YYYY-MM-DD, like 2026-10-05.")
    try:
        day = Date.fromisoformat(value.strip())
    except ValueError:
        raise ov.VaultError("That is not a real calendar date.") from None
    if day > today:
        raise ov.VaultError("I can only log for today or an earlier day.")
    return day


def _content_lines(content) -> list[str]:
    """The entry's lines, exactly as given less blank lines at either end.
    Refused if it would break the note's structure: a `#` or `##` heading
    would end the section early, and an unclosed code fence would swallow
    every heading written after it."""
    if not isinstance(content, str) or not content.strip():
        raise ov.VaultError("There is nothing to log.")
    if len(content) > ov.MAX_WRITE_CHARS:
        raise ov.VaultError("That is too long for one log entry.")
    lines = content.splitlines()
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    if any(level <= 2 for _i, level, _t in md.headings(lines)):
        raise ov.VaultError("That has a top-level heading in it, which would break "
                            "the log; give it without # or ## headings.")
    if md.fence_left_open(lines):
        raise ov.VaultError("That has a code block that is never closed, which "
                            "would break the log.")
    return lines


def _normal(lines) -> list[str]:
    return [" ".join(ln.split()) for ln in lines if ln.strip()]


def entry_heading(day: Date, moment: datetime, title: str) -> str:
    """`### HH:MM — Title` for today, from the local clock; `### Entry —
    Title` for an earlier day, whose time nobody gave."""
    when = moment.strftime("%H:%M") if day == moment.date() else UNDATED
    return f"### {when} — {title}" if title else f"### {when}"


def _entry_bodies(lines: list[str], start: int, end: int) -> list[list[str]]:
    """The body of every entry in lines[start:end], fence-aware."""
    heads = [i for i, level, text in md.headings(lines)
             if start <= i < end and level == 3 and _ENTRY.match(text)]
    bodies = []
    for n, i in enumerate(heads):
        stop = heads[n + 1] if n + 1 < len(heads) else end
        bodies.append(lines[i + 1:stop])
    return bodies


def add_entry(text: str, section: str, heading: str,
              body: list[str]) -> tuple[str | None, bool]:
    """(`text` with the entry added at the end of its `## <section>`, or the
    section created at the end of the note; whether the same entry was
    already there). None when the note has that section twice."""
    eol = md.line_ending(text)
    lines = text.splitlines(keepends=True)
    span = md.section_span(lines, section)
    if span is False:
        return None, False
    entry = [heading + eol] + [ln + eol for ln in body]
    if span is None:
        out = text
        if out and not out.endswith(("\n", "\r")):
            out += eol
        if out.strip():
            out += eol
        return out + f"## {section}{eol}{eol}" + "".join(entry), False
    start, end = span
    want = _normal(body)
    if any(_normal(b) == want for b in _entry_bodies(lines, start, end)):
        return text, True
    last = start - 1
    for i in range(start, end):
        if lines[i].strip():
            last = i
    if not lines[last].endswith(("\n", "\r")):
        lines[last] += eol
    insert = [eol] + entry
    if last + 1 < len(lines) and lines[last + 1].strip():
        insert.append(eol)              # a blank line before the next heading
    return "".join(lines[: last + 1] + insert + lines[last + 1:]), False


def _new_note(h1: str, section: str, heading: str, body: list[str]) -> str:
    return "\n".join([f"# {h1}", "", f"## {section}", "", heading, *body]) + "\n"


def log(content, kind, project=None, date=None, title=None) -> LogResult:
    """Add one entry to today's (or `date`'s) daily note or project log.
    Raises `ov.VaultError` to refuse."""
    kind = " ".join(str(kind or "").split()).casefold()
    if kind not in KINDS:
        raise ov.VaultError("A log is either daily or project.")
    body = _content_lines(content)
    ov.vault_root()
    moment = now()
    day = resolve_date(date, moment.date())
    stamp = day.isoformat()
    label = org.clean_name(title, org.MAX_TITLE_CHARS)

    if kind == DAILY:
        if project is not None and str(project).strip():
            raise ov.VaultError("A daily note has no project; a project's log "
                                "is kind project.")
        folder, h1, name = JOURNAL, stamp, None
    else:
        name = org.clean_name(project, org.MAX_PROJECT_CHARS)
        if not org.subject_key(name):
            raise ov.VaultError("I need the project's name for its log.")
        name = org.project_folder(name)
        folder, h1 = f"{org.PROJECTS}/{name}/{LOGS}", f"{name} — {stamp}"

    path = f"{folder}/{stamp}.md"
    section = SECTIONS[kind]
    heading = entry_heading(day, moment, label)
    try:
        return _write(path, h1, section, heading, body, kind, name, stamp)
    except OSError:
        raise ov.VaultError("I could not write to the vault just now.") from None


def _write(path, h1, section, heading, body, kind, name, stamp) -> LogResult:
    def result(action, shown):
        return LogResult(action, kind, name, stamp, shown)

    for _attempt in range(2):
        try:
            return result("created", ov.create_unlinked_note(
                path, _new_note(h1, section, heading, body)))
        except ov.NoteExists:
            pass
        shown, text, digest = ov.read_for_update(path)
        updated, already = add_entry(text, section, heading, body)
        if already:
            return result("unchanged", shown)
        if updated is None:
            raise ov.VaultError(f"That note has two {section} sections, so I "
                                "have left it for you to sort out.")
        try:
            ov.update_note(shown, updated, digest)
            return result("appended", shown)
        except ov.VaultError:
            continue                     # changed underneath: look again once
    raise ov.VaultError("That note kept changing while I worked on it, so I left it alone.")
