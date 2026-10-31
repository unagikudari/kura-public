#!/usr/bin/env python3
# Description: automatically reviews pending tagger_registration_request rows against
#              tagger_config floors, and promotes validated ones into real tagger_patch rows
# Kind: tool
# Effects: mutating (tagger_registration_request/tagger_patch/tagger_head_candidate; writes +
#          git-commits tagger/patches/<tag>/vN.json files)
# Runs-on: primary
"""tagger/auto_review.py — closes the two lifecycle steps this session had
deliberately left manual (user directive 2026-07-21: 自動化すること, said
right after being told exactly which steps were still human-gated and
why -- an explicit, informed instruction to remove that gate, not an
accident to second-guess).

Two passes, run in order:

1. review_pending() -- every tagger_registration_request with
   status='pending' gets checked against tagger_governed_registration_
   metrics_floor (api/tagger_core/transitions.py, config from tagger_config, both
   already dashboard-editable). Pass -> 'validated'. Fail -> 'rejected'
   with the specific reason, same wording this session used by hand for
   cum_in_cup/large_penis/paizuri/anilingus. This mechanizes a judgment
   this session was already making manually and consistently -- it does
   not invent a new standard.

2. promote_validated() -- every 'validated' registration with no
   promoted_patch_id yet gets turned into a real, git-tracked patch:
   writes tagger/patches/<tag>/vN.json (next version, same schema humans
   write by hand -- see tagger/patches/erection/v1.json for the
   reference shape), upserts the tagger_patch row via the existing
   tagger_governed_upsert_patch (0087/0089/0093, unmodified), then links
   everything via tagger_governed_promote_registration (0101): the
   registration's promoted_patch_id and every open tagger_head_candidate
   row for that tag flip to 'promoted' with the same link.

Deliberately NOT automated by this module (real, open gaps -- documented
here rather than silently glossed over):

- Content-head MODEL FILE distribution. These GT-trained heads
  (train_gt_heads.py, wd14-swinv2 embeddings + a small linear head) are
  head_type='content_head', not wd14_logit_patch -- they don't merge
  into the base model via build_release.py's MANIFEST.json allowlist
  path at all. tagger_patch.model_data is left NULL here because the
  actual .safetensors bytes live on the training node's local disk
  (e.g. trainer:~/tagger-heads/heads/gt_<tag>.safetensors), and there
  is still no upload/fetch mechanism moving them to primary or onward to
  other workers (content_head_model.load() only ever reads a pre-placed
  local file path, per infer.py's *_CKPT constants -- confirmed live,
  no HTTP fetch client exists for this, matching sync_patch_registry.py's
  own "No fetch client consumes this yet" comment for the same column).
  A tagger_patch row created here is real and correctly 'approved', but
  will not actually run inference anywhere until that distribution gap
  is closed separately.
- wd14_logit_patch-type promotion (MANIFEST.json curation +
  build_release.py + publish_release.py) is out of scope for this
  module -- every registration this pipeline produces today is
  content_head type (train_gt_heads.py's output shape), so there is
  nothing here that would need MANIFEST.json entries yet. If a future
  training path emits logit-patch-shaped artifacts, promote_validated()
  would need a second branch, not a change to this one.
"""
from __future__ import annotations

import json
import subprocess
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import text

from api.db.session import async_session
from api.tagger_core import artifact_kind
from tagger import head_policy
from api.tagger_core.config import load_config
from api.tagger_core.transitions import (
    TransitionError,
    acquire_lease,
    record_lifecycle_event,
    release_lease,
    validate_registration_metrics_floor,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
PATCHES = REPO_ROOT / "tagger" / "patches"


async def _latest_stage(db, tag: str) -> str | None:
    """Same query as tagger/reconciler.py's own _latest_stage -- kept as an
    independent copy rather than a cross-import so this module's lifecycle
    read doesn't depend on reconciler.py's internals staying stable."""
    row = (await db.execute(text(
        "SELECT to_stage FROM tagger_lifecycle_event WHERE tag = :tag "
        "ORDER BY occurred_at DESC, id DESC LIMIT 1"
    ), {"tag": tag})).one_or_none()
    return row.to_stage if row else None


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


def decide(metrics: dict | None, gt: dict | None, config) -> Decision:
    """この登録を配備してよいか。**review/promotion で同じ答えを返す**。

    判定順は 4 クラスの意味そのもの:
      不要   — 測った上で「head は要らない」。配備すると純減する
      保留   — 測れなかった。GT を足せば解ける (基準未達と混ぜない)
      掃除可 — 自前 head が届かず、打ち手は既存モデルの閾値変更 → 人へ
      配備可 — 判定の根拠が実際に入っているかだけ確かめる (fail-closed)
      それ以外 (旧形式で classification が無い行) — 旧 floor gate にかける
    """
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


async def review_pending(db) -> dict:
    config = await load_config(db)
    rows = (await db.execute(text(
        # gt も取る — hold の理由に正例/負例の件数を載せるため。これが無いと
        # 「測れなかった」と書けても「なぜ測れなかったか」が書けない。
        "SELECT id, tag, metrics, gt FROM tagger_registration_request WHERE status = 'pending'"
    ))).mappings().all()
    validated, rejected, held, unmeasurable, owner_decision = [], [], [], [], []
    for r in rows:
        # ── 前段 gate: 「不要」判定は floor にかけない (plan rev8 Finding 2) ──
        # 絶対 floor は「元の汚れ具合」に盲目なので、**元から正しいタグほど
        # 自動的に floor を満たす**。dark_anus (π=0.819) は floor 通過だが、
        # 配備すると FP 25 を消すために TP 34 を捨てる = 実効マイナス。
        # ここで止めないと実効ゼロの patch 行が自動生成される。
        # floor 関数の**判定基準と意味は変えない** (INV-R4) — 適用対象を絞るだけ。
        #
        # **終端に落とす** (nodec-cc-primary のレビュー、確定した欠陥 2 件):
        # 初版は「不要」を pending に据え置いた。しかし状態機械 (0077) の
        # CHECK は pending/validated/rejected の 3 つしかなく「意図的に配備
        # しない」に対応する終端が無い。据え置くと (a) review_pending が毎
        # サイクル全 pending を走査して同じ行を弾き続け **pending が単調増加**
        # し、(b) promote_validated は floor を再実行するが **disposition を
        # 見ない**ため、gate を通らずに validated へ達した行 (gate 導入前の
        # 既存行) の「不要」が promote されてしまう。
        # 終端にすれば両方閉じる。**再評価は行の復活ではなく再測定で入り直す** —
        # runner の冪等鍵は (tag, lifecycle_event_id) なので GT が動けば新しい
        # 測定が新しい登録行を作る。古い行を生かしておく必要は無い。
        d = decide(r["metrics"], r.get("gt"), config)
        if not d.ok:
            await db.execute(text(
                "SELECT tagger_governed_set_registration_status(:id, 'rejected', :reason, :rc)"
            ), {"id": r["id"], "reason": d.reason,
                "rc": BUCKET_TO_REVIEW_CLASS[d.bucket]})
            await db.commit()
            {"not_needed": held, "unmeasurable": unmeasurable,
             "owner_decision": owner_decision, "rejected": rejected}[d.bucket] \
                .append((r["tag"], d.reason))
            print(f"[auto_review] {r['tag']} (id={r['id']}): "
                  f"{d.bucket.upper()} -- {d.reason}")
            continue
        await db.execute(text(
            "SELECT tagger_governed_set_registration_status(:id, 'validated', NULL, 'deployable')"
        ), {"id": r["id"]})
        await db.commit()
        validated.append(r["tag"])
        print(f"[auto_review] {r['tag']} (id={r['id']}): validated")
    return {"validated": validated, "rejected": rejected,
            "not_needed": held, "unmeasurable": unmeasurable,
            "owner_decision": owner_decision}




async def _next_version(db, tag: str) -> int:
    """次の版番号。**真実は DB** (`tagger_patch`)。

    2026-08-05 実測の危険: 初版は `tagger/patches/<tag>/v*.json` を数えていた。
    しかし版の権威は governed な `tagger_patch` 行のほうで、ファイルは git に
    置いた写しでしかない。両者はずれる:

        canonical のファイル: v7, v8, v9   → 次は v10 と計算
        DB の実際の最大版:    v10          ← 既にある

    この状態で昇格すると **v10 を再発番**し、`ON CONFLICT DO UPDATE` で
    既存の v10 を別の測定で**黙って上書き**する。実際 dark_labia_minora が
    その状態だった (v10.json は nodec にしか存在せず、canonical に無かった —
    配布経路が GitHub を迂回していたため)。

    ファイルの有無で版が変わると、**チェックアウトごとに違う番号が出る**。
    DB は 1 つなので、そこから採れば誰が昇格させても同じ番号になる。
    """
    cur = (await db.execute(text(
        "SELECT max(version) FROM tagger_patch WHERE tag = :t"), {"t": tag})).scalar()
    return int(cur or 0) + 1


async def _version_for(db, tag: str, artifact_sha256: str | None) -> int:
    """この bytes に与える版番号。**同じ bytes には同じ版**。

    版は「どのモデルか」を指すもので、「何回昇格したか」ではない。同じ
    artifact を再昇格するたびに新しい版を振ると、**同じ bytes が別の名前で
    増える**:

        dark_labia_minora v9 / v10 → patch_sha256 が同一 (623c8fc41a3c…)
        smegma            v2..v7   → 3 版ずつ同じ bytes

    identity は中身の hash — artifact ではその原則を守っているのに、版番号
    だけ通し番号だった。既に同じ sha の行があればその版を返す
    (`tagger_governed_upsert_patch` は ON CONFLICT DO UPDATE なので、metrics は
    新しい測定で更新され、モデルの同一性は保たれる)。
    """
    if artifact_sha256:
        same = (await db.execute(text(
            "SELECT min(version) FROM tagger_patch "
            "WHERE tag = :t AND patch_sha256 = :s"),
            {"t": tag, "s": artifact_sha256})).scalar()
        if same is not None:
            return int(same)
    return await _next_version(db, tag)


def _looks_like_sha256(v: str | None) -> bool:
    """**bytes の hash に見えるものだけを artifact として登録する。**

    2026-08-05 まで `artifact_sha256` には測定 JSON の hash や
    `(none: insufficient_gt_for_cv)` のような固定文字列が入っていた。
    形を確かめずに artifact 行を作ると、**model でないものが artifact として
    登録され**、policy の FK が「実体がある」と誤って言うことになる。
    """
    import re
    return bool(v) and bool(re.fullmatch(r"[0-9a-f]{64}", v))


async def promote_validated(db) -> list[str]:
    config = await load_config(db)
    rows = (await db.execute(text(
        "SELECT id, tag, source_host, artifact_ref, artifact_sha256, metrics, gt "
        "FROM tagger_registration_request WHERE status = 'validated' AND promoted_patch_id IS NULL"
    ))).mappings().all()
    promoted = []
    blocked: list[tuple[str, str]] = []
    wrong_kind: list[tuple[str, str]] = []
    no_policy: list[str] = []
    new_files: list[Path] = []
    for r in rows:
        tag = r["tag"]
        metrics = r["metrics"] or {}
        # **配備の直前にもう一度判定する** (round8 CRIT backward-compat, plan rev2 F1)。
        # 行は status='validated' のまま残るので、判定基準が変わったあとに
        # 「昔の基準で通った head」が黙って配備されうる。ここで現行の手続きに
        # かけ直すことでそれを塞ぐ。
        #
        # **かける手続きは review_pending と同一 (`decide`)。** 初版はここだけ
        # 旧 floor gate を直接呼んでおり、新形式 (classification を出し
        # metrics["val"] を持たない) は必ず落ちた。結果 2026-08-05 に配備可
        # 18 件が 1 件も patch にならず、「学習したのにタガーが更新されない」
        # 状態になっていた。手続きを 2 箇所に書くと、片方だけが新形式を知って
        # いる状態が作れてしまう — 作れないようにする。
        d = decide(metrics, r.get("gt"), config)
        if not d.ok:
            # **状態は動かさない。** validated は状態機械 (0077/0079) の終端で、
            # 終端行の書き換えは governed 関数が拒否する。初版はここで
            # 'rejected' へ落とそうとして例外を上げ、**auto-cycle 全体が
            # 15 分ごとに落ちていた** (以降のタグの promotion も道連れ)。
            # 履歴は書き換えず、**配備しないことで止める** — 再評価は行の
            # 復活ではなく再測定で入り直す (冪等鍵は (tag, lifecycle_event_id))。
            blocked.append((tag, d.reason))
            print(f"[auto_review] {tag} (id={r['id']}): promotion BLOCKED -- {d.reason}",
                  file=sys.stderr)
            continue
        # head でないバイトは昇格させない (chastity_cage patch 181 = 484KB の
        # 測定 JSON が v4 approved になった実例). kind は PUT 時に artifact_kind
        # が構造判定して確定 (0139). kind IS NULL / 行なし = fail-closed.
        kind = (await db.execute(text(
            "SELECT kind FROM tagger_artifact WHERE content_sha256 = :s"
        ), {"s": r["artifact_sha256"]})).scalar_one_or_none()
        if kind != artifact_kind.CONTENT_HEAD_BUNDLE:
            wrong_kind.append((tag, str(kind)))
            print(f"[auto_review] {tag} (id={r['id']}): promotion BLOCKED -- "
                  f"artifact kind={kind!r} (content_head_bundle を要求)",
                  file=sys.stderr)
            continue
        # ── domain 宣言ゲート (2026-09-03) ─────────────────────────────
        # **昇格の前に** curated 宣言 (tagger_tag_rule) を要求する。宣言が
        # 無い/表現できないタグをここで止めれば registration は 'validated' の
        # まま残り、curator が行を足した次のサイクルで自動的に通る。policy
        # 書き込み側で止めると promoted_patch_id が埋まって二度と再訪されず、
        # 「patch はあるのに policy が永久に書かれない」になる。
        # 実害: dark_labia_minora は `in_pussy_domain` 宣言済みなのに昇格が
        # 読まず gate_tags=[] で配備され、アーカイブの 45% に発火した。
        domain_gates = await head_policy.domain_gate_tags(db, tag)
        if domain_gates is None:
            reason = (
                "domain 未宣言 — tagger_tag_rule にこのタグの行が無い (または "
                "any/none 条件で gate_tags に表現できない)。無条件適用でよいなら "
                "condition='always'、視覚 domain 限定なら in_pussy_domain のような "
                "all_tags 条件を evidence 付きで宣言すること"
            )
            blocked.append((tag, reason))
            print(f"[auto_review] {tag} (id={r['id']}): promotion BLOCKED -- {reason}",
                  file=sys.stderr)
            continue
        version = await _version_for(db, tag, r["artifact_sha256"])
        gt = r["gt"] or {}
        why = (
            f"auto-promoted (user directive 2026-07-21: 自動化すること): "
            f"配備直前の再判定 (auto_review.decide) を通過。"
            f"source_host={r['source_host']}, "
            f"artifact_ref={r['artifact_ref']}, registration_request id={r['id']}. "
            f"NOTE: content_head model file distribution is not yet automated -- "
            f"see tagger/auto_review.py's module docstring."
        )
        patch_json = {
            "tag": tag, "version": version, "supersedes": (version - 1) or None,
            "status": "approved", "why": why, "gt": gt, "metrics": metrics,
            "base_repo": "SmilingWolf/wd-swinv2-tagger-v3", "base_revision": None,
            "logit_index": None, "sha256": r["artifact_sha256"],
            "created_at": date.today().isoformat(), "created_by": "tagger/auto_review.py",
            # sync_patch_registry が JSON から head_type を読む — 書かないと
            # 既定 'wd14_logit_patch' に落ち、DB へ upsert した content_head を
            # 次の publish_release が黙って降格させる (2026-09-03 実測)。
            "head_type": "content_head",
        }
        patch_dir = PATCHES / tag
        patch_dir.mkdir(parents=True, exist_ok=True)
        json_path = patch_dir / f"v{version}.json"
        json_path.write_text(json.dumps(patch_json, indent=2, ensure_ascii=False, sort_keys=True) + "\n")
        new_files.append(json_path)

        # content_head, not wd14_logit_patch -- see module docstring. model_data
        # intentionally NULL (distribution gap, documented above).
        await db.execute(text(
            "SELECT tagger_governed_upsert_patch(:tag, :version, 'approved', :sha, :why, NULL, "
            "CAST(:metrics AS jsonb), CAST(:gt AS jsonb), 'content_head', :base_repo, NULL, NULL, "
            ":created_at, :created_by, NULL)"
        ), {
            "tag": tag, "version": version, "sha": r["artifact_sha256"], "why": why,
            "metrics": json.dumps(metrics), "gt": json.dumps(gt),
            "base_repo": "SmilingWolf/wd-swinv2-tagger-v3",
            "created_at": date.today(), "created_by": "tagger/auto_review.py",
        })
        patch_id = (await db.execute(text(
            "SELECT id FROM tagger_patch WHERE tag = :tag AND version = :version"
        ), {"tag": tag, "version": version})).scalar_one()

        await db.execute(text(
            "SELECT tagger_governed_promote_registration(:reg_id, :patch_id)"
        ), {"reg_id": r["id"], "patch_id": patch_id})
        await db.commit()

        # ── 適用方法を行にする (migration 0134) ──────────────────────────
        # **昇格しただけでは適用されない**のが 2026-08-05 までの状態だった:
        # 閾値も apply_mode も gate も infer.py / nodec_autotag_worker.py の
        # 定数に書かれており、新しいタグは人が書き足すまで永久に効かなかった
        # (monochrome / greyscale がその実例)。ここで測定から導出して書く —
        # **タグごとの分岐は無い。どのタグでも同じ手続き**。
        # **bytes を先に登録する。** policy の content_sha256 は
        # `tagger_artifact` への FK なので、artifact 行が無ければ policy は
        # 絶対に書けない (2026-08-05: そのため配備可のタグが 1 件も policy を
        # 持てなかった)。artifact は content-addressed = **同じ bytes なら同じ行**
        # なので、ここで作っても二重にならない。
        art_bytes = (metrics.get("artifact_bytes")
                     or (metrics.get("runner") or {}).get("artifact_bytes"))
        if r["artifact_sha256"] and _looks_like_sha256(r["artifact_sha256"]):
            await db.execute(text(
                "INSERT INTO tagger_artifact (content_sha256, byte_size) "
                "VALUES (:s, :n) ON CONFLICT (content_sha256) DO NOTHING"
            ), {"s": r["artifact_sha256"], "n": int(art_bytes or 0)})
            await db.commit()

        pol = head_policy.derive(metrics, config=config, gate_tags=domain_gates)
        if pol is None:
            # 閾値を導けない測定からは policy を作らない (fail-closed)。既定値で
            # 埋めると「測っていない閾値」が配備に混ざる。patch 行は残るので、
            # 再測定すれば次の昇格で policy が付く。
            print(f"[auto_review] {tag} (id={r['id']}): patch は作ったが "
                  f"**適用 policy を導けない** (動作点に閾値が無い) — "
                  f"再測定すれば付く。この head は適用されない", file=sys.stderr)
            no_policy.append(tag)
        else:
            try:
                await db.execute(text(
                    "SELECT tagger_governed_upsert_head_policy("
                    ":tag, :ver, :sha, :op, :thr, :recall, :fpr, :mode, "
                    "CAST(:gates AS text[]), :reg_id)"
                ), {"tag": tag, "ver": version, "sha": r["artifact_sha256"],
                    "op": pol["operating_point"], "thr": pol["threshold"],
                    "recall": pol["measured_recall"], "fpr": pol["measured_fpr"],
                    "mode": pol["apply_mode"], "gates": pol["gate_tags"],
                    "reg_id": r["id"]})
                await db.commit()
                print(f"[auto_review] {tag} v{version}: policy "
                      f"{pol['apply_mode']} thr={pol['threshold']:.2f} "
                      f"at={pol['operating_point']} recall={pol['measured_recall']:.3f}")
            except Exception as e:  # noqa: BLE001
                # artifact が未登録なら FK で落ちる — **それが正しい**
                # (bytes の無い head を適用対象に入れない)。patch は残す。
                await db.rollback()
                print(f"[auto_review] {tag} v{version}: policy を書けない — {e}",
                      file=sys.stderr)
                no_policy.append(tag)

        # Found live 2026-07-21: this promotion never recorded a lifecycle_event,
        # so tagger-health's lifecycle-consistency check flagged every tag this
        # promoted as "approved but no ledger entry -- possible bypass of
        # governed path". Fixed here + migration 0104 (candidate/training ->
        # approved skip-ahead edges, matching 0094's NULL->approved precedent --
        # this fully-automated path never records separate trained/verified
        # steps, so 'approved' is reached directly from whatever stage the tag
        # was actually last at).
        lease = None
        try:
            from_stage = await _latest_stage(db, tag)
            lease = await acquire_lease(db, f"lifecycle:{tag}", lease_seconds=30)
            await record_lifecycle_event(
                db, tag=tag, from_stage=from_stage, to_stage="approved",
                reason=f"auto-promoted (user directive 2026-07-21: 自動化すること): "
                       f"registration_request id={r['id']} -> patch id={patch_id} (v{version})",
                lease=lease,
            )
        except TransitionError as e:
            print(f"[auto_review] WARNING: {tag} promoted but ledger write failed: {e}", file=sys.stderr)
        finally:
            # Release the lifecycle:<tag> lease promptly so it never expires
            # into a stale-lease CRIT (incident 2026-07-22). Fencing-matched +
            # idempotent; a failure here is non-fatal (reclaim-on-acquire still
            # self-heals the row on the next acquire).
            if lease is not None:
                try:
                    await release_lease(db, lease)
                except TransitionError as e:
                    print(f"[auto_review] WARNING: lease release failed for {tag}: {e}", file=sys.stderr)

        promoted.append(f"{tag} v{version} (patch id={patch_id})")
        print(f"[auto_review] {tag} (id={r['id']}): promoted to patch id={patch_id} (v{version})")

    if new_files:
        rel_paths = [str(p.relative_to(REPO_ROOT)) for p in new_files]
        subprocess.run(["git", "add", *rel_paths], cwd=REPO_ROOT, check=True)
        subprocess.run(
            ["git", "commit", "-m",
             "feat(tagger): auto-promote " + ", ".join(rel_paths) + "\n\n"
             "Written by tagger/auto_review.py (user directive 2026-07-21: 自動化すること).\n\n"
             "Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"],
            cwd=REPO_ROOT, check=True,
        )
        print(f"[auto_review] git-committed {len(new_files)} patch file(s)")
        # **commit しただけでは誰にも届かない。**
        #
        # 2026-08-05 まで nodec / desk は primary の作業ツリーから直接 pull していた
        # ので、push しなくても伝わっていた。その経路を GitHub に一本化した結果、
        # **push しないと canonical にも他ノードにも届かない**。
        # `build_release.py` は `tagger/patches/<tag>/v{version}.json` を読むので、
        # 届かなければ release が組めなくなる。
        #
        # 失敗しても昇格は取り消さない (patch 行は既に DB にあり、そちらが権威) が、
        # **黙らない** — 溜まった未 push は「動いているのに伝わっていない」状態で、
        # 気づく手段が無いと今日の分裂を繰り返す。
        try:
            subprocess.run(["git", "push", "origin", "HEAD"],
                           cwd=REPO_ROOT, check=True, capture_output=True, timeout=120)
            print("[auto_review] pushed to origin")
        except Exception as e:  # noqa: BLE001
            unpushed = subprocess.run(
                ["git", "rev-list", "--count", "origin/main..HEAD"],
                cwd=REPO_ROOT, capture_output=True, text=True).stdout.strip() or "?"
            print(f"[auto_review] WARNING: push に失敗 ({type(e).__name__}) — "
                  f"未 push が {unpushed} 件たまっています。**他ノードには届いて"
                  f"いません** (patch 行は DB にあるので昇格自体は有効)",
                  file=sys.stderr)

    if no_policy:
        # **黙らない。** policy が無い head は適用されないので、patch が
        # できたことだけを見て「配備された」と読まれると 2026-08-05 の
        # 再現になる (patch はあるのに永久に効かない)。
        print(f"[auto_review] 適用 policy 無し: {len(no_policy)} 件 "
              f"— {', '.join(sorted(set(no_policy)))} "
              f"(patch はあるが **適用されない**)", file=sys.stderr)
    if blocked:
        # **黙って止めない。** 配備直前に止めた行は status='validated' のまま
        # 残るので、報告しないと「validated なのに patch が無い」という
        # 手掛かりのない状態になる (2026-08-05 に 18 件がそうなっていた)。
        print(f"[auto_review] promotion blocked: {len(blocked)} 件 "
              f"— {', '.join(t for t, _ in blocked)}", file=sys.stderr)
    if wrong_kind:
        print(f"[auto_review] promotion blocked (artifact が head でない): "
              f"{len(wrong_kind)} 件 — "
              f"{', '.join(f'{t}({k})' for t, k in wrong_kind)}", file=sys.stderr)
    return promoted


async def run() -> dict:
    async with async_session() as db:
        review = await review_pending(db)
        promoted = await promote_validated(db)
    return {"review": review, "promoted": promoted}


def main() -> int:
    import asyncio
    summary = asyncio.run(run())
    print(f"[auto_review] {summary}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
