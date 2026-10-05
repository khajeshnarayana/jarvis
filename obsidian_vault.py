"""Reading and writing Markdown notes in an Obsidian vault, and nothing else.

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
            raise VaultError("Only Markdown notes, sir; that is not a .md file.")
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
