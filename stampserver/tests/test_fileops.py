import os
import sys

# Make the flat top-level module (fileops.py) importable when running from
# the stampserver/ directory (mirrors tests/test_pad_image.py).
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from fileops import unique_suffixed_name


def _touch(directory, name):
    with open(os.path.join(directory, name), "w") as f:
        f.write("")


def test_basic_suffix(tmp_path):
    d = str(tmp_path)
    assert unique_suffixed_name(d, "clip.mp4", "_trimmed") == "clip_trimmed.mp4"


def test_collision_bumps_counter(tmp_path):
    d = str(tmp_path)
    _touch(d, "clip_trimmed.mp4")
    assert unique_suffixed_name(d, "clip.mp4", "_trimmed") == "clip_trimmed2.mp4"
    _touch(d, "clip_trimmed2.mp4")
    assert unique_suffixed_name(d, "clip.mp4", "_trimmed") == "clip_trimmed3.mp4"


def test_stamp_prefix_preserved(tmp_path):
    d = str(tmp_path)
    assert (
        unique_suffixed_name(d, "stamped.foo.clip.mp4", "_trimmed")
        == "stamped.foo.clip_trimmed.mp4"
    )


def test_stamp_prefix_with_collision(tmp_path):
    d = str(tmp_path)
    _touch(d, "stamped.foo.clip_trimmed.mp4")
    assert (
        unique_suffixed_name(d, "stamped.foo.clip.mp4", "_trimmed")
        == "stamped.foo.clip_trimmed2.mp4"
    )
