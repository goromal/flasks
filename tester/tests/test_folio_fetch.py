import os
import tempfile
import unittest
from unittest import mock

import folio_fetch
import wormhole


class FetchDb(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.cache = os.path.join(self._tmp.name, "folio_cache")

    def test_fetch_writes_and_reuses_path(self):
        with mock.patch.object(wormhole, "read_file", return_value=b"DBYTES") as rf:
            p1 = folio_fetch.fetch_db("box", "/home/a/folio.db", self.cache)
            p2 = folio_fetch.fetch_db("box", "/home/a/folio.db", self.cache)
        self.assertEqual(p1, p2)
        self.assertTrue(os.path.isfile(p1))
        with open(p1, "rb") as f:
            self.assertEqual(f.read(), b"DBYTES")
        self.assertEqual(rf.call_args_list[0][0], ("box", "/home/a/folio.db"))

    def test_distinct_inputs_distinct_paths(self):
        with mock.patch.object(wormhole, "read_file", return_value=b"X"):
            a = folio_fetch.fetch_db("box", "/p/folio.db", self.cache)
            b = folio_fetch.fetch_db("", "/p/folio.db", self.cache)
        self.assertNotEqual(a, b)

    def test_error_propagates(self):
        with mock.patch.object(wormhole, "read_file",
                               side_effect=wormhole.WormholeError("nope")):
            with self.assertRaises(wormhole.WormholeError):
                folio_fetch.fetch_db("box", "/p/folio.db", self.cache)


if __name__ == "__main__":
    unittest.main()
