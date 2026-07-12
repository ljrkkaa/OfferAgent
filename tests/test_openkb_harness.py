import json
import os

import pytest

from khoj.utils.openkb import (
    OpenKBError,
    read_openkb_page_range,
    wiki_search_documents,
)


def _ready_openkb(root):
    wiki = root / "wiki"
    wiki.mkdir(parents=True)
    (root / "manifest.json").write_text(json.dumps({"status": "ready"}), encoding="utf-8")
    return wiki


@pytest.mark.asyncio
async def test_wiki_search_disabled(monkeypatch, tmp_path):
    monkeypatch.setenv("KHOJ_OPENKB_ROOT", str(tmp_path))
    monkeypatch.delenv("KHOJ_ENABLE_OPENKB", raising=False)

    refs, queries, query = await wiki_search_documents("Redis", 5, object(), [], "c1")

    assert refs == []
    assert queries == []
    assert query == "Redis"


@pytest.mark.asyncio
async def test_wiki_search_reads_compiled_wiki(monkeypatch, tmp_path):
    wiki = _ready_openkb(tmp_path)
    (wiki / "index.md").write_text("# Index\n- [[concepts/redis-cache]] Redis cache", encoding="utf-8")
    concepts = wiki / "concepts"
    concepts.mkdir()
    (concepts / "redis-cache.md").write_text("# Redis Cache\n缓存穿透要用布隆过滤器。", encoding="utf-8")
    summaries = wiki / "summaries"
    summaries.mkdir()
    (summaries / "redis-paper.md").write_text("# Redis Paper\n缓存穿透案例。", encoding="utf-8")
    monkeypatch.setenv("KHOJ_OPENKB_ROOT", str(tmp_path))
    monkeypatch.setenv("KHOJ_ENABLE_OPENKB", "true")

    refs, queries, query = await wiki_search_documents("Redis 缓存穿透", 5, object(), [], "c1")

    assert query == "Redis 缓存穿透"
    assert queries
    assert refs[0]["uri"].startswith("openkb://local/wiki/")
    assert any(ref["wiki_path"] == "concepts/redis-cache.md" for ref in refs)
    assert any("布隆过滤器" in ref["compiled"] for ref in refs)


@pytest.mark.asyncio
async def test_wiki_search_reads_pageindex_range(monkeypatch, tmp_path):
    wiki = _ready_openkb(tmp_path)
    (wiki / "index.md").write_text("# Index\njava-concurrency pageindex", encoding="utf-8")
    summaries = wiki / "summaries"
    summaries.mkdir()
    summaries.joinpath("java-concurrency.md").write_text(
        "---\ndoc_type: pageindex\nfull_text: sources/java-concurrency.json\n---\n# Java Concurrency\nAQS acquire release page 2",
        encoding="utf-8",
    )
    sources = wiki / "sources"
    sources.mkdir()
    sources.joinpath("java-concurrency.json").write_text(
        json.dumps(
            [
                {"page": 1, "content": "intro"},
                {"page": 2, "content": "AQS acquire release content"},
                {"page": 3, "content": "next"},
            ],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("KHOJ_OPENKB_ROOT", str(tmp_path))
    monkeypatch.setenv("KHOJ_ENABLE_OPENKB", "true")

    refs, _, _ = await wiki_search_documents("AQS acquire release page 2", 5, object(), [], "c1")

    assert any(ref["evidence_type"] == "pageindex" for ref in refs)
    assert any(ref["source_pages"] == "2" for ref in refs)
    assert any("[Page 2]" in ref["compiled"] for ref in refs)


@pytest.mark.asyncio
async def test_wiki_search_skips_symlink_escape(monkeypatch, tmp_path):
    wiki = _ready_openkb(tmp_path)
    concepts = wiki / "concepts"
    concepts.mkdir()
    outside = tmp_path.parent / "openkb-secret.md"
    outside.write_text("outside secret token", encoding="utf-8")
    try:
        os.symlink(outside, concepts / "secret.md")
    except OSError:
        pytest.skip("symlinks are unavailable")
    monkeypatch.setenv("KHOJ_OPENKB_ROOT", str(tmp_path))
    monkeypatch.setenv("KHOJ_ENABLE_OPENKB", "true")

    refs, _, _ = await wiki_search_documents("outside secret token", 5, object(), [], "c1")

    assert refs == []


def test_read_openkb_page_range_rejects_path_escape(monkeypatch, tmp_path):
    wiki = _ready_openkb(tmp_path)
    (wiki / "sources").mkdir()
    monkeypatch.setenv("KHOJ_OPENKB_ROOT", str(tmp_path))
    monkeypatch.setenv("KHOJ_ENABLE_OPENKB", "true")

    with pytest.raises(OpenKBError):
        read_openkb_page_range("../secret.json", "1")


def test_read_openkb_page_range_formats_pages(monkeypatch, tmp_path):
    wiki = _ready_openkb(tmp_path)
    sources = wiki / "sources"
    sources.mkdir()
    sources.joinpath("doc.json").write_text(
        json.dumps(
            [
                {"page": 1, "content": "one"},
                {"page": 2, "content": "two", "images": [{"path": "sources/images/doc/p2.png"}]},
            ],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("KHOJ_OPENKB_ROOT", str(tmp_path))
    monkeypatch.setenv("KHOJ_ENABLE_OPENKB", "true")

    result = read_openkb_page_range("doc", "1-2")

    assert "[Page 1]\none" in result
    assert "[Images: sources/images/doc/p2.png]" in result
