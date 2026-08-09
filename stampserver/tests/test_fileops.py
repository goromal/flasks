import os
import sys

# Make the flat top-level module (fileops.py) importable when running from
# the stampserver/ directory (mirrors tests/test_pad_image.py).
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from fileops import unique_suffixed_name, validate_stamp_name


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


def test_stamp_prefix_with_spaces_preserved(tmp_path):
    d = str(tmp_path)
    assert (
        unique_suffixed_name(d, "stamped.my stamp.clip.mp4", "_trimmed")
        == "stamped.my stamp.clip_trimmed.mp4"
    )


def test_stamp_prefix_with_special_chars_preserved(tmp_path):
    d = str(tmp_path)
    assert (
        unique_suffixed_name(d, "stamped.a & b (2024)!.clip.mp4", "_trimmed")
        == "stamped.a & b (2024)!.clip_trimmed.mp4"
    )


def test_validate_accepts_spaces_and_symbols():
    assert validate_stamp_name("my stamp") == (True, "my stamp")
    assert validate_stamp_name("a & b (2024)! #1") == (True, "a & b (2024)! #1")


def test_validate_strips_surrounding_whitespace():
    assert validate_stamp_name("  spaced  ") == (True, "spaced")


def test_validate_rejects_period():
    ok, msg = validate_stamp_name("St. Louis")
    assert ok is False
    assert "period" in msg


def test_validate_rejects_slash():
    ok, msg = validate_stamp_name("a/b")
    assert ok is False
    assert "slash" in msg


def test_validate_rejects_empty_and_whitespace_only():
    assert validate_stamp_name("")[0] is False
    assert validate_stamp_name("   ")[0] is False


def test_validate_rejects_control_characters():
    ok, msg = validate_stamp_name("line1\nline2")
    assert ok is False
    assert "control" in msg
