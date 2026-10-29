"""検索候補が示した語が、そのまま pool mode でも当たることを固定する。

■ 何が起きていたか (2026-09-05, user 報告)

ヘッダーの検索候補は `/api/tags` 由来 — つまり **タグ**を出し、その使用回数
(`soba647` なら 5,431) を添える。利用者はその候補を選び、pool mode で検索した。
結果は **0 件**。

    /api/tags?query=soba647       → soba647 (artist) usages 5,431
    /api/pools?query=soba647      → 0          ← 候補が示した語なのに
    /api/pools?query=tag:soba647  → 16 pools
    /api/posts?query=soba647      → 5,431

pool 検索の裸語は **pool 名**にしか当たっていなかった。soba647 の pool は
`2025 Dec.1 JPEG unprocessed` のような set 名で、名前に語を含まない。post mode で
は裸語がタグ検索なので、同じ文字列の意味が mode で変わり、切り替えた瞬間に 0 件に
なる。UI が「5,431 件ある」と示した語が 0 件を返すのは、検索が壊れているのと同じ。

■ ここで固定するもの

  1. pool の裸語は「pool 名 **または** メンバーのタグ」— 候補どおりに当たる
  2. `name:` は従来どおり名前だけに絞れる (OR に飲み込まれない)
  3. `tag:` は post mode でも裸語と同義 — pool mode が付ける prefix を持ったまま
     mode を切り替えても 0 件にならない

対象データはハードコードしない: 実在するタグを API から選び、その語で測る。
"""
from __future__ import annotations

import json
import os
import subprocess
import urllib.parse
import urllib.request
from pathlib import Path

import pytest

API_BASE = os.environ.get("KURA_API_BASE", "http://127.0.0.1:8001").rstrip("/")


def _get(path: str, **params):
    qs = urllib.parse.urlencode(params)
    with urllib.request.urlopen(f"{API_BASE}{path}?{qs}", timeout=30) as r:
        return json.loads(r.read())


def _api_up() -> bool:
    try:
        urllib.request.urlopen(f"{API_BASE}/api/build", timeout=5)
        return True
    except Exception:
        return False


def _api_is_this_revision() -> tuple[bool, str]:
    """API が **この checkout の revision** で動いているか。

    ここは live API を測る test なので、答えるのは「叩いた先の revision」であって
    この作業ツリーではない。まだ deploy されていない fix を、古い revision で動く
    production API に対して測れば当然 red になり、その red は欠陥ではなく
    「別の revision を見た」という事実でしかない (2026-09-05 実測: 修正前の 8001 に
    対して 2 本が落ちた)。合わなければ skip し、理由に両方の sha を書く。
    """
    repo = Path(__file__).resolve().parent.parent
    try:
        with urllib.request.urlopen(f"{API_BASE}/api/build", timeout=5) as r:
            build = json.loads(r.read())
    except Exception as exc:  # noqa: BLE001
        # 文言は tests/test_live_api_tests_declare_their_dependency.py の
        # 機構が読む ("not reachable" を含むこと)。
        return False, (f"kura API not reachable at {API_BASE} "
                       f"({exc.__class__.__name__})")
    running, running_dirty = build.get("commit", ""), bool(build.get("dirty"))
    head = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=repo,
                          capture_output=True, text=True).stdout.strip()
    local_dirty = bool(subprocess.run(["git", "status", "--porcelain"], cwd=repo,
                                      capture_output=True, text=True).stdout.strip())
    if not running or not head:
        return False, "revision を照合できない (build 情報または git が無い)"
    same_commit = running.startswith(head) or head.startswith(running)
    # commit だけでは足りない: 未 commit の修正を持つ作業ツリーと、同じ commit で
    # 動いている production API は「同じ revision」に見えて中身が違う。実際それで
    # skip が効かず red になった (2026-09-05)。dirty も一致条件に入れる。
    same = same_commit and running_dirty == local_dirty
    return same, (f"API は {running}{' (dirty)' if running_dirty else ''} で動いており、"
                  f"この checkout は {head}{' (dirty)' if local_dirty else ''}")


_MATCH, _WHY = _api_is_this_revision()
pytestmark = pytest.mark.skipif(not _MATCH, reason=_WHY)


@pytest.fixture(scope="module")
def a_tag_with_pools() -> str:
    """pool を持つ実在タグを 1 つ選ぶ (無ければ skip — 捏造しない)。"""
    for cat_probe in ("a", "e", "i", "o", "s"):
        for sug in _get("/api/tags", query=cat_probe, limit=20):
            name = sug["names"][0]
            if _get("/api/pools", query=f"tag:{name}", limit=1)["total"] > 0:
                return name
    pytest.skip("no tag with pools found on this node")


def test_a_bare_term_finds_pools_by_member_tag(a_tag_with_pools):
    """候補が示した語をそのまま pool mode に入れて 0 件にならない。"""
    tag = a_tag_with_pools
    by_tag = _get("/api/pools", query=f"tag:{tag}", limit=1)["total"]
    bare = _get("/api/pools", query=tag, limit=1)["total"]
    assert by_tag > 0
    assert bare >= by_tag, (
        f"裸語 {tag!r} が tag: より少ない — 候補が示した語が pool mode で落ちている")


def test_name_still_narrows_to_the_name(a_tag_with_pools):
    """OR にしたことで `name:` が効かなくなっていない。

    最初の実装は裸語と `name:` を同じ list に入れてしまい、`name:` まで OR に
    飲み込まれた (`name:soba647` が 17 件 = 裸語と同じ)。件数の大小だけを見る
    検査はそれを素通しするので、**返ってきた pool の名前に語が入っているか**を見る。
    """
    tag = a_tag_with_pools
    res = _get("/api/pools", query=f"name:{tag}", limit=20)
    for pool in res["results"]:
        assert any(tag.lower() in n.lower() for n in pool["names"]), (
            f"name:{tag} が名前に語を含まない pool を返した: {pool['names']!r} — "
            "`name:` が名前限定でなくなっている")


def test_tag_prefix_means_the_same_thing_in_post_mode(a_tag_with_pools):
    """pool mode が付ける `tag:` を持ったまま post mode へ切り替えても同じ。"""
    tag = a_tag_with_pools
    bare = _get("/api/posts", query=tag, limit=1)["total"]
    qualified = _get("/api/posts", query=f"tag:{tag}", limit=1)["total"]
    assert bare == qualified, (
        f"post mode で `tag:{tag}` が裸語と違う結果 ({qualified} vs {bare}) — "
        "mode を切り替えた瞬間に 0 件になる形")


def test_an_unknown_term_is_still_zero():
    """OR にしたことで「無い語が当たる」ようになっていない。"""
    nonsense = "zzz-kura-no-such-term-zzz"
    assert _get("/api/pools", query=nonsense, limit=1)["total"] == 0
    assert _get("/api/posts", query=nonsense, limit=1)["total"] == 0
