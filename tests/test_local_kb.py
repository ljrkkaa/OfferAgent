import os
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path

from khoj.utils.local_kb import (
    LocalKBError,
    append_local_kb_note,
    kb_grep,
    kb_headings,
    kb_list,
    kb_read,
    kb_resolve_link,
    load_local_kb_profile_references,
    propose_local_kb_edit,
    resolve_local_kb_path,
)


class LocalKBTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    @contextmanager
    def env(self, **values):
        keys = {
            "KHOJ_LOCAL_KB_PATH",
            "KHOJ_OBSIDIAN_VAULT_PATH",
            "KHOJ_LOCAL_KB_PROFILE_MAX_FILES",
            "KHOJ_LOCAL_KB_PROFILE_MAX_CHARS",
            "KHOJ_LOCAL_VAULT_MAX_FILES",
            "KHOJ_LOCAL_VAULT_MAX_CHARS",
            "KHOJ_ALLOW_VAULT_WRITE",
        }
        old_values = {key: os.environ.get(key) for key in keys}
        try:
            for key in keys:
                os.environ.pop(key, None)
            os.environ.update({key: value for key, value in values.items() if value is not None})
            yield
        finally:
            for key, value in old_values.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

    def write(self, relpath, text):
        path = self.root / relpath
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def test_local_kb_path_takes_priority_over_obsidian_vault_path(self):
        other = tempfile.TemporaryDirectory()
        self.addCleanup(other.cleanup)
        Path(other.name, "only_obsidian.md").write_text("obsidian", encoding="utf-8")
        self.write("local.md", "local")

        with self.env(KHOJ_LOCAL_KB_PATH=str(self.root), KHOJ_OBSIDIAN_VAULT_PATH=other.name):
            result = kb_list(limit=100)
            files = [item["path"] for item in result.items if item["type"] == "file"]

        self.assertEqual(files, ["local.md"])
        self.assertEqual(result.total, 1)

    def test_profile_references_read_agents_indexes_and_entry_links(self):
        self.write("agents.md", "agent instructions")
        self.write("index.md", "home [[interview/java]] [project](projects/index.md)")
        self.write("interview/index.md", "interview home")
        self.write("interview/java.md", "java notes")
        self.write("projects/index.md", "project home")

        with self.env(KHOJ_LOCAL_KB_PATH=str(self.root)):
            references = load_local_kb_profile_references(max_files=10)

        files = [item["file"] for item in references]
        self.assertEqual(files[:3], ["agents.md", "index.md", "interview/index.md"])
        self.assertIn("interview/java.md", files)
        self.assertIn("projects/index.md", files)
        self.assertTrue(references[0]["compiled"].startswith("# agents.md"))
        self.assertEqual(references[0]["query"], "local_kb_profile")

    def test_profile_references_ignore_invalid_caps(self):
        self.write("agents.md", "agent instructions")

        with self.env(
            KHOJ_LOCAL_KB_PATH=str(self.root),
            KHOJ_LOCAL_KB_PROFILE_MAX_FILES="abc",
            KHOJ_LOCAL_KB_PROFILE_MAX_CHARS="bad",
        ):
            references = load_local_kb_profile_references(max_files=1, max_chars=12000)

        self.assertEqual(references[0]["file"], "agents.md")

    def test_profile_references_ignore_removed_local_vault_caps(self):
        self.write("agents.md", "agent instructions")
        self.write("index.md", "home")

        with self.env(
            KHOJ_LOCAL_KB_PATH=str(self.root),
            KHOJ_LOCAL_VAULT_MAX_FILES="1",
        ):
            references = load_local_kb_profile_references(max_files=10)

        self.assertEqual([item["file"] for item in references[:2]], ["agents.md", "index.md"])

    def test_read_file_limits_to_80_lines(self):
        self.write("notes.md", "\n".join(f"line {i}" for i in range(1, 101)))

        with self.env(KHOJ_LOCAL_KB_PATH=str(self.root)):
            result = kb_read("notes.md", max_lines=80)

        self.assertEqual(result.path, "notes.md")
        self.assertIn("line 80", result.text)
        self.assertNotIn("line 81", result.text)
        self.assertIn("Truncated after 80 lines", result.text)

    def test_root_jail_blocks_escape_and_hidden_paths(self):
        self.write(".secret/hidden.md", "nope")

        with self.env(KHOJ_LOCAL_KB_PATH=str(self.root)):
            with self.assertRaisesRegex(LocalKBError, "outside"):
                resolve_local_kb_path("../secret.md")
            with self.assertRaisesRegex(LocalKBError, "hidden"):
                resolve_local_kb_path(".secret/hidden.md")

    def test_list_skips_hidden_and_non_text_files(self):
        self.write("a.md", "a")
        self.write("b.txt", "b")
        self.write("c.pdf", "pdf")
        self.write(".hidden/d.md", "hidden")

        with self.env(KHOJ_LOCAL_KB_PATH=str(self.root)):
            result = kb_list(pattern="*.md", limit=100)
            files = [item["path"] for item in result.items if item["type"] == "file"]

        self.assertEqual(files, ["a.md"])
        self.assertEqual(result.total, 1)

    def test_directory_scans_skip_symlinks_outside_root(self):
        outside = tempfile.TemporaryDirectory()
        self.addCleanup(outside.cleanup)
        outside_file = Path(outside.name, "secret.md")
        outside_file.write_text("outside", encoding="utf-8")
        try:
            os.symlink(outside_file, self.root / "agents.md")
        except (OSError, NotImplementedError):
            self.skipTest("symlinks are unavailable")

        with self.env(KHOJ_LOCAL_KB_PATH=str(self.root)):
            result = kb_list(limit=100)
            files = [item["path"] for item in result.items if item["type"] == "file"]
            references = load_local_kb_profile_references()

        self.assertEqual(files, [])
        self.assertEqual(result.total, 0)
        self.assertEqual(references, [])

    def test_list_skips_symlink_directories_outside_root(self):
        outside = tempfile.TemporaryDirectory()
        self.addCleanup(outside.cleanup)
        Path(outside.name, "index.md").write_text("outside", encoding="utf-8")
        try:
            os.symlink(outside.name, self.root / "outside")
        except (OSError, NotImplementedError):
            self.skipTest("symlinks are unavailable")

        with self.env(KHOJ_LOCAL_KB_PATH=str(self.root)):
            result = kb_list(limit=100)

        self.assertEqual(result.items, [])
        self.assertEqual(result.total, 0)

    def test_grep_reads_local_files_with_context(self):
        self.write("interview/java.md", "before\nhashmap match\nafter\n")
        self.write("interview/python.md", "no match here\n")

        with self.env(KHOJ_LOCAL_KB_PATH=str(self.root)):
            result = kb_grep(
                "hashmap",
                path_prefix="interview",
                mode="regex",
                before=1,
                after=1,
            )

        self.assertEqual(result.line_count, 1)
        self.assertEqual(result.document_count, 1)
        self.assertEqual(
            result.lines,
            [
                "interview/java.md-1-  before",
                "interview/java.md:2: hashmap match",
                "interview/java.md-3-  after",
            ],
        )

    def test_kb_read_returns_numbered_lines_and_metadata(self):
        self.write("notes.md", "one\ntwo\nthree\nfour\n")

        with self.env(KHOJ_LOCAL_KB_PATH=str(self.root)):
            result = kb_read("notes.md", start_line=2, end_line=3)

        self.assertEqual(result.path, "notes.md")
        self.assertEqual(result.start_line, 2)
        self.assertEqual(result.end_line, 3)
        self.assertEqual(result.total_lines, 4)
        self.assertEqual(result.lines, [{"line": 2, "text": "two"}, {"line": 3, "text": "three"}])
        self.assertEqual(result.text, "2: two\n3: three")
        self.assertTrue(result.checksum.startswith("sha256:"))
        self.assertFalse(result.truncated)
        self.assertGreater(result.mtime, 0)

    def test_kb_read_caps_max_lines(self):
        self.write("notes.md", "\n".join(f"line {i}" for i in range(1, 11)))

        with self.env(KHOJ_LOCAL_KB_PATH=str(self.root)):
            result = kb_read("notes.md", start_line=1, end_line=10, max_lines=3)

        self.assertEqual(result.end_line, 3)
        self.assertTrue(result.truncated)
        self.assertIn("Truncated after 3 lines", result.text)
        self.assertNotIn("4: line 4", result.text)

    def test_kb_headings_parses_markdown_sections(self):
        self.write(
            "guide.md",
            "# Top\nintro\n## A\na\n### A1\na1\n## B\nb\n",
        )

        with self.env(KHOJ_LOCAL_KB_PATH=str(self.root)):
            result = kb_headings("guide.md")

        self.assertEqual(result.total_lines, 8)
        self.assertEqual(
            result.headings,
            [
                {"level": 1, "title": "Top", "start_line": 1, "end_line": 8},
                {"level": 2, "title": "A", "start_line": 3, "end_line": 6},
                {"level": 3, "title": "A1", "start_line": 5, "end_line": 6},
                {"level": 2, "title": "B", "start_line": 7, "end_line": 8},
            ],
        )

    def test_kb_headings_ignores_code_fence_hashes(self):
        self.write("guide.md", "# Real\n```python\n# not heading\n```\n## Next\n")

        with self.env(KHOJ_LOCAL_KB_PATH=str(self.root)):
            result = kb_headings("guide.md")

        self.assertEqual([heading["title"] for heading in result.headings], ["Real", "Next"])

    def test_kb_grep_literal_default_escapes_regex_chars(self):
        self.write("notes.md", "axb\na.b\n")

        with self.env(KHOJ_LOCAL_KB_PATH=str(self.root)):
            result = kb_grep("a.b", before=0, after=0)

        self.assertEqual(result.line_count, 1)
        self.assertEqual(result.matches[0]["line"], 2)
        self.assertEqual(result.lines, ["notes.md:2: a.b"])

    def test_kb_grep_regex_mode_is_explicit(self):
        self.write("notes.md", "axb\na.b\n")

        with self.env(KHOJ_LOCAL_KB_PATH=str(self.root)):
            literal = kb_grep("a.b", before=0, after=0)
            regex = kb_grep("a.b", mode="regex", before=0, after=0)

        self.assertEqual(literal.line_count, 1)
        self.assertEqual(regex.line_count, 2)

    def test_kb_grep_limits_large_result_sets(self):
        self.write("notes.md", "\n".join("needle" for _ in range(10)))

        with self.env(KHOJ_LOCAL_KB_PATH=str(self.root)):
            result = kb_grep("needle", before=0, after=0, max_results=3)

        self.assertEqual(len(result.matches), 3)
        self.assertTrue(result.truncated)

    def test_kb_resolve_link_supports_obsidian_and_markdown_links(self):
        self.write("index.md", "[Java](interview/java.md) [[projects/cache]] [[docs]]")
        self.write("interview/java.md", "java")
        self.write("projects/cache.md", "cache")
        self.write("docs/index.md", "docs")

        with self.env(KHOJ_LOCAL_KB_PATH=str(self.root)):
            markdown = kb_resolve_link("index.md", "[Java](interview/java.md)")
            wiki = kb_resolve_link("index.md", "[[projects/cache]]")
            directory = kb_resolve_link("index.md", "[[docs]]")

        self.assertEqual(markdown.status, "resolved")
        self.assertEqual(markdown.resolved, "interview/java.md")
        self.assertEqual(wiki.resolved, "projects/cache.md")
        self.assertEqual(directory.resolved, "docs/index.md")

    def test_kb_resolve_link_returns_ambiguous_for_duplicate_basenames(self):
        self.write("a/Java.md", "a")
        self.write("b/Java.md", "b")
        self.write("index.md", "[[Java]]")

        with self.env(KHOJ_LOCAL_KB_PATH=str(self.root)):
            result = kb_resolve_link("index.md", "[[Java]]")

        self.assertEqual(result.status, "ambiguous")
        self.assertEqual(result.candidates, ["a/Java.md", "b/Java.md"])

    def test_kb_resolve_link_requires_configured_root(self):
        with self.env():
            with self.assertRaisesRegex(LocalKBError, "not configured"):
                kb_resolve_link("index.md", "[[Java]]")

    def test_append_note_requires_write_flag(self):
        self.write("notes.md", "alpha\n")

        with self.env(KHOJ_LOCAL_KB_PATH=str(self.root)):
            result = append_local_kb_note("notes.md", "beta")

        self.assertFalse(result.changed)
        self.assertEqual(result.status, "disabled")
        self.assertEqual((self.root / "notes.md").read_text(encoding="utf-8"), "alpha\n")

    def test_append_note_writes_with_flag_and_heading(self):
        self.write("notes.md", "# Java\nold\n")

        with self.env(KHOJ_LOCAL_KB_PATH=str(self.root), KHOJ_ALLOW_VAULT_WRITE="true"):
            result = append_local_kb_note("notes.md", "HashMap 扩容要讲清楚。", heading="Java")

        self.assertTrue(result.changed)
        self.assertEqual(result.status, "written")
        self.assertEqual(result.start_line, 4)
        self.assertIn("HashMap 扩容要讲清楚。", (self.root / "notes.md").read_text(encoding="utf-8"))
        self.assertTrue(result.checksum.startswith("sha256:"))

    def test_append_note_creates_missing_parent_directories(self):
        with self.env(KHOJ_LOCAL_KB_PATH=str(self.root), KHOJ_ALLOW_VAULT_WRITE="true"):
            result = append_local_kb_note("daily/2026-07-01.md", "# Daily\nRedis review")

        self.assertTrue(result.changed)
        self.assertEqual(result.status, "written")
        self.assertEqual((self.root / "daily/2026-07-01.md").read_text(encoding="utf-8"), "# Daily\nRedis review\n")

    def test_propose_edit_does_not_modify_file(self):
        self.write("notes.md", "old answer\n")

        with self.env(KHOJ_LOCAL_KB_PATH=str(self.root), KHOJ_ALLOW_VAULT_WRITE="true"):
            result = propose_local_kb_edit("notes.md", "old", "new", reason="test")

        self.assertFalse(result.changed)
        self.assertEqual(result.status, "proposed")
        self.assertIn("-old answer", result.diff)
        self.assertIn("+new answer", result.diff)
        self.assertEqual((self.root / "notes.md").read_text(encoding="utf-8"), "old answer\n")


if __name__ == "__main__":
    unittest.main()
