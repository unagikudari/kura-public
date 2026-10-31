"""結論が出た測定に「僅差で落ちた」と報告しないことを固定する。

■ 2026-08-05 実測の欠陥

`vnext_escalate.evaluate()` は「この測定は結論に至ったか」の代理として
`verdict["any_arm_meets_floor"]` を使っていた。2 値 (PASS/HOLD) の時代は
それで正しい。**判定が 4 クラスになり、絶対 floor に「率 OR 規模」の規模側が
入った時点でこの代理は壊れた** — `any_arm_meets_floor` は絶対 (率) floor しか
見ないので、規模 floor で `deployable` になった測定でも False のまま。

smegma は `deployable` (lift 7.29x / 配備母集団の誤検出を差し引き 169,284 件減)
と判定されているのに、測定のたびに

    [vnext escalation E4] tag=smegma
    B_veto_ceiling:feat1024/P>=0.90 が min_p_deploy に僅差で不達
    (P_deploy=0.812 vs 0.850、差 0.038)

が owner へ届き続けた。**配備が決まったタグについて「僅差で落ちた」と報告して
いた。** dedup 鍵は GT bucket 化されているので鳴りっぱなしにはならないが、
GT が育つたびに再発火し、しかも内容が判定と食い違う。

さらに `next_hold_streak()` も同じ旧 2 値を受け取っていたため、deployable な
タグの HOLD 連続カウントが永久に増え続け、いずれ E6「HOLD が 3 回連続 —
手法の見直しが要る」に到達する。**配備が決まっているタグについて手法の
見直しを促す**ことになる。

これは今日 `auto_review` で直したものと同じ欠陥 —
**旧 2 値の gate が新 4 クラスの判定を知らない** — の 2 箇所目。
"""
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from tagger import vnext_escalate as E  # noqa: E402


# ── 実測から切り出した fixture ────────────────────────────────────────────
# ~/tagger-heads/vnext_state/smegma.measurement.json (2026-08-05
# 12:04:34) の値。自作しない — 自作 fixture は自分の誤解ごと green に固定する。

SMEGMA_FLOOR = {"min_precision": 0.85, "min_recall": 0.7, "min_p_deploy": 0.85,
                "min_p_deploy_lift": 10.0, "min_net_fp_removed": 10000.0}

SMEGMA_VERDICT = {
    # 実測の verdict には any_arm_meets_floor=False / verdict_absolute="HOLD" も
    # 入っているが、**この module は読まない**ので fixture にも置かない。
    # 読まない値を fixture に残すと「まだ効いている」と誤読される。
    "any_arm_above_chance": True,
    "binding_floor_terms_by_arm": {
        "feat1024": {"closest_rule": "P>=0.85",
                     "binding_terms": ["min_p_deploy (P_deploy=0.7562 < 0.85)"]}},
}

SMEGMA_ARMS = {
    "B_veto_ceiling:feat1024": {
        "auc": 0.9542, "above_chance": True,
        "nested": {
            "max_f1":   {"P": 0.8848, "R": 0.8870, "P_deploy": 0.7695073305698333},
            "P>=0.90":  {"P": 0.9088, "R": 0.7838, "P_deploy": 0.8124876895804609},
        },
    },
}

# 同じ測定の 4 クラス判定 (classification.verdict)。
SMEGMA_DISPOSITION = "deployable"

GT_POS, GT_NEG = 407, 1691


def _codes(disposition):
    return [e.code for e in E.evaluate(
        tag="smegma", verdict=SMEGMA_VERDICT, arms=SMEGMA_ARMS,
        floor=SMEGMA_FLOOR, hold_streak=0, gt_pos=GT_POS, gt_neg=GT_NEG,
        disposition=disposition)]


# ── 本題 ─────────────────────────────────────────────────────────────────

def test_deployable_measurement_does_not_raise_a_near_miss():
    """**これが実際に鳴っていた誤報**。配備可なら「僅差で不達」は事実に反する。"""
    assert "E4" not in _codes("deployable")


def test_a_measurement_without_a_verdict_still_raises_it():
    """判定そのものが無い測定 (例外で終わったなど) は未結論。黙る方向に倒すと
    本物の僅差が消えるので、**鳴る側**に倒す。"""
    assert "E4" in _codes(None)


def test_not_needed_is_also_a_conclusion():
    """「測った上で不要」も結論。不要と決まったものに僅差もない。"""
    assert "E4" not in _codes("not-needed")


def test_hold_still_escalates():
    """`hold` は測れなかった状態 = 未結論。ここを止めると本来の用途が死ぬ。"""
    assert "E4" in _codes("hold")


def test_cleanup_capable_still_escalates():
    """owner 判断待ちは未結論。際どさは owner の材料になる。"""
    assert "E4" in _codes("cleanup-capable")


def test_unknown_disposition_escalates():
    """知らないクラスを結論扱いしない (fail-closed は **鳴る**側)。"""
    assert "E4" in _codes("なにか未知のクラス")


# ── 語彙 ─────────────────────────────────────────────────────────────────

def test_concluded_vocabulary_is_exactly_the_two_settled_classes():
    assert E.CONCLUDED_DISPOSITIONS == frozenset({"deployable", "not-needed"})


def test_is_concluded_takes_only_the_disposition():
    """**旧 2 値への fallback を持たない**こと。真実の出所を 2 つにすると、
    片方だけ直したときに今回と同じ食い違いが再発する。"""
    import inspect
    assert list(inspect.signature(E.is_concluded).parameters) == ["disposition"]
    assert E.is_concluded("deployable") is True
    assert E.is_concluded("hold") is False
    assert E.is_concluded(None) is False


def test_the_module_never_reads_the_old_boolean():
    """`any_arm_meets_floor` は `vnext_arms` が絶対 (率) floor を表す内部値で
    あって測定の結論ではない。ここで読むと今回の誤報が復活する。"""
    src = (REPO / "tagger" / "vnext_escalate.py").read_text()
    code = "\n".join(l for l in src.splitlines() if not l.lstrip().startswith("#"))
    assert 'get("any_arm_meets_floor")' not in code


def test_evaluate_has_no_default_disposition():
    """既定値を置くと、渡し忘れた呼び出しが**黙って**旧挙動に落ちる。
    呼ぶ側に必ず「どの判定か」を言わせる。"""
    import inspect
    par = inspect.signature(E.evaluate).parameters["disposition"]
    assert par.default is inspect.Parameter.empty, "disposition に既定値がある"


# ── E6 / E7 も同じ gate を通ること ───────────────────────────────────────

def test_e6_does_not_fire_for_a_concluded_measurement():
    """配備が決まったタグに「手法の見直しが要る」と言わない。"""
    codes = [e.code for e in E.evaluate(
        tag="smegma", verdict=SMEGMA_VERDICT, arms=SMEGMA_ARMS,
        floor=SMEGMA_FLOOR, hold_streak=99, gt_pos=GT_POS, gt_neg=GT_NEG,
        disposition="deployable")]
    assert "E6" not in codes


def test_e6_still_fires_while_unresolved():
    codes = [e.code for e in E.evaluate(
        tag="smegma", verdict=SMEGMA_VERDICT, arms=SMEGMA_ARMS,
        floor=SMEGMA_FLOOR, hold_streak=99, gt_pos=GT_POS, gt_neg=GT_NEG,
        disposition="hold")]
    assert "E6" in codes


def test_e7_does_not_fire_for_a_concluded_measurement():
    """「正例を増やせば届く」— 既に届いているので足す必要が無い。"""
    v = dict(SMEGMA_VERDICT, binding=["min_val_pos_support (support=12 < 20)"])
    codes = [e.code for e in E.evaluate(
        tag="smegma", verdict=v, arms=SMEGMA_ARMS, floor=SMEGMA_FLOOR,
        hold_streak=0, gt_pos=GT_POS, gt_neg=GT_NEG, disposition="deployable")]
    assert "E7" not in codes


def test_e7_still_fires_while_unresolved():
    v = dict(SMEGMA_VERDICT, binding=["min_val_pos_support (support=12 < 20)"])
    codes = [e.code for e in E.evaluate(
        tag="smegma", verdict=v, arms=SMEGMA_ARMS, floor=SMEGMA_FLOOR,
        hold_streak=0, gt_pos=GT_POS, gt_neg=GT_NEG, disposition="hold")]
    assert "E7" in codes


# ── 判定に依らないもの (gate を広げすぎていないこと) ─────────────────────

def test_blocking_codes_are_unaffected_by_the_disposition():
    """E1 (例外) / E0 (構造前提) は測定の成否と無関係に上がる。gate を
    ここまで広げると、配備可のタグで replica drift を見落とす。"""
    codes = [e.code for e in E.evaluate(
        tag="smegma", verdict=SMEGMA_VERDICT, arms=SMEGMA_ARMS,
        floor=SMEGMA_FLOOR, hold_streak=0, gt_pos=GT_POS, gt_neg=GT_NEG,
        disposition="deployable",
        structural={"tag_in_vocab": True, "release_row_present": True,
                    "replica_schema_matches": False})]
    assert "E0" in codes


def test_e2_inverse_correlation_is_unaffected_by_the_disposition():
    """corpus arm の逆相関はレシピ汚染の証拠で、判定とは別の話。"""
    arms = dict(SMEGMA_ARMS,
                # dark_anus の実測値 (auc/n とも)。n を書かないと E2 は
                # 「判定できない」で黙る — 逆相関かどうかは標本の大きさで決まる。
                **{"C_dispatch_recipe:feat1024": {"auc": 0.405, "above_chance": True,
                                                  "n_pos": 172, "n_neg": 38,
                                                  "nested": {}}})
    codes = [e.code for e in E.evaluate(
        tag="smegma", verdict=SMEGMA_VERDICT, arms=arms, floor=SMEGMA_FLOOR,
        hold_streak=0, gt_pos=GT_POS, gt_neg=GT_NEG, disposition="deployable")]
    assert "E2" in codes


# ── HOLD streak ──────────────────────────────────────────────────────────

def test_hold_streak_resets_when_the_measurement_concludes():
    """配備可のタグの streak が増え続けると、いずれ E6 に到達する。"""
    streak, _ = E.next_hold_streak(2, (16, 33), True, GT_POS, GT_NEG)
    assert streak == 0


def test_hold_streak_still_grows_while_unresolved():
    streak, bucket = E.next_hold_streak(2, (16, 33), False, GT_POS, GT_NEG)
    assert streak == 3
    assert bucket == (GT_POS // E.POS_BUCKET, GT_NEG // E.NEG_BUCKET)


# ── 配線 (述語を直しても呼び出し側が渡さなければ意味が無い) ───────────────
#
# **1 箇所ずつ潰さない。** 初版は escalation だけを直し、runner に残った
# 4 箇所 (shadow の log / shadow の complete / 本番の complete / 戻り値) が
# 旧 2 値を "PASS"/"HOLD" に翻訳し続けていた。同じ測定が escalation では
# deployable、broker の task 履歴では HOLD と記録される状態になっていた。

def _runner_code() -> str:
    """コメント行を除いた runner の本体。注記中の旧名は検出対象にしない。"""
    src = (REPO / "tagger" / "vnext_runner.py").read_text()
    src = src[src.index("\nfrom __future__"):]          # module docstring を落とす
    return "\n".join(l for l in src.splitlines() if not l.lstrip().startswith("#"))


def test_runner_never_reads_the_old_boolean():
    """**本題**: runner のどこにも `any_arm_meets_floor` の読みが残らないこと。

    これは `vnext_arms` が絶対 (率) floor を表すために作る内部値であって、
    測定の結論ではない。結論として読んだ箇所が 1 つでも残ると、そこだけ
    判定が退行して見える。
    """
    assert 'any_arm_meets_floor' not in _runner_code()


def test_runner_does_not_emit_the_retired_two_value_vocabulary():
    """"PASS"/"HOLD" の文字列リテラルを吐かないこと。判定の語彙は 4 クラス
    ただ 1 つで、`vnext_class` の定数から取る。"""
    code = _runner_code()
    assert '"PASS"' not in code
    assert '"HOLD"' not in code


def test_runner_passes_the_disposition_to_evaluate():
    code = _runner_code()
    assert "disposition = klass.get(\"verdict\")" in code, "判定を 1 度だけ取り出す"
    assert "disposition=disposition" in code, "述語へ渡していない"
    assert "disposition=None" in code, "測定が無い経路で明示していない"


def test_runner_feeds_the_streak_from_the_four_class_verdict():
    """旧 2 値を渡すと streak だけが古い規則で動き、E6 が誤発火する。"""
    code = _runner_code()
    body = code[code.index("streak, bucket = vnext_escalate.next_hold_streak("):]
    head = body[:body.index(")", body.index("gt_neg"))]
    assert "is_concluded(disposition)" in head


def test_runner_reports_the_same_verdict_it_escalated_on():
    """broker へ返す `verdict` と escalation の判定材料が同じ値であること。
    ここが割れると、task 履歴と escalation が別の話をする。"""
    code = _runner_code()
    assert code.count('"verdict": disposition') == 2, "shadow と本番の両方で揃える"
    assert "return disposition" in code


# ─────────────────────────────────────────────────────────────────────────
# E4 は「その動作点の他の床項目が満たされている」ときだけ僅差と呼ぶ
#
# 2026-08-06 実測 (phimosis, vnext_state/phimosis.measurement.json):
#   max_f1  P=0.7069  R=0.6891   床 min_precision=0.85 / min_recall=0.70
# recall は 0.011 差だが precision は 0.143 差。E4 は recall だけを見て
# 「僅差で不達」を owner へ上げていた。**項目 1 つを全体として報告していた。**
#
# 数値は実測から切り出す (自作 fixture は自分の誤解ごと green で固定する)。
# ─────────────────────────────────────────────────────────────────────────

_PHIMOSIS_FLOOR = {"min_precision": 0.85, "min_recall": 0.70, "min_p_deploy": 0.85}


def _arm(**rules):
    return {"above_chance": True, "auc": 0.9341, "nested": dict(rules)}


def test_e4_stays_silent_when_another_floor_term_is_far_off():
    """**本題**: phimosis の max_f1。recall 0.011 差だが precision 0.143 差。"""
    es = E.evaluate(
        tag="phimosis", verdict={"floor": _PHIMOSIS_FLOOR},
        arms={"feat1024": _arm(max_f1={"P": 0.7068965517241379,
                                       "R": 0.6890756302521008,
                                       "P_deploy": 0.3720463152290958})},
        floor=_PHIMOSIS_FLOOR, hold_streak=0, gt_pos=238, gt_neg=1609,
        disposition="hold")
    assert not [e for e in es if e.code == "E4"], \
        "他の床項目が大きく外れているのに僅差として報告している"


def test_e4_fires_when_exactly_one_term_is_short_by_a_hair():
    """他が満たされていて 1 項目だけ margin 以内なら、これは本当の僅差。"""
    es = E.evaluate(
        tag="phimosis", verdict={"floor": _PHIMOSIS_FLOOR},
        arms={"feat1024": _arm(max_f1={"P": 0.90, "R": 0.6890756302521008,
                                       "P_deploy": 0.90})},
        floor=_PHIMOSIS_FLOOR, hold_streak=0, gt_pos=238, gt_neg=1609,
        disposition="hold")
    e4 = [e for e in es if e.code == "E4"]
    assert len(e4) == 1 and e4[0].detail["floor_key"] == "min_recall"


def test_e4_stays_silent_when_a_floor_term_is_unmeasurable():
    """測れていない項目があるなら「他は満たしている」と言えない。E4 は
    「あと一息だから手を入れろ」という誘いなので、根拠が欠けたまま誘わない。"""
    es = E.evaluate(
        tag="phimosis", verdict={"floor": _PHIMOSIS_FLOOR},
        arms={"feat1024": _arm(max_f1={"P": 0.90, "R": 0.6890756302521008})},
        floor=_PHIMOSIS_FLOOR, hold_streak=0, gt_pos=238, gt_neg=1609,
        disposition="hold")
    assert not [e for e in es if e.code == "E4"]


# ─────────────────────────────────────────────────────────────────────────
# E2 は「偶然の下振れ」を逆相関と呼ばない
#
# 2026-08-07 実測。条件は `auc < 0.5` の裸の閾値だった:
#   foreskin   auc=0.4996  n=71/224   SE=0.0394 → 0.01 SE 下 (偶然そのもの)
#   dark_anus  auc=0.4050  n=172/38   SE=0.0519 → 1.83 SE 下 (実害あり)
# 前者でも owner に「レシピの汚染が疑われる」が届いていた。
#
# 数値は実測から切り出す。
# ─────────────────────────────────────────────────────────────────────────

def _c_arm(auc, n_pos, n_neg):
    return {"C_corpus_aug": {"auc": auc, "n_pos": n_pos, "n_neg": n_neg,
                             "above_chance": True, "nested": {}}}


def _e2(arms):
    return [e for e in E.evaluate(tag="t", verdict={}, arms=arms, floor={},
                                  hold_streak=0, gt_pos=1, gt_neg=1,
                                  disposition="hold") if e.code == "E2"]


def test_e2_stays_silent_at_chance():
    """**本題**: foreskin の実測。0.5 を 0.01 標準誤差下回っただけ。"""
    assert not _e2(_c_arm(0.49962273641851107, 71, 224))


def test_e2_fires_when_meaningfully_below_chance():
    """dark_anus の実測。1.83 SE 下 — corpus の除外フィルタが 30,274 件中
    0 件しか除外できていなかった実害が裏にあった。"""
    assert len(_e2(_c_arm(0.4049877600979192, 172, 38))) == 1


def test_e2_scales_with_sample_size():
    """同じ AUC でも標本が小さければ偶然で説明でき、大きければできない。
    **固定した閾値ではこの区別ができない。**"""
    assert not _e2(_c_arm(0.45, 10, 10))
    assert _e2(_c_arm(0.45, 2000, 2000))


def test_e2_stays_silent_without_sample_sizes():
    """n が無ければ判定しない。E2 は「レシピを見直せ」という誘いなので、
    根拠が欠けたまま誘うと owner の時間を捨てさせる。"""
    assert not _e2({"C_corpus_aug": {"auc": 0.30, "above_chance": True, "nested": {}}})
