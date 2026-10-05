"""Wikilinks and the `## Related` section, as pure text functions — and the
rule that decides whether two notes are related enough to link.

No I/O here. `obsidian_organizer` reads and writes; this only answers "what
is the link to that note", "does this text already link there", "what is
this text with these links added to its Related section" and "are these two
notes related". Everything is deterministic.

What a note's TEXT can decide here is deliberately small: whether it already
links to a given note (so no second copy is added), and where its Related
section is (so links go inside it). A wikilink in a note body is read as
data — "this note points there" — and never as a request to point somewhere
else. Which notes get linked is decided only by their PATHS, below — and within a
path, only by the note's own subject: a project's name is who the note
belongs to, not what it is about.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

NOTE_SUFFIX = ".md"
RELATED_HEADING = "## Related"
MAX_LINKS_PER_SAVE = 3

# `[[target]]`, `[[target|alias]]`, `[[target#heading]]`, `![[embed]]`.
_WIKILINK = re.compile(r"\[\[([^\[\]|\n]*?)(?:\|[^\[\]\n]*)?\]\]")
# A name that would break the syntax it is written into, or a line.
_LINK_UNSAFE = re.compile(r"[\[\]|#^\n\r]")
_HEADING = re.compile(r"(#{1,6})[ \t]+(.*?)[ \t#]*\Z")
_FENCE = re.compile(r"[ \t]{0,3}(`{3,}|~{3,})")
_WORD = re.compile(r"\w+")

# Words that do not make two notes related on their own. A shared one adds a
# point; it can never be the whole reason for a link.
GENERIC = frozenset("""
a an and about for in of on the to with note notes project projects system
systems runtime information info update updates general misc miscellaneous
todo idea ideas overview plan plans design data database databases thing
things stuff log logs summary doc docs reference guide test tests setup
config configuration new old draft work item items list
""".split())


# --- links -------------------------------------------------------------------

def name_of(path: str) -> str:
    return path.rpartition("/")[2][: -len(NOTE_SUFFIX)]


def linkable(path: str) -> bool:
    """A note whose path can be written inside `[[ ]]` without breaking it."""
    return (path.lower().endswith(NOTE_SUFFIX) and bool(name_of(path).strip())
            and not _LINK_UNSAFE.search(path))


def wikilink(path: str, name_is_unique: bool) -> str:
    """`[[Name]]` when the name is the only note called that in the vault;
    otherwise `[[folder/Name|Name]]`, which Obsidian resolves by path."""
    name = name_of(path)
    if name_is_unique:
        return f"[[{name}]]"
    return f"[[{path[: -len(NOTE_SUFFIX)]}|{name}]]"


def _targets(text: str) -> list[str]:
    out = []
    for match in _WIKILINK.finditer(text):
        target = match.group(1).split("#", 1)[0].strip().replace("\\", "/")
        if target.lower().endswith(NOTE_SUFFIX):
            target = target[: -len(NOTE_SUFFIX)]
        if target:
            out.append(target.casefold())
    return out


def links_to(text: str, path: str) -> bool:
    """Whether any wikilink in `text`, manual or automatic, already points at
    the note at `path` — by name, by full path, or by a trailing part of it."""
    full = path[: -len(NOTE_SUFFIX)].casefold()
    for target in _targets(text):
        if target == full or full.endswith("/" + target):
            return True
    return False


# --- the Related section -----------------------------------------------------

def _line_ending(text: str) -> str:
    return "\r\n" if "\r\n" in text else "\n"


def _headings(lines: list[str]) -> list[tuple[int, int, str]]:
    """(line index, level, text) of every heading outside a code fence."""
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
    return out


def _related_span(lines: list[str]) -> tuple[int, int] | None | bool:
    """(first line after the heading, end) of the one Related section; None
    if there is none; False if there is more than one, which is the user's
    structure to sort out, not ours."""
    heads = _headings(lines)
    found = [(i, lvl) for i, lvl, t in heads if lvl == 2 and t.casefold() == "related"]
    if len(found) > 1:
        return False
    if not found:
        return None
    start, _ = found[0]
    end = len(lines)
    for i, lvl, _t in heads:
        if i > start and lvl <= 2:
            end = i
            break
    return start + 1, end


def related_is_last(text: str) -> int | None:
    """If the note's Related section runs to the end of the note, the
    character offset where its heading starts; else None. New material is
    inserted there rather than appended under the links."""
    lines = text.splitlines(keepends=True)
    span = _related_span(lines)
    if not span or span[1] != len(lines):
        return None
    return sum(len(ln) for ln in lines[: span[0] - 1])


def add_related(text: str, links: list[str]) -> str | None:
    """`text` with each of `links` added to its `## Related` section, or the
    section created at the end. Every other byte is kept. None if the note's
    structure is not one this will touch (two Related sections)."""
    if not links:
        return text
    eol = _line_ending(text)
    lines = text.splitlines(keepends=True)
    span = _related_span(lines)
    if span is False:
        return None
    bullets = [f"- {link}{eol}" for link in links]
    if span is None:
        body = text
        if body and not body.endswith(("\n", "\r")):
            body += eol
        if body.strip():
            body += eol
        return body + RELATED_HEADING + eol + eol + "".join(bullets)
    start, end = span
    # After the section's last non-blank line, so the links stay together.
    last = start - 1
    for i in range(start, end):
        if lines[i].strip():
            last = i
    if not lines[last].endswith(("\n", "\r")):
        lines[last] += eol
    insert = bullets
    if last == start - 1:
        insert = [eol] + bullets            # an empty section: one blank line
    return "".join(lines[: last + 1] + insert + lines[last + 1:])


# --- relatedness ---------------------------------------------------------------

def _singular(word: str) -> str:
    if len(word) > 4 and word.endswith("s") and not word.endswith("ss"):
        return word[:-1]
    return word


def _split(path: str) -> tuple[str, str]:
    """(the note's subject text, its project's name or "").

    A project's name says which project a note belongs to, never what it is
    about, so it is taken OUT of the subject: the folder name for a note in
    `01 Projects/<Project>/…`, and the `<Project> - ` prefix the organizer
    gives a project's decision in `04 Decisions`. A project note's filename
    may repeat its project ("JARVIS Runtime" in 01 Projects/JARVIS); those
    words are removed from the subject too, in `subject_words`.
    """
    parts = path.split("/")
    name = name_of(path)
    if len(parts) >= 3 and parts[0] == "01 Projects":
        return name, parts[1]
    if len(parts) == 2 and parts[0] == "04 Decisions" and " - " in name:
        project, _, rest = name.partition(" - ")
        return rest, project
    return name, ""


def _word_set(text: str) -> frozenset:
    return frozenset(_singular(w.casefold()) for w in _WORD.findall(text))


def known_projects(folder_names) -> frozenset:
    """The vault's project names, each as its set of words — the caller
    lists the folders in `01 Projects`; this only normalizes the names."""
    return frozenset(w for w in (_word_set(n) for n in folder_names) if w)


def subject_words(path: str, projects: frozenset = frozenset()) -> frozenset:
    """A note's subject as words: its filename, less its own project's name
    and less any of the vault's `projects` named in it, wherever the note
    lives. A project's name is who a note is about, never what: "JARVIS
    Runtime" in 03 Knowledge is about runtime. A name of several words is
    taken out only where all of them appear, so a project called "Delta
    Vision" does not take "vision" out of "Computer Vision"."""
    text, project = _split(path)
    words = _word_set(text) - _word_set(project)
    for name in projects:
        if name <= words:
            words -= name
    return words


def _distinctive(words: frozenset) -> frozenset:
    return frozenset(w for w in words
                     if w not in GENERIC and len(w) >= 3 and not w.isdigit())


def distinctive_words(path: str, projects: frozenset = frozenset()) -> frozenset:
    return _distinctive(subject_words(path, projects))


def generic_words(path: str, projects: frozenset = frozenset()) -> frozenset:
    return subject_words(path, projects) & GENERIC


def project_of(path: str) -> frozenset:
    """The named project a note belongs to, as words; empty if none. A
    top-level folder such as 03 Knowledge is a category, not a project."""
    return _word_set(_split(path)[1])


@dataclass(frozen=True)
class Relation:
    score: int
    shared: tuple[str, ...]      # the distinctive words in common
    concept: tuple[str, ...]     # generic words that qualify inside one project


# The rule, all of it. Subject words come from the filename alone, with
# project names taken out (`subject_words`): the note's own project, and any
# of the vault's projects its title names. Project identity counts only as
# membership — the "same named project" point below — never as a word.
#
#     score = 4 per shared DISTINCTIVE subject word
#           + 1 if both notes belong to the same NAMED project
#           + 1 if they share at least one GENERIC subject word
#
# Linked by either route:
#
#   A. at least one shared distinctive word ("PostgreSQL Indexing" and
#      "PostgreSQL Query Performance"), wherever the two notes live;
#   B. a project-scoped concept: both notes in the same named project, a
#      shared generic word, and each note with a distinctive word of its own
#      besides — "Runtime Security" and "Runtime Permissions" in
#      01 Projects/JARVIS are both about that project's runtime.
#
# Sharing a top-level folder (03 Knowledge, 00 Inbox) is no evidence at all:
# a category is not a subject, so "Python Runtime" and "JARVIS Runtime" in
# 03 Knowledge share only the generic "runtime" and never link. Sharing a
# project is worth one point and never qualifies on its own; generic words
# qualify only through route B. Only PATHS are scored, so a note's body has
# no say in whether it is linked; at most its search rank breaks a tie
# between notes that qualify.
W_DISTINCTIVE = 4
W_SAME_PROJECT = 1
W_GENERIC = 1


def relation(a: str, b: str, projects: frozenset = frozenset()) -> Relation:
    """`projects` is `known_projects` of the vault's project folders."""
    wa, wb = subject_words(a, projects), subject_words(b, projects)
    da, db = _distinctive(wa), _distinctive(wb)
    shared = da & db
    generic = (wa & wb) & GENERIC
    pa = project_of(a)
    same_project = bool(pa) and pa == project_of(b)
    score = W_DISTINCTIVE * len(shared)
    if same_project:
        score += W_SAME_PROJECT
    if generic - shared:
        score += W_GENERIC
    concept: frozenset = frozenset()
    # Each note has a distinctive word of its own: about something, not
    # only about the generic concept.
    if same_project and generic and da and db:
        concept = generic
    return Relation(score, tuple(sorted(shared)), tuple(sorted(concept)))


def related_enough(rel: Relation) -> bool:
    return bool(rel.shared) or bool(rel.concept)
