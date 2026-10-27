import hashlib
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

from media2notes import cli, whisper, ytdlp

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures")

INFO = {
    "id": "abc123XYZ_-",
    "extractor": "youtube",
    "extractor_key": "Youtube",
    "title": "A Talk",
    "uploader": "Some Channel",
    "channel": "Some Channel",
    "duration": 120,
    "upload_date": "20260101",
    "webpage_url": "https://www.youtube.com/watch?v=abc123XYZ_-",
    "chapters": [{"start_time": 0.0, "end_time": 120.0, "title": "Intro"}],
    "formats": [{"vcodec": "none", "ext": "m4a"}, {"vcodec": "avc1", "ext": "mp4"}],
    "subtitles": {"en": [{}], "live_chat": [{}]},
    "automatic_captions": {"de": [{}], "en": [{}], "en-orig": [{}]},
    "description": "not kept",
}


class FolderNaming(unittest.TestCase):
    def test_extractor_dash_id(self):
        self.assertEqual(ytdlp.folder_name(INFO), "youtube-abc123XYZ_-")

    def test_unsafe_characters_and_subextractors(self):
        info = {"extractor": "soundcloud:set", "id": "ep/12 ?x"}
        self.assertEqual(ytdlp.folder_name(info), "soundcloud-ep_12_x")

    def test_generic_uses_url_hash(self):
        info = {"extractor": "generic", "id": "default"}
        a = ytdlp.folder_name(info, "https://host/ep1/default.mp3")
        b = ytdlp.folder_name(info, "https://host/ep2/default.mp3")
        self.assertRegex(a, r"^generic-[0-9a-f]{12}$")
        self.assertNotEqual(a, b)
        self.assertEqual(a, ytdlp.folder_name(info, "https://host/ep1/default.mp3"))

    def test_missing_fields(self):
        self.assertEqual(ytdlp.folder_name({}), "unknown-unknown")


class Meta(unittest.TestCase):
    def test_subset(self):
        meta = ytdlp.meta_subset(INFO)
        self.assertNotIn("description", meta)
        self.assertNotIn("formats", meta)
        self.assertEqual(meta["title"], "A Talk")
        self.assertTrue(meta["has_video"])
        self.assertEqual(meta["captions"], {"manual": ["en"], "auto": ["en-orig", "en"]})
        self.assertEqual(ytdlp.pick_captions(meta), ("manual", "en"))

    def test_auto_when_no_manual(self):
        meta = ytdlp.meta_subset(dict(INFO, subtitles={}))
        self.assertEqual(ytdlp.pick_captions(meta), ("auto", "en-orig"))

    def test_none(self):
        meta = ytdlp.meta_subset(dict(INFO, subtitles={}, automatic_captions={"fr": []}))
        self.assertIsNone(ytdlp.pick_captions(meta))

    def test_audio_only(self):
        self.assertFalse(ytdlp.has_video({"formats": [{"vcodec": "none", "ext": "mp3"}]}))
        self.assertFalse(ytdlp.has_video({"ext": "mp3"}))
        self.assertTrue(ytdlp.has_video({"ext": "mp4"}))


class Model(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.d = self._tmp.name
        env = mock.patch.dict(os.environ, {"XDG_CACHE_HOME": os.path.join(self.d, "cache")})
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop(whisper.MODEL_ENV, None)

    def test_explicit_path_and_override(self):
        model = os.path.join(self.d, "abc-ggml-small.en.bin")
        open(model, "w").close()
        self.assertEqual(whisper.resolve_model(model), model)
        with mock.patch.dict(os.environ, {whisper.MODEL_ENV: model}):
            self.assertEqual(whisper.resolve_model("small.en"), model)
        with self.assertRaises(whisper.WhisperError):
            whisper.resolve_model(os.path.join(self.d, "missing.bin"))

    def test_cached_model_is_reused(self):
        cached = os.path.join(whisper.model_dir(), "ggml-small.en.bin")
        os.makedirs(os.path.dirname(cached))
        open(cached, "w").close()
        with mock.patch("media2notes.whisper.download_model") as download:
            self.assertEqual(whisper.resolve_model("small.en"), cached)
        download.assert_not_called()

    def _serve(self, payload):
        src = os.path.join(self.d, "src.bin")
        with open(src, "wb") as f:
            f.write(payload)
        return "file://" + src

    def test_download_verifies_checksum(self):
        payload = b"ggml model bytes"
        url = self._serve(payload)
        sha = hashlib.sha256(payload).hexdigest()
        with mock.patch.dict(whisper.MODELS, {"tiny.en": (url, sha)}):
            path = whisper.resolve_model("tiny.en")
        self.assertEqual(path, os.path.join(whisper.model_dir(), "ggml-tiny.en.bin"))
        with open(path, "rb") as f:
            self.assertEqual(f.read(), payload)

    def test_download_rejects_bad_checksum(self):
        url = self._serve(b"tampered")
        with mock.patch.dict(whisper.MODELS, {"tiny.en": (url, "0" * 64)}):
            with self.assertRaises(whisper.WhisperError) as e:
                whisper.resolve_model("tiny.en")
        self.assertIn("checksum mismatch", str(e.exception))
        self.assertEqual(os.listdir(whisper.model_dir()), [])

    def test_unknown_model_names_path(self):
        with self.assertRaises(whisper.WhisperError) as e:
            whisper.resolve_model("medium.en")
        self.assertIn(os.path.join(whisper.model_dir(), "ggml-medium.en.bin"), str(e.exception))

    def test_estimate(self):
        self.assertAlmostEqual(
            whisper.estimate_minutes(3600, "small.en"), 60 / whisper.REALTIME_FACTOR["small.en"]
        )
        self.assertEqual(whisper.model_label("/nix/store/abc-ggml-small.en.bin"), "small.en")
        self.assertEqual(whisper.model_label("/models/ggml-base.en.bin"), "base.en")
        self.assertEqual(whisper.model_label("small.en"), "small.en")


class Pipeline(unittest.TestCase):
    """Run the CLI end to end with yt-dlp / ffmpeg stubbed out."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = self._tmp.name
        self.calls = []

        def dump_json(url):
            self.calls.append("dump_json")
            return INFO

        def download_captions(url, kind, lang, work):
            self.calls.append(f"captions:{kind}:{lang}")
            path = os.path.join(work, f"subs.{lang}.vtt")
            with open(os.path.join(FIXTURES, "manual.en.vtt")) as src, open(path, "w") as dst:
                dst.write(src.read())
            return path

        def frames_step(url, meta, folder, work):
            self.calls.append("frames")
            open(os.path.join(work, "video.mp4"), "w").close()
            os.makedirs(os.path.join(folder, "frames"))
            open(os.path.join(folder, "frames", "0000.jpg"), "w").close()

        for name, fn in [
            ("media2notes.ytdlp.dump_json", dump_json),
            ("media2notes.ytdlp.download_captions", download_captions),
            ("media2notes.cli.frames_step", frames_step),
        ]:
            patcher = mock.patch(name, side_effect=fn)
            patcher.start()
            self.addCleanup(patcher.stop)

    def run_cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = cli.main(["https://youtu.be/abc123XYZ_-", "--out-root", self.root, *args])
        return code, out.getvalue(), err.getvalue()

    def test_first_run_writes_folder_and_prints_path_last(self):
        code, out, _ = self.run_cli()
        folder = os.path.join(self.root, "youtube-abc123XYZ_-")
        self.assertEqual(code, 0)
        self.assertEqual(out.splitlines()[-1], folder)
        self.assertEqual(
            sorted(os.listdir(folder)), ["frames", "meta.json", "source.txt", "transcript.md"]
        )
        with open(os.path.join(folder, "source.txt")) as f:
            self.assertEqual(f.read(), "captions-manual\n")
        with open(os.path.join(folder, "transcript.md")) as f:
            self.assertTrue(f.read().startswith("## [00:00] Intro\n"))
        with open(os.path.join(folder, "meta.json")) as f:
            self.assertEqual(json.load(f)["id"], "abc123XYZ_-")
        self.assertEqual(self.calls, ["dump_json", "captions:manual:en", "frames"])

    def test_rerun_skips_every_finished_step(self):
        self.run_cli()
        self.calls.clear()
        code, out, err = self.run_cli()
        self.assertEqual(code, 0)
        self.assertEqual(self.calls, [])
        self.assertIn("transcript exists; skipping", err)
        self.assertEqual(out.splitlines()[-1], os.path.join(self.root, "youtube-abc123XYZ_-"))

    def test_rerun_fills_missing_step(self):
        self.run_cli("--frames", "off")
        self.assertEqual(self.calls, ["dump_json", "captions:manual:en"])
        self.calls.clear()
        self.run_cli()
        self.assertEqual(self.calls, ["frames"])

    def test_download_failure_removes_partial_folder(self):
        with mock.patch(
            "media2notes.ytdlp.download_captions",
            side_effect=ytdlp.DownloadError("ERROR: [youtube] This video is DRM protected"),
        ):
            code, out, err = self.run_cli()
        self.assertEqual(code, 1)
        self.assertEqual(out, "")
        self.assertIn("DRM protected", err)
        self.assertFalse(os.path.exists(os.path.join(self.root, "youtube-abc123XYZ_-")))
        with open(os.path.join(self.root, cli.INDEX)) as f:
            self.assertEqual(json.load(f), {})

    def test_metadata_failure_creates_nothing(self):
        with mock.patch(
            "media2notes.ytdlp.dump_json",
            side_effect=ytdlp.DownloadError("ERROR: Unsupported URL: https://x"),
        ):
            code, _, err = self.run_cli()
        self.assertEqual(code, 1)
        self.assertIn("Unsupported URL", err)
        self.assertEqual(os.listdir(self.root), [])

    def test_missing_model_keeps_meta(self):
        with mock.patch.dict(os.environ, {whisper.MODEL_ENV: "/nonexistent/ggml-small.en.bin"}):
            code, out, err = self.run_cli("--force-whisper")
        folder = os.path.join(self.root, "youtube-abc123XYZ_-")
        self.assertEqual(code, 1)
        self.assertIn("points at a missing file", err)
        self.assertEqual(os.listdir(folder), ["meta.json"])

    def test_frames_failure_is_a_warning(self):
        with mock.patch("media2notes.cli.frames_step", side_effect=RuntimeError("boom")):
            code, out, err = self.run_cli()
        folder = os.path.join(self.root, "youtube-abc123XYZ_-")
        self.assertEqual(code, 0)
        self.assertIn("frames step failed", err)
        self.assertEqual(sorted(os.listdir(folder)), ["meta.json", "source.txt", "transcript.md"])


if __name__ == "__main__":
    unittest.main()
