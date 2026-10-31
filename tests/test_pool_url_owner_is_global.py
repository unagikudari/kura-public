"""`pool_url.url` のグローバル UNIQUE を、局所的な検査で守ろうとしない (kura#70)。

■ 何が起きていたか (2026-09-05 実測、直近 7 日)

    135  PUT /api/pool/{id}  →  500
     53  asyncpg UniqueViolationError: "pool_url_url_unique"

`update_pool` は `primarySourceUrl` を書く前に「**この** pool が primary_url を
持っているか」しか見ていなかった。制約は URL 単位でグローバルなので、他 pool が
既に持つ URL を渡すと UPDATE 側も INSERT 側も違反する。呼び手 (web UI / script) には
「サーバの不具合」としか見えないが、実際には **409 で表現できる正常な拒否**だった。

拡張の生 SQL の経路は例外なく
`ON CONFLICT (url) DO NOTHING` を持っている。ORM 経路だけが規律の外にあった —
個人の注意力ではなく経路の非対称性なので、ここで固定する。

■ ここで見るもの

  1. live: 他 pool が持つ URL を渡すと **409** (500 でも、黙って無視でもない)。
     409 の本文は所有者の pool_id を名指す — 「誰のものか」を言わないと、呼び手は
     次に何をすればよいか分からない
  2. live: 拒否のあとも所有権が動いていない (副作用のない拒否)
  3. static: 書き込みの前に所有者を引いている (ON CONFLICT DO NOTHING で畳むと、
     成功したように見えて設定されない別の嘘になるので、その形も禁じる)

live 部分は叩いた先が **この checkout の revision** のときだけ走る (kura#71 と同じ
guard) — 未 deploy の修正を古い revision の API で測っても、それは欠陥ではなく
「別の revision を見た」でしかない。
"""
from __future__ import annotations

import json
import os
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
API_BASE = os.environ.get("KURA_API_BASE", "http://127.0.0.1:8001").rstrip("/")


def _get(path: str, **params):
    qs = urllib.parse.urlencode(params)
    with urllib.request.urlopen(f"{API_BASE}{path}?{qs}", timeout=30) as r:
        return json.loads(r.read())


def _auth_header() -> dict:
    """kura 本体と同じ `Authorization: Token base64(user:token)`。

    値は api/env から読むだけで、どこにも出力しない。
    """
    import base64
    env = {}
    for line in (Path.home() / "kura" / "api" / "env").read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.strip().startswith("#"):
            k, _, v = line.strip().partition("=")
            env[k] = v.strip('"').strip("'")
    user, token = env.get("KURA_USER"), env.get("KURA_TOKEN")
    if not user or not token:
        pytest.skip("api/env に KURA_USER/KURA_TOKEN が無い")
    raw = base64.b64encode(f"{user}:{token}".encode()).decode()
    return {"Authorization": f"Token {raw}"}


def _put(path: str, body: dict):
    req = urllib.request.Request(
        f"{API_BASE}{path}", data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", **_auth_header()}, method="PUT")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.loads(r.read() or b"null")
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw or b"null")
        except json.JSONDecodeError:
            return e.code, {"raw": raw[:200].decode(errors="replace")}


def _api_is_this_revision() -> tuple[bool, str]:
    try:
        with urllib.request.urlopen(f"{API_BASE}/api/build", timeout=5) as r:
            build = json.loads(r.read())
    except Exception as exc:  # noqa: BLE001
        return False, f"kura API not reachable at {API_BASE} ({exc.__class__.__name__})"
    running, running_dirty = build.get("commit", ""), bool(build.get("dirty"))
    head = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=REPO,
                          capture_output=True, text=True).stdout.strip()
    local_dirty = bool(subprocess.run(["git", "status", "--porcelain"], cwd=REPO,
                                      capture_output=True, text=True).stdout.strip())
    if not running or not head:
        return False, "revision を照合できない (build 情報または git が無い)"
    same = (running.startswith(head) or head.startswith(running)) and running_dirty == local_dirty
    return same, (f"API は {running}{' (dirty)' if running_dirty else ''} で動いており、"
                  f"この checkout は {head}{' (dirty)' if local_dirty else ''}")


_MATCH, _WHY = _api_is_this_revision()
live = pytest.mark.skipif(not _MATCH, reason=_WHY)


@pytest.fixture(scope="module")
def two_pools_with_urls():
    """primary URL を持つ別々の pool を 2 つ選ぶ (実データ。作らない)。"""
    found = []
    for pool in _get("/api/pools", limit=40)["results"]:
        detail = _get(f"/api/pool/{pool['id']}")
        url = (detail.get("primarySourceUrl") or "").strip()
        if url and not url.startswith("manual:pool/"):
            found.append((pool["id"], url))
        if len(found) == 2:
            return found
    pytest.skip("primary URL を持つ pool が 2 つ見つからない")


@live
def test_taking_another_pools_url_is_a_409_not_a_500(two_pools_with_urls):
    (a_id, a_url), (b_id, _b_url) = two_pools_with_urls
    status, body = _put(f"/api/pool/{b_id}", {"primarySourceUrl": a_url})
    assert status == 409, f"期待 409、実際 {status} ({body})"
    detail = str(body.get("detail", ""))
    assert str(a_id) in detail, f"409 が所有者 pool を名指していない: {detail!r}"


@live
def test_the_refusal_leaves_ownership_where_it_was(two_pools_with_urls):
    (a_id, a_url), (b_id, b_url) = two_pools_with_urls
    _put(f"/api/pool/{b_id}", {"primarySourceUrl": a_url})
    assert _get(f"/api/pool/{a_id}").get("primarySourceUrl") == a_url
    assert _get(f"/api/pool/{b_id}").get("primarySourceUrl") == b_url


def test_the_write_path_asks_who_owns_the_url_first():
    """静的: 書き込みの前に所有者を引いている。"""
    src = (REPO / "api" / "routers" / "pools.py").read_text(encoding="utf-8")
    i = src.index("async def update_pool(")
    block_start = src.index('new_src_url = (body.get("primarySourceUrl")', i)
    block_end = src.index("if \"scope\" in body", block_start)
    fn = src[block_start:block_end]
    # 「なぜ ON CONFLICT を使わないか」を説明する comment 自体が引っかからないよう、
    # 検査は実行される行だけを見る。
    code = "\n".join(l for l in fn.splitlines() if not l.lstrip().startswith("#"))
    assert "PoolUrl.url == new_src_url" in fn, (
        "update_pool が URL の所有者を引いていない — 局所検査でグローバル制約を"
        "守ろうとする形に戻っている")
    assert "status_code=409" in fn, "他 pool のものを 409 で拒否していない"
    assert "ON CONFLICT" not in code, (
        "拒否を ON CONFLICT DO NOTHING で畳むと、成功したように見えて設定されない"
        "という別の嘘になる")
