import os
import tempfile
import unittest

from PIL import Image, ImageDraw

from media2notes import frames


def slide(text_pos, color=(255, 255, 255)):
    image = Image.new("RGB", (320, 180), color)
    draw = ImageDraw.Draw(image)
    draw.rectangle([text_pos, 10, text_pos + 60, 170], fill=(0, 0, 0))
    return image


class Hashing(unittest.TestCase):
    def test_identical_and_near_identical_match(self):
        a = slide(20)
        b = slide(20)
        b.putpixel((300, 5), (250, 250, 250))  # tiny noise
        self.assertLessEqual(frames.hamming(frames.dhash(a), frames.dhash(b)), frames.HAMMING_MAX)

    def test_different_slides_differ(self):
        self.assertGreater(
            frames.hamming(frames.dhash(slide(20)), frames.dhash(slide(220))),
            frames.HAMMING_MAX,
        )


class Dedupe(unittest.TestCase):
    def test_drops_near_duplicates_against_all_kept(self):
        candidates = [(0.0, 0b0), (5.0, 0b1), (60.0, 0xFFFF), (61.0, 0xFFFE), (120.0, 0b11)]
        # 5.0 / 120.0 are within 6 bits of 0.0; 61.0 within 1 bit of 60.0
        self.assertEqual(frames.dedupe(candidates), [(0.0, 0b0), (60.0, 0xFFFF)])

    def test_sorted_by_time(self):
        self.assertEqual(
            [t for t, _ in frames.dedupe([(30.0, 0xFF00), (10.0, 0x00FF)])], [10.0, 30.0]
        )


class Spread(unittest.TestCase):
    def test_under_cap_untouched(self):
        self.assertEqual(frames.spread([3.0, 1.0, 2.0], 100, cap=5), [1.0, 2.0, 3.0])

    def test_cap_spreads_over_duration(self):
        # 100 frames clustered in the first 100 s of a 1000 s video, plus 10 late ones
        times = [float(t) for t in range(100)] + [float(t) for t in range(100, 1000, 90)]
        chosen = frames.spread(times, 1000, cap=10)
        self.assertEqual(len(chosen), 10)
        self.assertEqual(len(set(chosen)), 10)
        # every 100 s decile of the video is represented once
        self.assertEqual(sorted({int(t // 100) for t in chosen}), list(range(10)))

    def test_cap_default_is_40(self):
        chosen = frames.spread([float(t) for t in range(500)], 500)
        self.assertEqual(len(chosen), frames.FRAME_CAP)
        self.assertEqual(frames.FRAME_CAP, 40)

    def test_missing_duration(self):
        self.assertEqual(len(frames.spread([float(t) for t in range(50)], None, cap=5)), 5)


class Names(unittest.TestCase):
    def test_zero_padded_seconds(self):
        self.assertEqual(frames.frame_name(754.6, 1800), "0754.jpg")
        self.assertEqual(frames.frame_name(5, 30), "0005.jpg")
        self.assertEqual(frames.frame_name(754, 12000), "00754.jpg")


class SelectFrames(unittest.TestCase):
    def test_pipeline_with_stubbed_ffmpeg(self):
        with tempfile.TemporaryDirectory() as d:
            work = os.path.join(d, "work")
            os.makedirs(work)
            cands = []
            for i, (t, pos) in enumerate([(0.0, 20), (1.0, 20), (60.0, 220), (120.0, 120)]):
                path = os.path.join(work, f"cand_{i:06d}.jpg")
                slide(pos).save(path, "JPEG", quality=95)
                cands.append((t, path))
            original = frames.extract_candidates
            frames.extract_candidates = lambda video, work_dir: cands
            try:
                out = os.path.join(d, "frames")
                count = frames.select_frames("video.mp4", work, out, 130)
            finally:
                frames.extract_candidates = original
            self.assertEqual(count, 3)
            self.assertEqual(sorted(os.listdir(out)), ["0000.jpg", "0060.jpg", "0120.jpg"])


if __name__ == "__main__":
    unittest.main()
