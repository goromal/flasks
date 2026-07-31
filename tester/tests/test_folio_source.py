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


if __name__ == "__main__":
    unittest.main()
