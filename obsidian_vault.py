"""Reading, writing and searching Markdown notes in an Obsidian vault, and
nothing else.

An Obsidian vault is a folder of plain Markdown files, so this works on the
filesystem directly: no plugin, no GUI automation, no shell. The vault is
`OBSIDIAN_VAULT_PATH`, read on every call so a test (or a restart-free edit of
the environment) can point it somewhere else.

Every path the brain supplies is a hostile string. It is relative to the vault
or it is refused: no absolute path, no `~`, no `..`, no hidden component
(which is how `.obsidian/`, the vault's own configuration, stays out of reach),
and no symlink that resolves outside the vault. The string checks reject the
obvious; the containment check on REAL paths is what actually decides, because
string checks cannot see a symlink and resolution alone would accept a
traversal reached through one. Same reasoning as `project_maker.target_for`.

Refused, never repaired: `../evil` is an error, not `evil`.
"""

from __future__ import annotations

import os
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

NOTE_SUFFIX = ".md"

# One write is a note, not a data dump.
MAX_WRITE_CHARS = 200_000
# What `read` will open. A note this size is already more than can be said.
MAX_READ_BYTES = 1_000_000
MAX_PATH_CHARS = 240
MAX_DEPTH = 12

_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
# `.txt`, `.png`, `.pdf` — an extension that says "not Markdown". A dot that is
# part of a title ("Notes v1.2", "Q3 plan 2.0") is not one.
def _has_foreign_extension(name: str) -> bool:
    stem, dot, ext = name.rpartition(".")
    return bool(stem and dot and ext.isascii() and ext.isalpha() and len(ext) <= 5)


class VaultError(Exception):
    """Something this module refuses to do. The message is speakable."""


def vault_root() -> Path:
    """The real path of the configured vault, or raise `VaultError`."""
    raw = (os.getenv("OBSIDIAN_VAULT_PATH") or "").strip()
    if not raw:
        raise VaultError("No Obsidian vault is configured. "
                         "Set OBSIDIAN_VAULT_PATH in .env.")
    root = Path(os.path.realpath(os.path.expanduser(raw)))
    if not root.is_dir():
        raise VaultError("The configured Obsidian vault is not a folder on disk.")
    return root


def _parts(relative: str) -> list[str]:
    """The components of a vault-relative path, or raise `VaultError`."""
    text = (relative or "").strip()
    if not text:
        raise VaultError("I need a path inside the vault.")
    if len(text) > MAX_PATH_CHARS:
        raise VaultError("That path is too long.")
    if _CONTROL.search(text):
        raise VaultError("That path holds a character I will not use.")
    if text.startswith(("/", "~", "\\")) or re.match(r"^[A-Za-z]:", text):
        raise VaultError("Paths must be inside the vault, not absolute.")
    if "\\" in text:
        raise VaultError("Use forward slashes in a vault path.")
    parts = [p.strip() for p in text.split("/") if p.strip() not in ("", ".")]
    if not parts:
        raise VaultError("I need a path inside the vault.")
    if len(parts) > MAX_DEPTH:
        raise VaultError("That path is nested too deeply.")
    for part in parts:
        if part == "..":
            raise VaultError("A path may not climb out of its folder.")
        if part.startswith("."):
            raise VaultError("Hidden folders, including .obsidian, are off limits.")
    return parts


def _check_real(root: Path, real: Path) -> None:
    """`real` must be the vault or inside it, and not reach a hidden folder
    by way of a symlink the string checks could not see."""
    if real != root and root not in real.parents:
        raise VaultError("That path leads outside the vault.")
    for part in real.relative_to(root).parts:
        if part.startswith("."):
            raise VaultError("Hidden folders, including .obsidian, are off limits.")


def _resolve(relative: str, *, note: bool) -> tuple[Path, Path]:
    """(vault root, absolute target) for a vault-relative path.

    The target need not exist yet, so the deepest ancestor that does is
    resolved and the rest is re-attached: a missing tail has no symlinks.
    """
    root = vault_root()
    parts = _parts(relative)
    if note:
        last = parts[-1]
        if last.lower().endswith(NOTE_SUFFIX):
            pass
        elif _has_foreign_extension(last):
            raise VaultError("Only Markdown notes, sir: that is not a .md file.")
        else:
            last += NOTE_SUFFIX
        if last.lower() == NOTE_SUFFIX:
            raise VaultError("A note needs a name.")
        parts[-1] = last

    candidate = root.joinpath(*parts)
    existing = candidate
    tail: list[str] = []
    while not os.path.lexists(existing) and existing != root:
        tail.append(existing.name)
        existing = existing.parent
    real = Path(os.path.realpath(existing)).joinpath(*reversed(tail))
    _check_real(root, real)
    return root, real


def _shown(root: Path, path: Path) -> str:
    return path.relative_to(root).as_posix()


def create_folder(path: str) -> str:
    """Create a folder, with parents. Already being there is success."""
    root, target = _resolve(path, note=False)
    if target.exists() and not target.is_dir():
        raise VaultError(f"{_shown(root, target)} exists and is not a folder.")
    if target.is_dir():
        return f"Folder already exists: {_shown(root, target)}"
    target.mkdir(parents=True, exist_ok=True)
    return f"Created folder: {_shown(root, target)}"


def _check_content(content: str) -> str:
    text = content if isinstance(content, str) else str(content or "")
    if not text.strip():
        raise VaultError("There is nothing to write.")
    if len(text) > MAX_WRITE_CHARS:
        raise VaultError("That is too long for one note write.")
    return text


def create_note(path: str, content: str) -> str:
    """Create a new note, with parents. Never overwrites."""
    text = _check_content(content)
    root, target = _resolve(path, note=True)
    shown = _shown(root, target)
    target.parent.mkdir(parents=True, exist_ok=True)
    body = text if text.endswith("\n") else text + "\n"
    try:
        # O_EXCL is the refusal to overwrite, and it is atomic: no window
        # between "does it exist" and "write it".
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644)
    except FileExistsError:
        raise VaultError(f"{shown} already exists, so I have not touched it.") from None
    with os.fdopen(fd, "wb") as f:
        f.write(body.encode("utf-8"))
    return f"Created note: {shown} ({len(body.encode('utf-8'))} bytes)"


def append(path: str, content: str) -> str:
    """Append to a note, creating it (and parents) if it is missing."""
    text = _check_content(content)
    root, target = _resolve(path, note=True)
    shown = _shown(root, target)
    target.parent.mkdir(parents=True, exist_ok=True)
    addition = text if text.endswith("\n") else text + "\n"
    fd = os.open(target, os.O_RDWR | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o644)
    with os.fdopen(fd, "r+b") as f:
        size = f.seek(0, os.SEEK_END)
        created = size == 0
        if size:
            f.seek(-1, os.SEEK_END)
            if f.read(1) != b"\n":
                addition = "\n" + addition
        data = addition.encode("utf-8")
        f.write(data)
    verb = "Created note" if created else "Appended to"
    return f"{verb}: {shown} (+{len(data)} bytes)"


def read(path: str) -> tuple[str, str]:
    """(vault-relative path, the note's Markdown)."""
    root, target = _resolve(path, note=True)
    shown = _shown(root, target)
    if not target.is_file():
        raise VaultError(f"There is no note at {shown}.")
    if target.stat().st_size > MAX_READ_BYTES:
        raise VaultError(f"{shown} is too large to read aloud.")
    return shown, target.read_text(encoding="utf-8", errors="replace")


def list_folders(path: str) -> list[str]:
    """The names of the visible, real (non-symlink) folders directly inside a
    vault folder, sorted. A missing folder has none."""
    root, target = _resolve(path, note=False)
    if not target.is_dir():
        return []
    names = []
    with os.scandir(target) as entries:
        for entry in entries:
            if entry.name.startswith(".") or entry.is_symlink():
                continue
            if entry.is_dir(follow_symlinks=False):
                names.append(entry.name)
    return sorted(names)


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------
#
# A plain lexical walk: no index, no database, nothing written. Every note
# under the scope folder is scored on its title (the filename), its folders
# and its body, and the best come back with a line of context each. Bounded
# in every direction so a huge vault costs a slow answer, never a hung JARVIS.

SEARCH_DEFAULT_LIMIT = 8
SEARCH_MAX_LIMIT = 20
MAX_QUERY_CHARS = 200
MAX_QUERY_TERMS = 8
# Entries looked at (any file, Markdown or not) before the walk stops.
SEARCH_MAX_ENTRIES = 20_000
# Notes whose body is read, and the bytes read across all of them.
SEARCH_MAX_NOTES = 5_000
SEARCH_MAX_TOTAL_BYTES = 50_000_000
# A note bigger than this is still found by its title and folders; its body
# is not read.
SEARCH_MAX_NOTE_BYTES = 512_000
EXCERPT_CHARS = 110

_WORD = re.compile(r"\w+")
# Words that say what the user wants done, not what the note is about:
# "what did I save about JARVIS runtime" is a search for "jarvis runtime".
_STOPWORDS = frozenset("""
a an and are about any anything as at be by can could did do does for from
had has have how i in into is it its me my of on or our over so tell that the
their them there these this those to under was we were what when where which
who why will with you your
find look search show saved save stored store wrote written note notes vault
obsidian
""".split())

# The weights, strongest first. A title is what a note is ABOUT; a body
# mention can be incidental, so no number of body hits on their own reaches
# what a title containing the whole query earns.
_W_TITLE_EXACT = 100     # the title IS the query
_W_TITLE_PHRASE = 60     # the query appears in the title, in order
_W_TITLE_ALL = 40        # every term is in the title, any order
_W_TITLE_TERM = 15       # per term in the title
_W_FOLDER_TERM = 8       # per term in a folder name
_W_BODY_PHRASE = 20      # the query appears in the body, in order
_W_BODY_TERM = 5         # per term in the body
_W_BODY_REPEAT_MAX = 4   # extra, at most, for a term said again and again
_W_ALL_TERMS = 10        # every term appears somewhere in the note


@dataclass(frozen=True)
class SearchHit:
    path: str       # vault-relative, with `.md`
    title: str      # the filename without `.md`
    score: int
    excerpt: str    # one line of context, never the note


@dataclass(frozen=True)
class SearchResult:
    hits: list[SearchHit]
    scanned: int        # notes considered
    truncated: bool     # a bound stopped the walk before the vault ended


def _tokens(text: str) -> list[str]:
    return [t.casefold() for t in _WORD.findall(text)]


def _query_terms(query: str) -> list[str]:
    text = " ".join(str(query or "").split())
    if len(text) > MAX_QUERY_CHARS:
        raise VaultError("That search is too long.")
    raw = _tokens(text)
    terms = [t for t in raw if t not in _STOPWORDS] or raw
    terms = list(dict.fromkeys(terms))[:MAX_QUERY_TERMS]
    if not terms:
        raise VaultError("What should I look for in the vault?")
    return terms


def _matches(term: str, token: str) -> bool:
    # A term of four letters or more also finds its longer forms:
    # "runtime" finds "runtimes", "decision" finds "decisions".
    return token == term or (len(term) >= 4 and token.startswith(term))


def _has_phrase(terms: list[str], tokens: list[str]) -> bool:
    if len(terms) < 2:
        return False
    return f" {' '.join(terms)} " in f" {' '.join(tokens)} "


def _score(terms, title_tokens, folder_tokens, body_tokens) -> int:
    score = 0
    if title_tokens == terms:
        score += _W_TITLE_EXACT
    elif _has_phrase(terms, title_tokens):
        score += _W_TITLE_PHRASE
    in_title = [t for t in terms if any(_matches(t, w) for w in title_tokens)]
    if len(terms) > 1 and len(in_title) == len(terms):
        score += _W_TITLE_ALL
    score += _W_TITLE_TERM * len(in_title)

    in_folders = [t for t in terms if any(_matches(t, w) for w in folder_tokens)]
    score += _W_FOLDER_TERM * len(in_folders)

    counts = Counter(body_tokens)
    in_body = []
    for term in terms:
        n = sum(c for w, c in counts.items() if _matches(term, w))
        if n:
            in_body.append(term)
            score += _W_BODY_TERM + min(n - 1, _W_BODY_REPEAT_MAX)
    if _has_phrase(terms, body_tokens):
        score += _W_BODY_PHRASE

    found = set(in_title) | set(in_folders) | set(in_body)
    if not found:
        return 0
    if len(terms) > 1 and len(found) == len(terms):
        score += _W_ALL_TERMS
    return score


def _excerpt(terms: list[str], body: str) -> str:
    """The body line that best matches, cut down around its first match; or
    the note's first line of text when only the title matched."""
    best, best_key = None, None
    first_text = None
    lines = body.splitlines()
    # YAML frontmatter is searched, but is not the note's first line of text.
    front_end = -1
    if lines and lines[0].strip() == "---":
        front_end = next((i for i, ln in enumerate(lines[1:], 1)
                          if ln.strip() == "---"), -1)
    for i, line in enumerate(lines):
        stripped = line.strip()
        if not stripped or stripped == "---":
            continue
        in_front = i <= front_end
        if first_text is None and not in_front:
            first_text = stripped
        tokens = _tokens(stripped)
        hit = sum(1 for t in terms if any(_matches(t, w) for w in tokens))
        if not hit:
            continue
        key = (_has_phrase(terms, tokens), hit)
        if best_key is None or key > best_key:
            best, best_key = stripped, key
    line = best or first_text or ""
    line = " ".join(line.split())
    if len(line) <= EXCERPT_CHARS:
        return line
    start = 0
    if best:
        lowered = [m for m in (re.search(re.escape(t), line, re.IGNORECASE)
                               for t in terms) if m]
        if lowered:
            start = max(0, min(m.start() for m in lowered) - EXCERPT_CHARS // 4)
    piece = line[start:start + EXCERPT_CHARS].strip()
    return ("…" if start else "") + piece + ("…" if start + EXCERPT_CHARS < len(line) else "")


def search(query: str, path: str = "", limit: int = SEARCH_DEFAULT_LIMIT) -> SearchResult:
    """The vault's Markdown notes that best match `query`, best first.

    `path` narrows the search to one folder inside the vault, checked exactly
    as every other path is. Ties are broken by path, so the same vault and the
    same query always give the same answer. Nothing is written.
    """
    terms = _query_terms(query)
    try:
        limit = int(limit)
    except (TypeError, ValueError):
        limit = SEARCH_DEFAULT_LIMIT
    limit = max(1, min(limit, SEARCH_MAX_LIMIT))

    if (path or "").strip() in ("", ".", "/"):
        root = start = vault_root()
    else:
        root, start = _resolve(path, note=False)
        if not start.is_dir():
            raise VaultError(f"There is no folder at {_shown(root, start)}.")

    scored: list[SearchHit] = []
    entries = notes = total_bytes = 0
    truncated = False
    # followlinks=False: a symlinked folder is listed but never entered.
    for dirpath, dirnames, filenames in os.walk(start, followlinks=False):
        dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
        for name in sorted(filenames):
            entries += 1
            if entries > SEARCH_MAX_ENTRIES or notes >= SEARCH_MAX_NOTES:
                truncated = True
                break
            if name.startswith(".") or not name.lower().endswith(NOTE_SUFFIX):
                continue
            if name.lower() == NOTE_SUFFIX:
                continue
            candidate = Path(dirpath) / name
            # A symlinked note is followed only to somewhere the vault rules
            # would let a path go anyway.
            try:
                real = Path(os.path.realpath(candidate))
                _check_real(root, real)
                if not real.is_file():
                    continue
                size = real.stat().st_size
            except (VaultError, OSError):
                continue
            notes += 1

            body = ""
            if size <= SEARCH_MAX_NOTE_BYTES and total_bytes + size <= SEARCH_MAX_TOTAL_BYTES:
                try:
                    body = real.read_text(encoding="utf-8", errors="replace")
                    total_bytes += size
                except OSError:
                    body = ""
            elif size <= SEARCH_MAX_NOTE_BYTES:
                truncated = True     # out of byte budget: title and folders only

            shown = _shown(root, candidate)
            title = name[: -len(NOTE_SUFFIX)]
            folders = shown.split("/")[:-1]
            score = _score(terms, _tokens(title), _tokens(" ".join(folders)),
                           _tokens(body))
            if score:
                scored.append(SearchHit(shown, title, score, _excerpt(terms, body)))
        if truncated and (entries > SEARCH_MAX_ENTRIES or notes >= SEARCH_MAX_NOTES):
            break

    scored.sort(key=lambda h: (-h.score, h.path.casefold(), h.path))
    return SearchResult(scored[:limit], notes, truncated)
