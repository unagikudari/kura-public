"""`kura_checkout_pull.sh` は origin/main と一致するときだけ本番へ出す。

2026-09-05 の実害: ~/kura に checkout されたままだった未 merge branch が本番に出た。
経路は 2 つあり、**どちらも HEAD が何であるかを一度も見ていなかった**:

1. `git pull --ff-only` は branch 引数を取らないので、その時 checkout されている
   branch に追従する (unit の Description は "origin main へ" と言っている)。
2. より静かなのがこちら。feature branch 側に上流の新 commit が無ければ pull は
   no-op になり `->` 行も出ないが、「プロセスが HEAD より古い」節が HEAD を見ずに
   try-restart する。09-03 以降 190 回発火しており、実際に branch を本番へ載せて
   いたのは主にこの静かな方だった。

述語を「HEAD が main branch か」にすると足りない — main のまま origin より先行して
いる形 (未 push の merge / local commit / rebase 途中) が素通りし、その日の HEAD 移動
43 回はほぼ全部そちらだった。だから **commit で比べる**。この test はその 1 本の述語を
4 つの形すべてに対して固定する。

script を読む test ではなく **走らせる** test なのは、1 と 2 が両方とも「宣言では
そう書いてあるのに配線されていなかった」形だったから。文字列一致では同じ穴が開く。
"""
import os
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts" / "kura_checkout_pull.sh"


def _git(cwd, *args, check=True):
    # a machine with no git identity (a fresh install) must still be able to commit here
    return subprocess.run(["git", "-c", "user.name=test", "-c", "user.email=test@example.invalid", *args],
                          cwd=cwd, check=check, capture_output=True, text=True)


@pytest.fixture
def clone(tmp_path):
    """`origin` を持つ本物の clone。worktree では branch 名 main を再利用できない。"""
    if not shutil.which("git"):
        pytest.skip("git がない")
    dst = tmp_path / "checkout"
    # `--branch main`: clone 元が feature branch に居ると (PR の worktree で preflight を
    # 走らせる形、2026-09-05 kura#89 で実測) `--depth 1` の clone はその branch しか
    # 持たず origin/main が存在しない → script は「origin/main= (空) より先行」と言って
    # 落ち、test は本物の defect と同じ顔で赤くなる。main を名指しで clone すれば
    # origin/main は常に在り、HEAD もそれに一致する。
    r = subprocess.run(["git", "clone", "-q", "--no-hardlinks", "--depth", "1",
                        "--branch", "main", str(REPO), str(dst)],
                       capture_output=True, text=True)
    if r.returncode != 0:
        pytest.skip(f"clone できない: {r.stderr[:200]}")
    # clone 元が main 以外に居ても、この checkout は main を持つ
    _git(dst, "checkout", "-q", "-B", "main", "origin/main", check=False)
    return dst


def _run(checkout, sandbox):
    """script を **完全に隔離して** 走らせる。

    `KURA_REPO` を temp clone に向けるだけでは足りない — 実測でこの test 自身が
    本番を壊した (2026-09-05):

      * `UNITS` は実在の unit 名なので `systemctl --user try-restart` が **本物の
        kura-api / kura-tagger を再起動した**
      * unit 同期節の宛先は `$HOME/.config/systemd/user/` なので、branch が仕込んだ
        unit file が **本物の `kura-checkout-pull.service` を上書きした**
        (`Description=poisoned by an unmerged branch` / `ExecStart=/bin/true`)

    「対象を差し替えたつもりで、副作用の宛先だけ実環境に残っていた」形で、この
    script が deploy 経路である以上 test も deploy を起こしうる。だから
    `HOME` を temp に移して書き込み先を封じ、`systemctl` を PATH 上の stub に
    差し替えて呼び出しを記録するだけにする。
    """
    bin_dir = sandbox / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    calls = bin_dir / "systemctl.log"
    stub = bin_dir / "systemctl"
    stub.write_text('#!/bin/sh\necho "$@" >> "%s"\nexit 0\n' % calls)
    stub.chmod(0o755)
    home = sandbox / "home"
    (home / ".config" / "systemd" / "user").mkdir(parents=True, exist_ok=True)
    env = dict(os.environ, KURA_REPO=str(checkout), HOME=str(home),
               PATH=f"{bin_dir}:{os.environ.get('PATH','')}")
    out = subprocess.run(["bash", str(SCRIPT)], env=env,
                         capture_output=True, text=True, timeout=120)
    out.systemctl_calls = calls.read_text() if calls.exists() else ""  # type: ignore[attr-defined]
    out.home = home  # type: ignore[attr-defined]
    return out


def test_origin_main_is_deployed(clone, tmp_path):
    """一致しているときは通す — guard が全部止めては deploy が死ぬ。"""
    out = _run(clone, tmp_path)
    assert out.returncode == 0, f"stderr={out.stderr}"
    assert "追従しない" not in out.stderr


def test_a_feature_branch_is_refused(clone, tmp_path):
    """2026-09-05 の当該形。branch に居るまま timer が走っても本番に出さない。"""
    _git(clone, "checkout", "-q", "-b", "perf/some-unmerged-work")
    out = _run(clone, tmp_path)
    assert out.returncode == 1
    assert "perf/some-unmerged-work" in out.stderr and "追従しない" in out.stderr
    assert "worktree" in out.stderr, "拒否は代わりの正しいやり方を示すこと"
    assert out.systemctl_calls == "", "拒否は systemctl を一度も呼ばないこと"


def test_a_detached_head_is_refused(clone, tmp_path):
    """`gh pr checkout` の一部や rebase 途中がこの形になる。"""
    head = _git(clone, "rev-parse", "HEAD").stdout.strip()
    _git(clone, "checkout", "-q", "--detach", head)
    out = _run(clone, tmp_path)
    assert out.returncode == 1
    assert "追従しない" in out.stderr
    assert out.systemctl_calls == "", "拒否は systemctl を一度も呼ばないこと"


def test_main_ahead_of_origin_is_refused(clone, tmp_path):
    """branch 名だけを見る述語が素通りさせる形 — 未 push の commit/merge。

    その日の HEAD 移動 43 回はほぼ全部これで、`gh pr checkout` による branch 切替
    より数が多い。ここが落ちるなら guard は branch 名を見ている。"""
    _git(clone, "commit", "-q", "--allow-empty", "-m", "unpushed local commit")
    out = _run(clone, tmp_path)
    assert out.returncode == 1
    assert "origin/main より先行している" in out.stderr
    assert "(1 commit)" in out.stderr, "何 commit 先行しているかを言うこと"
    assert out.systemctl_calls == "", "拒否は systemctl を一度も呼ばないこと"


def test_the_refusal_is_loud_not_silent(clone, tmp_path):
    """黙って何もしない guard は、壊れた guard と区別がつかない。

    2026-09-05 の静かな経路 (`->` 行を出さずに restart) がまさにその形だった。"""
    _git(clone, "checkout", "-q", "-b", "some-branch")
    out = _run(clone, tmp_path)
    assert out.returncode != 0, "exit code で timer unit を failed にすること"
    assert out.stderr.strip(), "理由を stderr に出すこと"


def test_the_guard_runs_before_units_are_installed(clone, tmp_path):
    """unit 同期も branch の内容を配る経路。述語より後ろにあってはいけない。

    `systemd/user/*` は tracked file なので、branch がそれを変更していれば
    同期節がそのまま node に install する — restart を止めても漏れる。"""
    unit_dir = clone / "systemd" / "user"
    unit_dir.mkdir(parents=True, exist_ok=True)
    (unit_dir / "kura-checkout-pull.service").write_text(
        textwrap.dedent("""\
        [Unit]
        Description=poisoned by an unmerged branch
        [Service]
        ExecStart=/bin/true
        """))
    _git(clone, "checkout", "-q", "-b", "branch-that-edits-units")
    _git(clone, "add", "-A")
    _git(clone, "-c", "user.email=t@t", "-c", "user.name=t",
         "commit", "-q", "-m", "edit a unit on a branch")
    out = _run(clone, tmp_path)
    assert out.returncode == 1
    assert "追従しない" in out.stderr
    installed = out.home / ".config" / "systemd" / "user" / "kura-checkout-pull.service"
    assert not installed.exists(), "拒否したのに unit が install されている"
    assert out.systemctl_calls == "", "拒否は systemctl を一度も呼ばないこと"


def test_an_unresolvable_origin_is_not_reported_as_being_ahead(clone, tmp_path):
    """`git rev-parse origin/main` does NOT fail on a missing ref — it echoes the
    string back. Comparing HEAD against the literal "origin/main" is never equal,
    so an unresolvable ref was reported as "ahead by ? commits" with
    `origin/main=origin/m` in the message: the misleading diagnosis produced by
    the block written to prevent misleading diagnoses.

    The two states need opposite actions from the operator — repair the fetch, or
    push your work — so they must not share a sentence. Found 2026-09-05 by
    running this suite from a worktree, where the clone had no origin/main.
    """
    # The remote stays reachable and `pull --ff-only origin main` still succeeds on
    # an explicit refspec; only the remote-TRACKING ref is gone. A shallow clone of
    # a repo whose HEAD is a feature branch produces exactly this shape.
    _git(clone, "config", "--unset", "remote.origin.fetch", check=False)
    _git(clone, "update-ref", "-d", "refs/remotes/origin/main", check=False)
    out = _run(clone, tmp_path)
    assert out.returncode == 1
    assert "解決できない" in out.stderr
    assert "先行しているのではない" in out.stderr, "未 push と混同しないこと"
    assert "? commit" not in out.stderr, "解決できない ref を commit 数で語らないこと"
    assert out.systemctl_calls == "", "拒否は systemctl を一度も呼ばないこと"
