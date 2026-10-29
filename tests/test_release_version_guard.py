"""Release numbers reserved by other linked worktrees must not be reused."""
import importlib.machinery
import importlib.util
from pathlib import Path
import subprocess

import pytest

HOOK = Path(__file__).resolve().parents[1] / 'scripts/hooks/pre-commit'


@pytest.fixture
def guard(tmp_path, monkeypatch):
    loader = importlib.machinery.SourceFileLoader('release_guard', str(HOOK))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    # Other gates have their own tests; keep this an actual Git version/ref test.
    for name in ['secret_gate', 'endpoints_snapshot_drift', 'replica_ddl_undeclared',
                 'replica_ddl_lies', 'vendored_refguard_drift']:
        monkeypatch.setattr(module, name, lambda staged: 0)
    monkeypatch.chdir(tmp_path)

    def git(*args):
        subprocess.run(['git', *args], check=True, capture_output=True)

    git('init', '-q')
    git('config', 'user.name', 'Version test')
    git('config', 'user.email', 'test@example.invalid')
    Path('VERSION').write_text('1.0.0\n')
    Path('CHANGELOG.md').write_text('## 1.0.0 — 2026-09-20\nfix: seed\n')
    git('add', 'VERSION', 'CHANGELOG.md')
    git('commit', '-qm', 'seed')

    def stage(version):
        Path('VERSION').write_text(version + '\n')
        Path('CHANGELOG.md').write_text(f'## {version} — 2026-09-20\nfix: next\n\n## 1.0.0 — 2026-09-20\nfix: seed\n')
        git('add', 'VERSION', 'CHANGELOG.md')

    return module, git, stage


@pytest.mark.parametrize('annotated', [False, True])
def test_tag_reserved_elsewhere_blocks_same_version(guard, capsys, annotated):
    module, git, stage = guard
    git('tag', *(['-a', '-m', 'reserved'] if annotated else []), 'v1.0.1')
    stage('1.0.1')
    assert module.main() == 1
    assert '(tag)' in capsys.readouterr().err


def test_version_must_exceed_higher_reserved_tag(guard, capsys):
    module, git, stage = guard
    git('tag', 'v1.0.3')
    stage('1.0.1')
    assert module.main() == 1
    assert '1.0.4' in capsys.readouterr().err
    stage('1.0.4')
    assert module.main() == 0


def test_non_release_tags_do_not_reserve_version(guard):
    module, git, stage = guard
    git('tag', 'v9.0.0-experiment')
    stage('1.0.1')
    assert module.main() == 0
