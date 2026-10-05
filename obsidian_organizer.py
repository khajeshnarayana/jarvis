"""Where a thing the user asked to keep belongs in their Obsidian vault, and
saving it there: create a note, append to the one that is clearly the same
subject, or do nothing because it is already written down.

The brain decides what the material IS — a category out of four, a subject,
a project name. This module decides everything structural: the folder, the
filename, whether an existing note is the same subject, create or append.
Every path it builds is handed to `obsidian_vault`, whose checks stay the
authority; nothing here touches the filesystem directly.

THE TRUST BOUNDARY — read this before changing anything below.

`store` looks at the vault before it writes, and the vault is untrusted: a
note can hold anything the user ever clipped into it. The brain is never
shown what `store` looks at. That is why `obsidian_store` can do in one call
what `obsidian_search` followed by `obsidian_append` cannot do in one turn
(the search taints the turn and the append is refused). It is not an
exemption from that gate — `obsidian_store` is gated like every other acting
tool — it is that no foreign text crosses into the brain's context, so there
is nothing for the gate to protect against.

What the vault is allowed to decide, and nothing more:

- WHICH existing note in the destination folder is the same subject. Only by
  its FILENAME, which must reduce to exactly the subject's own words
  (`_same_subject`), and only a note directly in that folder. So the path a
  reused note has is the fixed category folder, the project folder (whose
  name must reduce to the project the brain gave), and a title made of the
  subject the brain gave. Vault text cannot choose a different folder, a
  different category, or a title with words of its own in it.
- Whether the material is ALREADY in that note: its lines, whole and in
  order (`_already_has`). The only effect is "unchanged" instead of
  "appended"; it is never read as an instruction.

What the vault can never decide: the content written (always exactly the
`content` argument), the destination category, any path outside the folder
chosen from the brain's arguments, or any other action. No note body is
parsed for anything but that one substring test. Do not add a step that reads
meaning out of a note body to steer the save; that is the hole this design
exists to keep shut.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

import obsidian_vault as ov

PROJECTS = "01 Projects"
KNOWLEDGE = "03 Knowledge"
DECISIONS = "04 Decisions"
INBOX = "00 Inbox"

# The only four places a save is routed to. 02 Areas, 05 Journal, 06 JARVIS
# and 99 Archive get their own flows later; nothing here may reach them.
CATEGORY_FOLDERS = {
    "project": PROJECTS,
    "knowledge": KNOWLEDGE,
    "decision": DECISIONS,
    "inbox": INBOX,
}
_CATEGORY_ALIASES = {
    "project": "project", "projects": "project",
    "knowledge": "knowledge", "reference": "knowledge",
    "decision": "decision", "decisions": "decision",
    "inbox": "inbox",
}

MAX_TITLE_CHARS = 80
MAX_PROJECT_CHARS = 60
TITLE_WORDS_FROM_CONTENT = 8
# Candidates looked at when deciding whether the subject already has a note.
MATCH_CANDIDATES = 20

_WORD = re.compile(r"\w+")
# What may stay in a filename: letters, digits, spaces and light punctuation.
# Everything else — `/`, `\`, `:`, `#`, `^`, `[`, `]`, `|`, `*`, `?`, `"`,
# `<`, `>`, a dot, a control character — becomes a space.
_NAME_UNSAFE = re.compile(r"[^\w \-,'()&+]")
_LEADING_JUNK = re.compile(r"\A[\s\-,'()&+_]+")
# Markdown a first line starts with, which is not part of a title.
_LINE_MARKUP = re.compile(r"\A\s*(?:#{1,6}\s+|[-*+]\s+(?:\[[ xX]\]\s+)?|>\s*|\d+[.)]\s+)")

# Words that do not make two titles different subjects: "JARVIS Runtime" and
# "JARVIS Runtime Notes" are one note.
_FILLER = frozenset("""
a an and about for in of on the to with note notes
""".split())

# Only when the brain gave no category: words that mark a decision. Anything
# else without a category is the Inbox — a guess is worse than the Inbox.
_DECISION_CUES = re.compile(
    r"\b(?:we decided|i decided|decided to|decision:|we will use|we'll use|"
    r"we chose|chose to|going with)\b", re.IGNORECASE)


@dataclass(frozen=True)
class StoreResult:
    action: str        # "created" | "appended" | "unchanged"
    category: str      # one of CATEGORY_FOLDERS
    path: str          # vault-relative, with `.md`
    reused: bool       # an existing note was written to (or already had it)
    reason: str        # one of the fixed sentences below, never vault text


# Every reason `store` gives. Fixed text, so nothing a note says can become a
# sentence the brain reads as the outcome of the save.
R_NEW = "no existing note in that folder has this subject"
R_SAME = "an existing note has the same subject"
R_ALREADY = "that note already contains this material"
R_AMBIGUOUS = "more than one existing note looked alike, so neither was touched"
R_NO_CATEGORY = "no category was clear, so it went to the Inbox"
R_BAD_CATEGORY = "that category is not one I route to, so it went to the Inbox"
R_NO_PROJECT = "no usable project name was given, so it went to the Inbox"


def clean_name(text, limit: int) -> str:
    """A filename-safe version of `text`, or "" if nothing usable is left.

    Never a path: every separator is removed, so the result is always one
    component. Cut at a word boundary under `limit`.
    """
    value = _NAME_UNSAFE.sub(" ", str(text or ""))
    value = " ".join(value.split())
    value = _LEADING_JUNK.sub("", value).strip()
    if len(value) > limit:
        cut = value[:limit + 1].rsplit(" ", 1)[0]
        value = (cut if cut else value[:limit]).rstrip(" -,'(&+")
    return value


def _words(text: str) -> list[str]:
    return [w.casefold() for w in _WORD.findall(text)]


def _singular(word: str) -> str:
    if len(word) > 4 and word.endswith("s") and not word.endswith("ss"):
        return word[:-1]
    return word


def subject_key(text: str, drop: frozenset = frozenset()) -> frozenset:
    """The words that make a title the subject it is, in no order."""
    return frozenset(_singular(w) for w in _words(text)
                     if w not in _FILLER and _singular(w) not in drop)


def _same_subject(key: frozenset, title: str, drop: frozenset) -> bool:
    """A strict test: the candidate's title reduces to exactly `key`.

    One shared keyword is never enough, and neither is the subject plus a
    word of the note's own — "JARVIS Runtime Bugs" is not "JARVIS Runtime".
    """
    return bool(key) and subject_key(title, drop) == key


def normalise_category(category, project: str, content: str) -> tuple[str, str | None]:
    """(category, a reason if it was decided here rather than given)."""
    raw = " ".join(str(category or "").split()).casefold()
    if raw:
        known = _CATEGORY_ALIASES.get(raw)
        if known is None:
            return "inbox", R_BAD_CATEGORY
        return known, None
    if project:
        return "project", None
    if _DECISION_CUES.search(content):
        return "decision", None
    return "inbox", R_NO_CATEGORY


def title_for(title, content: str) -> str:
    """The note's title: the one given, else the first words of the content."""
    given = clean_name(title, MAX_TITLE_CHARS)
    if given:
        return given
    for line in content.splitlines():
        line = _LINE_MARKUP.sub("", line).strip()
        if _words(line):
            words = line.split()[:TITLE_WORDS_FROM_CONTENT]
            derived = clean_name(" ".join(words), MAX_TITLE_CHARS)
            if derived:
                return derived
    return "Untitled note"


def _project_folder(project: str) -> str:
    """The existing project folder whose name IS this project, else the
    cleaned name for a new one."""
    key = subject_key(project)
    for name in ov.list_folders(PROJECTS):
        if key and subject_key(name) == key:
            return name
    return project


def _lines(text: str) -> list[str]:
    return [" ".join(ln.split()) for ln in text.splitlines() if ln.strip()]


def _already_has(note_text: str, content: str) -> bool:
    """Every line of `content`, in order and consecutive, is already in the
    note — whole lines, whitespace collapsed. A phrase that merely occurs
    inside a longer line does not count, so a short save is never swallowed
    by an accidental match."""
    want, have = _lines(content), _lines(note_text)
    if not want:
        return False
    n = len(want)
    return any(have[i:i + n] == want for i in range(len(have) - n + 1))


def store(content, category=None, title=None, project=None) -> StoreResult:
    """Save `content` where it belongs. Raises `ov.VaultError` to refuse."""
    if not isinstance(content, str) or not content.strip():
        raise ov.VaultError("There is nothing to save.")
    if len(content) > ov.MAX_WRITE_CHARS:
        raise ov.VaultError("That is too long for one note write.")
    ov.vault_root()

    project_name = clean_name(project, MAX_PROJECT_CHARS)
    kind, routed_reason = normalise_category(category, project_name, content)
    if kind == "project" and not subject_key(project_name):
        kind, routed_reason = "inbox", R_NO_PROJECT
    subject = title_for(title, content)

    folder = CATEGORY_FOLDERS[kind]
    drop: frozenset = frozenset()
    if kind == "project":
        project_dir = _project_folder(project_name)
        folder = f"{folder}/{project_dir}"
        # Inside a project's folder the project's own name says nothing:
        # "JARVIS Runtime" and "Runtime" in 01 Projects/JARVIS are one note.
        drop = subject_key(project_name)
        if subject_key(subject, drop):
            filename = subject
        else:
            filename = "Overview"
    elif kind == "decision" and project_name:
        if subject_key(project_name) <= subject_key(subject):
            filename = subject
        else:
            filename = clean_name(f"{project_name} - {subject}", MAX_TITLE_CHARS)
    else:
        filename = subject

    target_path = f"{folder}/{filename}.md"
    key = subject_key(filename, drop)

    # The folder is made through the vault's own primitive, so its checks
    # apply to it exactly as to a path the brain typed.
    ov.create_folder(folder)

    # ---- the only place the vault is looked at -----------------------------
    # `ov.search` builds each hit's path from the filesystem walk itself, so
    # no note can name a path of its choosing; only a note DIRECTLY in
    # `folder` whose filename is this subject counts.
    matches = []
    if key:
        try:
            hits = ov.search(" ".join(sorted(key)), path=folder,
                             limit=MATCH_CANDIDATES).hits
        except ov.VaultError:
            hits = []
        for hit in hits:
            # The title is read off the walk's own path, the same string that
            # is later re-resolved, so the two can never disagree.
            parent, _, name = hit.path.rpartition("/")
            title = name[: -len(ov.NOTE_SUFFIX)]
            if parent == folder and _same_subject(key, title, drop):
                matches.append(hit.path)

    exact = [p for p in matches if p.casefold() == target_path.casefold()]
    if exact:
        chosen = exact[0]
    elif len(matches) == 1:
        chosen = matches[0]
    else:
        chosen = None

    if chosen is None:
        reason = routed_reason or (R_AMBIGUOUS if matches else R_NEW)
        ov.create_note(target_path, content)
        return StoreResult("created", kind, target_path, False, reason)

    # `chosen` came from the walk; it goes back through the resolver on
    # both the read and the append.
    shown, existing = ov.read(chosen)
    if _already_has(existing, content):
        return StoreResult("unchanged", kind, shown, True, R_ALREADY)
    ov.append(shown, "\n" + content)
    return StoreResult("appended", kind, shown, True, routed_reason or R_SAME)
