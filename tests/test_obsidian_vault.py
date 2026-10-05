import pytest

import obsidian_vault as ov


@pytest.fixture
def vault(monkeypatch, tmp_path):
    root = tmp_path / "Vault"
    (root / ".obsidian").mkdir(parents=True)
    monkeypatch.setenv("OBSIDIAN_VAULT_PATH", str(root))
    return root


def test_create_folder_nested_and_idempotent(vault):
    assert "Created folder" in ov.create_folder("01 Projects/JARVIS/Test")
    assert (vault / "01 Projects/JARVIS/Test").is_dir()
    assert "already exists" in ov.create_folder("01 Projects/JARVIS/Test")


def test_create_note_adds_md_makes_parents_and_never_overwrites(vault):
    ov.create_note("Inbox/New/Idea", "first")
    note = vault / "Inbox/New/Idea.md"
    assert note.read_text() == "first\n"
    with pytest.raises(ov.VaultError, match="already exists"):
        ov.create_note("Inbox/New/Idea.md", "second")
    assert note.read_text() == "first\n"


def test_append_creates_then_appends_cleanly(vault):
    assert "Created note" in ov.append("Journal/Today", "one")
    note = vault / "Journal/Today.md"
    note.write_text("one")                      # no trailing newline
    assert "Appended to" in ov.append("Journal/Today", "two")
    assert note.read_text() == "one\ntwo\n"


def test_read_returns_markdown(vault):
    ov.create_note("A/B", "# Title\nbody")
    shown, text = ov.read("A/B")
    assert shown == "A/B.md" and text == "# Title\nbody\n"


@pytest.mark.parametrize("bad", [
    "../escape", "a/../../escape", "/etc/passwd", "~/x", ".obsidian/app",
    "a/.obsidian/x", "", "a\x00b", "..",
])
def test_path_traversal_and_foreign_paths_are_refused(vault, bad):
    for call in (lambda: ov.create_note(bad, "x"), lambda: ov.append(bad, "x"),
                 lambda: ov.read(bad), lambda: ov.create_folder(bad)):
        with pytest.raises(ov.VaultError):
            call()
    assert sorted(p.name for p in vault.parent.iterdir()) == ["Vault"]
    assert list((vault / ".obsidian").iterdir()) == []


def test_non_markdown_note_is_refused(vault):
    for call in (ov.create_note, ov.append):
        with pytest.raises(ov.VaultError, match="Markdown"):
            call("notes.txt", "x")
    assert not (vault / "notes.txt").exists()


def test_symlink_escape_is_refused(vault, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (vault / "link").symlink_to(outside)
    with pytest.raises(ov.VaultError, match="outside"):
        ov.create_note("link/pwn", "x")
    assert list(outside.iterdir()) == []


def test_symlink_into_obsidian_dir_is_refused(vault):
    (vault / "sneaky").symlink_to(vault / ".obsidian")
    with pytest.raises(ov.VaultError):
        ov.create_note("sneaky/app", "x")
    assert list((vault / ".obsidian").iterdir()) == []


def test_unset_vault_says_so(monkeypatch):
    monkeypatch.delenv("OBSIDIAN_VAULT_PATH", raising=False)
    with pytest.raises(ov.VaultError, match="No Obsidian vault"):
        ov.read("x")


def test_server_gates_and_taint(vault, monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_DATA_DIR", str(tmp_path / "data"))
    import importlib
    import server
    importlib.reload(server)
    for t in ("obsidian_create_folder", "obsidian_create_note", "obsidian_append"):
        assert t in server.ACTING_TOOLS and t in server.TOOL_HANDLERS
    assert "obsidian_read" in server.TAINTING_TOOLS
    assert "obsidian_read" not in server.ACTING_TOOLS
    server.tool_obsidian_create_note({"path": "N", "content": "hi"})
    out = server.tool_obsidian_read({"path": "N"})
    assert 'untrusted="true"' in out and "hi" in out
    assert "outside" in server.tool_obsidian_read({"path": "../x"}) or \
        "climb" in server.tool_obsidian_read({"path": "../x"})


def test_list_folders_is_visible_real_folders_only(vault, tmp_path):
    for name in ("B", "a", ".hidden"):
        (vault / "P" / name).mkdir(parents=True)
    (vault / "P/file.md").write_text("x")
    (vault / "P/link").symlink_to(tmp_path)
    assert ov.list_folders("P") == ["B", "a"]
    assert ov.list_folders("Missing") == []
    for bad in ("../", ".obsidian", "/etc"):
        with pytest.raises(ov.VaultError):
            ov.list_folders(bad)
