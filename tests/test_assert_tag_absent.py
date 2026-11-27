"""付いていないタグを「無い」と確定する (`PUT /api/posts/tag-pin-bulk` / `PUT /api/post/{id}/tag-pin` の state='absent')。

## なぜ (2026-11-15、owner)

「今の仕組みでは、人間は tagger が誤って振った tag を負例にすることができても、
そうでないものを負例にすることはできない」。absent pin は「付いていたタグを人が
外した」ときにしか生まれなかったので、負例 GT はタガーの誤検知だけでできていた。
タガーが付けなかった画像を人が見て「無い」と残す経路が、この state。

## この test が守るもの

- 付いていない post に absent pin が立つ (= pin 同期が負例 GT にする入力)
- 付いている post には触れない (bulk は skipped、単発は 409) — 何も消さない
- 来歴の audit 行が残る / undo (state=null) で pin が消える

`tests/test_bulk_tag_remove.py` と同じく、動いている kura-api を HTTP で叩き、
使い捨ての post を作って消す。届かなければ skip。
"""
from __future__ import annotations

import pytest

from tests.test_bulk_tag_remove import (TAG, _give_the_tag, _req, _tag_names, _upload_post, pytestmark)  # noqa: F401


def _pins(post_ids: list[int]) -> dict:
    st, d = _req("GET", "/api/post-pin-states?ids=" + ",".join(map(str, post_ids)))
    assert st == 200, d
    return {int(k): v for k, v in (d.get("states") or {}).items()}


@pytest.fixture()
def pair():
    """[タグ付き, タグ無し] の使い捨て post。タグ自体は付与で作られる。"""
    with_tag, without = _upload_post(), _upload_post()
    _give_the_tag(with_tag)
    yield with_tag, without
    for pid in (with_tag, without):
        _req("DELETE", f"/api/post/{pid}")


def test_an_untagged_post_is_recorded_as_not_having_it(pair):
    with_tag, without = pair
    st, d = _req("PUT", "/api/posts/tag-pin-bulk", {"tag": TAG, "state": "absent", "post_ids": [with_tag, without]})
    assert st == 200, d
    assert (d["absent"], d["skipped"]) == (1, 1), d
    pins = _pins([with_tag, without])
    assert pins.get(without, {}).get(TAG) == "absent"
    assert pins.get(with_tag, {}).get(TAG) != "absent"
    assert TAG in _tag_names(with_tag), "a tagged post must be left as it was"


def test_undo_takes_the_decision_back(pair):
    _, without = pair
    assert _req("PUT", "/api/posts/tag-pin-bulk", {"tag": TAG, "state": "absent", "post_ids": [without]})[0] == 200
    st, d = _req("PUT", "/api/posts/tag-pin-bulk", {"tag": TAG, "state": None, "post_ids": [without]})
    assert st == 200 and d["unlocked"] == 1, d
    assert _pins([without]).get(without, {}).get(TAG) is None


def test_the_single_form_refuses_a_post_that_carries_the_tag(pair):
    with_tag, without = pair
    st, d = _req("PUT", f"/api/post/{with_tag}/tag-pin", {"tag": TAG, "state": "absent"})
    assert st == 409, d
    st, d = _req("PUT", f"/api/post/{without}/tag-pin", {"tag": TAG, "state": "absent"})
    assert st == 200 and d["pinned"] == "absent", d


def test_an_unknown_state_is_still_refused(pair):
    _, without = pair
    st, _ = _req("PUT", "/api/posts/tag-pin-bulk", {"tag": TAG, "state": "maybe", "post_ids": [without]})
    assert st == 422
