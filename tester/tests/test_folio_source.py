import os
import tempfile
import unittest

import folio_source
from tests.fixture import build_fixture_db


class FolioSourceBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.db = os.path.join(self._tmp.name, "folio.db")
        build_fixture_db(self.db)
        self.conn = folio_source.open_ro(self.db)
        self.addCleanup(self.conn.close)


class ListAndResolve(FolioSourceBase):
    def test_open_ro_is_readonly(self):
        with self.assertRaises(Exception):
            self.conn.execute("INSERT INTO tags VALUES (99,'x')")

    def test_list_books(self):
        self.assertEqual(folio_source.list_books(self.conn),
                         [{"id": 1, "title": "The Test Book", "author": "A. Author"}])

    def test_list_chapters_ordered(self):
        chs = folio_source.list_chapters(self.conn, 1)
        self.assertEqual([c["id"] for c in chs], [10, 11])
        self.assertEqual(chs[0]["title"], "Chapter One")

    def test_resolve_single_block(self):
        p = self.conn.execute("SELECT * FROM passages WHERE id=1000").fetchone()
        self.assertEqual(folio_source.resolve_passage_text(self.conn, p), "beta gam")

    def test_resolve_multi_block(self):
        p = self.conn.execute("SELECT * FROM passages WHERE id=1001").fetchone()
        # block101[7:]='block here.' + block102[:5]='Third'
        self.assertEqual(folio_source.resolve_passage_text(self.conn, p),
                         "block here.\n\nThird")


class Gather(FolioSourceBase):
    def test_whole_book(self):
        m = folio_source.gather_study_materials(self.conn, 1)
        self.assertEqual(m["book"]["title"], "The Test Book")
        self.assertEqual({s["body"] for s in m["summaries"]},
                         {"Book summary.", "Ch1 summary."})
        # book note + chapter note (passage note is inline on the passage, not here)
        self.assertEqual({n["body"] for n in m["notes"]},
                         {"Book note.", "Chapter one note."})
        pids = {p["id"] for p in m["passages"]}
        self.assertEqual(pids, {1000, 1001})
        p1000 = next(p for p in m["passages"] if p["id"] == 1000)
        self.assertEqual(p1000["text"], "beta gam")
        self.assertEqual(p1000["tags"], ["theme"])
        self.assertEqual(p1000["note"], "Passage note.")
        self.assertIsNone(m["block_text"])

    def test_chapter_scoped(self):
        m = folio_source.gather_study_materials(self.conn, 1, chapter_ids=[10])
        self.assertEqual({s["body"] for s in m["summaries"]}, {"Ch1 summary."})
        self.assertEqual({n["body"] for n in m["notes"]}, {"Chapter one note."})
        # passage 1000 starts in ch10; 1001 starts in ch10 (block101) -> both in
        self.assertEqual({p["id"] for p in m["passages"]}, {1000, 1001})

    def test_chapter_scoped_ch11(self):
        m = folio_source.gather_study_materials(self.conn, 1, chapter_ids=[11])
        self.assertEqual(m["summaries"], [])
        self.assertEqual(m["notes"], [])
        self.assertEqual(m["passages"], [])  # neither passage STARTS in ch11

    def test_include_block_text(self):
        m = folio_source.gather_study_materials(self.conn, 1, include_block_text=True)
        self.assertIn("Alpha beta gamma delta.", m["block_text"])
        self.assertIn("Fourth and last.", m["block_text"])
        m10 = folio_source.gather_study_materials(
            self.conn, 1, chapter_ids=[10], include_block_text=True)
        self.assertIn("Second block here.", m10["block_text"])
        self.assertNotIn("Fourth and last.", m10["block_text"])


class Compose(unittest.TestCase):
    def test_labeled_sections_and_omission(self):
        materials = {
            "book": {"id": 1, "title": "T", "author": "A"},
            "summaries": [{"scope": "book", "scope_id": 1, "body": "S", "generated_by": "user"}],
            "notes": [{"body": "N"}],
            "passages": [{"id": 9, "text": "quote", "tags": ["t"], "note": "pn", "color": "yellow"}],
            "block_text": None,
        }
        texts = folio_source.compose_source_texts(materials)
        joined = "\n\n".join(texts)
        self.assertIn("Summaries", joined)
        self.assertIn("quote", joined)
        self.assertIn("(tags: t)", joined)
        self.assertNotIn("Chapter text", joined)  # block_text None -> omitted


if __name__ == "__main__":
    unittest.main()
