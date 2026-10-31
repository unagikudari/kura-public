"""Extension source ownership, safe installation, and core-only behavior."""
from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from api import extension_hooks
from scripts.compose_extensions import install, STATE


def run_git(root, *args):
    return subprocess.check_output(['git', '-C', str(root), *args], text=True).strip()


def repo(path):
    path.mkdir()
    run_git(path, 'init', '-b', 'main')
    run_git(path, 'config', 'user.name', 'Test')
    run_git(path, 'config', 'user.email', 'test@example.invalid')
    (path / '.gitignore').write_text('*.pyc\n__pycache__/\n.kura-extensions.json\n.kura-extensions.lock\n/feature.py\n/other.py\n/api/optional/\n')
    run_git(path, 'add', '.')
    run_git(path, 'commit', '-m', 'initial')
    return path


def extension(path, name='notes', files=None, core_api=1, units=None):
    root = repo(path)
    files = files or {'feature.py': b'first\n'}
    for rel, data in files.items():
        target = root / 'overlay' / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    manifest = {'schema': 1, 'core_api': core_api, 'id': name, 'version': '0.1.0', 'files': list(files)}
    if units is not None:
        manifest['units'] = units
    (root / 'extension.json').write_text(json.dumps(manifest))
    run_git(root, 'add', '.')
    run_git(root, 'commit', '-m', 'payload')
    return root


def test_install_update_remove_and_version_pin(tmp_path):
    core = repo(tmp_path / 'core')
    ext = extension(tmp_path / 'notes')
    install(core, [ext], check=True)
    assert not (core / 'feature.py').exists()
    first = install(core, [ext])
    assert (core / 'feature.py').read_bytes() == b'first\n'
    assert first['extensions'][0]['commit'] == run_git(ext, 'rev-parse', 'HEAD')
    (ext / 'overlay/feature.py').write_bytes(b'second\n')
    run_git(ext, 'add', '.')
    run_git(ext, 'commit', '-m', 'update')
    second = install(core, [ext])
    assert first['extensions'][0]['commit'] != second['extensions'][0]['commit']
    assert (core / 'feature.py').read_bytes() == b'second\n'
    install(core, [])
    assert not (core / 'feature.py').exists()
    assert json.loads((core / STATE).read_text())['extensions'] == []


def test_refuses_dirty_source_and_modified_runtime_before_any_write(tmp_path):
    core, ext = repo(tmp_path / 'core'), extension(tmp_path / 'notes')
    (ext / 'overlay/feature.py').write_bytes(b'dirty')
    with pytest.raises(ValueError, match='clean'):
        install(core, [ext])
    assert not (core / STATE).exists()
    run_git(ext, 'checkout', '--', 'overlay/feature.py')
    install(core, [ext])
    (core / 'feature.py').write_bytes(b'local edits')
    before = (core / STATE).read_bytes()
    with pytest.raises(ValueError, match='local changes'):
        install(core, [])
    assert (core / STATE).read_bytes() == before
    assert (core / 'feature.py').read_bytes() == b'local edits'


def test_initial_adoption_requires_identical_bytes(tmp_path):
    core, ext = repo(tmp_path / 'core'), extension(tmp_path / 'notes')
    (core / 'feature.py').write_bytes(b'unknown')
    with pytest.raises(ValueError, match='unmanaged'):
        install(core, [ext])
    (core / 'feature.py').write_bytes(b'first\n')
    install(core, [ext])


def test_refuses_core_path_and_cross_extension_collision(tmp_path):
    core = repo(tmp_path / 'core')
    ext = extension(tmp_path / 'notes', files={'.gitignore': b'bad'})
    with pytest.raises(ValueError, match='collides'):
        install(core, [ext])
    a, b = extension(tmp_path / 'a'), extension(tmp_path / 'b', name='lora')
    with pytest.raises(ValueError, match='collides'):
        install(core, [a, b])
    assert not (core / 'feature.py').exists()


@pytest.mark.parametrize('path', ['../escape', '/absolute', '.git/config', 'a/../../b', 'a\\b'])
def test_refuses_unsafe_manifest_paths(tmp_path, path):
    core, ext = repo(tmp_path / 'core'), extension(tmp_path / 'notes')
    m = json.loads((ext / 'extension.json').read_text()); m['files'] = [path]
    (ext / 'extension.json').write_text(json.dumps(m))
    run_git(ext, 'add', '.'); run_git(ext, 'commit', '-m', 'unsafe')
    with pytest.raises(ValueError, match='unsafe'):
        install(core, [ext])


def test_refuses_symlink_destination_and_source(tmp_path):
    core, ext = repo(tmp_path / 'core'), extension(tmp_path / 'notes')
    target = tmp_path / 'outside'; target.write_bytes(b'first\n')
    (core / 'feature.py').symlink_to(target)
    with pytest.raises(ValueError, match='symlink'):
        install(core, [ext])
    (core / 'feature.py').unlink()
    payload = ext / 'overlay/feature.py'; payload.unlink(); payload.symlink_to(target)
    run_git(ext, 'add', '.'); run_git(ext, 'commit', '-m', 'symlink')
    with pytest.raises(ValueError, match='regular'):
        install(core, [ext])
    assert target.read_bytes() == b'first\n'


def test_two_extensions_can_be_selected_independently(tmp_path):
    core = repo(tmp_path / 'core')
    a = extension(tmp_path / 'a')
    b = extension(tmp_path / 'b', name='lora', files={'other.py': b'lora'})
    install(core, [a, b])
    result = install(core, [b])
    assert [e['id'] for e in result['extensions']] == ['lora']
    assert not (core / 'feature.py').exists()
    assert (core / 'other.py').read_bytes() == b'lora'


@pytest.fixture
def empty_extensions(tmp_path, monkeypatch):
    monkeypatch.setattr(extension_hooks, '_ROOT', tmp_path)
    extension_hooks.installed.cache_clear(); extension_hooks.load.cache_clear()
    yield tmp_path
    extension_hooks.installed.cache_clear(); extension_hooks.load.cache_clear()


def test_core_does_not_import_or_mount_extensions(empty_extensions, monkeypatch):
    monkeypatch.setattr(extension_hooks.importlib, 'import_module', lambda name: pytest.fail('core imported an extension'))
    assert extension_hooks.versions() == {}
    assert extension_hooks.include_routers(object(), machine_gate=[]) == []
    with pytest.raises(HTTPException) as exc:
        extension_hooks.require('notes')
    assert exc.value.status_code == 501


def test_installed_but_broken_extension_is_not_silently_ignored(empty_extensions, monkeypatch):
    (empty_extensions / STATE).write_text(json.dumps({'schema': 1, 'extensions': [{'id': 'notes', 'files': {}}]}))
    def broken(name):
        raise ModuleNotFoundError('missing dependency inside installed extension')
    monkeypatch.setattr(extension_hooks.importlib, 'import_module', broken)
    with pytest.raises(ModuleNotFoundError):
        extension_hooks.include_routers(object(), machine_gate=[])


def test_extension_receives_the_same_machine_gate(empty_extensions, monkeypatch):
    (empty_extensions / STATE).write_text(json.dumps({'schema': 1, 'extensions': [{'id': 'notes', 'files': {}}]}))
    calls = []
    module = SimpleNamespace(include_routers=lambda app, machine_gate: calls.append((app, machine_gate)))
    monkeypatch.setattr(extension_hooks.importlib, 'import_module', lambda name: module)
    app, gate = object(), [object()]
    assert extension_hooks.include_routers(app, machine_gate=gate) == ['notes']
    assert calls == [(app, gate)]


def test_installation_drift_is_detected_before_import(empty_extensions):
    (empty_extensions / 'feature.py').write_bytes(b'changed')
    (empty_extensions / STATE).write_text(json.dumps({'schema': 1, 'extensions': [
        {'id': 'notes', 'files': {'feature.py': '0' * 64}}]}))
    with pytest.raises(RuntimeError, match='extension file changed'):
        extension_hooks.load('notes')


def test_rejects_an_unpinned_extension_revision(tmp_path):
    core, ext = repo(tmp_path / 'core'), extension(tmp_path / 'notes')
    (core / 'release').mkdir()
    (core / 'release/extension_sources.json').write_text(json.dumps({'extensions': {'notes': {'commit': '0' * 40}}}))
    with pytest.raises(ValueError, match='not pinned'):
        install(core, [ext])
    assert not (core / 'feature.py').exists()


def test_any_well_formed_id_is_accepted_and_core_names_none(tmp_path):
    core = repo(tmp_path / 'core')
    install(core, [extension(tmp_path / 'notes', name='notes', core_api=2)])
    assert [e['id'] for e in json.loads((core / STATE).read_text())['extensions']] == ['notes']


@pytest.mark.parametrize('name', ['Upper', '../x', 'api.x', '', '1st'])
def test_a_malformed_id_is_refused(tmp_path, name):
    core = repo(tmp_path / 'core')
    with pytest.raises(ValueError, match='unsupported extension manifest'):
        install(core, [extension(tmp_path / 'ext', name=name)])


def test_an_unknown_core_api_is_refused(tmp_path):
    core = repo(tmp_path / 'core')
    with pytest.raises(ValueError, match='unsupported extension manifest'):
        install(core, [extension(tmp_path / 'ext', core_api=5)])


def test_the_declared_interface_is_importable():
    from api import extension_api
    assert extension_api.CORE_API == 4
    for name in extension_api.__all__:
        assert hasattr(extension_api, name), name


def test_the_interface_imports_nothing_until_called():
    # a standalone worker run by plain python3 reaches redact() without the API's dependencies
    code = ("import sys; import api.extension_api as e; "
            "assert not [m for m in sys.modules if m.split('.')[0] in ('fastapi', 'sqlalchemy') "
            "or m.startswith('api.routers')], sorted(sys.modules); print(e.redact('x'))")
    out = subprocess.run([sys.executable, '-c', code], cwd=Path(__file__).resolve().parents[1],
                         capture_output=True, text=True)
    assert out.returncode == 0 and out.stdout.strip() == 'x', out.stderr


def test_pool_annotations_are_keyed_by_the_extension_that_made_them(monkeypatch):
    import asyncio
    class Notes:
        async def annotate_pools(db, ids):
            return {i: {'domain': 'example', 'n': i} for i in ids if i != 2}
    monkeypatch.setattr(extension_hooks, 'installed', lambda: {'notes': {}, 'quiet': {}})
    monkeypatch.setattr(extension_hooks, 'load', lambda name: Notes if name == 'notes' else object())
    out = asyncio.run(extension_hooks.annotate_pools(None, [1, 2]))
    assert out == {1: {'notes': {'domain': 'example', 'n': 1}}}
    assert extension_hooks.annotated_domain(out[1]) == 'example'
    assert extension_hooks.annotated_domain({}) is None
    assert asyncio.run(extension_hooks.annotate_pools(None, [])) == {}


def test_tag_link_annotations_and_status_cards_come_from_extensions(monkeypatch):
    import asyncio
    class Notes:
        async def annotate_tag_links(db, links):
            return {l['id']: {'seen': l['url']} for l in links}
        async def status_cards(db):
            return [{'title': 'Notes', 'stats': [{'label': 'open', 'value': 1}]}]
    monkeypatch.setattr(extension_hooks, 'installed', lambda: {'notes': {}, 'quiet': {}})
    monkeypatch.setattr(extension_hooks, 'load', lambda name: Notes if name == 'notes' else object())
    links = asyncio.run(extension_hooks.annotate_tag_links(None, [{'id': 3, 'pool_id': None, 'url': 'u'}]))
    assert links == {3: {'notes': {'seen': 'u'}}}
    assert asyncio.run(extension_hooks.annotate_tag_links(None, [])) == {}
    assert asyncio.run(extension_hooks.status_cards(None)) == [
        {'title': 'Notes', 'stats': [{'label': 'open', 'value': 1}], 'extension': 'notes'}]


def test_declared_units_are_recorded_for_the_checkout_pull(tmp_path):
    core = repo(tmp_path / 'core')
    units = {'kura-notes-worker.service': 'scripts/*.py systemd/user/kura-notes-worker.service'}
    install(core, [extension(tmp_path / 'ext', units=units)])
    assert json.loads((core / STATE).read_text())['extensions'][0]['units'] == units


@pytest.mark.parametrize('units', [{'../evil': 'x'}, {'kura-x.service': ''}, {'kura-x.service': 'a\nb'}, ['kura-x.service']])
def test_malformed_units_are_refused(tmp_path, units):
    core = repo(tmp_path / 'core')
    with pytest.raises(ValueError, match='unsupported extension units'):
        install(core, [extension(tmp_path / 'ext', units=units)])
