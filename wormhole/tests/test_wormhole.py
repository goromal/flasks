import os
import tempfile
import unittest
from unittest import mock

import wormhole
from wormhole import WormholeError

SSH_PREFIX = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5", "--", "box"]


class LocalOps(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.d = self._tmp.name
        self.addCleanup(self._tmp.cleanup)
        os.mkdir(os.path.join(self.d, "sub"))
        with open(os.path.join(self.d, "b.TXT"), "w") as f:
            f.write("B")
        with open(os.path.join(self.d, "a.txt"), "w") as f:
            f.write("A")
        with open(os.path.join(self.d, "c.png"), "w") as f:
            f.write("C")
        with open(os.path.join(self.d, ".hidden"), "w") as f:
            f.write("H")

    def test_list_dir_sorted_dirs_first_hidden_skipped(self):
        self.assertEqual(wormhole.list_dir("", self.d), [
            {"name": "sub", "is_dir": True},
            {"name": "a.txt", "is_dir": False},
            {"name": "b.TXT", "is_dir": False},
            {"name": "c.png", "is_dir": False},
        ])

    def test_list_files_suffix_filter_case_insensitive(self):
        self.assertEqual(wormhole.list_files(None, self.d, (".txt",)),
                         ["a.txt", "b.TXT"])
        self.assertEqual(wormhole.list_files(None, self.d),
                         ["a.txt", "b.TXT", "c.png"])

    def test_read_write_delete_roundtrip_creates_parents(self):
        p = os.path.join(self.d, "new", "deep", "f.bin")
        wormhole.write_file("", p, b"\x00\x01")
        self.assertEqual(wormhole.read_file("", p), b"\x00\x01")
        wormhole.delete_file("", p)
        self.assertFalse(os.path.exists(p))

    def test_read_file_max_bytes(self):
        p = os.path.join(self.d, "a.txt")
        with self.assertRaises(WormholeError):
            wormhole.read_file("", p, max_bytes=0)
        self.assertEqual(wormhole.read_file("", p, max_bytes=1), b"A")

    def test_local_errors_raise_wormhole_error(self):
        with self.assertRaises(WormholeError):
            wormhole.read_file("", os.path.join(self.d, "missing"))
        with self.assertRaises(WormholeError):
            wormhole.list_dir("", os.path.join(self.d, "missing"))
        with self.assertRaises(WormholeError):
            wormhole.delete_file("", os.path.join(self.d, "missing"))

    def test_home_local(self):
        self.assertEqual(wormhole.home(""), os.path.expanduser("~"))
        self.assertEqual(wormhole.home(None), os.path.expanduser("~"))


class RemoteOps(unittest.TestCase):
    """Remote paths never touch a real ssh: _run is mocked and its argv asserted."""

    def test_list_dir_argv_and_parse(self):
        with mock.patch.object(wormhole, "_run",
                               return_value=b"sub/\nz.txt\n.hidden\na b.png\n") as run:
            entries = wormhole.list_dir("box", "/data/my dir")
        run.assert_called_once_with(SSH_PREFIX + ["ls -1p -- '/data/my dir'"], None)
        self.assertEqual(entries, [
            {"name": "sub", "is_dir": True},
            {"name": "a b.png", "is_dir": False},
            {"name": "z.txt", "is_dir": False},
        ])

    def test_read_file_argv(self):
        with mock.patch.object(wormhole, "_run", return_value=b"data") as run:
            self.assertEqual(wormhole.read_file("box", "/p/f.txt"), b"data")
        run.assert_called_once_with(SSH_PREFIX + ["cat -- /p/f.txt"], None)

    def test_write_file_argv_mkdir_and_stdin(self):
        with mock.patch.object(wormhole, "_run", return_value=b"") as run:
            wormhole.write_file("box", "/p/sub/f.txt", b"hello")
        run.assert_called_once_with(
            SSH_PREFIX + ["mkdir -p -- /p/sub && cat > /p/sub/f.txt"], b"hello")

    def test_delete_file_argv(self):
        with mock.patch.object(wormhole, "_run", return_value=b"") as run:
            wormhole.delete_file("box", "/p/f.txt")
        run.assert_called_once_with(SSH_PREFIX + ["rm -- /p/f.txt"], None)

    def test_home_remote(self):
        with mock.patch.object(wormhole, "_run", return_value=b"/home/andrew\n") as run:
            self.assertEqual(wormhole.home("box"), "/home/andrew")
        run.assert_called_once_with(SSH_PREFIX + ["pwd"], None)


class RunHelper(unittest.TestCase):
    def test_nonzero_exit_raises_with_stderr_tail(self):
        with self.assertRaises(WormholeError) as ctx:
            wormhole._run(["sh", "-c", "echo one >&2; echo two >&2; exit 3"])
        self.assertEqual(str(ctx.exception), "two")

    def test_missing_binary_raises(self):
        with self.assertRaises(WormholeError):
            wormhole._run(["definitely-not-a-real-binary-xyz"])


if __name__ == "__main__":
    unittest.main()
