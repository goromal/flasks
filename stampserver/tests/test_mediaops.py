import os
import subprocess
import sys

import cv2
import pytest
from PIL import Image

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from mediaops import edit_and_stamp


def test_image_edits_and_restamp_keep_original(tmp_path):
    source = tmp_path / 'stamped.old.photo.png'
    Image.new('RGB', (80, 60), 'red').save(source)
    result = edit_and_stamp(str(tmp_path), source.name, ['new'], [
        {'type': 'whiteout', 'x': 0, 'y': 0, 'width': 10, 'height': 10},
        {'type': 'crop', 'x': 0, 'y': 0, 'width': 40, 'height': 30},
        {'type': 'rotate', 'degrees': 90},
        {'type': 'pad', 'top': 2, 'bottom': 3, 'left': 4, 'right': 5},
    ], copy=True)
    assert result == 'stamped.new.photo_edited.png'
    assert Image.open(source).size == (80, 60)
    with Image.open(tmp_path / result) as edited:
        assert edited.size == (39, 45)
        assert edited.getpixel((34, 4)) == (255, 255, 255)


def test_blank_stamp_edits_in_place_and_copy_same_stamp(tmp_path):
    source = tmp_path / 'stamped.old.photo.png'
    Image.new('RGB', (80, 60), 'red').save(source)
    assert edit_and_stamp(str(tmp_path), source.name, ['old'], [
        {'type': 'crop', 'x': 0, 'y': 0, 'width': 40, 'height': 30},
    ], copy=False) == source.name
    assert Image.open(source).size == (40, 30)
    copied = edit_and_stamp(str(tmp_path), source.name, ['old'], [], copy=True)
    assert copied == 'stamped.old.photo_copy.png'
    assert source.exists()


def test_invalid_edit_does_not_change_source(tmp_path):
    source = tmp_path / 'photo.png'
    Image.new('RGB', (80, 60), 'red').save(source)
    with pytest.raises(ValueError):
        edit_and_stamp(str(tmp_path), source.name, ['new'], [
            {'type': 'crop', 'x': 70, 'y': 0, 'width': 20, 'height': 20},
        ], copy=False)
    assert source.exists()
    assert sorted(path.name for path in tmp_path.iterdir()) == [source.name]


def test_video_trim_crop_rotate_and_restamp(tmp_path):
    source = tmp_path / 'stamped.old.clip.mp4'
    subprocess.run([
        'ffmpeg', '-hide_banner', '-loglevel', 'error', '-f', 'lavfi',
        '-i', 'testsrc=size=64x48:rate=10', '-t', '2', '-c:v', 'mpeg4', str(source),
    ], check=True)
    result = edit_and_stamp(str(tmp_path), source.name, ['new'], [
        {'type': 'crop', 'x': 0, 'y': 0, 'width': 48, 'height': 32},
        {'type': 'rotate', 'degrees': 90},
        {'type': 'trim', 'points': [{'type': 'start', 'time': 0.5}, {'type': 'end', 'time': 1.5}]},
    ], copy=True)
    capture = cv2.VideoCapture(str(tmp_path / result))
    assert capture.isOpened()
    assert (capture.get(cv2.CAP_PROP_FRAME_WIDTH), capture.get(cv2.CAP_PROP_FRAME_HEIGHT)) == (32, 48)
    assert 0.8 <= capture.get(cv2.CAP_PROP_FRAME_COUNT) / capture.get(cv2.CAP_PROP_FPS) <= 1.2
    capture.release()
    assert source.exists()
