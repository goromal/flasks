import os
import random
import shutil
import subprocess
import tempfile
import unittest

from PIL import Image, ImageDraw

from media2notes import frames
from media2notes.frames import Candidate

STEP = 1.0 / frames.SAMPLE_FPS


def slide(text_pos, color=(255, 255, 255), size=(320, 180)):
    image = Image.new("RGB", size, color)
    draw = ImageDraw.Draw(image)
    draw.rectangle([text_pos, 10, text_pos + 60, size[1] - 10], fill=(0, 0, 0))
    return image


def text_card(size=(320, 180)):
    """A white card with rows of 'text' (dark bars), like a vice/virtue table."""
    image = Image.new("RGB", size, (250, 250, 245))
    draw = ImageDraw.Draw(image)
    for row in range(6):
        y = 20 + row * 25
        for col, x in enumerate((20, 120, 220)):
            draw.rectangle([x, y, x + 60 + 10 * ((row + col) % 3), y + 10], fill=(20, 20, 20))
    return image


def talking_head(t, size=(320, 180)):
    """A moving blob on a gradient, so consecutive samples differ like live video."""
    image = Image.new("RGB", size)
    draw = ImageDraw.Draw(image)
    for x in range(0, size[0], 8):
        shade = int(80 + 60 * ((x + 13 * t) % 64) / 64)
        draw.rectangle([x, 0, x + 8, size[1]], fill=(shade, shade // 2, 40))
    cx = int(160 + 60 * ((t * 7) % 20 - 10) / 10)
    draw.ellipse([cx - 40, 40, cx + 40, 160], fill=(220, 180, 150))
    return image


def drifting(base, n, seed):
    """``n`` hashes of live motion: each differs from ``base`` in 12 of the same 40
    bits, so consecutive samples move (> STILL) but stay near the shot (< DRIFT)."""
    rng = random.Random(seed)
    return [flips(base, rng.sample(range(40), 12)) for _ in range(n)]


def flips(value, bits):
    for b in bits:
        value ^= 1 << b
    return value


A = (1 << 128) - 1
B = ((1 << 256) - 1) ^ A  # 256 bits from A


class Hashing(unittest.TestCase):
    def test_identical_and_near_identical_match(self):
        a = slide(20)
        b = slide(20)
        b.putpixel((300, 5), (250, 250, 250))
        self.assertLessEqual(frames.hamming(frames.dhash(a), frames.dhash(b)), frames.STILL)

    def test_different_slides_differ(self):
        self.assertGreater(
            frames.hamming(frames.dhash(slide(20)), frames.dhash(slide(220))), frames.CUT
        )

    def test_hash_is_256_bits(self):
        self.assertLess(frames.dhash(text_card()), 1 << 256)


class Segmentation(unittest.TestCase):
    def test_hard_cut_splits(self):
        hashes = [A] * 10 + [B] * 10
        self.assertEqual(frames.segments(hashes), [(0, 10), (10, 20)])

    def test_dissolve_split_by_drift(self):
        # 8 steps of 20 bits each: no single step is a cut, but the total drift is
        start = 0
        hashes = [start] * 6
        value = start
        for i in range(8):
            value = flips(value, range(i * 20, i * 20 + 20))
            hashes.append(value)
        hashes += [value] * 6
        segs = frames.segments(hashes)
        self.assertGreater(len(segs), 1)
        self.assertEqual(segs[0], (0, 10))

    def test_hold_detection(self):
        hashes = drifting(A, 6, 1) + [B] * 20 + drifting(A, 6, 2)
        cands = frames.candidates(hashes)
        holds = [c for c in cands if c.hold]
        self.assertEqual(len(holds), 1)
        self.assertTrue(6 * STEP <= holds[0].time <= 26 * STEP)

    def test_short_still_is_not_a_hold(self):
        n_still = int(frames.HOLD_SECONDS / STEP) - 2
        hashes = drifting(A, 8, 3) + [B] * n_still
        self.assertFalse(any(c.hold for c in frames.candidates(hashes)))

    def test_periodic_samples_in_long_segments(self):
        hashes = drifting(A, int(70 / STEP), 4)  # 70 s, never still, never a cut
        cands = frames.candidates(hashes)
        times = [c.time for c in cands]
        for t in (20.0, 40.0, 60.0):
            self.assertIn(t, times)

    def test_steadiest_sample_avoids_dissolve_edges(self):
        # segment that fades in over 3 samples, then is steady
        steady = flips(A, range(30))
        hashes = [flips(steady, range(30 + 10 * k, 40 + 10 * k)) for k in (2, 1, 0)] + [steady] * 3
        self.assertIn(frames.steadiest(hashes, 0, len(hashes)), (3, 4, 5))

    def test_transitions_are_skipped(self):
        # a 0.5 s blip between two holds yields no candidate of its own
        hashes = [A] * 12 + [flips(A, range(100, 170))] + [B] * 12
        cands = frames.candidates(hashes)
        self.assertEqual([c.hold for c in cands], [True, True])


class Dedupe(unittest.TestCase):
    def test_holds_survive_unless_exact_repeat(self):
        h1 = 0
        h2 = flips(0, range(12))  # 12 bits apart: ordinary dup, distinct slide
        cands = [Candidate(10.0, h1, True), Candidate(40.0, h2, True), Candidate(90.0, h1, True)]
        self.assertEqual([c.time for c in frames.dedupe(cands)], [10.0, 40.0])

    def test_ordinary_frames_drop_against_holds_and_each_other(self):
        cands = [
            Candidate(10.0, 0, True),
            Candidate(20.0, flips(0, range(20)), False),  # near the hold
            Candidate(30.0, A, False),
            Candidate(31.0, flips(A, range(5)), False),  # near 30.0
        ]
        self.assertEqual([c.time for c in frames.dedupe(cands)], [10.0, 30.0])


class Choose(unittest.TestCase):
    def test_holds_win_the_cap(self):
        holds = [Candidate(float(t), flips(0, range(t, t + 40)), True) for t in (100, 140)]
        others = [Candidate(float(t), flips(A, range(t % 200, t % 200 + 40)), False) for t in range(0, 200, 3)]
        chosen = frames.choose(holds + others, 200, cap=10)
        self.assertEqual(len(chosen), 10)
        self.assertIn(100.0, chosen)
        self.assertIn(140.0, chosen)

    def test_too_many_holds_are_spread(self):
        holds = [Candidate(float(t), flips(0, range(t, t + 40)), True) for t in range(0, 200, 10)]
        chosen = frames.choose(holds, 200, cap=5)
        self.assertEqual(len(chosen), 5)


class Spread(unittest.TestCase):
    def test_under_cap_untouched(self):
        self.assertEqual(frames.spread([3.0, 1.0, 2.0], 100, cap=5), [1.0, 2.0, 3.0])

    def test_cap_spreads_over_duration(self):
        times = [float(t) for t in range(100)] + [float(t) for t in range(100, 1000, 90)]
        chosen = frames.spread(times, 1000, cap=10)
        self.assertEqual(len(chosen), 10)
        self.assertEqual(sorted({int(t // 100) for t in chosen}), list(range(10)))

    def test_cap_default_is_40(self):
        self.assertEqual(len(frames.spread([float(t) for t in range(500)], 500)), frames.FRAME_CAP)
        self.assertEqual(frames.FRAME_CAP, 40)

    def test_zero_cap_and_missing_duration(self):
        self.assertEqual(frames.spread([1.0, 2.0], 10, cap=0), [])
        self.assertEqual(len(frames.spread([float(t) for t in range(50)], None, cap=5)), 5)


class Names(unittest.TestCase):
    def test_zero_padded_seconds(self):
        self.assertEqual(frames.frame_name(754.6, 1800), "0754.jpg")
        self.assertEqual(frames.frame_name(5, 30), "0005.jpg")
        self.assertEqual(frames.frame_name(754, 12000), "00754.jpg")


@unittest.skipUnless(shutil.which("ffmpeg"), "ffmpeg not on PATH")
class SyntheticVideo(unittest.TestCase):
    """Regression for anixpkgs#730: a static text card entered and left by dissolves."""

    FPS = 10

    def make_video(self, path):
        # 0-20 s talking head, 0.6 s dissolve, 20-50 s text card, dissolve, 50-70 s head
        card = text_card()
        proc = subprocess.Popen(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
             "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", "320x180", "-r", str(self.FPS),
             "-i", "-", "-c:v", "mpeg4", "-q:v", "3", path],
            stdin=subprocess.PIPE,
        )
        dissolve = 6  # frames: 0.6 s, so no single frame crosses a 0.3 scene score
        for n in range(70 * self.FPS):
            t = n / self.FPS
            head = talking_head(t)
            if 20 * self.FPS <= n < 50 * self.FPS:
                k = n - 20 * self.FPS
                image = Image.blend(head, card, min(1.0, (k + 1) / dissolve))
            elif 50 * self.FPS <= n < 50 * self.FPS + dissolve:
                image = Image.blend(card, head, (n - 50 * self.FPS + 1) / dissolve)
            else:
                image = head
            proc.stdin.write(image.tobytes())
        proc.stdin.close()
        self.assertEqual(proc.wait(), 0)

    def test_text_card_is_captured(self):
        with tempfile.TemporaryDirectory() as d:
            video = os.path.join(d, "v.avi")
            self.make_video(video)
            out = os.path.join(d, "frames")
            count = frames.select_frames(video, os.path.join(d, "work"), out, 70)
            names = sorted(os.listdir(out))
            self.assertEqual(count, len(names))
            self.assertLessEqual(count, frames.FRAME_CAP)
            card_hash = frames.dhash(text_card())
            on_card = [n for n in names if 21 <= int(n[:-4]) <= 49]
            self.assertTrue(on_card, names)
            with Image.open(os.path.join(out, on_card[0])) as image:
                self.assertLessEqual(frames.hamming(frames.dhash(image), card_hash), frames.DUP)


if __name__ == "__main__":
    unittest.main()
