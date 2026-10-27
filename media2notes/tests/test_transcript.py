import os
import unittest

from media2notes import transcript

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures")


def fixture(name):
    with open(os.path.join(FIXTURES, name), encoding="utf-8") as f:
        return f.read()


class Stamps(unittest.TestCase):
    def test_format_stamp(self):
        self.assertEqual(transcript.format_stamp(0), "00:00")
        self.assertEqual(transcript.format_stamp(754.9), "12:34")
        self.assertEqual(transcript.format_stamp(3599), "59:59")
        self.assertEqual(transcript.format_stamp(3600), "1:00:00")
        self.assertEqual(transcript.format_stamp(3725), "1:02:05")

    def test_parse_clock(self):
        self.assertEqual(transcript.parse_clock("00:01.500"), 1.5)
        self.assertEqual(transcript.parse_clock("01:02:03.250"), 3723.25)
        self.assertEqual(transcript.parse_clock("00:00:02,000"), 2.0)


class ManualVtt(unittest.TestCase):
    def test_fragments_strip_markup(self):
        frags = transcript.vtt_fragments(fixture("manual.en.vtt"))
        self.assertEqual(
            frags,
            [
                (0.5, "Welcome to the show."),
                (3.2, "Today we talk about"),
                (3.2, "sleep & kids."),
                (7.0, "Let's start with the basics."),
                (12.0, "First, consistency matters."),
                (65.0, "Second, light exposure."),
            ],
        )

    def test_render_merges_about_ten_seconds(self):
        md = transcript.render(transcript.vtt_fragments(fixture("manual.en.vtt")))
        self.assertEqual(
            md,
            "[00:00] Welcome to the show. Today we talk about sleep & kids. "
            "Let's start with the basics.\n"
            "[00:12] First, consistency matters.\n"
            "[01:05] Second, light exposure.\n",
        )

    def test_render_chapter_headings(self):
        chapters = [
            {"start_time": 0.0, "title": "Intro"},
            {"start_time": 60.0, "title": "Light"},
            {"start_time": 500.0, "title": "Never reached"},
        ]
        md = transcript.render(
            transcript.vtt_fragments(fixture("manual.en.vtt")), chapters
        )
        self.assertEqual(
            md.splitlines(),
            [
                "## [00:00] Intro",
                "",
                "[00:00] Welcome to the show. Today we talk about sleep & kids. "
                "Let's start with the basics.",
                "[00:12] First, consistency matters.",
                "",
                "## [01:00] Light",
                "",
                "[01:05] Second, light exposure.",
            ],
        )

    def test_chapter_boundary_splits_line(self):
        frags = [(1.0, "a"), (3.0, "b"), (5.0, "c")]
        md = transcript.render(frags, [{"start_time": 4.0, "title": "Two"}])
        self.assertEqual(md.splitlines(), ["[00:01] a b", "", "## [00:04] Two", "", "[00:05] c"])


class RollingAutoVtt(unittest.TestCase):
    def test_rolling_duplicates_collapse(self):
        frags = transcript.vtt_fragments(fixture("auto.en.vtt"))
        self.assertEqual(
            frags,
            [
                (0.16, "so today we're going to talk"),
                (2.879, "about how memory works"),
                (5.52, "and why sleep matters"),
            ],
        )

    def test_render(self):
        md = transcript.render(transcript.vtt_fragments(fixture("auto.en.vtt")))
        self.assertEqual(
            md,
            "[00:00] so today we're going to talk about how memory works "
            "and why sleep matters\n",
        )


class Whisper(unittest.TestCase):
    def test_segments_drop_noise_and_hour_stamps(self):
        frags = transcript.whisper_fragments(fixture("whisper.json"))
        self.assertEqual(
            frags,
            [
                (0.0, "Welcome back to the podcast."),
                (8.0, "Our guest studies attention."),
                (3601.0, "Thanks for listening."),
            ],
        )
        md = transcript.render(frags)
        self.assertEqual(
            md,
            "[00:00] Welcome back to the podcast. Our guest studies attention.\n"
            "[1:00:01] Thanks for listening.\n",
        )

    def test_empty(self):
        self.assertEqual(transcript.render([]), "")


if __name__ == "__main__":
    unittest.main()
