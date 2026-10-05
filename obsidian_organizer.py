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
chosen from the brain's arguments, or any other action — beyond the one
further edit described under "Related notes" below, which is links, inside a
`## Related` section, to notes chosen by their paths. No note body is
parsed for anything but that one substring test. Do not add a step that reads
meaning out of a note body to steer the save; that is the hole this design
exists to keep shut.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace

import obsidian_links as links
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
    related: int = 0            # strongly related notes found (at most 3)
    links_added: int = 0        # new links written into the saved note —
                                # what the reply calls "related notes linked"
    backlinks_added: int = 0    # links back written into those notes
    backlinks_skipped: int = 0  # related notes it was not safe to edit


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


def project_folder(project: str) -> str:
    """The existing project folder whose name IS this project, else the
    cleaned name for a new one. `project` is already `clean_name`d. Only
    real, visible folders are candidates (`ov.list_folders`), so a symlinked
    project folder is never the one chosen."""
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
        project_dir = project_folder(project_name)
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
        return _with_links(StoreResult("created", kind, target_path, False, reason))

    # `chosen` came from the walk; it goes back through the resolver on
    # both the read and the append.
    shown, existing = ov.read(chosen)
    if _already_has(existing, content):
        # Nothing was added, so nothing is linked either: "unchanged" means
        # the note was not touched at all.
        return StoreResult("unchanged", kind, shown, True, R_ALREADY)
    _append_above_related(shown, content)
    return _with_links(StoreResult("appended", kind, shown, True,
                                   routed_reason or R_SAME))


def _append_above_related(path: str, content: str) -> None:
    """Append, except that a note ending in its `## Related` section gets the
    new material just ABOVE that section, not underneath the links. Falls
    back to a plain append whenever the in-place edit is not safe."""
    for _attempt in range(2):
        try:
            shown, text, digest = ov.read_for_update(path)
        except (ov.VaultError, OSError):
            break
        at = links.related_is_last(text)
        if at is None:
            break
        eol = "\r\n" if "\r\n" in text else "\n"
        head, tail = text[:at], text[at:]
        if head and not head.endswith(("\n", "\r")):
            head += eol
        if head.strip() and not head.endswith(eol + eol):
            head += eol
        block = eol.join(content.strip("\r\n").splitlines()) + eol + eol
        try:
            ov.update_note(shown, head + block + tail, digest)
            return
        except ov.VaultError:
            continue                     # changed underneath: look again once
        except OSError:
            break
    ov.append(path, "\n" + content)


# ---------------------------------------------------------------------------
# Related notes
# ---------------------------------------------------------------------------
#
# After a save, up to three strongly related notes are linked from the saved
# note's `## Related` section, and the saved note is linked back from each of
# theirs. The trust boundary above holds here too:
#
# - WHICH notes are related is decided by `links.relation` on PATHS alone —
#   the subject words of filenames the walk produced, with every project
#   name (the folders in 01 Projects) taken out, so two notes are never related merely for being in one
#   project, and never at all for being in one top-level folder. A note's body cannot make it related; at most its search rank
#   orders notes that already qualify on their names.
# - Every link written is built by `links.wikilink` from such a path. Text
#   inside a note — `[[99 Archive/Secrets]]`, "link me to …" — is read only
#   to see whether a link to a note ALREADY exists, never as a target.
# - The only edit made to a related note is links added inside its
#   `## Related` section (`links.add_related`), compare-and-swapped through
#   `ov.update_note`. Its body, title, frontmatter and other sections are
#   never touched, and any note that cannot be edited that way is skipped,
#   not forced.
# - Only the four folders a save can be routed to are candidates, so these
#   edits never reach 02 Areas, 05 Journal, 06 JARVIS or 99 Archive.

RELATED_FOLDERS = (INBOX, PROJECTS, KNOWLEDGE, DECISIONS)
RELATED_CANDIDATES_PER_FOLDER = 10


def _queries(words) -> list[str]:
    """Search queries that between them look for every one of `words`.
    Search drops its stopwords from a query that has other words, and a
    subject word can be one ("Obsidian Integration"); any word the joined
    query would not look for is asked for on its own."""
    words = sorted(words)
    if not words:
        return []
    joined = " ".join(words)
    try:
        kept = set(ov.query_terms(joined))
    except ov.VaultError:
        kept = set()
    return [joined] + [w for w in words if w not in kept]


def related_notes(saved: str) -> list[str]:
    """At most three notes strongly related to `saved`, best first."""
    # The project folders are the vault's project names, which never count
    # as subject words. Without that list nothing is linked: a link made on
    # a project's name is the imprecision this exists to prevent.
    try:
        projects = links.known_projects(ov.list_folders(PROJECTS))
    except (ov.VaultError, OSError):
        return []
    searches = [(folder, q) for q in _queries(links.distinctive_words(saved, projects))
                for folder in RELATED_FOLDERS]
    # Route B (`links.relation`) links on a generic word only inside one
    # named project, so those words are looked for only where that
    # project's notes are: its folder, and the decisions filed under it.
    if links.project_of(saved) and links.distinctive_words(saved, projects):
        parts = saved.split("/")
        home = f"{PROJECTS}/{parts[1]}" if parts[0] == PROJECTS else PROJECTS
        for q in _queries(links.generic_words(saved, projects)):
            searches += [(home, q), (DECISIONS, q)]
    rank: dict[str, int] = {}
    for folder, query in searches:
        try:
            hits = ov.search(query, path=folder,
                             limit=RELATED_CANDIDATES_PER_FOLDER).hits
        except ov.VaultError:
            continue                     # e.g. the folder does not exist
        for hit in hits:
            rank.setdefault(hit.path, hit.score)
    scored = []
    for path, search_score in rank.items():
        if path.casefold() == saved.casefold() or not links.linkable(path):
            continue
        rel = links.relation(saved, path, projects)
        if links.related_enough(rel):
            scored.append((-rel.score, -search_score, path.casefold(), path))
    scored.sort()
    return [entry[-1] for entry in scored[: links.MAX_LINKS_PER_SAVE]]


def _add_links(path: str, wanted: list[str]) -> int | None:
    """Add each link in `wanted` that `path` does not already have to its
    Related section. The number added, or None if the note could not be
    edited safely. One retry if it changed underneath."""
    for _attempt in range(2):
        try:
            shown, text, digest = ov.read_for_update(path)
        except (ov.VaultError, OSError):
            return None
        new = [link for target, link in wanted if not links.links_to(text, target)]
        if not new:
            return 0
        updated = links.add_related(text, new)
        if updated is None:
            return None
        try:
            ov.update_note(shown, updated, digest)
            return len(new)
        except ov.VaultError:
            continue                     # changed underneath: look again once
        except OSError:
            return None
    return None


def _with_links(result: StoreResult) -> StoreResult:
    """`result`, after linking the saved note to its related notes and back.
    Linking never fails the save: whatever could not be done is counted."""
    try:
        related = related_notes(result.path)
    except (ov.VaultError, OSError):
        related = []
    if not related:
        return result
    try:
        all_paths, truncated = ov.list_note_paths()
    except (ov.VaultError, OSError):
        all_paths, truncated = [], True
    names: dict[str, int] = {}
    for p in all_paths:
        key = links.name_of(p).casefold()
        names[key] = names.get(key, 0) + 1

    def link_for(p: str) -> str:
        unique = not truncated and names.get(links.name_of(p).casefold(), 0) == 1
        return links.wikilink(p, unique)

    forward = _add_links(result.path, [(p, link_for(p)) for p in related])
    back_added = back_skipped = 0
    if links.linkable(result.path):
        back = (result.path, link_for(result.path))
        for p in related:
            added = _add_links(p, [back])
            if added is None:
                back_skipped += 1
            else:
                back_added += added
    else:
        back_skipped = len(related)
    return replace(result, related=len(related), links_added=forward or 0,
                   backlinks_added=back_added, backlinks_skipped=back_skipped)
