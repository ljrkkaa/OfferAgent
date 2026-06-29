from khoj.routers.helpers import load_local_vault_references


def test_load_local_vault_references_reads_agents_first(tmp_path, monkeypatch):
    (tmp_path / "java.md").write_text("hashmap notes", encoding="utf-8")
    (tmp_path / "agents.md").write_text("agent instructions", encoding="utf-8")

    monkeypatch.setenv("KHOJ_OBSIDIAN_VAULT_PATH", str(tmp_path))
    monkeypatch.delenv("KHOJ_LOCAL_VAULT_MAX_FILES", raising=False)
    monkeypatch.delenv("KHOJ_LOCAL_VAULT_MAX_CHARS", raising=False)

    references = load_local_vault_references()

    assert [item["file"] for item in references] == ["agents.md", "java.md"]
    assert references[0]["compiled"].startswith("# agents.md")
