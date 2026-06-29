import os
import re
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path

from khoj.utils.local_kb import (
    LocalKBError,
    grep_local_kb_files,
    list_local_kb_files,
    load_local_kb_profile_references,
    read_local_kb_file,
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
            files, total = list_local_kb_files()

        self.assertEqual(files, ["local.md"])
        self.assertEqual(total, 1)

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

    def test_read_file_limits_to_80_lines(self):
        self.write("notes.md", "\n".join(f"line {i}" for i in range(1, 101)))

        with self.env(KHOJ_LOCAL_KB_PATH=str(self.root)):
            result = read_local_kb_file("notes.md")

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
            files, total = list_local_kb_files(pattern="*.md")

        self.assertEqual(files, ["a.md"])
        self.assertEqual(total, 1)

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
            files, total = list_local_kb_files()
            references = load_local_kb_profile_references()

        self.assertEqual(files, [])
        self.assertEqual(total, 0)
        self.assertEqual(references, [])

    def test_grep_reads_local_files_with_context(self):
        self.write("interview/java.md", "before\nhashmap match\nafter\n")
        self.write("interview/python.md", "no match here\n")

        with self.env(KHOJ_LOCAL_KB_PATH=str(self.root)):
            result = grep_local_kb_files(
                re.compile("hashmap", re.IGNORECASE | re.MULTILINE),
                path_prefix="interview",
                lines_before=1,
                lines_after=1,
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


if __name__ == "__main__":
    unittest.main()
