# Description: head の適用方法を DB の行から読む唯一の入口 (定数を持たない)
# Kind: lib
# Effects: read-only (tagger_head_policy / tagger_patch)
"""どの head を、どの閾値で、どう当てるか — **行から読む**。

■ なぜこの module があるか (2026-08-05, user 指摘)

「すべてのタグが平等に昇格できるようにしろ。その際にマジックナンバーや
特殊変数で管理するな。スキーマを整えろ。一本化しろ」

昇格は自動化されていたのに、**適用は人が Python を編集しないと起きなかった**:

    infer.py            GREYOWL_SMALL_PENIS_CKPT / _DARK_PENIS_ / _DARK_PERINEUM_
                        load_greyowl_heads() の dict リテラル
                        GREYOWL_ADD_THRESH = 0.5
                        PIXAI_LOW_THRESH = {"smegma": 0.25}
                        WD14_FIXED_GATES = {"wrinkled_labia": ("pussy",), ...}
    nodec_autotag_worker._HEAD_SPECS
    reap_authorized.json の "authorized" 配列

monochrome / greyscale は patch まで到達したが、どの loader もその名前を
知らないので**永久に適用されない**。タグが平等でないとはこのこと。

■ 置き換えるもの

`tagger_head_policy` (migration 0134) の 1 行が、上の定数すべてを代替する。
値は昇格時に**測定から導出**される (auto_review.promote_validated) ので、
新しいタグが増えても書き足すコードは無い。

    threshold   ← 測定の動作点が選んだ閾値 (nested[rule].thr)
    apply_mode  ← 測定を経たか (recall の高低ではない — 下の注記)
    gate_tags   ← 測定が domain を絞っていればその tag、でなければ空
    content_sha256 ← artifact の identity (ファイル名ではなく中身の hash)

■ ここに閾値の既定値を置かない

`GREYOWL_ADD_THRESH = 0.5` のような「全 head 共通の既定閾値」は作らない。
根拠の無い閾値は、測定した閾値と見分けがつかないまま配備に混ざる。
policy 行が無い head は**適用しない** (fail-closed) — 「既定で 0.5」にすると、
測定していない head が黙って発火する。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

# **recall は記録であって拒否権ではない** (user 2026-08-05 の判断)。
#
# 初版はここに `min_recall = 0.85` を置き、下回る head を `add_only` に落として
# いた。**それは user が同じ日に明示的に取り下げた veto の再導入だった**:
#
#   「リープしろって言ってるんだ。旧 wd14 が振ったものが消えるだけだ。
#     人間の gt が消えるわけじゃない」
#
# 実際 reap の SQL は構造的に人手 GT へ届かない — 再タグした当の post だけを
# 対象に、今回の出力に無く、かつ `NOT pt.locked` (取り込み元由来を除外) かつ
# `post_tag_pin state='present'` が無い (人が確認したものを除外) 行しか消さない。
# 消えるのは**旧 wd14 が振って新しいタガーがもう出さないもの**だけ。
#
# したがって recall が低い head は「危険」ではなく「掃除が遅い」だけ。閾値で
# 弾くと、実測 recall 0.155 の smegma のような **最も汚れたタグほど掃除できない**
# という逆転が起きる (旧 reap_authorized.json の `_doc` が同じ結論に達している)。
#
# **では何が add_only を決めるのか**: 削除安全性の校正を経ていない head。
# grey_owl の 4 head (small_penis/dark_penis/dark_perineum/half-erect) は
# 教師が最小 46/92 枚で「削除の校正データが無い」ため ADD-ONLY 運用と明記されて
# いる (infer.py の元コメント)。これは recall の高低ではなく **測定を経たか**の
# 区別なので、`derive()` (vnext 測定から作る) は replace、legacy backfill は
# 明示的に add_only を渡す。
ADD_ONLY = "add_only"
REPLACE = "replace"


@dataclass(frozen=True)
class HeadPolicy:
    """1 つの head の適用方法。**この dataclass の外に適用の知識を置かない。**"""

    tag: str
    patch_version: int
    content_sha256: str
    operating_point: str
    threshold: float
    measured_recall: float
    measured_fpr: float
    apply_mode: str
    gate_tags: tuple[str, ...] = ()
    registration_id: int | None = None

    @property
    def removes_tags(self) -> bool:
        """この head は既存タグを消すか。呼び出し側が文字列比較しないための窓口。"""
        return self.apply_mode == REPLACE

    def fires_on(self, present_tags) -> bool:
        """この画像に当てるか。gate_tags が空なら無条件。

        gate は **すべて**揃っている必要がある (any ではなく all)。domain gate は
        「この視覚領域でだけ校正されている」の意味なので、一部だけ一致した画像は
        校正の外側にある。
        """
        if not self.gate_tags:
            return True
        have = set(present_tags or ())
        return all(g in have for g in self.gate_tags)


def apply_mode_for(*, measured: bool) -> str:
    """**削除してよいかは「測定を経たか」で決まる** (recall の高低ではない)。

    `measured=True` = vnext の測定を通り、正当な動作点と差し引き正の効果が
    確認された head → `replace` (この head の判定で置き換える)。
    `measured=False` = 削除安全性の校正を経ていない head → `add_only`。

    recall を閾値にしない理由は上の注記のとおり — それは user が取り下げた
    veto であり、**最も汚れたタグほど掃除できない**逆転を生む。
    """
    return REPLACE if measured else ADD_ONLY


# ── 測定 → policy の導出 (昇格時に 1 度だけ走る) ─────────────────────────

def operating_point_of(metrics: Mapping[str, Any] | None) -> tuple[str, str] | None:
    """`classification.at` を (arm, rule) に割る。'feat1024@max_f1' → ('feat1024','max_f1')。

    ここで形を仮定しない — 割れなければ None を返し、呼び出し側が fail-closed に
    落とす。「たぶんこの形だろう」で進むと、別形式の測定が黙って誤った閾値で
    配備される。
    """
    at = ((metrics or {}).get("classification") or {}).get("at")
    if not isinstance(at, str) or "@" not in at:
        return None
    arm, _, rule = at.partition("@")
    return (arm, rule) if arm and rule else None


def _arm_node(metrics: Mapping[str, Any], arm: str) -> Mapping[str, Any] | None:
    """測定から arm の節を引く。`vnext_arms` の grouping (B_veto_ceiling など) を
    跨いで探す — 配備側が測定の内部構造を知らずに済むように、探索はここだけ。"""
    for key in ("arms", "B_veto_ceiling", "C_dispatch_recipe", "E_stock_pixai"):
        node = metrics.get(key)
        if not isinstance(node, Mapping):
            continue
        if arm in node and isinstance(node[arm], Mapping):
            return node[arm]
        # arms は "B_veto_ceiling:feat1024" のように平らな場合がある
        for k, v in node.items():
            if isinstance(v, Mapping) and str(k).endswith(f":{arm}"):
                return v
    return None


def derive(metrics: Mapping[str, Any] | None, *,
           config: Mapping[str, Any] | None = None,
           gate_tags: Sequence[str] = ()) -> dict | None:
    """測定 1 件から policy の値を作る。導けなければ None (fail-closed)。

    **新しいタグのために書き足すコードはここには無い** — どのタグでも同じ手続き。
    """
    op = operating_point_of(metrics)
    if op is None:
        return None
    arm, rule = op
    node = _arm_node(metrics or {}, arm)
    nested = (node or {}).get("nested") if isinstance(node, Mapping) else None
    point = nested.get(rule) if isinstance(nested, Mapping) else None
    if not isinstance(point, Mapping):
        return None
    thr, recall, fpr = point.get("thr"), point.get("R"), point.get("FPR")
    if not isinstance(thr, (int, float)) or not (0.0 < float(thr) <= 1.0):
        # **閾値が無い測定からは policy を作らない。** 既定値で埋めると
        # 「測っていない閾値」が配備に混ざる (この module が無くす対象そのもの)。
        return None
    if not isinstance(recall, (int, float)) or not isinstance(fpr, (int, float)):
        return None
    return {
        "operating_point": f"{arm}@{rule}",
        "threshold": float(thr),
        "measured_recall": float(recall),
        "measured_fpr": float(fpr),
        # derive() は vnext 測定からしか呼ばれない = 測定を経た head。
        "apply_mode": apply_mode_for(measured=True),
        "gate_tags": list(gate_tags),
    }


# ── domain 宣言 → gate 導出 (昇格側の唯一の入口) ─────────────────────────

_DOMAIN_SQL = """
SELECT c.name, c.all_tags, c.any_tags, c.none_tags
FROM tagger_tag_rule r
JOIN tagger_tag_condition c ON c.name = r.condition
WHERE r.tag = :tag
"""


async def domain_gate_tags(db, tag: str) -> tuple[str, ...] | None:
    """curated 宣言 (tagger_tag_rule → tagger_tag_condition) から、この tag の
    head を発火させてよい視覚 domain を gate_tags として導出する。

    2026-09-03 の実害が根拠: dark_labia_minora は DB に `in_pussy_domain` と
    **宣言済み**だったのに、昇格 (derive) が宣言を読まず gate_tags=[] で policy を
    書いた。校正域 (uncensored∩pussy、域内 FPR 31%) の外を含む全画像に無条件
    発火し、アーカイブの 45% (447,770 post) に付いた。宣言と配備が別帳簿だと
    「書いてあるのに効かない」が作れてしまう — ここで一本化する。

    返り値:
      ()          — 'always' 系の宣言のみ (curator が無条件適用を明示した)
      ('pussy',)  — all_tags 条件の和集合 (この domain 内でのみ発火。複数
                    producer 行があれば**厳しい方に倒す** = 全条件の和)
      None        — 宣言が 1 行も無い、または gate_tags で表現できない条件
                    (any_tags / none_tags を含む)。呼び手は fail-closed —
                    「宣言が無い」を「無条件でよい」と読んだのが上の実害。
    """
    from sqlalchemy import text
    rows = (await db.execute(text(_DOMAIN_SQL), {"tag": tag})).mappings().all()
    if not rows:
        return None
    gates: set[str] = set()
    for r in rows:
        if (r["any_tags"] or []) or (r["none_tags"] or []):
            # gate_tags は all-of しか表現できない。any/none を黙って落とすと
            # 宣言より緩い配備になる — 表現できないなら止める。
            return None
        gates.update(r["all_tags"] or [])
    return tuple(sorted(gates))


# ── 読み出し (推論側の唯一の入口) ────────────────────────────────────────

# The policy in force per tag is defined once, by the tagger_current_head_policy view
# (migration 0186): the latest approved version. Its artifact may still not be a head
# bundle; `artifact_kind` says, and callers that load bytes check it.
_SELECT = """
SELECT p.tag, p.patch_version, p.content_sha256, p.operating_point, p.threshold,
       p.measured_recall, p.measured_fpr, p.apply_mode, p.gate_tags, p.registration_id
FROM tagger_current_head_policy p
ORDER BY p.tag
"""


def _row_to_policy(r) -> HeadPolicy:
    g = r["gate_tags"] if isinstance(r, Mapping) else r.gate_tags
    return HeadPolicy(
        tag=r["tag"], patch_version=r["patch_version"],
        content_sha256=r["content_sha256"], operating_point=r["operating_point"],
        threshold=float(r["threshold"]), measured_recall=float(r["measured_recall"]),
        measured_fpr=float(r["measured_fpr"]), apply_mode=r["apply_mode"],
        gate_tags=tuple(g or ()), registration_id=r["registration_id"])


async def load_all(db) -> list[HeadPolicy]:
    """配備中の head policy を全部読む (async SQLAlchemy 側)。"""
    from sqlalchemy import text
    rows = (await db.execute(text(_SELECT))).mappings().all()
    return [_row_to_policy(r) for r in rows]


def load_all_sync(conn) -> list[HeadPolicy]:
    """psycopg2 接続から読む (worker 側は同期)。"""
    cur = conn.cursor()
    cur.execute(_SELECT)
    cols = [c[0] for c in cur.description]
    return [_row_to_policy(dict(zip(cols, row))) for row in cur.fetchall()]


def authorized_tags(policies: Sequence[HeadPolicy]) -> set[str]:
    """既存タグを消してよい tag の集合。

    **reap_authorized.json の "authorized" 配列を置き換える。** あちらは 9 タグを
    手で列挙していたので、昇格しても列に載らない限り reap されなかった
    (= タグが平等でなかった)。ここでは昇格した head の apply_mode から導く。
    """
    return {p.tag for p in policies if p.removes_tags}
