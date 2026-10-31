import io

import pytest
from PIL import Image

import video_media as vm


def test_probe(tmp_path, make_video):
    path = make_video(tmp_path / "v.mp4")
    info = vm.probe(path)
    assert info["duration"] == pytest.approx(20.0, abs=0.2)
    assert info["fps"] == pytest.approx(10.0)
    assert (info["width"], info["height"]) == (160, 120)
    assert info["has_audio"] is True


def test_probe_without_audio(tmp_path, make_video):
    path = make_video(tmp_path / "v.mp4", audio=False)
    assert vm.probe(path)["has_audio"] is False


def test_probe_rejects_non_video(tmp_path):
    bad = tmp_path / "x.mp4"
    bad.write_bytes(b"not a video")
    with pytest.raises(vm.MediaError):
        vm.probe(str(bad))


def test_grab_frame_colour_and_size(tmp_path, make_video, is_blue):
    path = make_video(tmp_path / "v.mp4", size="1280x720")
    red, blue = vm.grab_frame(path, 2.0), vm.grab_frame(path, 9.0)
    assert not is_blue(red) and is_blue(blue)
    img = Image.open(io.BytesIO(blue))
    assert img.format == "JPEG"
    assert max(img.size) == 512


def test_grab_frame_never_upscales(tmp_path, make_video):
    path = make_video(tmp_path / "v.mp4")
    assert Image.open(io.BytesIO(vm.grab_frame(path, 1.0))).size == (160, 120)


def test_export_concatenates_segments(tmp_path, make_video, is_blue):
    path = make_video(tmp_path / "v.mp4")
    dest = str(tmp_path / "out.mp4")
    vm.export(path, [{"start": 7.0, "end": 9.0}, {"start": 10.0, "end": 12.5}],
              dest, has_audio=True)
    info = vm.probe(dest)
    assert info["duration"] == pytest.approx(4.5, abs=0.25)
    assert info["has_audio"]
    assert is_blue(vm.grab_frame(dest, 0.1)) and is_blue(vm.grab_frame(dest, 4.2))


def test_export_failure_leaves_no_file(tmp_path):
    dest = tmp_path / "out.mp4"
    with pytest.raises(vm.MediaError):
        vm.export(str(tmp_path / "missing.mp4"), [{"start": 0, "end": 1}],
                  str(dest), has_audio=False)
    assert not dest.exists()
    assert not (tmp_path / "out.mp4.part").exists()


def test_export_rejects_empty():
    with pytest.raises(vm.MediaError):
        vm.export("x.mp4", [], "y.mp4", has_audio=False)


def test_export_argv_without_audio():
    argv = vm.export_argv("in.mp4", [{"start": 1, "end": 3}], "o.mp4", False)
    assert "[0:v:0]concat=n=1:v=1:a=0[v]" in argv
    assert "[a]" not in argv


def test_export_can_be_stopped_and_leaves_nothing(tmp_path, make_video):
    path = make_video(tmp_path / "v.mp4")
    dest = tmp_path / "out.mp4"
    with pytest.raises(vm.MediaError, match="cancelled"):
        vm.export(path, [{"start": 0, "end": 20}], str(dest), True,
                  should_stop=lambda: True)
    assert not dest.exists() and not (tmp_path / "out.mp4.part").exists()
