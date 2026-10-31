"""「不要」判定は floor 判定にかけない — 前段 gate (plan rev8 Finding 2)。

なぜ必要か: 絶対 floor (`min_p_deploy`) は**元の汚れ具合に盲目**なので、
元から正しいタグほど自動的に floor を満たす。dark_anus (π=0.819) は floor を
通過するが、配備すると FP 25 を消すために TP 34 を捨てる = 実効マイナス。
止めなければ実効ゼロの patch 行が自動生成され、その後の測定を汚す。

固定するのは 4 点:
  1. disposition=not-needed は floor 判定にかけず **終端 (rejected) に落とす**
  2. floor 関数は**呼ばれない** (判定基準を変えていないこと = INV-R4 の証拠)
  3. disposition が無い/別値の行は**従来どおり** floor にかかる (後方互換)
  4. promote_validated も disposition を見る (gate を通らず validated に達した行)

**なぜ pending 据え置きをやめたか** (nodec-cc-primary のレビュー、確定した欠陥):
状態機械 (0077) の CHECK は pending/validated/rejected の 3 つで、「意図的に
配備しない」に対応する終端が無い。据え置くと (a) review_pending が毎サイクル
全 pending を走査して同じ行を弾き続け pending が単調増加し、(b) promote_validated
は floor を再実行するが disposition を見ないため、gate を通らずに validated へ
達した行の「不要」が promote される。**「不要」は定義上 floor を通る**ので
floor 再検査では絶対に止まらない。再評価は行の復活ではなく**再測定**で入り直す
(runner の冪等鍵は (tag, lifecycle_event_id))。
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import pytest  # noqa: E402

from api.tagger_core import artifact_kind  # noqa: E402

from tagger import auto_review


class _Result:
    def __init__(self, rows, scalar=None):
        self._rows = rows
        self._scalar = scalar

    def mappings(self):
        return self

    def all(self):
        return self._rows

    def scalar_one(self):
        return self._scalar

    def scalar(self):
        return self._scalar

    def scalar_one_or_none(self):
        return self._scalar


class _FakeDB:
    """SELECT には行を返し、それ以外は状態遷移として記録するだけ。"""

    def __init__(self, rows):
        self._rows = rows
        self.transitions: list[tuple] = []

    async def execute(self, stmt, params=None):
        sql = str(stmt)
        if "SELECT id, tag, metrics" in sql:
            return _Result(self._rows)
        self.transitions.append((sql, params))
        return _Result([])

    async def commit(self):
        pass


def _run(rows, monkeypatch):
    called: list[dict] = []

    async def _load_config(db):
        return {}

    def _floor(metrics, config):
        called.append(metrics)

    monkeypatch.setattr(auto_review, "load_config", _load_config)
    monkeypatch.setattr(auto_review, "validate_registration_metrics_floor", _floor)
    db = _FakeDB(rows)
    out = asyncio.run(auto_review.review_pending(db))
    return out, db, called


def test_not_needed_goes_terminal_and_never_reaches_the_floor(monkeypatch):
    rows = [{"id": 1, "tag": "dark_anus",
             "metrics": {"disposition": "not-needed", "P_deploy": 0.95,
                         "classification": {"at": "feat1024@max_f1", "lift": 1.04,
                                            "net_fp_removed_deploy": 577.0,
                                            "why": "改善が無いに等しい"}}}]
    out, db, called = _run(rows, monkeypatch)

    assert called == [], "floor 関数を呼んではいけない (判定基準を変えていない証拠)"
    assert out["validated"] == []
    assert len(out["not_needed"]) == 1
    # **終端に落ちること** — pending に残すと毎サイクル走査され単調増加する
    assert db.transitions, "pending 据え置きにしない (終端が無いと積もり続ける)"
    sql, params = db.transitions[0]
    assert "rejected" in sql
    # 理由は後から検証できる形で残す (どの動作点での話かで符号が変わる)
    assert "feat1024@max_f1" in params["reason"] and "1.04" in params["reason"]


def test_rows_without_disposition_still_go_through_the_floor(monkeypatch):
    """後方互換: 既存行 (disposition 無し) の扱いは 1 ミリも変わらない。"""
    rows = [{"id": 2, "tag": "fellatio", "metrics": {"P_deploy": 0.95}}]
    out, db, called = _run(rows, monkeypatch)

    assert out["validated"] == ["fellatio"] and out["not_needed"] == []
    assert called and called[0]["P_deploy"] == 0.95
    assert any("validated" in sql for sql, _ in db.transitions)


def test_other_dispositions_are_not_swallowed(monkeypatch):
    """4 クラスとも明示的な終端を持ち、どれも黙って消えないこと。

    **2026-08-05 に意味が変わった。** 初版は「not-needed 以外は全て floor 判定へ
    渡す」を固定していた — 当時 gate が扱うのは 1 クラスだけだったので、その
    記述は正しかった。しかし実測でその素通しが害だと分かった:

      新形式は `classification` を出し、旧 floor gate は `metrics["val"]` の
      tp/fp/fn を読む。**形が違うので旧 gate は新形式を必ず落とす。**
      結果、cleanup-capable 3 件 (smegma は net −163,583 件の判定) と
      **deployable 2 件**が「検証内訳が無い」として却下されていた。

    通すのは二重チェックではなく壊れたチェックだった。テストの意図
    (どの disposition も黙って消さない) はそのままに、「渡す」ではなく
    「それぞれの終端に着く」を固定する。
    """
    rows = [{"id": 3, "tag": "licking_testicle",
             "metrics": {"disposition": "cleanup-capable"}},
            {"id": 4, "tag": "x",
             "metrics": {"disposition": "deployable",
                         "classification": {"at": "a", "lift": 9.9,
                                            "verdict": "deployable"}}}]
    out, _db, called = _run(rows, monkeypatch)

    assert out["not_needed"] == []
    assert called == [], "新形式を旧 floor gate に渡さない (必ず落ちる)"
    assert [t for t, _ in out["owner_decision"]] == ["licking_testicle"]
    assert out["validated"] == ["x"]


def test_metrics_null_does_not_crash(monkeypatch):
    out, _db, called = _run([{"id": 5, "tag": "t", "metrics": None}], monkeypatch)
    assert out["not_needed"] == [] and len(called) == 1


# ── promote 経路 ────────────────────────────────────────────────────────
#
# **2026-08-05 の実障害**。ここの旧テストは 2 つとも**壊れた挙動を固定して
# いた**:
#
#   (a) 「配備可の行が旧 floor gate に**届く**こと」を assert していた。
#       旧 gate は metrics["val"] の tp/fp/fn を読むので、新形式 (classification を
#       出し val を持たない) は**必ず落ちる**。結果、配備可 18 件
#       (monochrome / greyscale / smegma / dark_labia_*) が status='validated' の
#       まま 1 件も patch にならず、「学習したのにタガーが更新されない」状態に
#       なっていた。
#
#   (b) 止めた行を 'rejected' へ落とすこと (`any("rejected" in sql ...)`) を
#       assert していた。**validated は状態機械 (0077/0079) の終端**で、実際の
#       governed 関数は「not found or not pending」で例外を上げる。偽 DB は
#       何でも受け取るのでテストは green のまま、本番では
#       kura-tagger-auto-cycle.service が 15 分ごとに落ちていた。
#
# 偽 DB が本物より寛容だと、こうして「テストが通る」形で欠陥が残る。
# 以下の _PromoteDB は**本物と同じく終端行の書き換えを拒否する**。


class _PromoteDB(_FakeDB):
    """本物の governed 関数と同じ規律を持つ偽 DB。

    `tagger_governed_set_registration_status` は pending 行しか動かせない。
    ここを寛容にすると、本番で例外になる呼び出しが test では通ってしまう。
    版番号は **DB が権威**なので、ここも DB として答える。
    """

    max_version = 0

    async def execute(self, stmt, params=None):
        sql = str(stmt)
        if "FROM tagger_registration_request WHERE status = 'validated'" in sql:
            return _Result(self._rows)
        if "SELECT kind FROM tagger_artifact" in sql:
            # 偽 DB のデフォルトは「正しくアップロードされた head」— Bug A の
            # 修正で保証される状態。kind 門そのものを検証したいテストは
            # 個別に artifact_kind をずらす。
            return _Result([], scalar=artifact_kind.CONTENT_HEAD_BUNDLE)
        if "FROM tagger_tag_rule" in sql:
            # 新契約 (2026-09-03): 昇格は curated domain 宣言を要求する。偽 DB の
            # デフォルトは「always 宣言済み」。宣言側の挙動そのものは
            # test_promotion_derives_gate_from_declaration.py が固定する。
            return _Result([{"name": "always", "all_tags": [],
                             "any_tags": [], "none_tags": []}])
        if "tagger_governed_set_registration_status" in sql:
            raise RuntimeError(
                "tagger_governed_set_registration_status: request "
                f"{(params or {}).get('id')} not found or not pending")
        self.transitions.append((sql, params))
        if "SELECT id FROM tagger_patch" in sql:
            return _Result([], scalar=777)
        if "max(version)" in sql:
            # 版番号は **DB が権威** (ファイルではない)。偽 DB もそれを返す。
            return _Result([], scalar=self.max_version)
        return _Result([])


def _promote(rows, monkeypatch, tmp_path, floor=None):
    """promote_validated を**最後まで**走らせる。

    途中で止まる偽物にすると「配備されたか」を確かめられない — 実際、旧テストは
    floor で例外を投げて止めていたので、その先が動かないことに気づけなかった。
    """
    called: list = []

    async def _load_config(db):
        return {}

    async def _stage(db, tag):
        return "training"

    async def _acquire(db, name, lease_seconds=30):
        return object()

    async def _record(db, **kw):
        return None

    async def _release(db, lease):
        return None

    monkeypatch.setattr(auto_review, "load_config", _load_config)
    monkeypatch.setattr(auto_review, "validate_registration_metrics_floor",
                        floor or (lambda m, c: called.append(m)))
    monkeypatch.setattr(auto_review, "PATCHES", tmp_path)
    monkeypatch.setattr(auto_review, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(auto_review, "_latest_stage", _stage)
    monkeypatch.setattr(auto_review, "acquire_lease", _acquire)
    monkeypatch.setattr(auto_review, "record_lifecycle_event", _record)
    monkeypatch.setattr(auto_review, "release_lease", _release)
    # git は触らない (patch ファイルは tmp_path に書かれるので REPO_ROOT の外)
    monkeypatch.setattr(auto_review.subprocess, "run", lambda *a, **k: None)
    db = _PromoteDB(rows)
    return asyncio.run(auto_review.promote_validated(db)), db, called


NOT_NEEDED_ROW = {"id": 9, "tag": "dark_anus", "source_host": "trainer",
                  "artifact_ref": "x", "artifact_sha256": "deadbeef",
                  "gt": {"positive": 172, "negative": 38},
                  "metrics": {"disposition": "not-needed", "P_deploy": 0.95,
                              "classification": {"at": "feat1024@max_f1",
                                                 "lift": 1.04,
                                                 "net_fp_removed_deploy": 577.0,
                                                 "why": "改善が無いに等しい"}}}

# 実測から切り出した配備可の行 (id=105 monochrome, 2026-08-05 04:39)。
DEPLOYABLE_ROW = {"id": 105, "tag": "monochrome", "source_host": "trainer",
                  "artifact_ref": "gt_monochrome.safetensors",
                  "artifact_sha256": "cafe", "gt": {"positive": 313, "negative": 1500},
                  "metrics": {"disposition": "deployable",
                              "classification": {"at": "feat1024@max_f1",
                                                 "lift": 31.2, "verdict": "deployable",
                                                 "net_fp_removed_deploy": 582697.1,
                                                 "why": "絶対 floor を満たす"}}}


def test_promote_blocks_not_needed_rows_that_never_saw_the_gate(monkeypatch, tmp_path):
    """gate 導入前から status='validated' で残っている「不要」を promote しない。

    「不要」は**定義上 floor を通る** (元から綺麗なタグ) ので、floor 再検査では
    絶対に止まらない。判定 (`decide`) にかけて初めて止まる。
    """
    promoted, db, called = _promote([NOT_NEEDED_ROW], monkeypatch, tmp_path)

    assert promoted == [], "配備してはいけない"
    assert called == [], "floor 再検査に届く前に止める (floor では止まらないため)"
    assert not list(tmp_path.rglob("v*.json")), "patch ファイルを書いてはいけない"


def test_promote_does_not_rewrite_a_terminal_row(monkeypatch, tmp_path):
    """**本題 (b)**: 止めるときに status を動かさない。

    validated は終端。ここで 'rejected' を試みると本物は例外を上げ、
    **auto-cycle 全体が落ちて以降のタグの promotion も道連れ**になる
    (2026-08-05 実測)。履歴は書き換えず、配備しないことで止める。
    """
    promoted, db, _ = _promote([NOT_NEEDED_ROW], monkeypatch, tmp_path)
    assert promoted == []
    assert not any("set_registration_status" in sql for sql, _ in db.transitions)


def test_one_blocked_row_does_not_kill_the_rest(monkeypatch, tmp_path):
    """止めた行があっても後続は配備される。1 件の巻き添えで全体が止まらない。"""
    promoted, _db, _ = _promote([NOT_NEEDED_ROW, DEPLOYABLE_ROW], monkeypatch, tmp_path)
    assert [p.split()[0] for p in promoted] == ["monochrome"]


def test_promote_does_not_send_the_new_format_to_the_old_floor_gate(monkeypatch, tmp_path):
    """**本題 (a)**: 配備可の行を旧 gate に渡さない。

    旧 gate は metrics["val"] を読むので新形式は必ず落ち、**配備が永久に
    起きない**。通すのは二重チェックではなく壊れたチェック。
    """
    def _floor(m, c):
        raise AssertionError("新形式を旧 floor gate に渡している")

    promoted, _db, _ = _promote([DEPLOYABLE_ROW], monkeypatch, tmp_path, floor=_floor)
    assert [p.split()[0] for p in promoted] == ["monochrome"], "配備されていない"
    assert (tmp_path / "monochrome" / "v1.json").exists(), "patch ファイルが書かれていない"


def test_promote_still_uses_the_floor_for_old_format_rows(monkeypatch, tmp_path):
    """後方互換: classification を持たない旧行は従来どおり floor にかかる。"""
    row = dict(DEPLOYABLE_ROW, id=11, tag="fellatio", metrics={"P_deploy": 0.95})
    _promoted, _db, called = _promote([row], monkeypatch, tmp_path)
    assert called and called[0]["P_deploy"] == 0.95


def test_review_and_promote_ask_the_same_question():
    """**手続きは 1 つ**。2 箇所に書くと片方だけが新形式を知る状態が作れる。"""
    src = (REPO / "tagger" / "auto_review.py").read_text()
    body = src[src.index("async def promote_validated("):]
    assert "decide(" in body, "promote が共有の判定手続きを使っていない"
    assert "validate_registration_metrics_floor" not in body, \
        "promote が旧 gate を直接呼んでいる"
