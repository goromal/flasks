import importlib.util
import json
import os
import sys

from PIL import Image


def _server(monkeypatch, tmp_path):
    secrets = tmp_path / 'secrets.json'
    secrets.write_text(json.dumps({
        'username': 'test',
        'secret_key': 'test-secret',
        'password_hash': 'test-hash',
    }))
    monkeypatch.setattr(sys, 'argv', [
        'stampserver', '--secrets-file', str(secrets), '--data-dir', str(tmp_path),
        '--state-dir', str(tmp_path / '.state'),
    ])
    path = os.path.join(os.path.dirname(__file__), '..', 'stampserver.py')
    spec = importlib.util.spec_from_file_location('stampserver_form_test', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_blank_restamp_with_keep_original_duplicates(monkeypatch, tmp_path):
    module = _server(monkeypatch, tmp_path)
    source = tmp_path / 'stamped.foo.photo.png'
    Image.new('RGB', (10, 10), 'red').save(source)
    module.stampserver.filedeck = source.name
    module.stampserver.filelist = [(source.name, 'PNG')]

    with module.app.test_request_context('/restamp/foo', method='POST', data={
        'action': 'restamp', 'text': '', 'preserve_originals': 'on',
    }):
        module._handle_restamp_post(['foo'])

    assert source.exists()
    assert (tmp_path / 'stamped.foo.photo_copy.png').exists()
    assert module.stampserver.filelist == [(source.name, 'PNG')]


def test_autoplay_blank_restamp_still_skips(monkeypatch, tmp_path):
    module = _server(monkeypatch, tmp_path)
    source = tmp_path / 'stamped.foo.photo.png'
    Image.new('RGB', (10, 10), 'red').save(source)
    module.stampserver.filedeck = source.name
    module.stampserver.filelist = [(source.name, 'PNG')]

    with module.app.test_request_context('/restamp/foo', method='POST', data={
        'text': '', 'preserve_originals': 'on',
    }):
        module._handle_restamp_post(['foo'])

    assert source.exists()
    assert sorted(path.name for path in tmp_path.glob('*.png')) == [source.name]
    assert module.stampserver.filelist == []


def _delete(module, stamp, confirm):
    with module.app.test_request_context('/delete-stamp/' + stamp, method='POST', data={'confirm': confirm}):
        response = module.delete_stamp.__wrapped__(stamp)
        return response, module.flask.get_flashed_messages()


def test_delete_stamp_requires_exact_confirmation(monkeypatch, tmp_path):
    module = _server(monkeypatch, tmp_path)
    target = tmp_path / 'stamped.foo.photo.png'
    target.write_bytes(b'x')

    response, messages = _delete(module, 'foo', 'fo')

    assert target.exists()
    assert response.location.endswith('/restamp/foo')
    assert 'nothing was deleted' in messages[0]


def test_delete_stamp_removes_subtree_and_keeps_others(monkeypatch, tmp_path):
    module = _server(monkeypatch, tmp_path)
    for name in ('stamped.foo.a.png', 'stamped.foo.stamped.bar.b.mp4',
                 'stamped.foobar.c.png', 'stamped.baz.d.png', 'e.png'):
        (tmp_path / name).write_bytes(b'x')
    module.stampserver.filelist = [('stamped.foo.a.png', 'PNG')]

    response, messages = _delete(module, 'foo', ' foo ')

    assert sorted(p.name for p in tmp_path.iterdir() if p.name != 'secrets.json') == [
        'e.png', 'stamped.baz.d.png', 'stamped.foobar.c.png',
    ]
    assert response.location == '/'
    assert 'Deleted 2 file(s)' in messages[0]
    assert module.stampserver.filelist == []


def test_delete_substamp_redirects_to_parent(monkeypatch, tmp_path):
    module = _server(monkeypatch, tmp_path)
    (tmp_path / 'stamped.foo.stamped.bar.b.png').write_bytes(b'x')
    (tmp_path / 'stamped.foo.a.png').write_bytes(b'x')

    response, _ = _delete(module, 'foo/bar', 'foo/bar')

    assert not (tmp_path / 'stamped.foo.stamped.bar.b.png').exists()
    assert (tmp_path / 'stamped.foo.a.png').exists()
    assert response.location.endswith('/restamp/foo')


CROP_EDITS = json.dumps([{'type': 'crop', 'x': 0, 'y': 0, 'width': 5, 'height': 5}])


def test_edit_with_keep_original_queues_and_stays_on_file(monkeypatch, tmp_path):
    module = _server(monkeypatch, tmp_path)
    source = tmp_path / 'stamped.foo.photo.png'
    Image.new('RGB', (10, 10), 'red').save(source)
    module.stampserver.filedeck = source.name
    module.stampserver.filelist = [(source.name, 'PNG')]

    with module.app.test_request_context('/restamp/foo', method='POST', data={
        'action': 'restamp', 'text': '', 'preserve_originals': 'on', 'edits': CROP_EDITS,
    }):
        module._handle_restamp_post(['foo'])
        assert module.flask.g.keep_original

    assert module.stampserver.filelist == [(source.name, 'PNG')]
    assert [job['status'] for job in module.edit_jobs.snapshot()] == ['queued']
    module.edit_jobs.run_pending()
    assert Image.open(tmp_path / 'stamped.foo.photo_edited.png').size == (5, 5)
    assert Image.open(source).size == (10, 10)


def test_edit_replacing_original_queues_and_leaves_deck(monkeypatch, tmp_path):
    module = _server(monkeypatch, tmp_path)
    source = tmp_path / 'photo.png'
    Image.new('RGB', (10, 10), 'red').save(source)
    module.stampserver.filedeck = source.name
    module.stampserver.filelist = [(source.name, 'PNG')]

    with module.app.test_request_context('/', method='POST', data={'text': '', 'edits': CROP_EDITS}):
        module._handle_stamp_post()

    assert module.stampserver.filelist == []
    assert not source.exists()
    module.edit_jobs.run_pending()
    assert Image.open(source).size == (5, 5)
