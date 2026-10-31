"""v-next 決定論 runner — 訓練判断から LLM を外す実行主体。

plan tagger-vnext-deterministic-runner rev6 Step 10/12/13/14/18。
trainer の timer から oneshot で起動し、1 tick で以下を行う:

    heartbeat → GPU gate → claim → reconciliation → 測定 → 判定
    → registration POST (どの判定でも) → complete

**なぜ heartbeat が最初なのか (INV-R8)**: broker の
`_reroute_dead_targets` は「status=ready かつ updated_at > now-10min」でない
target 宛 task を**他 agent へ振り直す**。作業がある tick でしか heartbeat を
打たない設計だと、idle の谷で dead 判定に落ち、GPU も kura replica も無い
ノードへ GPU 学習 task が飛ぶ (2026-08-02 実害: 16 件が nodec/desk へ散布)。
よって heartbeat は **作業の有無にも GPU gate の結果にも依存させない**。

**周期の導出 (Step 13)**: `DEAD_TARGET_AGENT_STALE` = `DEAD_TARGET_TASK_MIN_AGE`
= 10 分。heartbeat 間隔はこれを**下回らねばならない**。timer 周期 5 分 +
最悪 1 tick 落ち = 10 分ちょうどでは境界に張り付くため、周期 5 分・
許容落ち 1 回で余裕 5 分を持たせる。unit 側 `OnUnitActiveSec=5min` と
この定数はペアで動く — 片方だけ変えないこと。

**INV-R1**: kura DB へは書かない。読みは primary 直読と local replica、書きは
primary:8443 API のみ。broker task 面 (claim/heartbeat/complete) は別勘定。
**INV-R3**: **どの判定でも** registration-request を出す (silent drop 禁止)。
判定の語彙は 4 クラス (不要/配備可/掃除可/保留) ただ 1 つ — 旧 2 値の
PASS/HOLD は `vnext_arms` が絶対 (率) floor を表すための内部値であって、
**測定の結論ではない**。この module は結論として旧 2 値を読まない。
**INV-R5**: 配備しない。出口は registration 行まで。
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import socket
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Mapping

from tagger import vnext_escalate
from tagger.vnext_class import HOLD
from tagger.vnext_state import (AttemptLedger, IdempotencyKey, ordered_work_items,
                                should_noop)

# ── 定数 (Step 13 の導出結果を含む) ──────────────────────────────────────
AGENT_ID = os.environ.get("VNEXT_AGENT_ID", "trainer-taggerrunner-primary")
AGENT_HOST, AGENT_EXECUTOR = AGENT_ID.split("-", 2)[:2]
BROKER_URL = os.environ.get("BROKER_URL", "http://localhost:8000")
from tagger.api_target import api_base
KURA_API = api_base(required=False)

# broker の dead 判定は 10 分。timer は 5 分周期 (unit と対で維持)。
DEAD_TARGET_STALE_MINUTES = 10
TIMER_PERIOD_MINUTES = 5
assert TIMER_PERIOD_MINUTES * 2 <= DEAD_TARGET_STALE_MINUTES, (
    "heartbeat 周期が broker の dead 判定に対して余裕不足 — Step 13 の導出を見直すこと")

LEASE_SECONDS = 3600            # 測定は分オーダー。lease は heartbeat で延長する
WALL_CLOCK_BUDGET_S = int(os.environ.get("VNEXT_BUDGET_S", 3600))
GPU_GATE = Path.home() / "deploy-checkout" / "scripts" / "gpu_batch_gate.py"
GPU_HOLD_EXIT = 75              # gate が「今は走るな」と言うときの exit code
STATE_DIR = Path.home() / "tagger-heads" / "vnext_state"

_ctx = ssl.create_default_context()
_ctx.check_hostname = False
_ctx.verify_mode = ssl.CERT_NONE


def log(event: str, **kv: Any) -> None:
    parts = " ".join(f"{k}={v}" for k, v in kv.items())
    print(f"[vnext-runner] {event} {parts}".rstrip(), flush=True)


# ── broker (task 面) ────────────────────────────────────────────────────
RUNNER_KEY_FILE = Path.home() / "tagger-heads" / ".runner_key"


def _broker_key() -> str:
    """runner 専用の per-agent キー。

    **他 agent のキーで代用しない**: broker は per-agent キーから
    caller_agent_id を解決するので、cc のキーを使うと runner は
    trainer-cc-primary として振る舞い、Phase 1 の「訓練 task は runner 宛
    のみ」という前提が静かに崩れる (LLM 窓と同一 identity になる)。
    キーが無ければ起動しない (fail-closed)。

    識別の bootstrap 経路について: 同一ホストの非対話 id を per-agent キーで
    名乗る broker 側の分岐は、trusted 非対話 executor (reviewer / claude_ios)
    への昇格経路になるため 2026-08-02 に revert された (nodec-cc-primary の
    レビューが実チェーンを提示)。よって runner の identity は broker DB に
    agents 行を直接用意する運用対処で与える。
    """
    if RUNNER_KEY_FILE.exists():
        key = RUNNER_KEY_FILE.read_text().strip()
        if key:
            return key
    raise RuntimeError(
        f"{RUNNER_KEY_FILE} が無い — {AGENT_ID} の per-agent キーを用意すること "
        "(broker の agents 行と対で作る)")


def broker(method: str, path: str, body: dict | None = None) -> Any:
    req = urllib.request.Request(
        BROKER_URL + path, method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"X-API-Key": _broker_key(), "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as res:
        raw = res.read()
        return json.loads(raw) if raw else None


def heartbeat() -> None:
    """agent-status を書いて AgentRow の鮮度を更新する (INV-R8)。

    作業の有無・GPU gate の結果に依存させない。ここが落ちても tick 全体は
    続ける (heartbeat 不達で測定を止める理由は無い) が、必ず記録は残す。
    """
    try:
        broker("POST", "/memories", {
            # type は broker の必須列挙 (agent_status)、kind は AgentRow upsert の
            # 分岐キー (agent-status)。両方要る — 片方だけだと 422 か素通り。
            "type": "agent_status", "kind": "agent-status", "status": "ready",
            "scope": "global",
            "source_agent_id": AGENT_ID, "source_executor": AGENT_EXECUTOR,
            "host": AGENT_HOST,
            "content": f"{AGENT_ID} alive (vnext runner tick)",
            "tags": ["capability:tagger-training", "kind:agent-status"],
        })
        log("heartbeat.ok", agent=AGENT_ID)
    except Exception as e:  # noqa: BLE001 — heartbeat 失敗で tick を殺さない
        log("heartbeat.failed", error=type(e).__name__, detail=str(e)[:120])


# ── GPU gate (Step 10 / C4: 第二 arbiter を作らない) ────────────────────
def gpu_admits() -> tuple[bool, str]:
    """fleet 唯一の arbiter (gpu_batch_gate.py) に従う。自前の VRAM 予約はしない。"""
    if not GPU_GATE.exists():
        return False, "gate_missing"          # fail-closed
    try:
        p = subprocess.run([sys.executable, str(GPU_GATE), "--check"],
                           capture_output=True, text=True, timeout=120)
    except Exception as e:  # noqa: BLE001
        return False, f"gate_error:{type(e).__name__}"
    if p.returncode == 0:
        return True, "admit"
    if p.returncode == GPU_HOLD_EXIT:
        return False, "hold"
    return False, f"gate_rc:{p.returncode}"


# ── kura (読みは replica / 書きは primary API のみ = INV-R1) ────────────────
def kura_read(sql: str, args: tuple = ()) -> list[tuple]:
    """Bulk reads (KURA_READ_DSN, default the local replica; tagger/kura_db.py)."""
    from tagger.kura_db import connect_read
    conn = connect_read()
    try:
        cur = conn.cursor()
        cur.execute(sql, args)
        return cur.fetchall()
    finally:
        conn.close()


def kura_read_primary(sql: str, args: tuple = ()) -> list[tuple]:
    """primary canonical を直読する。replica に無い/複製されないものを見るとき用。

    接続先は KURA_CANONICAL_DSN (既定は primary、tagger/kura_db.py)。以前は呼び出しごとに
    psycopg2.connect をべた書きしていて、同じ資格情報が複数箇所に散っていた。
    """
    from tagger.kura_db import connect_canonical
    conn = connect_canonical()
    try:
        cur = conn.cursor()
        cur.execute(sql, args)
        return cur.fetchall()
    finally:
        conn.close()


def kura_post(path: str, body: dict) -> dict:
    tok = (Path.home() / "tagger-heads" / ".ktok").read_text().strip()
    auth = "Token " + base64.b64encode(f"owner:{tok}".encode()).decode()
    req = urllib.request.Request(
        KURA_API + path, method="POST", data=json.dumps(body).encode(),
        headers={"Authorization": auth, "Content-Type": "application/json"})
    with urllib.request.urlopen(req, context=_ctx, timeout=60) as res:
        return json.loads(res.read())


def current_stage(tag: str) -> tuple[str | None, int]:
    """primary canonical の (最新 stage, そのイベント id)。replica は非複製なので primary 直読。

    **イベント id は冪等キーの片割れ** (INV-R11)。タグ名だけを鍵にすると、GT が
    育って reconciler が再 dispatch しても「判定済み」で永久に skip され、
    **タグは生涯 1 回しか測定されない** — 「GT が育つ → 再測定 → 良くなる」という
    ループそのものが成立しなくなる (2026-08-03 実測、初版は id を 0 に固定していた)。
    逆に同一イベントの再配達は同じ鍵になるので、二重測定にはならない。
    """
    rows = kura_read_primary("""select to_stage, id from tagger_lifecycle_event
                            where tag=%s order by occurred_at desc, id desc limit 1""", (tag,))
    return (rows[0][0], int(rows[0][1])) if rows else (None, 0)


# ── 構造前提 (E0) ───────────────────────────────────────────────────────
def _schema_drift_verdict(local: str | None, remote: str | None):
    """(問題なしか, 記録する語) を返す。**版の差は向きで意味が変わる。**

        canonical が先行  → replica に無い列が送られて replication が止まる。
                            これがこの検査の存在理由 (2026-08-02 / 08-06 の実害)
        replica が先行    → 安全。published table を触る migration では
                            **推奨される順序**そのもの
        一致              → 問題なし

    版は 4 桁 0 埋めの連番 (0134, 0135, ... ) なので文字列比較で順序が付く。
    **その形でないものは順序を判断しない** — 分からないものを安全と読まない
    ので、一致していなければ問題ありとして扱う (fail-closed)。
    """
    if not local or not remote:
        return False, "unreadable"
    if local == remote:
        return True, None
    ordered = all(v.isdigit() and len(v) == 4 for v in (local, remote))
    if not ordered:
        return False, "unordered_revisions"
    if local > remote:
        return True, "replica_ahead"        # 推奨される順序。止めない
    return False, "canonical_ahead"         # 実害のある向き


def structural_context(tag: str) -> dict[str, Any]:
    """測定して意味があるか = 世界が想定どおりかを確かめる (Step 17 E0)。"""
    ctx: dict[str, Any] = {}
    try:
        ctx["tag_in_vocab"] = bool(kura_read(
            "select 1 from tag_name where name=%s and ord=0 limit 1", (tag,)))
    except Exception as e:  # noqa: BLE001
        log("structural.tag_lookup_failed", error=str(e)[:120])
    try:
        ctx["release_row_present"] = bool(kura_read(
            "select 1 from tagger_release_active limit 1"))
    except Exception as e:  # noqa: BLE001
        log("structural.release_lookup_failed", error=str(e)[:120])
    try:
        # kura の帳簿は `kura_alembic_version` (alembic.ini / api/alembic/env.py が
        # version_table でそう指定している)。素の `alembic_version` は szurubooru の
        # 遺物で、kura の migration とは無関係。
        #
        # 2026-08-02 に「帳簿はノード間で不一致 (実 schema とも乖離)」と判断して
        # version 一致を諦め「両方が読める」だけに弱めたのは、**間違った表を見て
        # いたから**だった。実測 (2026-08-04): 素の alembic_version は trainer
        # 5b5c940b4e78 / primary 0078 と食い違うが、本物の kura_alembic_version は
        # 両ノードとも 0126 で一致している。壊れていたのは帳簿ではなく参照先で、
        # そのせいで replica drift の検査が実質無効になっていた。
        _VERSION_TABLE = "kura_alembic_version"
        local = kura_read(f"select version_num from {_VERSION_TABLE}")
        remote = kura_read_primary(f"select version_num from {_VERSION_TABLE}")
        # 実 schema の確認は版と別軸で残す。版が一致していても、published table
        # への ADD COLUMN が replica に届いていない形はありうる。
        constraint_ok = bool(kura_read(
            "select 1 from pg_constraint where conname='ck_post_tag_audit_source'"))
        # **向きで意味が変わる。** この検査が守っているのは「canonical が先に
        # 進み、replica に無い列を送って apply worker を止める」形 (2026-08-02、
        # および 2026-08-06 に WAL 18 MB が滞留した実害)。
        #
        # 逆向き — **replica が先行** — は危険ではなく、published table を触る
        # migration では**むしろ推奨される順序**。2026-08-06 以降そう運用して
        # おり、その結果この検査が blocking で測定を止めていた (E0 を 3 回)。
        # 正しい手順を踏むほど鳴る検査は、鳴っても情報を運ばない。
        ok, why = _schema_drift_verdict(
            local[0][0] if local else None, remote[0][0] if remote else None)
        ctx["replica_schema_matches"] = bool(ok and constraint_ok)
        if why:
            log("structural.schema_version_drift", verdict=why,
                local=local[0][0] if local else None,
                primary=remote[0][0] if remote else None)
    except Exception as e:  # noqa: BLE001
        # 検査が走らなかったことを「一致していた」と読まない。key を立てないので
        # vnext_escalate は「観測できていない前提」として扱う。
        log("structural.schema_check_failed", error=str(e)[:120])
    return ctx


# ── 判定 → registration (Step 14 / 18) ──────────────────────────────────
def _assert_gt_int_keys(gt: Mapping[str, Any]) -> dict[str, int]:
    """`positive`/`negative` は reconciler の成長ゲートが読む契約キー。

    round-8 の anal_object_insertion (registration id=33) が 'pos'/'neg' で
    書き、baseline が 0/0 と読まれて「GT が育った」が偽で真になり、同一
    ラベル集合で 1 ラウンド空回りした。ここで落とすのはその再発防止。
    """
    for key in ("positive", "negative"):
        if key not in gt or not isinstance(gt[key], int):
            raise ValueError(
                f"gt['{key}'] must be an int (got {gt.get(key)!r}) — "
                "reconciler の成長ゲートが読む契約キー")
    return {k: v for k, v in gt.items() if isinstance(v, int)}


def _artifact_for_registration(doc: Mapping[str, Any], fallback_path) -> tuple[str, str]:
    """登録・アップロードする artifact の (sha, ref) を 1 箇所で決める.

    deployable な測定では _save_deployable_head が artifact_sha256/artifact_ref に
    content-addressed な head bundle を確定させる. それがあるのに測定 JSON を渡すと
    (a) 測定 JSON が model の顔で登録され, (b) _upload_artifact が bundle sha に
    測定 JSON を PUT して primary が 400 を返す (chastity_cage の "HTTP 400 転送破損"
    の実体) — bundle が永遠に primary へ届かない. sha と ref は必ず同じ出所から採る.
    head の無い測定は測定 JSON 自身を artifact とする (kind=measurement_json,
    auto_review 側の kind 門が patch 昇格を止める).
    """
    sha = doc.get("artifact_sha256")
    ref = doc.get("artifact_ref")
    if sha and ref:
        return sha, ref
    return _sha256_of(Path(fallback_path)), str(fallback_path)


def _try_backfill_registration(tag: str, prev_doc, prev_path,
                               gt: Mapping[str, int]) -> None:
    """前回の測定結果をそのまま登録し、欠けている baseline を作る。

    GPU は使わない — skip の目的 (再測定の省略) を壊さずに、成長ゲートが
    読む baseline だけを埋める。失敗しても呼び出し側を止めない (黙らない)。

    `gt` は呼び出し側の cur_gt (= tagger_gt_label の**総数**) を必ず渡す。
    prev_doc 自身の 'gt' は使わない — gt_full 化 (dcbfb12 以前) の測定 doc は
    eval-set cap 後の件数を 'gt' に持っており (greyscale 実測 2026-09-04:
    doc 2490/1500 vs 総数 3294/4859)、それを再登録すると baseline が総数に
    永遠に一致せず、reconciler の成長ゲートが毎サイクル真 → 再 dispatch →
    gt-unchanged → また backfill、の紙のループになる (24h で 37 回
    candidate→training、毎回 auto-promote まで完走して repo に commit)。
    backfill は prev_gt == cur_gt の分岐でしか呼ばれないので、cur_gt は
    前回測定時点の総数そのものでもある — 嘘にならない。
    """
    try:
        # backfill 経路は key を持たないのでここで引く。0 を渡すと「event 不明」の
        # 1 行に全タグの backfill が潰れるので、必ず実際の event を使う。
        _, _event_id = current_stage(tag)
        art_sha, art_ref = _artifact_for_registration(prev_doc, prev_path)
        reg = submit_registration(tag, {**dict(prev_doc), "gt": dict(gt)},
                                  art_ref, art_sha,
                                  lifecycle_event_id=_event_id)
        log("registration.submitted", tag=tag, id=reg.get("id"),
            lifecycle_event=_event_id, why="backfill")
    except Exception as exc:  # noqa: BLE001
        log("registration.failed", tag=tag, error=type(exc).__name__,
            detail=str(exc)[:200], why="backfill")


def _try_hold_registration(tag: str, gt: Mapping[str, int] | None, *,
                           reason: str, detail: str) -> int | None:
    """HOLD 登録を試みる。失敗しても呼び出し側の流れを止めない。

    別関数にしてあるのは、呼び出し側 (ManifestError の処理) に `except` を
    入れ子にしないため。入れ子にすると「ManifestError の処理はどこまでか」を
    読む側 (人も、tests/test_vnext_runner_gate.py の走査も) が境界を誤る。
    登録に失敗しても task は閉じる — 開けたままだと lease が切れるまで誰も
    触れない。ただし**黙らない**。
    """
    if gt is None:
        log("registration.skipped", tag=tag, why="GT 件数が読めない")
        return None
    try:
        reg_id = submit_hold_registration(tag, gt, reason=reason, detail=detail).get("id")
        log("registration.submitted", tag=tag, id=reg_id, verdict=HOLD)
        return reg_id
    except Exception as exc:  # noqa: BLE001
        log("registration.failed", tag=tag, error=type(exc).__name__,
            detail=str(exc)[:200])
        return None


def submit_hold_registration(tag: str, gt: Mapping[str, int], *, reason: str,
                             detail: str) -> dict:
    """**測定に到達できなかった HOLD** も登録する (INV-R3 の silent drop 禁止)。

    なぜ要るか (2026-08-04 実測): 成長ゲートは
    `_last_registration_positive()` = 「直近 registration の gt->>'positive'」を
    baseline に使う。登録が 1 件も無いと baseline が None になり、reconciler は
    **毎サイクル同じタグを再 dispatch する**。reconciler 自身の docstring が
    その失敗形を名指ししている — 「a tag rejected for insufficient GT would get
    redispatched every single cycle it stays candidate, spamming the training
    node with identical retries」。
    実際 deepthroat は登録 0 件のまま 1 日 8 回の reopen 枠を使い切り、14 タグが
    training に滞留した。**登録を出さないことが、まさにその暴走を招いていた。**

    通常の `submit_registration` と分けてあるのは、あちらの「classification が
    無ければ登録しない」fail-closed を弱めないため。こちらは測定に**到達して
    いない**ので classification が無いのが正常で、artifact も存在しない。
    artifact_ref / artifact_sha256 は NOT NULL なので、**ハッシュと誤読されない
    番兵**を入れる (実体が無いことが読んで分かる文字列)。
    floor は metrics が無いので当然通らず rejected になる — それが正しい:
    GT baseline だけが記録され、次に GT が育つまで再 dispatch されない。
    """
    return kura_post("/tagger/registration-request", {
        "tag": tag, "source_host": AGENT_HOST,
        "artifact_ref": f"(no artifact: {reason})",
        "artifact_sha256": f"(none: {reason})",
        "metrics": {
            "runner": {"agent": AGENT_ID},
            # 4 クラスのうち「保留」。測定に到達していないので arms も verdict も無い。
            "disposition": HOLD,
            "classification": {"verdict": HOLD, "why": detail,
                               "net_fp_removed_deploy": None, "lift": None,
                               "at": None, "binding": ["min_gt_positive (split 層別下限)"]},
            "reason": reason,
        },
        "gt": _assert_gt_int_keys(dict(gt)),
    })


def _upload_artifact(sha: str | None, ref: str | None) -> None:
    """head の bytes を primary へ置く。**identity は中身の hash** なので冪等。

    送れなくても測定・登録は止めない (次の tick で送り直せる) が、**黙らない** —
    送れていない head は他ノードから見えないので、静かに失敗すると
    「登録されているのに誰も適用できない」状態が続く。
    """
    import re as _re
    if not sha or not _re.fullmatch(r"[0-9a-f]{64}", sha):
        return                      # 測定 JSON の hash 等は artifact ではない
    path = Path(ref) if ref else None
    if path is None or not path.is_file():
        log("artifact.missing", sha=sha[:12], ref=str(ref))
        return
    try:
        tok = (Path.home() / "tagger-heads" / ".ktok").read_text().strip()
        auth = "Token " + base64.b64encode(f"owner:{tok}".encode()).decode()
        req = urllib.request.Request(
            f"{KURA_API}/tagger/artifact/{sha}", method="PUT",
            data=path.read_bytes(),
            headers={"Authorization": auth,
                     "Content-Type": "application/octet-stream"})
        with urllib.request.urlopen(req, context=_ctx, timeout=300) as res:
            res.read()
        log("artifact.uploaded", sha=sha[:12], bytes=path.stat().st_size)
    except Exception as e:  # noqa: BLE001
        log("artifact.upload_failed", sha=sha[:12], error=type(e).__name__,
            detail=str(e)[:160])


def submit_registration(tag: str, result: Mapping[str, Any], artifact_ref: str,
                        artifact_sha256: str, lifecycle_event_id: int = 0) -> dict:
    """どの判定でも必ず出す (INV-R3)。status は決めない (INV-R4)。"""
    gt = _assert_gt_int_keys(result.get("gt", {}))
    # 判定が無いまま登録すると auto_review の前段 gate が素通りし、「不要」な
    # head が自動 promote される。**欠けている = 安全ではなく未確認**なので
    # ここで落とす (fail-closed、E0 の「確認できなかったものを正常と読まない」と同じ)。
    if not (result.get("classification") or {}).get("verdict"):
        raise ValueError(
            "classification が無い測定は登録できない — vnext_arms.measure_tag が "
            "4 クラス判定を付けているか確認する (plan rev8)")
    metrics = {
        "verdict": result.get("verdict"),
        "arms": {n: {k: a.get(k) for k in ("auc", "above_chance", "nested",
                                           "p_deploy", "p_deploy_lift")}
                 for n, a in _arms_of(result).items()},
        "floor_live_primary": result.get("floor_live_primary"),
        "manifest_hashes": result.get("manifest_hashes"),
        "runner": {"agent": AGENT_ID, "schema": result.get("schema")},
        # **disposition は auto_review の前段 gate が読む** (plan rev8 Finding 2):
        # 「不要」は絶対 floor を満たしてしまうため、そのままだと実効ゼロの
        # patch 行が自動生成される。floor 関数の判定基準は変えず、適用対象を
        # 絞る前段 gate で pending に留める (INV-R4 の精密化どおり)。
        "disposition": (result.get("classification") or {}).get("verdict"),
        # **判定に使った動作点を残す** (primary-gemini-primary のレビュー指摘 2)。
        # 判定は「正当な動作点のうち差し引き最大の点」で行うが、patch JSON は
        # 閾値も動作点も持たない (model_data も NULL — 配布は未自動化)。つまり
        # 実際に配備する側は別の規則で動作点を選びうる。**別の点で配備すれば
        # 判定は移らない** — dark_anus は max_f1 で +577、P>=0.90 なら −6,630。
        # ここに残しておけば、後から「どの点での話だったか」を突き合わせられる。
        "classification": result.get("classification"),
    }
    # **bytes を先に送る。** artifact が primary に無ければ、昇格側は policy を
    # 書けない (content_sha256 は tagger_artifact への FK) し、他ノードは
    # head を取りに行く先が無い。ここで送らないと head は学習ノードの
    # ローカルディスクに留まり続ける — 「配布は未自動化」と docstring に
    # 書かれていた穴がこれ。
    _upload_artifact(artifact_sha256, artifact_ref)
    return kura_post("/tagger/registration-request", {
        "tag": tag, "source_host": AGENT_HOST,
        "artifact_ref": artifact_ref, "artifact_sha256": artifact_sha256,
        "metrics": metrics, "gt": gt,
        # **測定の識別子** (INV-R11)。HOLD では artifact が作られず
        # artifact_sha256 が固定値になるので、これが無いと同じタグの HOLD が
        # すべて 1 行に潰れ、GT が育っても記録が初回のまま残る (0132 で実測)。
        "lifecycle_event_id": lifecycle_event_id,
    })


# ── 1 タグの処理 ────────────────────────────────────────────────────────
def process_task(task: Mapping[str, Any], ledger: AttemptLedger,
                 deadline: float, dry_run: bool, register: bool = True) -> str:
    tag = task.get("_tag") or ""
    task_id = task["id"]

    # ── reconciliation (INV-R11): kura の stage が唯一の真実 ──
    # 鍵は (tag, 最新 lifecycle イベント id)。GT が育って再 dispatch されれば
    # 新しいイベント id になるので改めて測定され、同一イベントの再配達は
    # 同じ鍵なので測定は 1 回だけになる。
    stage, event_id = current_stage(tag)
    key = IdempotencyKey(tag=tag, lifecycle_event_id=event_id)
    if should_noop(stage):
        log("noop", tag=tag, stage=stage, reason="kura stage は training ではない")
        if not dry_run:
            broker("POST", f"/tasks/{task_id}/complete",
                   {"handled_by": AGENT_ID,
                    "result_payload": {"disposition": "noop", "stage": stage}})
        return "noop"

    if not ledger.should_attempt(key):
        # 既に判定済み (done) か dead-letter。**task を閉じないと queue に残り、
        # 毎 tick これを拾っては何もせず終わる空回りになる** (実測 2026-08-03:
        # deepthroat が 5 分ごとに skipped を繰り返し、後続タグが 1 件も進まな
        # かった)。判定済みであることを結果に残して閉じる。
        state = "dead-letter" if ledger.is_dead(key) else "already-judged"
        log("skip", tag=tag, reason=state)
        if not dry_run:
            try:
                broker("POST", f"/tasks/{task_id}/complete", {
                    "handled_by": AGENT_ID,
                    "result_payload": {"disposition": "noop", "reason": state,
                                       "note": "この (tag, lifecycle event) は既に判定済み"}})
            except Exception as e:  # noqa: BLE001
                log("skip.complete_failed", tag=tag, error=type(e).__name__)
        return "skipped"

    # ── E0: 測定前の構造前提 ──
    esc = vnext_escalate.structural_preconditions(structural_context(tag))
    if vnext_escalate.blocking(esc):
        for e in esc:
            log("escalate", tag=tag, code=e.code, summary=e.summary)
        ledger.record_attempt(key, "escalated", {"codes": [e.code for e in esc]})
        # **owner へ届ける**。ここは長らく log だけで、escalation は誰にも
        # 届いていなかった (配送関数は存在するのに繋がっていない = 「機構が
        # あると思っていたが強制力が無い」系の 3 例目)。
        gt = _current_gt(tag) or {}
        _deliver_escalations(tag, esc, gt.get("positive", 0), gt.get("negative", 0), None)
        # **task を閉じる**。閉じないと queue に残り、次の tick も同じ E0 で
        # 止まる。構造前提の欠落は再測定では直らない (release 行が無い・語彙から
        # 消えた等) ので、拾い直しても意味が無く、queue を汚すだけ。
        # 登録は出さない — E0 は GT の問題ではないので、GT baseline を書くと
        # 「GT が育てば直る」という誤った含意になる。直し方は escalation 側にある。
        if not dry_run:
            try:
                broker("POST", f"/tasks/{task_id}/complete", {
                    "handled_by": AGENT_ID,
                    "result_payload": {
                        "verdict": HOLD, "reason": "structural_precondition",
                        "escalations": [e.code for e in esc],
                        "detail": "; ".join(e.summary for e in esc)[:300],
                        "note": "測定前の構造前提が満たせない。再測定では直らないので "
                                "owner の対応が要る (escalation を送出済み)"}})
            except Exception as ce:  # noqa: BLE001
                log("escalate.complete_failed", tag=tag, error=type(ce).__name__)
        return "escalated"

    # ── GT が前回測定から動いていなければ測り直さない ──
    # lifecycle イベント id が変われば鍵は変わるので「再測定してよい」状態には
    # なるが、**GT が同一なら結果も同一**で GPU/CPU を捨てるだけになる
    # (2026-08-03: reopen 周期が 8/24h に緩んだ結果、同じ GT で fellatio /
    # dark_anus / chastity_cage を 1 時間おきに測り直していた)。判定は前回の
    # 測定結果をそのまま再掲する — silent drop はしない (INV-R3)。
    prev_path = STATE_DIR / f"{tag}.measurement.json"
    # **比較は自前で残した生の GT 件数で行う**。測定結果の `gt` は「ゲート内に
    # 入った件数」で、`tagger_gt_label` の総数とは別物 — 同じ名前の別量を
    # 突き合わせると永遠に一致せず、省略が効かない (2026-08-03 実測: fellatio が
    # 毎回 45 秒を捨てていた)。
    gt_stamp = STATE_DIR / f"{tag}.gt.json"
    cur_gt = _current_gt(tag)
    if prev_path.exists() and gt_stamp.exists() and cur_gt is not None:
        try:
            prev_doc = json.loads(prev_path.read_text())
            prev_gt = json.loads(gt_stamp.read_text())
        except Exception:  # noqa: BLE001
            prev_doc, prev_gt = {}, None
        if prev_gt == cur_gt:
            # ── skip してよいのは baseline がある時だけ ──────────────────
            # 成長ゲートは**直近 registration の GT** を baseline にする。
            # 登録が無いまま skip すると、reconciler は毎サイクル同じタグを
            # 再 dispatch し、runner は毎回 gt-unchanged で帰る — タグは
            # 前に進まないのに仕事だけ回り続ける (insufficient_gt と同じ形。
            # 2026-08-04 に 14 タグが training に滞留した原因の系統)。
            # shadow 期間 (--no-registration) の測定は登録を残していないので、
            # この状態は実際に存在する。
            # **配備できる policy を作れない測定は skip の根拠にならない。**
            #
            # 2026-08-06 実測: greyscale は GC が「policy が無い」と candidate へ
            # 戻すたびに、ここで gt-unchanged と判定され **測定を省略して古い
            # 測定を再登録**していた。その測定は動作点の閾値 (thr) を持たない
            # 世代のものなので policy は作れず、また GC が戻す — **一度も測り
            # 直さない無限ループ**。GPU も再 dispatch 枠も消費し続ける。
            #
            # reconciler の成長ゲートには同じ条件を入れたが、runner 側の
            # skip 経路が残っていた (今日 9 回目の「片方だけ直す」)。
            from tagger import head_policy as _hp
            if _hp.derive(prev_doc) is None:
                log("gt.unchanged.remeasure", tag=tag,
                    why="前回結果から配備 policy を作れない (動作点の閾値が無い)")
                prev_gt = None
            elif ((prev_doc.get("classification") or {}).get("verdict") == "deployable"
                  and not prev_doc.get("artifact_sha256")):
                # **配備可と言いながら head を 1 つも作れていない測定**は、
                # 再登録しても同じ行き止まりに戻る。
                #
                # 実測 2026-08-08 (dark_labia_majora): classification=deployable /
                # derive() は policy を作れる / だが artifact_sha256 も
                # artifact_ref も None。backfill が「測定 JSON 自身」を artifact
                # として登録し、0139 の門 (kind='content_head_bundle' 必須) が
                # policy を止め、配備できないので GC が reopen し、また skip する。
                # 8/6 の測定のまま lifecycle だけが 2 日間回っていた。
                #
                # skip の目的は GPU の節約だが、**節約した先が行き止まりなら
                # 節約になっていない**。測り直せば head が作られる可能性がある。
                log("gt.unchanged.remeasure", tag=tag,
                    why="前回は配備可だが head が作られていない (artifact 無し)")
                prev_gt = None
            baseline = _last_registration_gt(tag) if prev_gt == cur_gt else None
            if prev_gt == cur_gt and baseline != cur_gt:
                if (prev_doc.get("classification") or {}).get("verdict"):
                    # 前回の測定結果がそのまま登録できる → GPU を使わずに
                    # baseline だけ作る (skip の目的は再測定の省略なので保つ)。
                    log("gt.unchanged.backfill_registration", tag=tag,
                        baseline=baseline, gt=cur_gt)
                    if not dry_run:
                        _try_backfill_registration(tag, prev_doc, prev_path, cur_gt)
                else:
                    # **登録できない結果は skip の根拠にならない。**
                    # 旧スキーマ (classification 無し) はここに来る — 測り直せば
                    # schema/2 になり、通常経路が登録まで面倒を見る。
                    log("gt.unchanged.remeasure", tag=tag,
                        why="前回結果が登録できる形でなく baseline も無い")
                    prev_gt = None  # skip せず測定へ落とす
            if prev_gt == cur_gt:
                # **前回の判定をそのまま名乗る** — `verdict_absolute` (旧 2 値) を
                # 報告すると、規模 floor で deployable になった測定が
                # 「HOLD(gt-unchanged)」として記録され、判定が退行して見える。
                prev_disposition = (prev_doc.get("classification") or {}).get("verdict") or HOLD
                log("gt.unchanged", tag=tag, gt=cur_gt, verdict=prev_disposition)
                ledger.record_attempt(key, "gt_unchanged", {"gt": cur_gt})
                ledger.mark_done(key)
                if not dry_run:
                    broker("POST", f"/tasks/{task_id}/complete", {
                        "handled_by": AGENT_ID,
                        "result_payload": {
                            "reason": "gt_unchanged",
                            "verdict": prev_disposition,
                            "gt": cur_gt, "measurement": str(prev_path),
                            "note": "前回測定から GT が動いていないため再測定を省略 "
                                    "(判定は前回のまま)"}})
                return f"{prev_disposition}(gt-unchanged)"

    # ── 測定 (唯一の実装 = INV-R9) ──
    from tagger import vnext_arms
    from tagger.vnext_manifest import ManifestError
    t0 = time.time()
    try:
        result = vnext_arms.measure_tag(tag)
    except ManifestError as e:
        # **測定できないことが分かった = それ自体が判定**。split の層別下限を
        # 満たせないのは「正例が CV に足りない」ことの機械的な現れで、
        # 分類不能 (E0/E5) でも実装エラーでもない。HOLD として記録し
        # task を閉じる — ここを error 扱いにすると 5 分ごとに再測定を
        # 試み続け、しかも「なぜ止まっているか」が registration にも
        # task にも残らない (INV-R3 の silent drop と同じ害)。
        # 実例 2026-08-03 deepthroat: 正例 11 件で外側 fold に pos=0 が出た。
        log("measure.insufficient_gt", tag=tag, detail=str(e)[:200])
        ledger.record_attempt(key, "hold_insufficient_gt", {"error": str(e)[:300]})
        ledger.mark_done(key)
        # **登録を出す** — 出さないと成長ゲートの baseline が無いままになり、
        # reconciler が毎サイクル同じタグを再 dispatch する (2026-08-04 実測:
        # deepthroat は登録 0 件で 1 日 8 回の reopen 枠を使い切っていた)。
        reg_id = None if dry_run else _try_hold_registration(
            tag, cur_gt, reason="insufficient_gt_for_cv",
            detail=f"GT の正例が CV の層別下限に届かない: {str(e)[:200]}")
        if not dry_run:
            broker("POST", f"/tasks/{task_id}/complete", {
                "handled_by": AGENT_ID,
                "result_payload": {
                    "verdict": HOLD, "binding": ["min_gt_positive (split 層別下限)"],
                    "reason": "insufficient_gt_for_cv", "detail": str(e)[:300],
                    "registration_id": reg_id,
                    "note": "GT の正例が CV の層別下限に届かない。正例を増やせば測定可能",
                }})
        return f"{HOLD}(insufficient_gt)"
    except Exception as e:  # noqa: BLE001
        log("measure.failed", tag=tag, error=type(e).__name__, detail=str(e)[:200])
        ledger.record_attempt(key, "error", {"error": str(e)[:300]})
        # 例外で終わった測定に判定は無い。**None を明示的に渡す** — 既定値で
        # 黙って埋めると「判定が無い」と「判定が読めなかった」が同じ形になる。
        esc = vnext_escalate.evaluate(tag=tag, verdict={}, arms={}, floor={},
                                      hold_streak=0, gt_pos=0, gt_neg=0,
                                      disposition=None, exception=e)
        return "error"
    log("measure.done", tag=tag, seconds=round(time.time() - t0, 1))

    if time.time() > deadline:
        # 予算超過: 測定結果は捨てず記録し、次 tick で続きから (Step 10)。
        log("budget.exceeded", tag=tag)

    verdict = result.get("verdict") or {}
    arms = _arms_of(result)
    # ── 4 クラス判定 (plan rev8) ──────────────────────────────────────
    # PASS/HOLD の 2 値だと「元から綺麗なタグに不要な head を配備可と言い、
    # 汚れたタグの有用な head を却下する」逆転が起きる (2026-08-03 実測)。
    # **判定そのものは vnext_arms.measure_tag が済ませている** — 判定に必要な
    # 数値 (腕ごとの above_chance、正当な動作点) は測定側にしか無く、ここで
    # verdict から復元しようとすると意味を取り違える (実際 capability_gain を
    # 「偶然超え」と誤読して lift 5.9 倍の smegma を「判別不能」と呼んだ)。
    klass = result.get("classification") or {}
    log("classified", tag=tag, verdict=klass.get("verdict"),
        net=(None if klass.get("net_fp_removed_deploy") is None
             else round(klass["net_fp_removed_deploy"])),
        lift=klass.get("lift"), at=klass.get("at"))
    gt = result.get("gt") or {}
    gt_pos, gt_neg = int(gt.get("positive", 0)), int(gt.get("negative", 0))

    # **この関数から先、判定を語るのは disposition ただ 1 つ。**
    # 旧 2 値 (`verdict["any_arm_meets_floor"]`) は絶対 (率) floor しか見ないので、
    # 規模 floor で deployable になった測定でも False のまま。escalation の可否・
    # HOLD streak・broker への報告・戻り値の 4 箇所すべてが旧値を読んでいて、
    # 配備が決まったタグに毎測定 E4 が鳴っていた (2026-08-05 smegma 実測)。
    # 旧値は `vnext_arms` が `absolute_pass` (率の floor を満たす腕があるか) を
    # 作るための入力であって、**測定の結論ではない**。
    disposition = klass.get("verdict")

    prev = ledger.last_detail(key)
    streak, bucket = vnext_escalate.next_hold_streak(
        int(prev.get("hold_streak", 0)), prev.get("gt_bucket"),
        vnext_escalate.is_concluded(disposition), gt_pos, gt_neg)

    esc = vnext_escalate.evaluate(
        tag=tag, verdict=verdict, arms=arms,
        floor=result.get("floor_live_primary") or {}, hold_streak=streak,
        gt_pos=gt_pos, gt_neg=gt_neg, disposition=disposition)
    for e in esc:
        log("escalate", tag=tag, code=e.code, blocking=e.blocking, summary=e.summary)
    # **届けなければ escalation は存在しないのと同じ** (INV-R6: LLM の唯一の
    # 入口は述語合致時の dispatch)。初版は log に出すだけで誰にも届かず、
    # dark_anus の E2 (corpus arm 逆相関 AUC 0.405) が実際に誰にも見られない
    # まま流れた (2026-08-03 実測)。dedup 鍵は GT bucket 化済みなので、同じ
    # 状況では 1 回しか鳴らない。
    if not dry_run:
        _deliver_escalations(tag, esc, gt_pos, gt_neg, out_path=None)

    # ── registration (どの判定でも出す = INV-R3) ──
    out_path = STATE_DIR / f"{tag}.measurement.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(result, ensure_ascii=False, indent=1))
    # 次回「GT が動いたか」を判断するための刻印 (測定結果の gt とは別量)。
    if cur_gt is not None:
        (STATE_DIR / f"{tag}.gt.json").write_text(json.dumps(cur_gt))
    art_sha, art_ref = _artifact_for_registration(result, out_path)

    if dry_run or not register:
        # Phase 1 (本番経路の shadow): claim/complete は本番どおり行い
        # registration だけ止める。measurement は成果物として残すので、
        # Phase 2 で有効化したときに同じ判定が再現するかを確かめられる。
        log("registration.disabled", tag=tag, phase="shadow" if not dry_run else "dry-run",
            verdict=disposition, measurement=str(out_path))
        if not dry_run:
            broker("POST", f"/tasks/{task_id}/complete", {
                "handled_by": AGENT_ID,
                "result_payload": {
                    "phase": "shadow", "registration": "disabled",
                    "verdict": disposition, "why": klass.get("why"),
                    "net_fp_removed_deploy": klass.get("net_fp_removed_deploy"),
                    "at": klass.get("at"), "lift": klass.get("lift"),
                    "binding": verdict.get("binding_floor_terms_by_arm"),
                    "measurement": str(out_path),
                    "escalations": [e.code for e in esc]}})
    else:
        reg = submit_registration(tag, result, art_ref, art_sha,
                                  lifecycle_event_id=key.lifecycle_event_id)
        log("registration.submitted", tag=tag, id=reg.get("id"),
            lifecycle_event=key.lifecycle_event_id)
        broker("POST", f"/tasks/{task_id}/complete", {
            "handled_by": AGENT_ID,
            "result_payload": {
                "verdict": disposition, "why": klass.get("why"),
                "net_fp_removed_deploy": klass.get("net_fp_removed_deploy"),
                "at": klass.get("at"), "lift": klass.get("lift"),
                "binding": verdict.get("binding"),
                "registration_id": reg.get("id"),
                "escalations": [e.code for e in esc],
            }})

    ledger.record_attempt(key, "done", {"hold_streak": streak, "gt_bucket": list(bucket)})
    ledger.mark_done(key)
    return disposition


def _deliver_escalations(tag: str, escalations, gt_pos: int, gt_neg: int,
                         out_path) -> None:
    """escalation を owner へ届ける。**鳴らしすぎない**ことも要件。

    dedup 鍵は (code, tag, GT bucket) — 同じ状況で繰り返し鳴らさず、GT が
    まとまって育てば改めて鳴る。情報提供型 (blocking=False) も届けるが
    importance を下げる: E7 は「正例を増やせば届く」という**次の行動が分かる**
    情報で、握り潰すと HOLD の理由が人間に見えなくなる。
    """
    for e in escalations:
        try:
            broker("POST", "/tasks/dispatch", {
                "content": (f"[vnext escalation {e.code}] tag={tag}\n{e.summary}\n\n"
                            f"詳細: {json.dumps(e.detail, ensure_ascii=False)[:800]}\n"
                            f"GT: positive={gt_pos} negative={gt_neg}\n"
                            f"測定: {out_path or STATE_DIR / f'{tag}.measurement.json'}\n"
                            f"(runner が自動送出。blocking={e.blocking})"),
                "task_kind": "message",
                "target_agent_id": "trainer-cc-primary",
                "target_host": "trainer",
                "source_agent_id": AGENT_ID,
                "dedup_key": e.dedup_key(tag, gt_pos, gt_neg),
                "importance": "medium" if e.blocking else "low",
            })
            log("escalate.delivered", tag=tag, code=e.code)
        except Exception as ex:  # noqa: BLE001 — 配送失敗で測定結果は捨てない
            log("escalate.deliver_failed", tag=tag, code=e.code,
                error=type(ex).__name__)


def _current_gt(tag: str) -> dict[str, int] | None:
    """いまの GT 件数 (primary canonical)。読めなければ None = 判定を省略しない。"""
    from tagger.kura_db import connect_canonical
    try:
        conn = connect_canonical(connect_timeout=10)
    except Exception:  # noqa: BLE001
        return None
    try:
        cur = conn.cursor()
        cur.execute("select verdict, count(*) from tagger_gt_label "
                    "where tag=%s group by 1", (tag,))
        got = {r[0]: int(r[1]) for r in cur.fetchall()}
        return {"positive": got.get("positive", 0), "negative": got.get("negative", 0)}
    except Exception:  # noqa: BLE001
        return None
    finally:
        conn.close()


def _last_registration_gt(tag: str) -> dict[str, int] | None:
    """そのタグの**直近 registration が記録した GT** (primary canonical)。

    reconciler の成長ゲートが `_last_registration_positive()` で読むのと同じ値。
    None は「登録が 1 件も無い」か「読めなかった」= **baseline が無い**。

    読めなければ None を返す (fail-closed 側): 呼び出し側は「baseline がある」と
    仮定して skip してはいけない。
    """
    from tagger.kura_db import connect_canonical
    try:
        conn = connect_canonical(connect_timeout=10)
    except Exception:  # noqa: BLE001
        return None
    try:
        cur = conn.cursor()
        cur.execute("select gt from tagger_registration_request "
                    "where tag=%s order by requested_at desc limit 1", (tag,))
        row = cur.fetchone()
        if not row or not row[0]:
            return None
        gt = row[0]
        pos, neg = gt.get("positive"), gt.get("negative")
        if not isinstance(pos, int) or not isinstance(neg, int):
            return None
        return {"positive": pos, "negative": neg}
    except Exception:  # noqa: BLE001
        return None
    finally:
        conn.close()


def _arms_of(result: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    """measure_tag の出力から arm 群を平らに取り出す。

    出力は `B_veto_ceiling: {feat1024: {...}, logit: {...}}` のように
    grouping されている一方、escalation 述語は arm 名 → metrics の平坦な
    map を見る。ここで 1 度だけ潰す (述語側に構造知識を持ち込まない)。
    """
    flat: dict[str, Mapping[str, Any]] = {}
    for group in ("A_deployed", "B_veto_ceiling", "C_dispatch_recipe", "E_stock_pixai"):
        node = result.get(group)
        if not isinstance(node, Mapping):
            continue
        # **A_deployed の AUC キーは `auc` ではない** —
        # `auc_in_gate_keep_vs_remove` (現タガーが「残す/消す」をどれだけ
        # 判別できているか)。`"auc" in node` だけを見ていたため **現タガーの腕が
        # 丸ごと落ちていた** (2026-08-05 実測: 登録の metrics.arms に候補腕しか
        # 無く、比較の基準側が欠けていた)。乖離を見るには基準が要る。
        if "auc" in node or "auc_in_gate_keep_vs_remove" in node:
            n = dict(node)
            if "auc" not in n and "auc_in_gate_keep_vs_remove" in n:
                n["auc"] = n["auc_in_gate_keep_vs_remove"]
            flat[group] = n
            continue
        for space, arm in node.items():         # 入れ子 (B / C)
            if isinstance(arm, Mapping) and "auc" in arm:
                flat[f"{group}:{space}"] = arm
    return flat


def _sha256_of(path: Path) -> str:
    import hashlib
    return hashlib.sha256(path.read_bytes()).hexdigest()


# ── tick ────────────────────────────────────────────────────────────────
def _extract_tag(task: Mapping[str, Any]) -> tuple[str | None, int]:
    """reconciler の task 本文から対象タグを取り出す (`tag=<name> が`)。"""
    import re
    m = re.search(r"tag=([A-Za-z0-9_\-:.]+)", task.get("content") or "")
    return (m.group(1) if m else None), 0


def tick(dry_run: bool = False, limit: int = 1, register: bool = True) -> int:
    # 1) heartbeat は常に最初 (INV-R8) — GPU gate より前、作業の有無に依らない
    heartbeat()

    # 2) GPU gate (第二 arbiter を作らない)
    admit, why = gpu_admits()
    if not admit:
        log("gpu.hold" if why == "hold" else "gpu.failed", reason=why)
        return GPU_HOLD_EXIT if why == "hold" else 1

    # 3) claim 対象を探す
    try:
        inbox = broker("GET", f"/tasks/inbox?target_agent_id={AGENT_ID}&limit=20") or []
    except Exception as e:  # noqa: BLE001
        log("inbox.failed", error=type(e).__name__, detail=str(e)[:150])
        return 1
    work = []
    for t in inbox:
        if t.get("task_kind") != "code-edit":
            continue
        tag, _ = _extract_tag(t)
        if not tag:
            continue
        work.append({**t, "_tag": tag})
    if not work:
        log("no-work")
        return 0

    ledger = AttemptLedger(STATE_DIR / "ledger.json")
    # 並び順を決めるためだけの鍵 (タグ昇順)。**実際の冪等キーは process_task が
    # kura の lifecycle イベント id から作る** — ここで id を埋めると task 本文
    # 由来の固定値が鍵になり、再 dispatch が永久に skip される。
    keys = ordered_work_items([IdempotencyKey(tag=w["_tag"], lifecycle_event_id=0)
                               for w in work])
    by_tag = {w["_tag"]: w for w in work}
    deadline = time.time() + WALL_CLOCK_BUDGET_S
    done = 0
    for key in keys:
        if done >= limit or time.time() > deadline:
            break
        task = by_tag[key.tag]
        if not dry_run:
            try:
                broker("POST", f"/tasks/{task['id']}/claim",
                       {"handled_by": AGENT_ID, "lease_seconds": LEASE_SECONDS})
            except urllib.error.HTTPError as e:
                log("claim.failed", tag=key.tag, code=e.code)
                continue
        outcome = process_task(task, ledger, deadline, dry_run, register)
        log("tag.done", tag=key.tag, outcome=outcome)
        # 空振り (既判定/stage 不一致) は「1 タグ処理した」に数えない。数えると
        # 判定済みタグが列の先頭にいる限り毎 tick そこで打ち切られ、後続が
        # 永遠に進まない (2026-08-03 実測)。
        if outcome not in ("skipped", "noop"):
            done += 1
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="v-next deterministic training runner")
    ap.add_argument("--dry-run", action="store_true",
                    help="claim/registration/complete を行わず判定だけ出す")
    ap.add_argument("--limit", type=int, default=1, help="1 tick で処理するタグ数")
    ap.add_argument("--no-registration", action="store_true",
                    help="Phase 1 (shadow): claim/complete はするが registration を出さない")
    ap.add_argument("--heartbeat-only", action="store_true",
                    help="heartbeat だけ打って終了 (Phase 1 の liveness 確認用)")
    args = ap.parse_args()
    if not KURA_API:
        log("configuration-required", fix="set KURA_URL or KURA_API to the canonical Kura endpoint")
        return 2
    if socket.gethostname().split(".")[0] != AGENT_HOST:
        log("wrong-host", expected=AGENT_HOST, actual=socket.gethostname())
        return 2
    if args.heartbeat_only:
        heartbeat()
        return 0
    return tick(dry_run=args.dry_run, limit=args.limit,
                register=not args.no_registration)


if __name__ == "__main__":
    raise SystemExit(main())
