"""head 学習の **計画づくり** — どの post を正例に、どれを負例のどの層に置くか。

GPU も backbone も要らない純粋関数だけを置く。`train_heads.py` は SQL と学習を持ち、
「何を学習集合に入れるか」の判断はここに集める。分けた理由は 2 つ:

  * この判断こそがレビューで割れた場所で、テストできる形にしないと議論が止まらない
  * 埋め込み 232,000 枚を回さないと確かめられない設計は、確かめられないのと同じ

---

## 層 (S1/S2/S3) — なぜ 3 つか

plan `kura-generic-head-trainer` rev 3 / 敵対的レビュー 2 席。

**S1 確定負例** — `verified_log` の hard-neg。人間が「これは違う」と剥がした例で、head を
騙した実績がある。最重要教師なので**クォータで削らない**。

**S2 混同候補** — 他タグの正例で、自タグを持たない post。キャラ head の実際の失敗は
「無関係な画像を誤検出する」ではなく「別のキャラと取り違える」なので、そこを直接叩く。
**ただし確定負例ではない** (下の PU 節)。

**S3 一般背景** — corpus 全域からの一様サンプル。これが無いと head は「狭いドメインでのみ
較正された」状態になり、`tagger/infer.py` の `WD14_FIXED_GATES` が書いているとおり
**ドメイン外では測定した precision が意味を失う**。解剖学タグは `uncensored∩pussy` のような
gate タグを持てるが、**キャラは持てない** (どんな文脈にも登場する)。gate を書けないから
ungated で走る、ungated で走るから corpus 全域で較正する — この順序が S3 の存在理由。

層は `rank` を持ち、`build_negatives` が昇順を **validator で強制**する。順序が呼び出し側の
慣習だと、引数の並べ替え 1 つで確定負例が弱い重みに落ちる (round 1 指摘)。

## PU — 「タグが無い」は「写っていない」ではない

kura のタグ付けは網羅的ではない。`'ada_wong' != any(tags)` は「Ada Wong が写っていない」
ではなく「そう付けられていない」。S2 をそのまま確定負例にすると、**正例を負例として
学習する**。

なので S2 は `weight < 1` の弱い負例として入り、人間が確認したものだけが S1 へ昇格する。

**`S2_DEFAULT_WEIGHT` は PU 学習から導出された値ではない** (round 1、両席)。class prior
推定だけでも定数は導けない。**実験的な設定値**であり、独立したラベル付き calibration set で
選び感度を報告すべきもの。FPR 評価用 support で調整してはならない (それは評価集合を
チューニングに使うこと)。設定であることが manifest に残る。

## 決定性

同じ GT・同じ設定なら同じ計画が出なければならない。既存コードは `neg[:len(pos)*2]` で
切っていたが、`_verneg` が **set** なので順序が未規定で、上限を超えると**どれが生き残るかが
実質任意**だった (2026-09-04 実測、`train_heads.py` 112/126/129 行)。

**注意**: `stable_order` は Python の md5。SQL 側の `order by md5(id::text)` と同じ順序に
なる保証はテストで固定しているが、**SQL が `LIMIT n` で先に絞った集合を Python で並べ直しても
絞り込みの偏りは解消しない** (round 1 指摘)。クォータ前に完全候補集合を渡すか、明示的な
overfetch 契約を持つこと — ここはその契約を要求するだけで、守らせるのは呼び出し側。
"""
from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass, field
from datetime import date
from typing import Callable, Iterable, Mapping, Sequence

#: S2 (混同候補) の重み。**導出値ではなく設定値**。上の PU 節を参照。
S2_DEFAULT_WEIGHT = 0.3

#: 確定負例 (S1) と、人間が確認して昇格した S2 の重み。
CONFIRMED_WEIGHT = 1.0

#: この選択アルゴリズムの版。manifest に残す — 計画を再構成するとき、どの規則で
#: 選ばれたかが分からなければ再構成にならない。
SELECTOR_VERSION = "planner/2 (stratified+PU, 2026-09-04)"
HASH_SPEC = "md5(str(post_id).encode()) hex, ascending"


class PlanError(RuntimeError):
    """計画づくりが fail closed で止まった。黙って学習に進ませないための型。"""


@dataclass(frozen=True)
class ShortfallWaiver:
    """不足を **期限付きで** 通す許可。期限を過ぎた waiver は無いのと同じ (fail closed)。

    `reason` は「なぜ今は不足のまま学習してよいか」。`expires` を過ぎると `build_negatives`
    が止まる — 記録が読まれるのを待つのではなく、読まざるを得ない失敗にする (round 3、両席)。
    """
    reason: str
    expires: date
    #: 誰が/何が許可したか (plan ref・issue・レビュー round 等)。manifest に残る。
    granted_by: str

    def is_valid_on(self, today: date) -> bool:
        return today <= self.expires


@dataclass(frozen=True)
class Stratum:
    """1 つの負例層。

    `rank` が優先順位で、`build_negatives` が昇順を強制する。`quota=None` は無制限
    (S1 のみ許す)。`confirmed` は「確定負例か」— PU の弱ラベルと区別する。
    """
    name: str
    ids: Sequence[int]
    weight: float
    quota: int | None
    rule: str
    rank: int
    confirmed: bool = False
    #: この id 列が **完全な候補集合**か。SQL が `LIMIT n` で絞った結果なら False にし、
    #: `source_limit=n` を渡す。既定を True にしないのは、黙って不足する方が既定に
    #: なってはいけないから — 呼び出し側に明示させる。
    complete: bool = True
    source_limit: int | None = None
    #: 不足したときの扱い。**既定は止める。** 通すには期限付きの `ShortfallWaiver` が要る。
    #:
    #: round 2 の版は `strict_shortfall: bool = False` で、既存 8 タグは「manifest に
    #: 出すだけ」だった。round 3 で両席が blocking: **記録は誰も読まなければ無いのと同じ**で、
    #: 「一回限りの実測は将来を守らない」のと同じ形。旧経路も同じ不足を抱えていたので
    #: 止めると本番 8 タグが全滅する、という事実は変わらない — だから **期限付きで通す**。
    #: 期限が切れれば planner が止まり、誰かが LIMIT を上げるか waiver を更新するしかない。
    #: 読まれる保証の無い記録を、読まざるを得ない失敗に変えるのが waiver の役目。
    shortfall_waiver: "ShortfallWaiver | None" = None


@dataclass
class Plan:
    tag: str
    pos: list[int]
    neg: list[int]
    weights: list[float]
    manifest: dict = field(default_factory=dict)


def stable_order(ids: Iterable[int]) -> list[int]:
    """安定な順序。既存 SQL の `order by md5(id::text)` と同じ流儀。

    id 昇順にしないのは、上限で切るときに常に若い id ばかり残るのを避けるため
    (若い post は古い post で、視覚的に偏る)。ハッシュ順なら偏らず、かつ再現する。
    """
    return sorted(ids, key=lambda i: hashlib.md5(str(i).encode()).hexdigest())


def _validate_strata(strata: Sequence[Stratum]) -> None:
    """層の不変条件を **validator で** 強制する。

    round 1 の指摘: 優先順位・無制限 quota・weight 1.0 が「呼び出し側の慣習」に留まって
    いた。慣習は引数の並べ替え 1 つで壊れる。
    """
    if not strata:
        raise PlanError("負例の層が 1 つも無い。空の負例で学習させない。")
    ranks = [s.rank for s in strata]
    if ranks != sorted(ranks):
        raise PlanError(
            f"層が rank 昇順で渡されていない: {[(s.name, s.rank) for s in strata]}。"
            " 順序は先勝ちの意味を持つので、並べ替えは確定負例を弱い重みに落とす。")
    if len(set(ranks)) != len(ranks):
        raise PlanError(f"rank が重複している: {ranks}。先勝ちの順序が決まらない。")
    for s in strata:
        if s.confirmed and s.weight != CONFIRMED_WEIGHT:
            raise PlanError(
                f"層 {s.name} は confirmed だが weight={s.weight}。"
                f" 確定負例は {CONFIRMED_WEIGHT} でなければ「確定」の意味が消える。")
        if not s.confirmed and s.weight >= CONFIRMED_WEIGHT:
            raise PlanError(
                f"層 {s.name} は未確定 (PU) だが weight={s.weight}。"
                " 未ラベルを確定負例として学習することになる。")
        if s.quota is None and not s.confirmed:
            raise PlanError(
                f"層 {s.name} は未確定なのに quota が無制限。"
                " 弱ラベルが確定負例を数で圧倒する。")
        if s.quota is not None and s.quota < 0:
            raise PlanError(f"層 {s.name} の quota が負: {s.quota}")
        # 黙った不足を禁じる (round 2 指摘)。SQL が `LIMIT n` で絞った列に対して
        # `quota > n` を要求すると、**足りないことに誰も気づかない**まま少ない負例で
        # 学習する。docstring で「完全集合を渡せ」と書くのは要求であって検査ではない。
        if not s.complete and s.source_limit is None:
            raise PlanError(
                f"層 {s.name} は complete=False なのに source_limit が無い。"
                " どこで切られたか分からなければ、不足しているかも分からない。")


def build_negatives(strata: Sequence[Stratum], *, exclude: Iterable[int] = (),
                    today: date | None = None) -> tuple[list[int], list[float], dict]:
    """層を順に詰めて負例列と重み列を作る。

    `exclude` は正例など、負例に入ってはならない id。**クォータの前に**落とす。
    同じ id が複数層に現れたら **先に来た層 (rank の小さい方) が勝つ**。

    **層内の重複も一意化する** — 旧版は `[5,5,5,7]` をそのまま 4 件として詰め、同じ負例が
    3 回入って暗黙に 3 倍の重みを持った (round 1 指摘、実測で再現)。

    `today` は waiver の期限判定に使う。テストが日付を動かせるように引数にしてある —
    「期限が切れたら止まる」は、切れた日を作れなければ検証できない。
    """
    _validate_strata(strata)
    today = today or date.today()
    ex = set(exclude)
    seen: set[int] = set()
    neg: list[int] = []
    weights: list[float] = []
    chosen: list[dict] = []
    manifest: dict = {
        "selector_version": SELECTOR_VERSION,
        "hash_spec": HASH_SPEC,
        "strata": [],
        "excluded_by_pos": 0,
        "cross_stratum_overlap": 0,
        "shortfalls": [],
    }

    for s in strata:
        # 層内の一意化 → 除外 → 既出除去 を **クォータの前に** すべて済ませる。
        # ループの中で落とすと、上限が先に埋まったとき manifest の除外数が嘘になる。
        uniq = stable_order(set(s.ids))
        dropped_dup_in = len(list(s.ids)) - len(set(s.ids))
        dropped_ex = sum(1 for i in set(s.ids) if i in ex)
        dropped_cross = sum(1 for i in set(s.ids) if i not in ex and i in seen)
        pool = [i for i in uniq if i not in ex and i not in seen]

        # 黙った不足の検出 (round 2 指摘)。SQL が LIMIT で切られた列に quota が届いて
        # いない場合、**足りないことに誰も気づかない**。既定は止める。通すには期限内の
        # waiver が要り、期限切れは waiver 無しと同じ (round 3 指摘、両席 blocking)。
        shortfall = None
        if not s.complete and s.source_limit is not None:
            if len(set(s.ids)) >= s.source_limit and (
                    s.quota is None or s.quota > s.source_limit):
                shortfall = {"source_limit": s.source_limit,
                             "fetched": len(set(s.ids)),
                             "quota": s.quota,
                             "note": "SQL が LIMIT に張り付いており、quota はそれを超える",
                             "waiver": None}
                w = s.shortfall_waiver
                if w is None:
                    raise PlanError(
                        f"層 {s.name}: SQL が LIMIT {s.source_limit} で切られており"
                        f" (取得 {len(set(s.ids))} 件)、quota={s.quota} はそれを超える。"
                        " 黙って不足するので、完全候補集合を渡すか LIMIT を上げること。"
                        " 既知の不足を期限付きで通すなら ShortfallWaiver を付けること。")
                if not w.is_valid_on(today):
                    raise PlanError(
                        f"層 {s.name}: 不足の waiver が {w.expires.isoformat()} に失効している"
                        f" (今日 {today.isoformat()}、理由: {w.reason}、許可: {w.granted_by})。"
                        " 期限切れの waiver は無いのと同じ。LIMIT を上げるか、根拠を書いて"
                        " waiver を更新すること。")
                shortfall["waiver"] = {**asdict(w), "expires": w.expires.isoformat(),
                                       "valid_on": today.isoformat()}

        taken_ids: list[int] = []
        for i in pool:
            if s.quota is not None and len(taken_ids) >= s.quota:
                break
            seen.add(i)
            taken_ids.append(i)
            neg.append(i)
            weights.append(s.weight)
            chosen.append({"post_id": i, "stratum": s.name, "rank": s.rank,
                           "weight": s.weight, "confirmed": s.confirmed})

        manifest["strata"].append({
            "name": s.name, "rank": s.rank, "rule": s.rule, "weight": s.weight,
            "confirmed": s.confirmed, "quota": s.quota,
            "offered": len(list(s.ids)), "unique": len(set(s.ids)),
            "eligible": len(pool), "taken": len(taken_ids),
            "dropped_duplicate_within_stratum": dropped_dup_in,
            "dropped_as_positive": dropped_ex,
            "dropped_as_duplicate": dropped_cross,
            "quota_exhausted": s.quota is not None and len(taken_ids) >= s.quota,
            "complete": s.complete, "source_limit": s.source_limit,
            "shortfall": shortfall,
        })
        if shortfall:
            manifest["shortfalls"].append({"stratum": s.name, **shortfall})
        manifest["excluded_by_pos"] += dropped_ex
        manifest["cross_stratum_overlap"] += dropped_cross

    # BACKSTOP — そして **どのテストも到達できない** (mutant P17 が生き残った)。
    # 上の層内 dedup と `seen` による層跨ぎ除去が正しい限り、ここは決して発火しない。
    # 発火させるには 1 行上の dedup を壊すしかなく、それは mutant P9 が別に殺している。
    # 削らないのは round 1 で両席が「最終 neg の uniqueness を assert せよ」と求めたから。
    # 到達不能な防護であることを、テストで隠さずここに書いておく。
    if len(neg) != len(set(neg)):
        raise PlanError(
            "負例に重複が残っている。同じ post が複数回入ると暗黙に重み付けされる。")
    manifest["chosen"] = chosen
    return neg, weights, manifest


def assert_disjoint(tag: str, pos: Sequence[int], neg: Sequence[int]) -> None:
    """正例と負例が交わっていたら **止める**。

    キャラは共演する。A と B が同じ画像に写っていれば、B の正例は A の負例候補として
    現れる — その画像に A も写っていれば、A にとっては正例。ここを黙って通すと
    **正例を負例として学習する**。
    """
    overlap = set(pos) & set(neg)
    if overlap:
        raise PlanError(
            f"[{tag}] 正例が負例にも入っている ({len(overlap)} 件, 例: {sorted(overlap)[:5]})。"
            " キャラの共演で起きる。負例の構成規則が自タグ保有を除外できていない。")


def assert_no_split_leak(tag: str, train: Sequence[int], val: Sequence[int]) -> None:
    """同じ post が train と val の両方に居たら止める。

    派生画像 (同一 post の別 crop / 再エンコード) が跨ぐと、val の数字は「覚えたものを
    思い出せるか」を測る。**post_id 単位で split を確定してから**埋め込み・tensor 化に
    進むこと (round 1 指摘) — tensor になってからでは post の同一性が消える。
    """
    leak = set(train) & set(val)
    if leak:
        raise PlanError(
            f"[{tag}] train と val に同じ post が {len(leak)} 件ある"
            f" (例: {sorted(leak)[:5]})。val の指標が意味を失う。")


def split_by_post(ids: Sequence[int], *, val_fraction: float = 0.2,
                  min_val: int = 5) -> tuple[list[int], list[int]]:
    """post_id 単位で train/val に切る。**埋め込みより前に呼ぶ。**

    安定ハッシュ順の先頭を val にするので、同じ入力なら同じ split。
    """
    ordered = stable_order(set(ids))
    k = max(min_val, int(len(ordered) * val_fraction))
    k = min(k, len(ordered))
    return ordered[k:], ordered[:k]


def finalize(tag: str, pos: Sequence[int], strata: Sequence[Stratum],
             *, val_fraction: float = 0.2, positives: Mapping | None = None,
             today: date | None = None) -> Plan:
    """**Plan を返す唯一の公開経路。** 検査をここに集める。

    round 1 の指摘: `assert_no_split_leak` に本番呼び出し経路が無く、呼ばれない
    assertion は無いのと同じだった。検査を通らずに Plan が手に入る道を残さない。
    """
    pos_u = stable_order(set(pos))
    if not pos_u:
        raise PlanError(f"[{tag}] 正例が 0 件。学習させない。")

    if positives is not None and positives.get("final") != len(pos_u):
        # 正例の導出 (raw → excluded → restored → final) は builder が書く。その final が
        # 実際に渡された正例数と食い違えば、導出の記録は嘘 — 記録して検査する (round 3)。
        raise PlanError(
            f"[{tag}] 正例の導出記録 final={positives.get('final')} が実際の正例数"
            f" {len(pos_u)} と一致しない。導出の記録が嘘になっている。")
    neg, weights, manifest = build_negatives(strata, exclude=pos_u, today=today)
    if positives is not None:
        manifest["positives"] = dict(positives)
    if not neg:
        # 正例だけで二値分類器を学習することになる。層が空だったのか、正例が
        # 負例候補を食い尽くしたのか (共演で起きる) は manifest に残るが、いずれにせよ
        # 学習させない。テストがこの穴を見つけた: pos=[1,2,3] と S1=[3] で負例が
        # 空になっても finalize が通っていた。
        raise PlanError(
            f"[{tag}] 負例が 0 件になった。excluded_by_pos="
            f"{manifest['excluded_by_pos']}, 層ごとの offered/eligible は manifest 参照。"
            " 正例だけで分類器は学習できない。")
    assert_disjoint(tag, pos_u, neg)

    ptr, pva = split_by_post(pos_u, val_fraction=val_fraction)
    ntr, nva = split_by_post(neg, val_fraction=val_fraction)
    assert_no_split_leak(tag, ptr + ntr, pva + nva)

    manifest["split"] = {"val_fraction": val_fraction,
                         "pos_train": len(ptr), "pos_val": len(pva),
                         "neg_train": len(ntr), "neg_val": len(nva)}
    manifest["pos_count"] = len(pos_u)
    manifest["tag"] = tag
    return Plan(tag=tag, pos=pos_u, neg=neg, weights=weights, manifest=manifest)


# ── concept 種別の dispatch ────────────────────────────────────────────────
#
# 旧実装は `if q.get("gt")=="verified": ... else: ...` の **二分岐**で、3 種目を足すと
# `else` に落ちて `q["pos"]` で KeyError になった。明示キーの表にして、**知らない種別は
# 黙って落とさず raise する**。

KindBuilder = Callable[..., Plan]
_BUILDERS: dict[str, KindBuilder] = {}


def register_kind(name: str) -> Callable[[KindBuilder], KindBuilder]:
    def deco(fn: KindBuilder) -> KindBuilder:
        if name in _BUILDERS:
            raise PlanError(f"concept 種別 {name!r} が二重登録されている")
        _BUILDERS[name] = fn
        return fn
    return deco


def kind_of(spec: Mapping) -> str:
    """spec からその concept の種別を決める。既存 2 種別の判定はそのまま。"""
    if spec.get("kind"):
        return str(spec["kind"])
    if spec.get("gt") == "verified":
        return "verified_log"
    if "pos" in spec and "neg" in spec:
        return "tag_query"
    raise PlanError(
        f"concept の種別を判定できない: keys={sorted(spec)}。"
        " `kind` を明示するか、既存 2 種別 (verified_log / tag_query) の形にすること。"
        " 判定できないものを既定の分岐へ落とすと、KeyError まで気づけない。")


def build(tag: str, spec: Mapping, **ctx) -> Plan:
    kind = kind_of(spec)
    builder = _BUILDERS.get(kind)
    if builder is None:
        raise PlanError(
            f"[{tag}] 未登録の concept 種別 {kind!r}。登録済み: {sorted(_BUILDERS)}。"
            " 黙って既存の分岐へ落とさない — それが旧実装の欠陥だった。")
    return builder(tag, spec, **ctx)


def registered_kinds() -> list[str]:
    return sorted(_BUILDERS)


# ── 既存 2 種別の builder ──────────────────────────────────────────────────
#
# **DB は触らない。** train_heads.py が SQL を撃ち、id 列を `ctx` で渡す。planner を
# DB-free に保つのは、判断だけをテストできるようにするため。
#
# ## 旧経路との parity について — 何を parity と呼ぶか
#
# 旧経路は `neg = list(dict.fromkeys(list(_verneg[name]) + sql_neg))[:2*len(pos)]`。
# `_verneg[name]` は **set** なので `list(...)` の順序が未規定で、**旧経路はそれ自体が
# 非決定的**だった (2026-09-04 実測、train_heads.py 112/126/129 行)。
#
# よって「旧と byte 一致」は原理的に定義できない。ここで保証するのは:
#
#   * **truncation が効かない領域** (verified_neg + sql_neg <= 2*len(pos)) では
#     **集合が一致する**。順序は違う (旧は set 順、新は安定ハッシュ順)。
#   * **truncation が効く領域**では一致しない。旧は非決定的に落とし、新はクォータで
#     決定的に落とす。**これは意図した差**であって回帰ではない。
#
# 差を隠さずテストで固定する (tests/test_heads_planner.py の parity 節)。

OLD_NEG_RATIO = 2          # 旧 `neg[:len(pos)*2]` の 2
#: 既存 8 タグの neg / neg_fill SQL に付いている `limit`。完全候補集合ではない。
OLD_SQL_LIMIT = 500


def _compat_strata(verified_neg, other_neg, pos_count, *, ratio=OLD_NEG_RATIO,
                   shortfall_waiver: ShortfallWaiver | None = None):
    """既存 2 種別を層に写す。確定負例 (人手) を S1、SQL 由来を S3 相当に置く。

    SQL 由来を S2 (PU) にしないのは、既存 8 タグの負例が**そのタグを持たない**という
    タグクエリの結果で、キャラの PU 問題 (タグ付けが網羅的でない) とは別物だから。
    解剖学タグは gate つきの狭いドメインで運用されており、そこでは「タグが無い = 無い」
    が実務上成り立つ — その判断は既存の 8 タグの運用実績が担保している。

    予算の導出 `budget = max(0, ratio * final_pos - |S1|)` は manifest に残す (round 3、
    「raw/excluded/restored/final/budget の導出を記録・検査せよ」)。
    """
    s1_unique = len(set(verified_neg))
    budget = max(0, ratio * pos_count - s1_unique)
    strata = [
        Stratum("S1", list(verified_neg), CONFIRMED_WEIGHT, None,
                "verified_log hard-neg (人手で剥がした)", rank=1, confirmed=True),
        # 既存 8 タグの neg SQL は全て `limit 500`。完全候補集合ではないので、
        # 契約として明示する — budget が 500 を超えたら planner が止める (waiver が
        # 無ければ。期限内の waiver があれば記録して通す)。
        Stratum("S3", list(other_neg), CONFIRMED_WEIGHT, budget,
                "既存タグクエリ由来 (gate 付きドメイン内, LIMIT 500)", rank=3,
                confirmed=True, complete=False, source_limit=OLD_SQL_LIMIT,
                shortfall_waiver=shortfall_waiver),
    ]
    derivation = {"rule": "budget = max(0, ratio * final_pos - s1_unique)",
                  "ratio": ratio, "final_pos": pos_count, "s1_unique": s1_unique,
                  "budget": budget}
    return strata, derivation


def _positives_record(*, raw, excluded_applied, restored_verified, final, rule):
    """正例の導出を 1 つの dict に。`finalize` が `final` を実正例数と突き合わせる。"""
    return {"raw": raw, "excluded_applied": excluded_applied,
            "restored_verified": restored_verified, "final": final, "rule": rule}


@register_kind("verified_log")
def _build_verified_log(tag, spec, *, verified_pos, verified_neg, neg_fill,
                        shortfall_waiver=None, today=None, **_):
    """curated GT 概念 (phimosis 等)。正負とも verified_log 由来、neg_fill で hard-neg 補充。"""
    pos = stable_order(set(verified_pos))
    strata, budget = _compat_strata(verified_neg, neg_fill, len(pos),
                                    shortfall_waiver=shortfall_waiver)
    positives = _positives_record(
        raw=len(pos), excluded_applied=0, restored_verified=0, final=len(pos),
        rule="verified_log の pos 全件 (機械適用の除外は無い — 全件が curated)")
    positives["budget"] = budget
    return finalize(tag, pos, strata, positives=positives, today=today)


@register_kind("tag_query")
def _build_tag_query(tag, spec, *, sql_pos, sql_neg, applied, verified_pos, verified_neg,
                     shortfall_waiver=None, today=None, **_):
    """純人手 GT。機械適用を除外し、人間検証済み正例は復帰させる (旧経路と同じ規則)。

    導出は **除外後** の件数で予算を組む。round 3 で「除外前の数を使っているのが本当の
    欠陥ではないか」と私が書き、検算で偽だと分かった — 以後は記録が答える。
    """
    ap, vp = set(applied), set(verified_pos)
    raw = set(sql_pos)
    excluded = {i for i in raw if i in ap and i not in vp}
    restored = {i for i in raw if i in ap and i in vp}
    pos = stable_order(raw - excluded)
    strata, budget = _compat_strata(verified_neg, sql_neg, len(pos),
                                    shortfall_waiver=shortfall_waiver)
    positives = _positives_record(
        raw=len(raw), excluded_applied=len(excluded), restored_verified=len(restored),
        final=len(pos),
        rule="raw から applied_log 由来を除外、うち verified_log が pos と言うものは復帰")
    positives["budget"] = budget
    return finalize(tag, pos, strata, positives=positives, today=today)
