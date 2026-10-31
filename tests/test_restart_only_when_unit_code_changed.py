"""checkout-pull は、その unit のコードが変わったときだけ再起動する。

■ 何が起きたか (2026-09-27 実測、primary)

docs だけの commit (1.215.3、CLAUDE.md の 1 行) を checkout-pull が取り込み、
kura-api / tagger と一緒に拡張の import worker を再起動した。worker は 30 分かかる
取り込みの途中で殺された。lease は reaper が回収するので set は失われないが、
取り込みはやり直しになる。(下の kura-notes-worker は、拡張が manifest で宣言する
unit の代役。)

■ ここで固定すること

scripts/kura_checkout_pull.sh の「unit code paths」block を、実際の git repo に
対して bash で動かす (文字列検査ではなく挙動):

1. docs だけの commit では kura-notes-worker のコードは「変わっていない」
2. scripts/ の commit では「変わった」
3. 既定 (repo 全体) の unit は、docs だけの commit でも「変わった」— 従来どおり
4. 古さの判定 (code_at) も同じ規則: docs commit は kura-notes-worker の時刻を進めない
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SH = (REPO / "scripts" / "kura_checkout_pull.sh").read_text(encoding="utf-8")
START = "# --- unit code paths"
END = "# --- end unit code paths ---"


def _block() -> str:
    i, j = SH.index(START), SH.index(END)
    return SH[i:j]


def _git(repo: Path, *args: str, when: int | None = None) -> str:
    # Isolated from the operator's git config: a global hooksPath or signing
    # setting must not decide whether this fixture can commit.
    env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t",
               GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t",
               GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1")
    if when is not None:
        env["GIT_AUTHOR_DATE"] = env["GIT_COMMITTER_DATE"] = f"@{when} +0000"
    r = subprocess.run(["git", "-C", str(repo), *args],
                       capture_output=True, text=True, env=env)
    assert r.returncode == 0, f"git {args[0]} failed: {r.stderr.strip()}"
    return r.stdout.strip()


def _commit(repo: Path, path: str, when: int) -> str:
    f = repo / path
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(f"{path} {when}\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", path, when=when)
    return _git(repo, "rev-parse", "HEAD")


def _call(repo: Path, expr: str) -> subprocess.CompletedProcess:
    script = f'REPO="{repo}"\n{_block()}\n{expr}\n'
    return subprocess.run(["bash", "-c", script], capture_output=True, text=True)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    _git(tmp_path, "init", "-q")
    # an extension's worker unit, as compose records it from that extension's manifest
    import json
    (tmp_path / ".kura-extensions.json").write_text(json.dumps({"schema": 1, "extensions": [
        {"id": "notes", "units": {"kura-notes-worker.service":
                                  "scripts/*.py systemd/user/kura-notes-worker.service"}}]}))
    (tmp_path / ".gitignore").write_text(".kura-extensions.json\n")
    return tmp_path


def test_a_docs_only_commit_does_not_change_the_walker(repo):
    a = _commit(repo, "scripts/notes_worker.py", 1000)
    b = _commit(repo, "CLAUDE.md", 2000)
    r = _call(repo, f'code_changed {a} {b} kura-notes-worker.service && echo CHANGED || echo SAME')
    assert r.stdout.strip() == "SAME", r.stderr


def test_a_scripts_commit_changes_the_walker(repo):
    a = _commit(repo, "CLAUDE.md", 1000)
    b = _commit(repo, "scripts/split_pool_by_filename_set.py", 2000)
    r = _call(repo, f'code_changed {a} {b} kura-notes-worker.service && echo CHANGED || echo SAME')
    assert r.stdout.strip() == "CHANGED", r.stderr


def test_the_walker_unit_file_counts_as_its_code(repo):
    a = _commit(repo, "CLAUDE.md", 1000)
    b = _commit(repo, "systemd/user/kura-notes-worker.service", 2000)
    r = _call(repo, f'code_changed {a} {b} kura-notes-worker.service && echo CHANGED || echo SAME')
    assert r.stdout.strip() == "CHANGED", r.stderr


def test_other_units_keep_restarting_on_any_commit(repo):
    """Backend dependencies retain the broad boundary; unrelated root files count."""
    a = _commit(repo, "scripts/x.py", 1000)
    b = _commit(repo, "CLAUDE.md", 2000)
    for unit in ("kura-api.service", "kura-tagger@full.service"):
        r = _call(repo, f'code_changed {a} {b} {unit} && echo CHANGED || echo SAME')
        assert r.stdout.strip() == "CHANGED", (unit, r.stderr)


def test_staleness_uses_the_same_rule(repo):
    """docs commit は kura-notes-worker の「最新コード時刻」を進めない。
    進めると、pull 経路で再起動を避けても次の tick の古さ判定が殺す。"""
    _commit(repo, "scripts/notes_worker.py", 1000)
    _commit(repo, "CLAUDE.md", 2000)
    walk = _call(repo, "code_at kura-notes-worker.service").stdout.strip()
    api = _call(repo, "code_at kura-api.service").stdout.strip()
    assert walk == "1000"
    assert api == "2000"


def test_code_at_is_zero_when_nothing_matches(repo):
    _commit(repo, "CLAUDE.md", 1000)
    assert _call(repo, "code_at kura-notes-worker.service").stdout.strip() == "0"


def test_both_restart_paths_use_the_rule():
    """pull 経路と古さ判定の両方がこの block を通ること (片方だけだと、もう片方が殺す)。"""
    after_block = SH[SH.index(END):]
    assert "code_at" in after_block, "古さ判定が unit ごとの時刻を使っていない"
    assert "code_changed" in after_block, "pull 経路が unit ごとの差分を見ていない"


def test_a_shell_script_under_scripts_is_not_walker_code(repo):
    """walker は scripts/ の .py しか読まない。この checkout-pull script 自身の
    変更で walker を殺さない。"""
    a = _commit(repo, "scripts/notes_worker.py", 1000)
    b = _commit(repo, "scripts/kura_checkout_pull.sh", 2000)
    r = _call(repo, f'code_changed {a} {b} kura-notes-worker.service && echo CHANGED || echo SAME')
    assert r.stdout.strip() == "SAME", r.stderr


def test_a_deleted_python_file_still_counts(repo):
    """pathspec を bash に glob させると、消えた file は展開に出てこず見落とす。"""
    _commit(repo, "scripts/old_helper.py", 1000)
    a = _commit(repo, "CLAUDE.md", 1500)
    (repo / "scripts" / "old_helper.py").unlink()
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "rm", when=2000)
    b = _git(repo, "rev-parse", "HEAD")
    r = _call(repo, f'cd "{repo}"; code_changed {a} {b} kura-notes-worker.service && echo CHANGED || echo SAME')
    assert r.stdout.strip() == "CHANGED", r.stderr


def test_frontend_release_does_not_restart_api_even_on_next_timer_tick(repo):
    _commit(repo, "api/main.py", 1000)
    a = _commit(repo, "web/src/routes/+layout.svelte", 2000)
    _commit(repo, "VERSION", 3000)
    _commit(repo, "CHANGELOG.md", 4000)
    _commit(repo, "tests/test_example.py", 5000)
    b = _commit(repo, "scripts/kura_checkout_pull.sh", 6000)
    assert _call(repo, f'code_changed {a} {b} kura-api.service && echo CHANGED || echo SAME').stdout.strip() == "SAME"
    assert _call(repo, 'code_at kura-api.service').stdout.strip() == "1000"
    assert _call(repo, 'code_at kura-tagger@full.service').stdout.strip() == "1000"


@pytest.mark.parametrize("path", ["api/services/files.py", "scripts/notes_helper.py", "release/extension_sources.json", "systemd/user/kura-api.service.d/runtime.conf"])
def test_backend_dependencies_still_restart_api(repo, path):
    a = _commit(repo, "web/src/routes/+layout.svelte", 1000)
    b = _commit(repo, path, 2000)
    assert _call(repo, f'code_changed {a} {b} kura-api.service && echo CHANGED || echo SAME').stdout.strip() == "CHANGED"
    assert _call(repo, 'code_at kura-api.service').stdout.strip() == "2000"
