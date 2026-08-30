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

    def test_list_dir_sorted_dirs_first_hidden_shown(self):
        self.assertEqual(wormhole.list_dir("", self.d), [
            {"name": "sub", "is_dir": True},
            {"name": ".hidden", "is_dir": False},
            {"name": "a.txt", "is_dir": False},
            {"name": "b.TXT", "is_dir": False},
            {"name": "c.png", "is_dir": False},
        ])

    def test_list_files_suffix_filter_case_insensitive(self):
        self.assertEqual(wormhole.list_files(None, self.d, (".txt",)),
                         ["a.txt", "b.TXT"])
        self.assertEqual(wormhole.list_files(None, self.d),
                         [".hidden", "a.txt", "b.TXT", "c.png"])

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
                               return_value=b"./\n../\nsub/\nz.txt\n.hidden\na b.png\n") as run:
            entries = wormhole.list_dir("box", "/data/my dir")
        run.assert_called_once_with(SSH_PREFIX + ["ls -1pa -- '/data/my dir'"], None)
        self.assertEqual(entries, [
            {"name": "sub", "is_dir": True},
            {"name": ".hidden", "is_dir": False},
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


class ResolveHost(unittest.TestCase):
    """<name>.local is redirected to ~/secrets/<name>/i.txt when present."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        patcher = mock.patch.object(wormhole, "_SECRETS_DIR", self._tmp.name)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _write_hint(self, name, contents):
        d = os.path.join(self._tmp.name, name)
        os.mkdir(d)
        with open(os.path.join(d, "i.txt"), "w") as f:
            f.write(contents)

    def test_local_with_hint_resolves_to_ip(self):
        self._write_hint("box", "192.168.50.86\n")
        self.assertEqual(wormhole.resolve_host("box.local"), "192.168.50.86")

    def test_local_without_hint_unchanged(self):
        self.assertEqual(wormhole.resolve_host("box.local"), "box.local")

    def test_empty_hint_falls_back(self):
        self._write_hint("box", "\n")
        self.assertEqual(wormhole.resolve_host("box.local"), "box.local")

    def test_multiline_hint_uses_first_line(self):
        self._write_hint("box", "192.168.50.86\n# comment\n")
        self.assertEqual(wormhole.resolve_host("box.local"), "192.168.50.86")

    def test_non_local_host_untouched(self):
        self._write_hint("box", "192.168.50.86\n")
        self.assertEqual(wormhole.resolve_host("box"), "box")
        self.assertEqual(wormhole.resolve_host("10.0.0.5"), "10.0.0.5")

    def test_empty_and_none_host_untouched(self):
        self.assertEqual(wormhole.resolve_host(""), "")
        self.assertIsNone(wormhole.resolve_host(None))

    def test_bare_suffix_untouched(self):
        self.assertEqual(wormhole.resolve_host(".local"), ".local")

    def test_ssh_argv_carries_resolved_ip(self):
        self._write_hint("box", "192.168.50.86\n")
        with mock.patch.object(wormhole, "_run", return_value=b"/home/andrew\n") as run:
            wormhole.home("box.local")
        run.assert_called_once_with(
            ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5",
             "--", "192.168.50.86", "pwd"], None)


class Cli(unittest.TestCase):
    """`wormhole resolve` prints the resolved host and exits 0."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        patcher = mock.patch.object(wormhole, "_SECRETS_DIR", self._tmp.name)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _run_cli(self, argv):
        import io
        from contextlib import redirect_stdout
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = wormhole.main(argv)
        return rc, buf.getvalue().strip()

    def test_resolve_with_hint_prints_ip(self):
        d = os.path.join(self._tmp.name, "box")
        os.mkdir(d)
        with open(os.path.join(d, "i.txt"), "w") as f:
            f.write("192.168.50.86\n")
        self.assertEqual(self._run_cli(["resolve", "box.local"]), (0, "192.168.50.86"))

    def test_resolve_without_hint_echoes_host(self):
        self.assertEqual(self._run_cli(["resolve", "box.local"]), (0, "box.local"))

    def test_no_subcommand_errors(self):
        with self.assertRaises(SystemExit):
            wormhole.main([])


if __name__ == "__main__":
    unittest.main()


class SelfHostIsLocal(unittest.TestCase):
    """Naming this machine must read local disk, not ssh out and back in.

    The round trip is pointless (same filesystem) and it fails with
    "Permission denied (publickey,...)" whenever the machine's own key is not
    usable from the calling process -- which is how a file on local disk ends
    up reported as an auth error.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.d = self._tmp.name
        self.addCleanup(self._tmp.cleanup)
        with open(os.path.join(self.d, "a.txt"), "wb") as f:
            f.write(b"LOCAL")

    def test_own_hostname_and_mdns_name_read_locally(self):
        with mock.patch.object(wormhole.socket, "gethostname",
                               return_value="jetson-orin-agx"):
            with mock.patch.object(wormhole, "_run") as run:
                for host in ("jetson-orin-agx", "jetson-orin-agx.local",
                             "JETSON-ORIN-AGX.local", " jetson-orin-agx.local ",
                             "localhost", "127.0.0.1", "::1", "", None):
                    data = wormhole.read_file(host, os.path.join(self.d, "a.txt"))
                    self.assertEqual(data, b"LOCAL", host)
                run.assert_not_called()

    def test_own_lan_ip_from_secrets_reads_locally(self):
        # ~/secrets/<name>/i.txt maps our mDNS name to our LAN IP, so a UI that
        # stored the IP must be recognised as us too.
        secrets = tempfile.TemporaryDirectory()
        self.addCleanup(secrets.cleanup)
        os.mkdir(os.path.join(secrets.name, "jetson-orin-agx"))
        with open(os.path.join(secrets.name, "jetson-orin-agx", "i.txt"), "w") as f:
            f.write("192.168.50.56\n")
        with mock.patch.object(wormhole, "_SECRETS_DIR", secrets.name), \
             mock.patch.object(wormhole.socket, "gethostname",
                               return_value="jetson-orin-agx"), \
             mock.patch.object(wormhole, "_run") as run:
            data = wormhole.read_file("192.168.50.56",
                                      os.path.join(self.d, "a.txt"))
            self.assertEqual(data, b"LOCAL")
            run.assert_not_called()

    def test_other_hosts_still_go_over_ssh(self):
        with mock.patch.object(wormhole.socket, "gethostname",
                               return_value="jetson-orin-agx"):
            with mock.patch.object(wormhole, "_run",
                                   return_value=b"REMOTE") as run:
                self.assertEqual(wormhole.read_file("ats.local", "/x/a.txt"),
                                 b"REMOTE")
                run.assert_called_once()
                # A name that merely contains ours is not ours.
                run.reset_mock()
                wormhole.read_file("jetson-orin-agx-backup.local", "/x/a.txt")
                run.assert_called_once()

    def test_write_and_delete_also_stay_local(self):
        target = os.path.join(self.d, "sub", "new.txt")
        with mock.patch.object(wormhole.socket, "gethostname",
                               return_value="jetson-orin-agx"):
            with mock.patch.object(wormhole, "_run") as run:
                wormhole.write_file("jetson-orin-agx.local", target, b"W")
                self.assertEqual(open(target, "rb").read(), b"W")
                wormhole.delete_file("jetson-orin-agx.local", target)
                self.assertFalse(os.path.exists(target))
                run.assert_not_called()

    def test_unresolvable_hostname_does_not_break_dispatch(self):
        with mock.patch.object(wormhole.socket, "gethostname",
                               side_effect=OSError("no hostname")):
            with mock.patch.object(wormhole, "_run",
                                   return_value=b"REMOTE") as run:
                self.assertEqual(wormhole.read_file("ats.local", "/x/a.txt"),
                                 b"REMOTE")
                run.assert_called_once()
                self.assertEqual(
                    wormhole.read_file("localhost", os.path.join(self.d, "a.txt")),
                    b"LOCAL")
