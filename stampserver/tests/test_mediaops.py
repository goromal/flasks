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


def _frame_lumas(path):
    capture = cv2.VideoCapture(str(path))
    lumas = []
    while True:
        ok, frame = capture.read()
        if not ok:
            break
        lumas.append(float(cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY).mean()))
    capture.release()
    return lumas


def _stream_duration(path, stream):
    result = subprocess.run([
        'ffprobe', '-v', 'error', '-select_streams', stream, '-show_entries', 'stream=duration',
        '-of', 'default=noprint_wrappers=1:nokey=1', str(path),
    ], capture_output=True, text=True, check=True)
    return float(result.stdout)


def test_video_trim_keeps_exact_segment_frames_in_order(tmp_path):
    # Frame N has luma 2N, so each output frame identifies its source frame.
    # A long GOP puts the cut points between keyframes.
    source = tmp_path / 'clip.mp4'
    subprocess.run([
        'ffmpeg', '-hide_banner', '-loglevel', 'error',
        '-f', 'lavfi', '-i', "color=black:size=64x48:rate=10,format=gray,geq=lum='2*N'",
        '-f', 'lavfi', '-i', 'sine=frequency=440:sample_rate=48000',
        '-t', '10', '-c:v', 'libx264', '-qp', '0', '-g', '50', '-pix_fmt', 'yuv420p',
        '-c:a', 'aac', '-shortest', str(source),
    ], check=True)
    result = edit_and_stamp(str(tmp_path), source.name, [], [
        {'type': 'trim', 'points': [
            {'type': 'start', 'time': 6.3}, {'type': 'end', 'time': 6.8},
            {'type': 'start', 'time': 2.1}, {'type': 'end', 'time': 3.1},
        ]},
    ], copy=True)
    lumas = _frame_lumas(tmp_path / result)
    expected = [2 * n for n in list(range(21, 31)) + list(range(63, 68))]
    assert len(lumas) == len(expected)
    assert all(abs(luma - want) <= 2 for luma, want in zip(lumas, expected)), lumas
    assert abs(_stream_duration(tmp_path / result, 'a:0') - 1.5) <= 0.05
    assert abs(_stream_duration(tmp_path / result, 'v:0') - 1.5) <= 0.05


def test_video_trim_decodes_only_selected_ranges(tmp_path, monkeypatch):
    source = tmp_path / 'clip.mp4'
    subprocess.run([
        'ffmpeg', '-hide_banner', '-loglevel', 'error', '-f', 'lavfi',
        '-i', 'testsrc=size=64x48:rate=10', '-t', '4', '-c:v', 'mpeg4', str(source),
    ], check=True)
    commands = []
    run = subprocess.run

    def recording_run(command, *args, **kwargs):
        commands.append(command)
        return run(command, *args, **kwargs)

    monkeypatch.setattr(subprocess, 'run', recording_run)
    edit_and_stamp(str(tmp_path), source.name, [], [
        {'type': 'trim', 'points': [
            {'type': 'start', 'time': 0.5}, {'type': 'end', 'time': 1.0},
            {'type': 'start', 'time': 2.5}, {'type': 'end', 'time': 3.0},
        ]},
    ], copy=True)
    ffmpeg = next(command for command in commands if command[0] == 'ffmpeg')
    inputs = [index for index, arg in enumerate(ffmpeg) if arg == '-i']
    assert len(inputs) == 2
    assert [ffmpeg[index - 4:index] for index in inputs] == [
        ['-ss', '0.500000', '-t', '0.500000'], ['-ss', '2.500000', '-t', '0.500000'],
    ]
    assert 'trim=' not in ' '.join(ffmpeg)
