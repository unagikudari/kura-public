"""タグ別の採用規則を**スキーマから**読む。コード側に閾値も gate も置かない。

■ なぜ (user 2026-08-06:「すべてのタグが平等に昇格できるようにしろ。マジック
   ナンバーや特殊変数で管理するな。スキーマを整えろ。一本化しろ」)

`tagger/infer.py` にはタグ別の判断が dict リテラルで残っていた:

    PIXAI_LOW_THRESH / PIXAI_HIGH_THRESH
    MLDANBOORU_LOW_THRESH / MLDANBOORU_HIGH_THRESH
    WD14_FIXED_GATES
    MUTEX_PAIRS / MUTEX_PAIRS_SOLO_ONLY
    CHASTITY_*_REMOVE_BELOW / _KEEP_ABOVE

読み方も `dict.get(name, DEFAULT)` = **行が無ければ既定値で埋める**。埋まると
「設定されたのか、埋められたのか」が区別できない。migration 0138 でこれらを
すべて行にしたので、ここはその読み口。

■ 条件の評価器は 1 本

gate も mutex の適用条件も「**タグ集合に対する条件**」という 1 概念。
0138 以前は worker が `all(...)`、infer が `any(...)` と **同名で逆の意味**
だった (要素 1 個の間は同値なので露見しない)。`_is_solo()` に至っては
「solo タグがある」ですらなく `(solo|1girl|1boy) かつ 複数系が無い` だった。

`tagger_tag_condition` の all/any/none 3 節と `holds()` 1 本で全部書ける。
**評価器が 1 つしかないので、2 実装が食い違うことが起きない。**
"""
from __future__ import annotations

import time
from collections.abc import Container, Mapping, Sequence
from dataclasses import dataclass, field

# 既定値をコードに置かない。**行が無ければ「規則が無い」**であって
# 「既定値を使う」ではない — producer の既定閾値もまた行 (0138 表 B)。
_TTL_SECONDS = 300.0


@dataclass(frozen=True)
class Condition:
    """タグ集合に対する条件。3 節そろえて初めて既存の全用途が書ける。"""

    all_tags: tuple[str, ...] = ()
    any_tags: tuple[str, ...] = ()
    none_tags: tuple[str, ...] = ()

    def holds(self, present: Container[str]) -> bool:
        if any(t not in present for t in self.all_tags):
            return False
        if self.any_tags and not any(t in present for t in self.any_tags):
            return False
        return all(t not in present for t in self.none_tags)


ALWAYS = Condition()


@dataclass(frozen=True)
class Rule:
    """ある producer の あるタグ出力を、どう採用するか。

    `threshold` 以上で付与/保持、`remove_below` 未満で削除、その間 (保留域) は
    触らない。`remove_below == threshold` なら保留域は幅 0 で、これが素直な
    「閾値 1 本」の規則になる — 特別扱いを作らずに両方を表せる。
    """

    producer: str
    tag: str
    threshold: float
    remove_below: float
    may_add: bool
    may_remove: bool
    condition: Condition = ALWAYS


@dataclass
class RuleSet:
    rules: dict[tuple[str, str], Rule] = field(default_factory=dict)
    # (producer, category) -> threshold。タグ別の行が無いときの値。
    producer_defaults: dict[tuple[str, int], float] = field(default_factory=dict)
    # (tag_a, tag_b, condition) — a と b は同時に立たない。
    exclusions: tuple[tuple[str, str, Condition], ...] = ()

    def threshold(self, producer: str, tag: str, category: int) -> float | None:
        """このタグをこの producer から採るときの閾値。

        規則行 → producer x category の既定 → **None**。
        None は「この producer はこのタグを採らない」で、呼ぶ側が既定値を
        でっち上げてはいけない。
        """
        r = self.rules.get((producer, tag))
        if r is not None:
            return r.threshold
        return self.producer_defaults.get((producer, category))

    def rule(self, producer: str, tag: str) -> Rule | None:
        return self.rules.get((producer, tag))

    def resolve_exclusions(self, scores: Mapping[str, float]) -> dict[str, float]:
        """排他を解く。**条件は `Condition.holds` 1 本で判定する。**

        条件は解き始める前のタグ集合で評価する — 解きながら評価すると、
        どのタグを先に消したかで結果が変わる (`_is_solo` は solo/1girl/1boy を
        見るので、それ自身が排他の対象になりうる)。
        """
        present = set(scores)
        out = dict(scores)
        for a, b, cond in self.exclusions:
            if a in out and b in out and cond.holds(present):
                out.pop(b if out[a] >= out[b] else a)
        return out


def _condition(raw: Mapping[str, object]) -> Condition:
    return Condition(
        all_tags=tuple(raw.get("all_tags") or ()),
        any_tags=tuple(raw.get("any_tags") or ()),
        none_tags=tuple(raw.get("none_tags") or ()),
    )


def parse(doc: Mapping[str, object]) -> RuleSet:
    """API 応答から RuleSet を組む。**ここに既定値は書かない。**"""
    rs = RuleSet()
    for r in doc.get("rules") or ():
        cond = _condition(r)
        rs.rules[(r["producer"], r["tag"])] = Rule(
            producer=r["producer"], tag=r["tag"],
            threshold=float(r["threshold"]),
            remove_below=float(r["remove_below"]),
            may_add=bool(r["may_add"]), may_remove=bool(r["may_remove"]),
            condition=cond)
    for d in doc.get("producer_defaults") or ():
        rs.producer_defaults[(d["producer"], int(d["category"]))] = float(d["threshold"])
    rs.exclusions = tuple(
        (e["tag_a"], e["tag_b"], _condition(e)) for e in (doc.get("exclusions") or ()))
    return rs


class _Cache:
    def __init__(self) -> None:
        self.at = 0.0
        self.value: RuleSet | None = None


_CACHE = _Cache()


def load(fetch, *, now=time.monotonic) -> RuleSet:
    """規則を取る。`fetch()` は API 応答の dict を返す呼び出し可能。

    **取れなければ古いものを使わない。** 規則が変わったのに古い値で当て続けると、
    どのノードが何を当てたか誰にも分からなくなる (2026-08-06 に head で実際に
    起きた)。例外はそのまま上げて、呼ぶ側に fail-closed させる。
    """
    t = now()
    if _CACHE.value is not None and t - _CACHE.at < _TTL_SECONDS:
        return _CACHE.value
    rs = parse(fetch())
    _CACHE.value, _CACHE.at = rs, t
    return rs


def reset_cache() -> None:
    _CACHE.value, _CACHE.at = None, 0.0
