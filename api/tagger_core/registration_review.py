"""Canonical evidence checks shared by read-only review and publication."""
from .label_identity import label_identity, read_label_rows
from .learning_artifacts import digest
from .qualification_metrics import quality_floor
from .transitions import TransitionError, validate_registration_metrics_floor


def _cleanup_reason(metrics: dict | None) -> str:
    """`cleanup-capable` (掃除可) の理由を、owner が判断できる形で残す。

    これは **肯定的な判定** — 「絶対 floor には届かないが、正当な動作点で
    誤検出を差し引き N 件減らせる。自動配備はしないので owner 判断へ」。
    それが 2026-08-05 まで `no val breakdown provided` (検証内訳が無い) として
    却下されていた。smegma は **誤検出を 163,583 件減らせる**と判定されながら、
    記録上は「内訳を出さなかった」ことになっていた。

    状態機械 (0077) は pending/validated/rejected の 3 つしか持たない。pending に
    留めると review_pending が毎サイクル走査して単調増加するので終端は rejected に
    するが、**`cleanup-capable:` 前置で機械判別可能**にして、owner が「掃除可なのに
    埋もれている件」を一覧できるようにする。
    """
    c = (metrics or {}).get("classification") or {}
    bits = ["disposition=cleanup-capable"]
    if c.get("at"):
        bits.append(f"at={c['at']}")
    if c.get("lift") is not None:
        bits.append(f"lift={c['lift']:.2f}x")
    if c.get("net_fp_removed_deploy") is not None:
        bits.append(f"net={c['net_fp_removed_deploy']:,.0f}")
    if c.get("binding"):
        bits.append(f"binding={','.join(c['binding'])}")
    why = c.get("why") or "絶対 floor には届かないが配備母集団の誤検出を減らす"
    return f"cleanup-capable: {' '.join(bits)} — **owner 判断待ち** — {why}"


def _deployable_evidence_missing(metrics: dict | None) -> str | None:
    """`deployable` を validated にする前に、判定の根拠が実際に入っているかを見る。

    4 クラス判定器は `classification` を出し、旧 floor gate は `metrics["val"]` の
    tp/fp/fn を読む — **形が違うので旧 gate は新形式を必ず落とす**。だから
    deployable を旧 gate に通すのは二重チェックではなく壊れたチェックであり、
    素通しになる。かわりに **判定そのものの根拠**を確かめる。

    根拠が無ければ None を返さず理由を返す — 「判定器がそう言ったから」だけで
    validated にはしない (fail-closed)。
    """
    c = (metrics or {}).get("classification") or {}
    missing = [k for k in ("at", "lift", "verdict") if c.get(k) in (None, "")]
    if missing:
        return f"classification に {'/'.join(missing)} が無い"
    if c.get("verdict") != "deployable":
        return f"classification.verdict={c.get('verdict')!r} が disposition と一致しない"
    return None


def _hold_reason(metrics: dict | None, gt: dict | None) -> str:
    """**「測れなかった」を「基準未達」と書かない。**

    runner が disposition=hold を返すのは「判定に必要なものが揃っていないので
    判断を保留した」という意味で、**測った上で基準に届かなかったのとは別**。
    区別せずに floor gate へ流すと、gate は当然失敗し、その症状
    (`no val breakdown provided` = 検証内訳が無い) が理由として記録される。
    実際 2026-08-05 時点で **却下 55 件のうち 25 件がこのメッセージ**で、
    そのうち 7 件は metrics に `insufficient_gt_for_cv` と明記されていた。

    外から見ると「性能が足りないタグ」に見えるが、実際は「正例が 0〜1 件で
    CV が組めない」— **GT を足せば解けるものが、否決されたものとして見える**。
    monochrome は正例 1 / 負例 8,062、greyscale は正例 0 / 負例 687 だった。

    `hold:` 前置は機械判別のため — dashboard や query が「GT 待ち」と
    「基準未達」を分けられるようにする。
    """
    m = metrics or {}
    c = m.get("classification") or {}
    bits = ["disposition=hold"]
    if m.get("reason"):
        bits.append(f"reason={m['reason']}")
    g = gt or {}
    if g.get("positive") is not None or g.get("negative") is not None:
        bits.append(f"gt(pos={g.get('positive')}, neg={g.get('negative')})")
    if c.get("binding"):
        bits.append(f"binding={','.join(c['binding'])}")
    why = c.get("why") or "判定に必要なものが揃っていない"
    return f"hold: {' '.join(bits)} — {why}"


def _not_needed_reason(metrics: dict | None) -> str:
    """「不要」の理由を、あとから検証できる形にする。

    「不要」とだけ書くと、読んだ人は正しさを確かめられない。判定に使った
    動作点と差し引き件数を添える (どの点での話かで符号が変わるため:
    dark_anus は max_f1 で +577、P>=0.90 なら −6,630)。
    """
    c = (metrics or {}).get("classification") or {}
    bits = [f"disposition=not-needed"]
    if c.get("at"):
        bits.append(f"at={c['at']}")
    if c.get("lift") is not None:
        bits.append(f"lift={c['lift']:.2f}x")
    if c.get("net_fp_removed_deploy") is not None:
        bits.append(f"net={c['net_fp_removed_deploy']:,.0f}")
    why = c.get("why") or "配備母集団で見て head が要らない"
    return f"{' '.join(bits)} — {why}"


# ── 判定の手続きは 1 つ (review_pending と promote_validated が共有する) ──
#
# **なぜ 1 箇所にするか (2026-08-05 実測の障害)**
#
# 初版は review_pending だけを 4 クラス対応にし、promote_validated には
# `validate_registration_metrics_floor` (旧 gate) が残っていた。旧 gate は
# `metrics["val"]` の tp/fp/fn を読むが、新形式は `classification` を出すので
# **形が違う = 必ず落ちる**。結果:
#
#   - 配備可 18 件 (monochrome / greyscale / smegma / dark_labia_* …) が
#     status='validated' のまま **1 件も patch にならなかった**。
#     「学習したのにタガーが更新されない」の正体がこれ。
#   - さらに旧 gate の失敗を受けて validated 行を 'rejected' へ落とそうとし、
#     状態機械 (0077/0079) が終端行の書き換えを拒否して例外 →
#     **kura-tagger-auto-cycle.service が 15 分ごとに失敗**し、
#     以降の全タグの promotion も道連れになっていた。
#
# 判定手続きが 2 箇所にあると、片方だけが新形式を知っている状態が作れてしまう。
# 作れないようにする — 手続きはここ 1 つで、両方がこれを呼ぶ。


class Decision:
    """登録 1 件に対する判定。`ok` なら patch にしてよい。

    `bucket` は集計用の名前 (validated / not_needed / unmeasurable /
    owner_decision / rejected)。理由文には機械判別用の前置を付ける。
    """

    __slots__ = ("ok", "reason", "bucket")

    def __init__(self, ok: bool, reason: str = "", bucket: str = "validated"):
        self.ok, self.reason, self.bucket = ok, reason, bucket


# bucket (集計名) → tagger_registration_request.review_class (0154 の型付き列)。
# rejected_reason の前置 prose は rendering に降格 — 機械はこの列を読む (#55 P3)。
# decide() が新しい bucket を返すようになったら、この map と 0154 の CHECK を
# 同 commit で広げる (tests/test_registration_review_class.py が不一致で落ちる)。
BUCKET_TO_REVIEW_CLASS = {
    "validated": "deployable",
    "not_needed": "not_needed",
    "unmeasurable": "unmeasurable",
    "owner_decision": "owner_decision",
    "rejected": "floor_rejected",
}


def decide(metrics: dict | None, gt: dict | None, config, *, artifact_sha256=None, tag=None) -> Decision:
    """この登録を配備してよいか。**review/promotion で同じ答えを返す**。

    判定順は 4 クラスの意味そのもの:
      不要   — 測った上で「head は要らない」。配備すると純減する
      保留   — 測れなかった。GT を足せば解ける (基準未達と混ぜない)
      掃除可 — 自前 head が届かず、打ち手は既存モデルの閾値変更 → 人へ
      配備可 — 判定の根拠が実際に入っているかだけ確かめる (fail-closed)
      それ以外 (旧形式で classification が無い行) — 旧 floor gate にかける
    """
    if "serving_qualification" in (metrics or {}):
        # Negative measurements are terminal, not a request to approve a bad
        # model. Passing this gate still does not prove production precision.
        try:
            report = metrics["serving_qualification"]
            body = {k: v for k, v in report.items() if k != "sha256"}
            if (report["schema"] != "dedicated-serving-qualification/1"
                    or digest(body) != report["sha256"]
                    or not tag or report.get("tag") != tag
                    or not artifact_sha256 or report["bundle_sha256"] != artifact_sha256
                    or report["snapshot_sha256"] != metrics["snapshot_sha256"]
                    or report["threshold"] != metrics["threshold"]):
                raise ValueError("binding")
            quality = quality_floor(report["heldout"], config)
            if report["parity"]["passed"] is not True:
                return Decision(False, "serving_parity_failed", "rejected")
            if not quality["passed"]:
                return Decision(False, "serving_quality_failed:" + ",".join(quality["reasons"]), "rejected")
        except (ValueError, TypeError, KeyError, AttributeError):
            return Decision(False, "serving_evidence_invalid", "rejected")
        if metrics.get("production_review_insufficient_support"):
            return Decision(False, "production_evaluation_insufficient_support", "unmeasurable")
        return Decision(False, "awaiting_production_precision_and_fleet_qualification", "owner_decision")
    if (metrics or {}).get("requires_preprocessing_review") or (metrics or {}).get("feature_contract"):
        return Decision(False, "preprocessing contract requires independent calibration and fleet review", "owner_decision")
    disposition = (metrics or {}).get("disposition")
    if disposition == "not-needed":
        return Decision(False, _not_needed_reason(metrics), "not_needed")
    if disposition == "hold":
        return Decision(False, _hold_reason(metrics, gt), "unmeasurable")
    if disposition == "cleanup-capable":
        return Decision(False, _cleanup_reason(metrics), "owner_decision")
    if disposition == "deployable":
        # 旧 gate に通すのは二重チェックではなく**壊れたチェック**なので、
        # かわりに判定そのものの根拠を確かめる。
        gap = _deployable_evidence_missing(metrics)
        if gap:
            return Decision(False, f"deployable と申告されたが根拠が無い: {gap}",
                            "rejected")
        return Decision(True)
    try:
        validate_registration_metrics_floor(metrics, config)
    except TransitionError as e:
        return Decision(False, str(e), "rejected")
    return Decision(True)


async def review_current_registration(db, row, config):
    baseline = (row["gt"] or {}).get("label_sha256")
    if baseline is not None:
        current = label_identity(await read_label_rows(db, row["tag"]))
        if baseline != current:
            return Decision(False, "stale_training_labels", "not_needed")
    decision = decide(row["metrics"], row["gt"], config,
                      artifact_sha256=row["artifact_sha256"], tag=row["tag"])
    if (decision.reason == "awaiting_production_precision_and_fleet_qualification"
            and (row["metrics"] or {}).get("production_review")):
        from .canonical_evaluation import review_evidence_state

        return Decision(False, await review_evidence_state(db, row, config), "owner_decision")
    return decision
