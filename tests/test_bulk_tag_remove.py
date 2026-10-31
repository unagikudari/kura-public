"""1 タグ × N post の一括解除 (`PUT /api/posts/tag-remove-bulk`)。

## なぜ (2026-09-06、user 報告)

「過検知を検索して解除しているが、一括選択から削除に時間がかかって困っている」。
本番のアクセスログで実測:

    20 分で PATCH 891 件
    適用ループ内 (<0.6s の連続) 742 区間、**中央値 101 ms**
    PATCH 1 件の往復 (proxy 込み) 73-98 ms
    個別 SQL は sub-ms / index は揃っている / ロック待ちなし / API の CPU 0.1%

遅いのは DB でもクエリでもなく、**選択件数ぶんの HTTP 往復を 1 件ずつ待っていた**。

## この test が守るもの

速さではなく**意味論**。速い経路が別の意味を持つのが最悪だから。

user 裁定 (2026-09-06):「人間が操作したというのが human negative/positive の要点で
あって一括操作に問題はない。この操作によって tagger が再学習を行い tag は補正される」
— つまりこの削除は「手で直して終わり」ではなく**再学習の入力**である。だから
`absent` pin と監査行は速さより優先される: pin を落とせば負例 GT が生まれず、
タガーは次の巡回で同じ過検知を付け直す。速いだけで仕事が戻ってくる。

`tests/test_invm_human_pin.py` の慣習に従い、動いている kura-api を HTTP で叩き、
使い捨ての post を自分で作って自分で消す。API に届かなければ skip する
(見ていないことを PASS と書かないため)。
"""
from __future__ import annotations

import base64
import io
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

ENV_FILE = Path.home() / "kura" / "api" / "env"
TAG = "test_bulk_remove_probe"


def _load_env() -> dict:
    env = {}
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.strip().startswith("#"):
                k, _, v = line.strip().partition("=")
                env[k] = v.strip('"').strip("'")
    return env


_ENV = _load_env()
API_BASE = os.environ.get("KURA_API_BASE", "http://127.0.0.1:8001").rstrip("/")
KURA_USER = os.environ.get("KURA_USER") or _ENV.get("KURA_USER", "")
KURA_TOKEN = os.environ.get("KURA_TOKEN") or _ENV.get("KURA_TOKEN", "")
API_KEY = os.environ.get("KURA_API_KEY") or _ENV.get("KURA_API_KEY", "")


def _headers(content_type: str | None = "application/json") -> dict:
    h = {}
    if content_type:
        h["Content-Type"] = content_type
    if KURA_USER and KURA_TOKEN:
        # scheme は "Token" — kura の _parse_token_header がそう読む
        # (test_invm_human_pin.py と同じ形)。"Basic" だと 401 になる。
        h["Authorization"] = "Token " + base64.b64encode(
            f"{KURA_USER}:{KURA_TOKEN}".encode()).decode()
    if API_KEY:
        h["X-Api-Key"] = API_KEY
    return h


def _req(method: str, path: str, body: dict | None = None) -> tuple[int, dict]:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(f"{API_BASE}{path}", data=data,
                                 headers=_headers(), method=method)
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, json.loads(r.read() or "{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read() or "{}")
        except Exception:  # noqa: BLE001
            return e.code, {}


def _reachable() -> bool:
    try:
        urllib.request.urlopen(f"{API_BASE}/api/build", timeout=5)
        return bool(KURA_USER and KURA_TOKEN)
    except Exception:  # noqa: BLE001
        return False


pytestmark = pytest.mark.skipif(
    not _reachable(),
    reason=f"kura-api に届かない / 資格が無い ({API_BASE}) — 測れていないので PASS ではなく skip")


def _unique_png() -> bytes:
    """毎回**内容の違う** 1x1 PNG。

    同じバイト列を 2 回上げると `00_post_dup_skip` が 2 件目を既存 post に潰すので、
    fixture の 2 件が同一 post になり「2 件中 1 件しか消えない」という偽の失敗が出る
    (最初この test はそれで落ちた)。色を変えて内容を分ける。
    """
    import struct, zlib, random

    def chunk(kind: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + kind + data
                + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF))

    rgb = bytes(random.randrange(256) for _ in range(3))
    raw = b"\x00" + rgb                      # filter byte + 1 pixel
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw))
            + chunk(b"IEND", b""))


def _upload_post() -> int:
    png = _unique_png()
    boundary = "----kurabulkremovetest"
    body = io.BytesIO()
    body.write(f"--{boundary}\r\n".encode())
    body.write(b'Content-Disposition: form-data; name="file"; filename="probe.png"\r\n')
    body.write(b"Content-Type: image/png\r\n\r\n")
    body.write(png)
    body.write(f"\r\n--{boundary}--\r\n".encode())
    req = urllib.request.Request(
        f"{API_BASE}/api/upload", data=body.getvalue(),
        headers=_headers(f"multipart/form-data; boundary={boundary}"), method="POST")
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read())["id"]


def _post(post_id: int) -> dict:
    st, d = _req("GET", f"/api/post/{post_id}")
    assert st == 200, d
    return d


def _tag_names(post_id: int) -> set[str]:
    return {t["names"][0] for t in _post(post_id).get("tags", [])}


def _give_the_tag(post_id: int) -> None:
    p = _post(post_id)
    names = [t["names"][0] for t in p.get("tags", [])] + [TAG]
    st, d = _req("PATCH", f"/api/post/{post_id}", {"version": p["version"], "tags": names})
    assert st == 200, d


@pytest.fixture()
def probes():
    """使い捨ての post を 2 つ作り、対象タグを載せて渡す。後片付けまで持つ。"""
    ids = [_upload_post(), _upload_post()]
    for pid in ids:
        _give_the_tag(pid)
    yield ids
    for pid in ids:
        _req("DELETE", f"/api/post/{pid}")


def test_one_request_clears_the_tag_from_every_post(probes):
    a, b = probes
    st, d = _req("PUT", "/api/posts/tag-remove-bulk", {"tag": TAG, "post_ids": [a, b]})
    assert st == 200, d
    assert d["removed"] == 2 and d["skipped"] == 0
    assert TAG not in _tag_names(a) and TAG not in _tag_names(b)


def test_a_post_that_does_not_carry_it_is_skipped_not_an_error(probes):
    """一括では部分成立が正 — tag-pin-bulk と同じ判断。"""
    a, b = probes
    _req("PUT", "/api/posts/tag-remove-bulk", {"tag": TAG, "post_ids": [a]})
    st, d = _req("PUT", "/api/posts/tag-remove-bulk", {"tag": TAG, "post_ids": [a, b]})
    assert st == 200 and d["removed"] == 1 and d["skipped"] == 1


def test_the_human_decision_survives_as_a_negative(probes):
    """**この test がこの endpoint の存在理由**。

    user 裁定: この操作は再学習の入力である。`absent` pin が残らなければ負例 GT が
    生まれず、タガーは次の巡回で同じ過検知を付け直す — 速いだけで仕事が戻ってくる。
    """
    a, _ = probes
    _req("PUT", "/api/posts/tag-remove-bulk", {"tag": TAG, "post_ids": [a]})
    st, d = _req("GET", f"/api/post-pin-states?ids={a}")   # 引数名は ids (api.ts と同じ)
    assert st == 200, f"pin state を読めない ({st}) — この test の核心が測れていない: {d}"
    assert (d.get("states") or {}).get(str(a), {}).get(TAG) == "absent", (
        "absent pin が残っていない — 人の判断が負例として記録されず、"
        "タガーが同じタグを付け直せてしまう")


def test_the_version_moves_so_an_open_tab_cannot_undo_it(probes):
    """単発 PATCH は version を進める。速い経路がそれを怠ると、開いたままのタブが
    古い version で書き戻して削除を無かったことにできる。"""
    a, _ = probes
    before = _post(a)["version"]
    _req("PUT", "/api/posts/tag-remove-bulk", {"tag": TAG, "post_ids": [a]})
    assert _post(a)["version"] > before


def test_it_refuses_shapes_it_cannot_mean(probes):
    a, _ = probes
    assert _req("PUT", "/api/posts/tag-remove-bulk", {"tag": "", "post_ids": [a]})[0] == 422
    assert _req("PUT", "/api/posts/tag-remove-bulk", {"tag": TAG, "post_ids": []})[0] == 422
    assert _req("PUT", "/api/posts/tag-remove-bulk",
                {"tag": TAG, "post_ids": list(range(1001))})[0] == 422
    assert _req("PUT", "/api/posts/tag-remove-bulk",
                {"tag": "no_such_tag_probe_xyz", "post_ids": [a]})[0] == 404
