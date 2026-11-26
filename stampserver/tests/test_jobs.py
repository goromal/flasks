import json
import os
import sys
import threading

from PIL import Image

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import jobs
from jobs import EditJobs, WORK_DIR

CROP = [{'type': 'crop', 'x': 0, 'y': 0, 'width': 40, 'height': 30}]


def _queue(tmp_path):
    return EditJobs(str(tmp_path / 'state' / 'jobs.json'), threading.RLock())


def _media(tmp_path):
    media = tmp_path / 'media'
    media.mkdir()
    return media


def _image(path, size=(80, 60)):
    Image.new('RGB', size, 'red').save(path)
    return path


def _visible(media):
    return sorted(p.name for p in media.iterdir() if p.name != WORK_DIR)


def _saved(tmp_path):
    return json.loads((tmp_path / 'state' / 'jobs.json').read_text())


def test_copy_job_keeps_original_and_adds_edited_copy(tmp_path):
    media = _media(tmp_path)
    source = _image(media / 'stamped.old.photo.png')
    queue = _queue(tmp_path)

    queue.submit(str(media), source.name, ['new'], CROP, copy=True)
    assert _visible(media) == [source.name]
    queue.run_pending()

    assert _visible(media) == ['stamped.new.photo_edited.png', source.name]
    assert Image.open(source).size == (80, 60)
    assert Image.open(media / 'stamped.new.photo_edited.png').size == (40, 30)
    assert os.listdir(media / WORK_DIR) == []
    assert queue.snapshot()[0]['status'] == 'done'


def test_move_job_sets_original_aside_until_edit_lands(tmp_path):
    media = _media(tmp_path)
    source = _image(media / 'stamped.old.photo.png')
    queue = _queue(tmp_path)

    queue.submit(str(media), source.name, ['old'], CROP, copy=False)
    assert _visible(media) == []
    queue.run_pending()

    assert _visible(media) == [source.name]
    assert Image.open(source).size == (40, 30)


def test_copy_jobs_on_one_file_get_distinct_names(tmp_path):
    media = _media(tmp_path)
    source = _image(media / 'photo.png')
    queue = _queue(tmp_path)

    queue.submit(str(media), source.name, [], CROP, copy=True)
    queue.submit(str(media), source.name, [], [{'type': 'rotate', 'degrees': 90}], copy=True)
    queue.run_pending()

    assert _visible(media) == ['photo.png', 'photo_edited.png', 'photo_edited2.png']
    assert Image.open(media / 'photo_edited2.png').size == (60, 80)


def test_copy_job_survives_original_being_renamed(tmp_path):
    media = _media(tmp_path)
    source = _image(media / 'photo.png')
    queue = _queue(tmp_path)

    queue.submit(str(media), source.name, [], CROP, copy=True)
    os.rename(source, media / 'stamped.cats.photo.png')
    queue.run_pending()

    assert _visible(media) == ['photo_edited.png', 'stamped.cats.photo.png']


def test_failed_move_job_restores_original(tmp_path):
    media = _media(tmp_path)
    source = _image(media / 'photo.png')
    queue = _queue(tmp_path)

    queue.submit(str(media), source.name, [], [{'type': 'crop', 'x': 0, 'y': 0, 'width': 999, 'height': 1}], copy=False)
    queue.run_pending()

    job = queue.snapshot()[0]
    assert job['status'] == 'failed'
    assert 'outside the media bounds' in job['error']
    assert _visible(media) == ['photo.png']
    assert Image.open(source).size == (80, 60)
    assert os.listdir(media / WORK_DIR) == []


def test_failed_move_job_restores_beside_a_reused_name(tmp_path):
    media = _media(tmp_path)
    source = _image(media / 'photo.png')
    queue = _queue(tmp_path)

    queue.submit(str(media), source.name, ['dogs'], CROP, copy=False)
    _image(media / 'stamped.dogs.photo.png')
    _image(media / 'photo.png', (5, 5))
    queue.run_pending()

    job = queue.snapshot()[0]
    assert job['status'] == 'failed'
    assert 'photo_restored.png' in job['error']
    assert Image.open(media / 'photo_restored.png').size == (80, 60)


def test_move_job_is_rejected_up_front_when_target_is_taken(tmp_path):
    media = _media(tmp_path)
    source = _image(media / 'photo.png')
    _image(media / 'stamped.dogs.photo.png')
    queue = _queue(tmp_path)

    try:
        queue.submit(str(media), source.name, ['dogs'], CROP, copy=False)
    except FileExistsError:
        pass
    else:
        raise AssertionError('expected FileExistsError')
    assert source.exists()
    assert queue.snapshot() == []


def test_restart_reruns_interrupted_render(tmp_path, monkeypatch):
    media = _media(tmp_path)
    source = _image(media / 'photo.png')
    queue = _queue(tmp_path)
    job = queue.submit(str(media), source.name, [], CROP, copy=False)

    # Simulate a crash mid-render: saved as running, with a partial output.
    with queue.lock:
        queue._claim()
    partial = media / WORK_DIR / (job['id'] + '.out.png')
    partial.write_bytes(b'partial')

    monkeypatch.setattr(jobs.threading, 'Thread', _NoThread)
    restarted = _queue(tmp_path)
    restarted.start()
    assert restarted.snapshot()[0]['status'] == 'queued'
    assert not partial.exists()
    restarted.run_pending()

    assert _visible(media) == ['photo.png']
    assert Image.open(source).size == (40, 30)


def test_restart_finishes_interrupted_commit_without_reapplying(tmp_path, monkeypatch):
    media = _media(tmp_path)
    source = _image(media / 'photo.png')
    queue = _queue(tmp_path)
    queue.submit(str(media), source.name, [], CROP, copy=False)

    # Crash right after os.replace: the edited file is in place, the stash
    # still exists, and the job is saved as committing.
    real_remove = jobs._remove
    monkeypatch.setattr(jobs, '_remove', _crash_on_stash(real_remove))
    try:
        queue.run_pending()
    except _Crash:
        pass
    monkeypatch.setattr(jobs, '_remove', real_remove)
    assert _saved(tmp_path)[0]['status'] == 'committing'

    monkeypatch.setattr(jobs.threading, 'Thread', _NoThread)
    restarted = _queue(tmp_path)
    restarted.start()

    assert restarted.snapshot()[0]['status'] == 'done'
    assert _visible(media) == ['photo.png']
    assert Image.open(source).size == (40, 30)
    assert os.listdir(media / WORK_DIR) == []


def test_restart_fails_job_whose_stash_vanished(tmp_path, monkeypatch):
    media = _media(tmp_path)
    source = _image(media / 'photo.png')
    queue = _queue(tmp_path)
    job = queue.submit(str(media), source.name, [], CROP, copy=True)
    os.remove(media / WORK_DIR / (job['id'] + '.src.png'))

    monkeypatch.setattr(jobs.threading, 'Thread', _NoThread)
    restarted = _queue(tmp_path)
    restarted.start()

    assert restarted.snapshot()[0]['status'] == 'failed'


def test_finished_jobs_are_pruned_and_clearable(tmp_path, monkeypatch):
    monkeypatch.setattr(jobs, 'KEEP_FINISHED', 2)
    media = _media(tmp_path)
    source = _image(media / 'photo.png')
    queue = _queue(tmp_path)
    for _ in range(3):
        queue.submit(str(media), source.name, [], CROP, copy=True)
    queue.run_pending()

    assert len(queue.snapshot()) == 2
    queue.clear_finished()
    assert queue.snapshot() == []
    assert _saved(tmp_path) == []


class _NoThread:
    def __init__(self, *args, **kwargs):
        pass

    def start(self):
        pass


class _Crash(Exception):
    pass


def _crash_on_stash(real_remove):
    def remove(path):
        if '.src.' in os.path.basename(path):
            raise _Crash()
        real_remove(path)
    return remove
