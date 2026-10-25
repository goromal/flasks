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
