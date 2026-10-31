"""「測れなかった」を「基準未達」と書かないことを固定する。

2026-08-05 実測: `tagger_registration_request` の却下 55 件のうち **25 件**が
`validate_registration_metrics_floor: no val breakdown provided` で、そのうち
7 件は metrics に `insufficient_gt_for_cv` と明記されていた。

原因: `review_pending()` が `disposition == "hold"` を扱わず floor gate へ素通し
していた。gate は当然失敗し、**症状**(検証内訳が無い)が理由として記録され、
**原因**(正例が 0〜1 件で CV が組めない)が捨てられていた。

外から見ると「性能が足りないタグ」に見えるが、実際は **GT を足せば解けるもの**。
monochrome は正例 1 / 負例 8,062、greyscale は正例 0 / 負例 687 だった —
`auto:global-deletions` (人手削除の集積) 由来の candidate は **定義上すべて負例**
なので、この経路の 33 件は正例が別途供給されない限り同じ形で落ち続ける。

`not-needed` (測った上での判断) と `hold` (測れなかった) は別物であり、
同じ扱いにしてはいけない。
"""
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))


def _fns():
    """auto_review は import に DB を要求するので、対象関数だけ取り出す。"""
    src = (REPO / "tagger" / "auto_review.py").read_text()
    i = src.index("def _cleanup_reason(")
    j = src.index("async def review_pending(") if "async def review_pending(" in src \
        else src.index("def _next_version(")
    ns = {}
    exec(compile(src[i:j], "auto_review_reasons", "exec"), ns)  # noqa: S102
    return ns


def _decide():
    """判定手続き (`decide`) だけを取り出す。**旧形式の行は floor gate へ
    落ちる**ので、その経路は呼ばれたら分かるようにしておく。"""
    ns = _fns()

    class _TransitionError(Exception):
        pass

    def _floor(metrics, config):
        raise _TransitionError("validate_registration_metrics_floor: "
                               "no val breakdown provided")

    ns["TransitionError"] = _TransitionError
    ns["validate_registration_metrics_floor"] = _floor
    src = (REPO / "tagger" / "auto_review.py").read_text()
    i = src.index("class Decision:")
    j = src.index("async def review_pending(")
    exec(compile(src[i:j], "auto_review_decide", "exec"), ns)  # noqa: S102
    return ns


# 実測から切り出した fixture (自作しない — 自作 fixture は自分の誤解ごと green に固定する)
GREYSCALE_METRICS = {
    "reason": "insufficient_gt_for_cv",
    "runner": {"agent": "trainer-taggerrunner-primary"},
    "disposition": "hold",
    "classification": {
        "at": None, "lift": None, "verdict": "hold",
        "binding": ["min_gt_positive (split 層別下限)"],
        "net_fp_removed_deploy": None,
        "why": "GT の正例が CV の層別下限に届かない: check2 stratification floor: "
               "outer fold 0 has pos=0/neg=138 < floor 1/1",
    },
}
GREYSCALE_GT = {"negative": 687, "positive": 0}


def test_hold_reason_names_the_cause_not_the_symptom():
    r = _fns()["_hold_reason"](GREYSCALE_METRICS, GREYSCALE_GT)
    assert "insufficient_gt_for_cv" in r, "原因が書かれていない"
    assert "no val breakdown" not in r, "症状を理由として書いている"


def test_hold_reason_carries_the_positive_negative_counts():
    """何件投入すれば解けるかが理由から読めること。これが無いと
    「GT を足せばよい」と分かっても、いくつ足すのか分からない。"""
    r = _fns()["_hold_reason"](GREYSCALE_METRICS, GREYSCALE_GT)
    assert "pos=0" in r and "neg=687" in r


def test_hold_reason_is_machine_distinguishable():
    """dashboard / query が「GT 待ち」と「基準未達」を分けられること。"""
    assert _fns()["_hold_reason"](GREYSCALE_METRICS, GREYSCALE_GT).startswith("hold:")


def test_hold_reason_includes_the_binding_constraint():
    r = _fns()["_hold_reason"](GREYSCALE_METRICS, GREYSCALE_GT)
    assert "min_gt_positive" in r


def test_hold_reason_survives_missing_pieces():
    """metrics が痩せていても落ちない。理由が書けないことを理由に
    記録を落とすと、また「静かに消える」形になる。"""
    fns = _fns()
    assert fns["_hold_reason"]({"disposition": "hold"}, None).startswith("hold:")
    assert fns["_hold_reason"](None, None).startswith("hold:")
    assert fns["_hold_reason"]({}, {"positive": 3}).startswith("hold:")


def test_not_needed_reason_is_not_prefixed_as_hold():
    """`not-needed` は測った上での判断なので hold と混ぜない。"""
    r = _fns()["_not_needed_reason"]({
        "classification": {"at": "max_f1", "lift": 1.1,
                           "net_fp_removed_deploy": -6630, "why": "配備で純減"}})
    assert not r.startswith("hold:")
    assert "disposition=not-needed" in r


def test_hold_is_decided_before_the_floor_gate():
    """**本題**: hold は floor gate に届く前に結論が出ること。届くと gate が
    先に失敗し、症状 (検証内訳が無い) が理由として記録される。

    2026-08-05 に判定手続きを `decide()` へ 1 本化したので、分岐の位置を
    ソースで走査するのではなく **手続きの答え**で確かめる (書き方を変えても
    落ちない)。"""
    fns = _decide()
    d = fns["decide"](GREYSCALE_METRICS, GREYSCALE_GT, config=None)
    assert d.ok is False
    assert d.bucket == "unmeasurable"
    assert "insufficient_gt_for_cv" in d.reason
    assert "no val breakdown" not in d.reason


def test_pending_query_selects_gt():
    """gt を取らないと hold の理由に件数を載せられない。"""
    src = (REPO / "tagger" / "auto_review.py").read_text()
    assert "id, tag, metrics, gt FROM tagger_registration_request" in src


def test_unmeasurable_is_counted_separately_from_rejected():
    """「測れなかった」を品質不合格として数えない。同じバケツに入れると
    「性能が悪いタグが 55 件」に見え、GT 待ちが埋もれる。"""
    fns = _decide()
    assert fns["decide"](GREYSCALE_METRICS, GREYSCALE_GT, None).bucket == "unmeasurable"
    assert fns["decide"]({"disposition": "cleanup-capable"}, None, None).bucket \
        != "unmeasurable"
    body = (REPO / "tagger" / "auto_review.py").read_text()
    assert '"unmeasurable": unmeasurable' in body


# ── 4 クラス判定の残り 2 つ ───────────────────────────────────────────────
# 判定器は 不要 / 配備可 / 掃除可 / 保留 の 4 つを出すが、auto_review は
# not-needed と hold しか扱っていなかった。**新形式は classification を出し、
# 旧 floor gate は metrics["val"] の tp/fp/fn を読む** — 形が違うので旧 gate は
# 新形式を必ず落とす。結果 2026-08-05 まで:
#   deployable (配備可)       2 件 → 「内訳が無い」で却下
#   cleanup-capable (掃除可)  3 件 → 同上 (smegma は net −163,583 件の判定)
# 通すのは二重チェックではなく壊れたチェックだった。

SMEGMA_CLEANUP = {
    "disposition": "cleanup-capable",
    "classification": {
        "at": "feat1024@max_f1", "lift": 5.762757690122249,
        "verdict": "cleanup-capable",
        "binding": ["min_p_deploy_lift (lift=6.1x < 10.0x)"],
        "net_fp_removed_deploy": 163583,
        "why": "絶対 floor には届かないが、正当な動作点 feat1024@max_f1 で 5.76 倍の"
               "改善があり、配備母集団の誤検出を差し引き 163,583 件減らす。"
               "**自動配備はしない** — owner 判断へ",
    },
}
DEPLOYABLE = {
    "disposition": "deployable",
    "classification": {"at": "feat1024@P>=0.90", "lift": 12.4, "verdict": "deployable",
                       "net_fp_removed_deploy": 900},
}


def test_cleanup_reason_carries_the_numbers_owner_needs():
    r = _fns()["_cleanup_reason"](SMEGMA_CLEANUP)
    assert "net=163,583" in r, "何件減らせるかが読めない"
    assert "lift=5.76x" in r
    assert "at=feat1024@max_f1" in r, "どの動作点での話か分からないと符号が変わる"
    assert "no val breakdown" not in r


def test_cleanup_reason_is_machine_distinguishable_as_owner_decision():
    r = _fns()["_cleanup_reason"](SMEGMA_CLEANUP)
    assert r.startswith("cleanup-capable:"), "owner が一覧できない"


def test_cleanup_reason_survives_a_thin_classification():
    assert _fns()["_cleanup_reason"]({}).startswith("cleanup-capable:")
    assert _fns()["_cleanup_reason"](None).startswith("cleanup-capable:")


def test_deployable_with_evidence_passes():
    assert _fns()["_deployable_evidence_missing"](DEPLOYABLE) is None


def test_deployable_without_evidence_is_refused():
    """「判定器がそう言ったから」だけで validated にしない (fail-closed)。"""
    fns = _fns()
    assert fns["_deployable_evidence_missing"]({"disposition": "deployable"}) is not None
    assert fns["_deployable_evidence_missing"](
        {"disposition": "deployable", "classification": {"at": "x", "lift": 1.0}}) is not None


def test_deployable_verdict_must_agree_with_disposition():
    """disposition と classification.verdict の食い違いを黙って通さない。"""
    bad = {"disposition": "deployable",
           "classification": {"at": "x", "lift": 1.0, "verdict": "cleanup-capable"}}
    assert "一致しない" in _fns()["_deployable_evidence_missing"](bad)


def test_all_four_dispositions_are_decided_without_the_floor_gate():
    """**本題**: 4 つとも floor gate に触れずに結論が出ること。1 つでも漏れると
    その判定は「内訳が無い」で却下される。gate は呼ばれたら例外を上げる偽物に
    してあるので、届いた時点で落ちる。"""
    fns = _decide()
    expect = {"not-needed": (False, "not_needed"),
              "hold": (False, "unmeasurable"),
              "cleanup-capable": (False, "owner_decision"),
              "deployable": (True, "validated")}
    for disp, (ok, bucket) in expect.items():
        m = {"disposition": disp,
             "classification": {"at": "feat1024@max_f1", "lift": 9.9,
                                "verdict": disp, "net_fp_removed_deploy": 1.0}}
        d = fns["decide"](m, {"positive": 1, "negative": 2}, None)
        assert (d.ok, d.bucket) == (ok, bucket), f"{disp}: {d.reason}"


def test_old_format_rows_still_reach_the_floor_gate():
    """後方互換: classification を持たない旧行は従来どおり floor にかかる。
    ここまで塞ぐと、旧行が無検査で通ってしまう。"""
    fns = _decide()
    d = fns["decide"]({"P_deploy": 0.95}, None, None)
    assert d.ok is False and d.bucket == "rejected"
    assert "no val breakdown" in d.reason, "floor gate に届いていない"


def test_owner_decision_is_its_own_bucket():
    """掃除可を品質不合格と同じバケツに入れると、owner 判断待ちが埋もれる。"""
    fns = _decide()
    assert fns["decide"](SMEGMA_CLEANUP, None, None).bucket == "owner_decision"
    body = (REPO / "tagger" / "auto_review.py").read_text()
    assert '"owner_decision": owner_decision' in body
