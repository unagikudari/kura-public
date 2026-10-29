# Description: 測定結果を 4 クラス (不要/配備可/掃除可/保留) に分類する純関数。
# Kind: lib
# Effects: pure (no I/O — fixture-testable)
"""測定結果の 4 クラス判定 — plan tagger-vnext-deterministic-runner rev8 (approved)。

**なぜ 2 値では駄目だったか** (2026-08-03 の実測):
絶対 floor (`min_p_deploy >= 0.85`) だけで PASS/HOLD を出すと判定が逆転する。
  - dark_anus: π=0.819 で**元から 82% 正しい** → floor を自動的に満たし PASS。
    しかし改善は lift 1.05 = 実質ゼロ。**配備しても得るものが無い**。
  - smegma: π=0.129 の汚染タグ (corpus 201,369 件)。floor には届かないので
    HOLD = 「何も起きなかった」と読める。実際には lift 5.9 倍の分離能がある。
つまり**元から綺麗なタグに不要な head を配備可と言い、汚れたタグの有用な head を
却下する**。原因は絶対 floor が「元の汚れ具合」に対して盲目なこと。

**この module は verdict dict を解釈しない** (2026-08-03 の実装事故):
初版は verdict のキーを推測して読み、`capability_gain` を「偶然を超えた」の意味だと
誤読した。実際は「10 倍 lift floor を満たす」の意味で、実測 7 タグすべてで False。
結果、lift 5.9 倍の smegma に「現手法では判別できない、定義を見直せ」と**正反対の
助言**を出した。自作の fixture でテストしたので誰も気づかない。
以降、**呼び出し側 (vnext_arms) が正規化した数値を渡す**。ここは辞書を掘らない。

**核となる量** (rev8 Finding 1、prevalence 補正が必須): 判定は**配備母集団の
スケール**で行う。検証プール上の生の tp/fp は負例の間引き方で歪み、**符号すら
変わりうる** (reference では生 +1,372 に対し補正後 +167,246 と桁が 2 つ違った)。

    expected_fp_removed = (1−π)·(1−FPR)·N
    expected_tp_lost    = π·(1−R)·N
    net_fp_removed_deploy = N·[ (1−π)(1−FPR) − π(1−R) ]

N = corpus_flagged_n (そのタグが付いている corpus 内の件数 = 配備母集団)、
π = deploy_prevalence (in-gate の真陽性率)、R と FPR はクラス条件付きなので
**負例 cap に不偏**。N > 0 なので**符号は角括弧だけで決まる** (INV-R12)。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Sequence

# lift がこれ未満なら「改善が無いに等しい」。tagger_config の
# min_p_deploy_lift_negligible で上書きできる policy dial (コードに焼かない)。
DEFAULT_NEGLIGIBLE_LIFT = 1.25

NOT_NEEDED = "not-needed"
DEPLOYABLE = "deployable"
CLEANUP_CAPABLE = "cleanup-capable"
HOLD = "hold"


@dataclass(frozen=True)
class OperatingPoint:
    """**正当な動作点** — recall と支持数の floor を保った (arm, rule) 1 点。

    「正当」= **recall floor と支持数 floor を保つこと、それだけ**。これを外すと
    「ほとんど発火しないので lift は無限大」が最良点として選ばれる。

    **`vnext_arms.clears_lift` と同じではない** (nodec-cc-primary のレビュー指摘、
    2026-08-03): あちらは recall/支持数に加えて **`lift >= min_p_deploy_lift`
    (既定 10 倍) を含む**。ここに lift floor を混ぜると、**smegma (lift 5.95 倍) の
    正当な動作点が 0 件になり hold に落ちて逆転が復活する** — この module が
    存在する理由そのものが消える。lift floor は配備可否を決める policy dial で
    あって、動作点が意味を持つかどうかとは別の軸。**「clears_lift に揃える」は
    禁止**。
    """
    arm: str
    rule: str
    recall: float
    fpr: float
    lift: float | None = None
    p_deploy: float | None = None


@dataclass(frozen=True)
class Classification:
    """4 クラス判定の結果。`why` は必ず「次に何をすればよいか」まで書く。"""
    verdict: str
    why: str
    net_fp_removed_deploy: float | None = None
    lift: float | None = None
    at: str | None = None
    binding: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {"verdict": self.verdict, "why": self.why,
                "net_fp_removed_deploy": self.net_fp_removed_deploy,
                "lift": self.lift, "at": self.at, "binding": list(self.binding)}


def net_fp_removed_deploy(*, pi: float, recall: float, fpr: float,
                          n_flagged: float) -> float:
    """配備母集団で「FP を何件減らし、TP を何件捨てるか」の差し引き (INV-R12)。

    検証プール上の生カウントを引き算してはいけない — GT は検証済み pool 由来で
    corpus の構成比を保存しないため、比率がずれていれば**符号が反転しうる**。
    """
    return n_flagged * ((1.0 - pi) * (1.0 - fpr) - pi * (1.0 - recall))


def classify(*, any_arm_above_chance: bool, absolute_pass: bool,
             legit_points: Sequence[OperatingPoint], pi: float | None,
             n_flagged: float | None, binding_terms: Sequence[str] = (),
             negligible_lift: float = DEFAULT_NEGLIGIBLE_LIFT,
             absolute_pass_is_baseline_only: bool = False,
             min_net_fp_removed: float | None = None) -> Classification:
    """測定 1 件を 4 クラスに分ける。

    引数はすべて**呼び出し側が正規化済み**の値 (辞書を掘らない、上の注記):
      any_arm_above_chance — permutation null を超えた腕が 1 つでもあるか
      absolute_pass        — 絶対 floor を満たす腕があるか (any_arm_meets_floor)
      legit_points         — recall/支持数 floor を保つ (arm, rule) の一覧
      pi, n_flagged        — 配備母集団の真陽性率と件数
      binding_terms        — 届かなかったときに「何が足りないか」

    判定順に意味がある:
      1. **判別できていない** (above-chance の腕が無い) → 保留。以降の議論は無意味
      2. **正当な動作点が無い** → 保留 (recall/支持数を割った点しか無い)
      3. **配備しても損** (net <= 0) → 不要。floor を満たしても真陽性を削るだけ
      4. **改善が無いに等しい** (lift < negligible) → 不要
      5. **baseline (stock) 腕だけが floor を通った** → 掃除可。自前 head は
         1 つも届いておらず、打ち手は既存モデルの閾値変更であって head の登録
         ではない。**機械に実行できるものが無い**ので人へ回す (INV-R4)
      6. それ以外 (判別できていて実利もある) → **配備可**

    **4〜6 の境目**: 実際に配備を止めているのは 3 と 4 — 「差し引きが正」と
    「改善が negligible を超える」の 2 つだけ。絶対 floor (率/規模) はどちらの
    経路でも結論を `deployable` にするので、**判定ではなく説明文を選んでいる**
    (2026-08-05、user「掃除可なら判断はいらない。削除しろ」で owner 判断を廃止)。
    `min_net_fp_removed` を「配備を絞る歯止め」だと思って触っても何も起きない —
    test_vnext_class.py::test_the_scale_floor_no_longer_gates_the_verdict が
    その事実を固定している。歯止めを足すならここではなく 3/4 の側に足す。

    **配備 ≠ 削除**: ここでの「配備可」は head を載せて今後の誤検出を抑える
    こと。既に付いたタグの削除は reap 側が GT 正例 recall で別途認可する
    (tagger/reap_authorized.json)。
    """
    binding = tuple(str(t) for t in binding_terms)

    if not any_arm_above_chance:
        return Classification(
            HOLD, "どの腕も並べ替え帰無分布を超えていない — 現手法では判別できない。"
                  "定義の見直しか、別の特徴空間が要る", binding=binding)

    if not legit_points:
        return Classification(
            HOLD,
            "偶然は超えているが、recall と支持数の floor を保った動作点が無い "
            "(発火をほぼ止めれば precision は上がるが、それは分類ではない)"
            + (f" — binding: {', '.join(binding)}" if binding else ""),
            binding=binding)

    # 各正当点での配備効果。差し引きが最大の点を代表に選ぶ (その点が実際に
    # 配備しうる点だから — 正当性の判定は既に legit_points で済んでいる)。
    best: tuple[float, OperatingPoint] | None = None
    if pi is not None and n_flagged:
        for op in legit_points:
            net = net_fp_removed_deploy(pi=pi, recall=op.recall, fpr=op.fpr,
                                        n_flagged=n_flagged)
            if best is None or net > best[0]:
                best = (net, op)

    net_val = best[0] if best else None
    at = f"{best[1].arm}@{best[1].rule}" if best else None
    lift = best[1].lift if best else max(
        (op.lift for op in legit_points if op.lift is not None), default=None)

    if net_val is not None and net_val <= 0:
        return Classification(
            NOT_NEEDED,
            f"配備母集団で見ると差し引きマイナス ({net_val:,.0f} 件) — "
            "FP を減らす以上に TP を捨てる。**head は要らない**",
            net_val, lift, at, binding)

    if lift is not None and lift < negligible_lift:
        return Classification(
            NOT_NEEDED,
            f"改善が {lift:.2f} 倍で無いに等しい (閾値 {negligible_lift} 倍) — "
            "元から十分に正しいタグなので head は要らない",
            net_val, lift, at, binding)

    # **絶対 floor は率 OR 規模。** min_p_deploy_lift は率しか見ないので、規模の
    # 違う効果を区別できない (実測 2026-08-05: lift 5.76x で 163,583 件減らす
    # smegma が落ち、lift 8.07x で 358 件しか減らさない chastity_cage が残りかけた)。
    # net_fp_removed_deploy は増える誤検出も勘定に入れた差し引きなので、これで
    # 「率は届かないが規模で明らかに得」を **機械が決められる** — owner の手作業に
    # 回さない。規模経路にも negligible_lift は効かせる (母集団が巨大なだけの
    # marginal な head を通さないため)。
    scale_pass = (min_net_fp_removed is not None
                  and net_val is not None and net_val >= min_net_fp_removed
                  and lift is not None and lift >= negligible_lift)
    if (absolute_pass or scale_pass) and not absolute_pass_is_baseline_only:
        if absolute_pass:
            why = "絶対 floor を満たし、配備母集団でも実質的な改善がある"
        else:
            why = (f"率の floor には届かない"
                   + (f" ({', '.join(binding)})" if binding else "")
                   + f" が、**規模の floor を満たす** — 正当な動作点 {at} で"
                     f" 配備母集団の誤検出を差し引き {net_val:,.0f} 件減らす"
                     f" (規模 floor {min_net_fp_removed:,.0f} 件、改善 {lift:.2f} 倍)")
        return Classification(DEPLOYABLE, why, net_val, lift, at, binding)

    if absolute_pass:
        # **floor を通ったのが baseline (stock) 腕だけ** — 自前 head は 1 つも
        # 届いていない。ここを deployable と呼ぶと「head の開発失敗」が
        # 「配備可」に化ける (primary-gemini-primary のレビュー指摘 3、fellatio が実例:
        # 通過腕は E_stock_pixai のみ)。改善自体は本物なので却下もしない —
        # **既存モデルの閾値を変える**という別種の打ち手なので owner 判断へ回す。
        why = "既存モデル (baseline) の閾値変更で floor を満たす — **自前 head は 1 つも届いていない**"
        if lift is not None:
            why += f"。改善は {lift:.2f} 倍"
        if net_val is not None:
            why += f"、配備母集団で差し引き {net_val:,.0f} 件"
        why += (f" (動作点 {at})" if at else "") + "。**head として配備できるものが無い** — 打ち手は既存モデルの閾値変更であって head の登録ではない"
        return Classification(CLEANUP_CAPABLE, why, net_val, lift, at, binding)

    # **判断を挟まない** (user 2026-08-05「掃除可なら判断はいらない」)。
    # ここに来た時点で以下は既に確かめてある: 偶然を超えている / 正当な動作点が
    # ある (recall と支持数の floor を保つ) / 差し引きが正 / 改善が negligible を
    # 超える。**残っているのは「絶対 floor という一律の閾値に届かない」だけ**で、
    # それは効果の有無ではなく閾値の粗さの話だった (lift 5.76x で 163,583 件
    # 減らす head が落ち、lift 8.07x で 358 件の head が残る逆転が実測された)。
    #
    # 旧版はここを owner 判断へ回していたが、**回された側に増える判断材料は無い**
    # — 数字は既に全部揃っている。人を挟むと「掃除可のまま誰も触らない」滞留に
    # なるだけだった (実測: smegma / fellatio / chastity_cage が滞留)。
    #
    # **これは「head を載せて今後の誤検出を抑える」であって「既に付いたタグを
    # 削除する」ではない。** 削除は reap 権限が別途 GT 正例 recall で測って
    # 認可する (tagger/reap_authorized.json) — 再現率の低いタグで削除すると
    # タガー自身の見落としを誤検出と誤認して真のタグを消すため。
    why = "絶対 floor には届かない"
    if binding:
        why += f" ({', '.join(binding)})"
    why += f" が、正当な動作点 {at} で" if at else " が、正当な動作点で"
    if lift is not None:
        why += f" {lift:.2f} 倍の改善があり"
    if net_val is not None:
        why += f"、配備母集団の誤検出を差し引き {net_val:,.0f} 件減らす"
    why += "。**配備する** — 判定に必要な数字は揃っており、人が足せるものは無い"
    return Classification(DEPLOYABLE, why, net_val, lift, at, binding)
