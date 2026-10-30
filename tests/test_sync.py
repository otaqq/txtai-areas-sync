import json
import tempfile
import time
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

from txtai_areas_sync import sync


class FakeRag:
    """Records what would be sent to txtai-areas."""

    def __init__(self, url, api_key):
        self.upserts, self.deletes = [], []
        FakeRag.last = self

    def upsert(self, area, docs):
        self.upserts.append((area, [d["id"] for d in docs]))

    def delete(self, area, prefixes):
        self.deletes.append((area, sorted(prefixes)))
        return len(prefixes)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        sync.configure(self.tmp / "data")
        sync.STATE_DIR.mkdir(parents=True)
        sync.TEXT.update({"intro": "(intro)", "source": "Source: {where}"})
        self.log = mock.patch.object(sync, "log").start()
        self.addCleanup(mock.patch.stopall)


class TestSplit(Base):
    def test_markdown_sections(self):
        text = "# Title\n\nintro text\n\n## A\n\nalpha\n\n```\n## not a section\n```\n\n## B\n\nbeta\n"
        parts = dict(sync.split_markdown(text, "docs", "guide.md"))
        self.assertEqual(list(parts), ["(intro)", "A", "B"])
        self.assertIn("## not a section", parts["A"])
        self.assertTrue(parts["A"].startswith("# Title\n\nSource: docs/guide.md\n\n## A"))

    def test_same_heading_twice(self):
        parts = [n for n, _ in sync.split_markdown("# T\n\n## A\n\nx\n\n## A\n\ny\n", "", "t.md")]
        self.assertEqual(parts, ["A", "A (2)"])

    def test_text_words_from_config(self):
        sync.TEXT.update({"intro": "(冒頭)", "source": "（出典: {where}）"})
        parts = dict(sync.split_markdown("# T\n\nfirst\n\n## A\n\nx\n", "s", "t.md"))
        self.assertIn("(冒頭)", parts)
        self.assertIn("（出典: s/t.md）", parts["A"])

    def test_chunks_carry_title_and_section(self):
        body = "\n\n".join("x" * 600 for _ in range(4))
        out = sync.chunks(f"# Title\n\n## Sec\n\n{body}")
        self.assertGreater(len(out), 1)
        self.assertTrue(all(len(c) <= sync.CHUNK + 40 for c in out))
        self.assertTrue(out[1].startswith("Title › Sec\n"))

    def test_pages_without_text_are_left_out(self):
        text = "short\f" + "word " * 30
        self.assertEqual([n for n, _ in sync.split_pages(text, "", "book.pdf")], ["p.2"])


class TestSchedule(Base):
    def at(self, h, m=15):
        return datetime(2026, 10, 30, h, m).timestamp()

    def test_cron_once_per_hour_counted_from_the_start(self):
        cfg = {"source": [{"name": "books", "cron": "0 5-7 * * *"}]}
        sync.mark_runs(["books"], self.at(5))  # a long run from 05:15 that ends at 06:06
        self.assertEqual(sync.due(cfg, self.at(6)), ["books"])
        sync.mark_runs(["books"], self.at(6))
        self.assertEqual(sync.due(cfg, self.at(6, 45)), [])
        self.assertEqual(sync.due(cfg, self.at(8)), [])

    def test_every_and_at(self):
        cfg = {"kinds": {"notes": {"every": "1d", "at": "03"}}, "source": [{"name": "n", "kind": "notes"}]}
        self.assertEqual(sync.due(cfg, self.at(2)), [])
        self.assertEqual(sync.due(cfg, self.at(3)), ["n"])

    def test_cron_weekday(self):
        sat = time.localtime(datetime(2026, 10, 31, 3).timestamp())
        self.assertTrue(sync.cron_hour("0 3 * * 6,0", sat))
        self.assertFalse(sync.cron_hour("0 3 * * 1-5", sat))


class TestFiles(Base):
    def test_one_format_prefers_epub(self):
        files = {"a/book.pdf": 1, "a/book.epub": 2, "a/other.pdf": 3, "a/notes.md": 4}
        self.assertEqual(sorted(sync.one_format(files, ["epub", "pdf"])), ["a/book.epub", "a/notes.md", "a/other.pdf"])

    def test_shelves_map(self):
        root = self.tmp / "shelves"
        for d in ("math.ai.papers", "math.hm.books", "Not An Area"):
            (root / d).mkdir(parents=True)
        m = sync.shelves_map({"name": "s", "path": str(root), "only": "ai", "prefix": "lab"})
        self.assertEqual(list(m), ["lab.math.ai.papers"])

    def test_git_auth(self):
        src = {"token_env": "T", "token_auth": "basic"}
        opt = sync.git_auth(src, {"T": "abc"})
        self.assertTrue(opt[1].startswith("http.extraHeader=Authorization: Basic "))
        self.assertEqual(sync.git_auth({"token_env": "T"}, {}), [])

    def test_local_config_overrides(self):
        (self.tmp / "sources.toml").write_text(
            '[rag]\nurl = "http://a"\n[[source]]\nname = "x"\ntype = "dir"\npath = "/a"\narea = "one"\n')
        (self.tmp / "sources.local.toml").write_text(
            '[text]\nintro = "(冒頭)"\n[[source]]\nname = "x"\ntype = "dir"\npath = "/b"\narea = "two"\n')
        cfg = sync.load_sources(self.tmp / "sources.toml")
        self.assertEqual([s["area"] for s in cfg["source"]], ["two"])
        self.assertEqual(sync.TEXT["intro"], "(冒頭)")


class TestRun(Base):
    def setUp(self):
        super().setUp()
        self.docs = self.tmp / "docs"
        (self.docs / "sub").mkdir(parents=True)
        (self.docs / "a.md").write_text("# A\n\n## One\n\nfirst\n\n## Two\n\nsecond\n")
        (self.docs / "sub" / "b.md").write_text("# B\n\nonly an intro\n")
        self.cfg = {"rag": {"url": "http://rag"},
                    "source": [{"name": "d", "type": "dir", "path": str(self.docs), "area": "notes"}]}
        mock.patch.object(sync, "Rag", FakeRag).start()

    def sync(self):
        self.assertEqual(sync.run(self.cfg, {}, None, False), 0)
        return FakeRag.last

    def test_first_run_sends_every_part(self):
        rag = self.sync()
        sent = [i for _, ids in rag.upserts for i in ids]
        self.assertEqual(sorted(sent), ["d:a.md#One#0", "d:a.md#Two#0", "d:sub/b.md#(intro)#0"])
        state = json.loads(sync.STATE_FILE.read_text())
        self.assertEqual(state["d:a.md#One"]["area"], "notes")

    def test_nothing_changed_sends_nothing(self):
        self.sync()
        rag = self.sync()
        self.assertEqual((rag.upserts, rag.deletes), ([], []))

    def test_changed_section_only(self):
        self.sync()
        (self.docs / "a.md").write_text("# A\n\n## One\n\nfirst\n\n## Two\n\nsecond, changed\n")
        rag = self.sync()
        self.assertEqual(rag.deletes, [("notes", ["d:a.md#Two#"])])
        self.assertEqual(rag.upserts, [("notes", ["d:a.md#Two#0"])])

    def test_removed_file_is_deleted(self):
        self.sync()
        (self.docs / "sub" / "b.md").unlink()
        rag = self.sync()
        self.assertEqual(rag.deletes, [("notes", ["d:sub/b.md#(intro)#"])])
        self.assertNotIn("d:sub/b.md#(intro)", json.loads(sync.STATE_FILE.read_text()))

    def test_dry_run_changes_nothing(self):
        self.assertEqual(sync.run(self.cfg, {}, None, True), 0)
        self.assertEqual(FakeRag.last.upserts, [])
        self.assertFalse(sync.STATE_FILE.exists())

    def test_source_without_area_is_an_error(self):
        del self.cfg["source"][0]["area"]
        self.assertEqual(sync.run(self.cfg, {}, None, False), 1)


if __name__ == "__main__":
    unittest.main()
