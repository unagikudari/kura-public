"""Runner-gate building blocks for plan tagger-vnext-deterministic-runner
(Steps 5/6/8 -- the pieces of Step 23 whose subjects exist in this change):

  - non-trainable exclusion predicate (Step 5, one shared predicate)
  - gt payload shape (the round-8 'pos'/'neg' key bug, at the source)
  - floor comes from the DB, not from code (Step 16 flavour of Step 8)
  - negative sampler honours the TOTAL cap (bug 2 -- the old per-group-quota
    implementation is reproduced here verbatim to demonstrate the no-op)
  - leakage grouping (bug 1 -- pool vs pool-free vs recovered singletons)
  - idempotency / attempt ledger / dead-letter (Step 6, INV-R11)

E0-E7 escalation and stale-cancel tests are deliberately ABSENT (not
skipped): vnext_escalate.py / vnext_runner.py are later steps and a skipped
name would fake coverage for code that does not exist yet.

All pure -- no DB, no network, no torch."""
import json
import random
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from api.tagger_core.config import (  # noqa: E402
    NON_TRAINABLE_TAG_CATEGORIES,
    NON_TRAINABLE_TAGS,
    is_non_trainable,
)
from tagger.vnext_arms import (  # noqa: E402
    build_grouping,
    deployed_patch_version,
    gt_payload,
    deploy_lift,
    load_floor,
    p_deploy,
    sample_capped,
)
from tagger.vnext_state import (  # noqa: E402
    AttemptLedger,
    IdempotencyKey,
    ordered_work_items,
    run_once,
    should_noop,
)


# ------------------------------------------------- Step 5: exclusion predicate

def test_literal_marker_excluded_in_any_category():
    assert "tagger-exclude" in NON_TRAINABLE_TAGS
    for cat in (None, "general", "meta"):
        assert is_non_trainable("tagger-exclude", cat)


@pytest.mark.parametrize("cat", sorted(NON_TRAINABLE_TAG_CATEGORIES))
def test_identity_meta_categories_excluded(cat):
    assert is_non_trainable("Sakura🔞", cat)
    assert is_non_trainable("anything_at_all", cat)


@pytest.mark.parametrize("cat", ["general", "sex_act", "clothing", "pose",
                                 "male_genitals", "character"])
def test_visual_categories_trainable(cat):
    assert not is_non_trainable("licking_testicle", cat)


def test_unknown_category_does_not_exclude_on_its_own():
    # a category lookup failure must degrade to the old behaviour, not
    # silently drop candidates
    assert not is_non_trainable("some_new_tag", None)


def test_expected_category_vocabulary():
    # character は 2026-09-04 に trainable 化 (user 裁定「開いて」— キャラは
    # 画像に写る視覚概念で、pixai char head が画像だけから当てるのがその証明。
    # 当初の除外は artist=WHO made との過剰一般化だった)。artist/copyright/meta
    # の除外はそのまま — ここが縮んだら identity タグが学習系へ漏れている。
    assert NON_TRAINABLE_TAG_CATEGORIES == {"meta", "artist", "copyright"}


# --------------------------------------------------------- Step 8: gt payload

def test_gt_payload_uses_validator_vocabulary():
    """round-8's registration id=33 wrote 'pos'/'neg'; the reconciler's growth
    baseline read 0/0 and re-dispatched an unchanged dataset. The payload
    builder must emit the validator's exact int-keyed vocabulary."""
    p = gt_payload(72, 8527)
    assert p == {"positive": 72, "negative": 8527}
    assert set(p) == {"positive", "negative"}
    assert all(type(v) is int for v in p.values())


def test_gt_payload_coerces_to_int():
    p = gt_payload(True, 3.0)  # defensive: bool/float in, int out
    assert type(p["positive"]) is int and type(p["negative"]) is int


# ---------------------------------------- arm A: deployed patch version

def test_deployed_patch_version_reads_manifest_approved_entry(tmp_path):
    """The hardcoded 'v1' bug: anilingus is approved at v7 in the live
    MANIFEST.json (v1-v6 superseded), so a hardcoded v1.safetensors silently
    scored the WRONG, long-superseded weights as 'the deployed row'. The
    MANIFEST allowlist -- the same one build_release.py reads -- is the only
    source of truth for what is actually live."""
    manifest = tmp_path / "MANIFEST.json"
    manifest.write_text(json.dumps({"approved": [
        {"tag": "anilingus", "version": 7, "patch_sha256": "deadbeef"},
        {"tag": "erection", "version": 1, "patch_sha256": "cafef00d"},
    ]}))
    assert deployed_patch_version("anilingus", manifest_path=manifest) == 7
    assert deployed_patch_version("erection", manifest_path=manifest) == 1


def test_deployed_patch_version_none_when_tag_not_approved(tmp_path):
    """A retired/never-approved tag has no deployed row -- None, not a
    fabricated version, so the caller takes the 'skipped' branch instead of
    silently scoring a nonexistent or stale file."""
    manifest = tmp_path / "MANIFEST.json"
    manifest.write_text(json.dumps({"approved": [
        {"tag": "erection", "version": 1, "patch_sha256": "cafef00d"},
    ]}))
    assert deployed_patch_version("phimosis", manifest_path=manifest) is None


def test_deployed_patch_version_none_on_missing_or_bad_manifest(tmp_path):
    assert deployed_patch_version("anilingus", manifest_path=tmp_path / "nope.json") is None
    bad = tmp_path / "MANIFEST.json"
    bad.write_text("{not json")
    assert deployed_patch_version("anilingus", manifest_path=bad) is None


# -------------------------------------------- Step 14: P_deploy (prevalence)

def test_p_deploy_is_invariant_to_negative_subsampling():
    """The whole point of P_deploy (Step 8b finding): sub-sampling the negatives
    inflates the MEASURED precision but leaves R and FPR — and therefore
    P_deploy — unchanged. Same head, same operating point, 6x fewer negatives."""
    R, fpr, pi = 0.7613, 33 / 1500, 398 / 9473
    # measured precision on the capped set vs on the full population
    P_capped = 303 / (303 + 33)
    P_full = 303 / (303 + 33 * (9018 / 1500))
    assert P_capped > 0.90 and P_full < 0.65      # measured P moves a lot
    pd_capped = p_deploy(R, fpr, pi)
    pd_full = p_deploy(R, (33 * (9018 / 1500)) / 9018, pi)  # same FPR, full denom
    assert abs(pd_capped - pd_full) < 1e-9        # P_deploy does not
    assert 0.59 < pd_capped < 0.62                # and it is BELOW the 0.85 floor


def test_p_deploy_falls_with_rarer_tags():
    """Prevalence ceiling: the same head deploys worse as the tag gets rarer."""
    vals = [p_deploy(0.8, 0.02, pi) for pi in (0.5, 0.1, 0.01, 0.001)]
    assert vals == sorted(vals, reverse=True)
    assert vals[0] > 0.9 and vals[-1] < 0.1


def test_p_deploy_perfect_and_degenerate_cases():
    assert p_deploy(1.0, 0.0, 0.05) == 1.0        # no false positives
    assert p_deploy(0.0, 0.0, 0.05) == 0.0        # fires on nothing -> not 0/0
    assert p_deploy(0.9, 0.01, 0.0) == 0.0        # nothing to find


def test_p_deploy_matches_closed_form():
    R, fpr, pi = 0.75, 0.02, 0.04
    assert p_deploy(R, fpr, pi) == pytest.approx(
        (pi * R) / (pi * R + (1 - pi) * fpr))


# --------------------------- baseline-relative floor (user 2026-08-02, opt (b))

def test_lift_is_precision_relative_to_the_base_tagger():
    """The baseline for a veto head IS the prevalence: of everything the base
    tagger flags, a pi fraction is already right. lift = P_deploy / pi."""
    pi = 0.042
    assert deploy_lift(0.621, pi) == pytest.approx(0.621 / pi)
    assert deploy_lift(pi, pi) == pytest.approx(1.0)      # no improvement
    assert deploy_lift(None, pi) is None                   # fail-closed input
    assert deploy_lift(0.6, 0.0) is None                   # nothing to improve on


def test_lift_rises_as_recall_falls_so_the_recall_guard_is_load_bearing():
    """Measured on licking_testicle: lift climbs 8.8x -> 17.2x as the threshold
    rises and recall drops 0.90 -> 0.60. A lift criterion WITHOUT the recall
    floor therefore rewards a head for firing on almost nothing -- 'improve the
    tag' would degenerate into 'stop detecting it'. This test pins the shape of
    that failure so the guard is never dropped as redundant."""
    pi = 0.042
    # (recall, FPR) walking the threshold up
    points = [(0.902, 0.0620), (0.844, 0.0327), (0.749, 0.0200), (0.598, 0.0100)]
    lifts = [deploy_lift(p_deploy(R, fpr, pi), pi) for R, fpr in points]
    assert lifts == sorted(lifts), f"lift should rise as recall falls: {lifts}"
    # the highest-lift point is also the WORST recall -- exactly what the guard
    # exists to reject
    assert points[-1][0] < 0.7 <= points[-3][0]


def test_absolute_floor_expressed_as_a_lift_is_much_stricter():
    """At pi=0.042 the absolute min_p_deploy=0.85 is a 20.2x lift -- above the
    best operating point any measured arm reaches while holding R>=0.7 (14.8x).
    That equivalence is the whole reason the baseline-relative axis exists."""
    pi = 0.042
    assert deploy_lift(0.85, pi) == pytest.approx(20.238, abs=0.01)
    best_at_recall_floor = deploy_lift(p_deploy(0.749, 0.0200, pi), pi)
    assert best_at_recall_floor < deploy_lift(0.85, pi)
    assert 14.0 < best_at_recall_floor < 15.5


def test_lift_floor_is_a_governed_config_key():
    """min_p_deploy_lift must be a real config key, not a code constant: it has
    to be in DEFAULTS (set_config refuses unknown keys) and it must be readable
    from tagger_config like every other floor. Migration 0125 seeds the row."""
    from api.tagger_core.config import DEFAULTS
    assert "min_p_deploy_lift" in DEFAULTS
    assert "min_p_deploy" in DEFAULTS
    floor = load_floor(query=lambda sql: [("min_precision", "0.85"),
                                          ("min_recall", "0.7"),
                                          ("min_p_deploy", "0.85"),
                                          ("min_p_deploy_lift", "12.5")])
    assert floor["min_p_deploy_lift"] == 12.5   # DB wins over the code fallback


# ------------------------------------------------------ Step 8: floor from DB

def test_floor_read_from_db_rows_not_code():
    # values deliberately DIFFERENT from api/tagger_core/config.py DEFAULTS
    # (min_pred_pos_support: DEFAULTS says 100, live primary says 20) -- the
    # loader must return what the DB says, proving nothing is baked in.
    rows = [("min_precision", "0.99"), ("min_recall", "0.42"),
            ("min_pred_pos_support", "20"), ("not_a_floor_key", "7")]
    floor = load_floor(query=lambda sql: rows)
    assert floor["min_precision"] == 0.99
    assert floor["min_recall"] == 0.42
    assert floor["min_pred_pos_support"] == 20.0
    assert "not_a_floor_key" not in floor


def test_floor_empty_table_fails_closed():
    with pytest.raises(RuntimeError, match="refusing to fall back"):
        load_floor(query=lambda sql: [])


def test_floor_missing_core_key_fails_closed():
    with pytest.raises(RuntimeError):
        load_floor(query=lambda sql: [("min_precision", "0.85")])  # no min_recall


# ------------------------------------------------------- Step 8: sampler cap

def _old_sampler(ids, group_of, cap, seed):
    """The lt_round8 implementation, verbatim -- kept HERE as the executable
    record of bug 2: per-group quota with max(1, ...) keeps >= 1 member of
    every group, so singleton-group domination makes the cap a no-op."""
    if len(ids) <= cap:
        return sorted(ids)
    _rnd = random.Random(seed)
    _by_g = {}
    for _i in ids:
        _by_g.setdefault(group_of[_i], []).append(_i)
    _quota = cap / len(ids)
    _sampled = []
    for _g, _ids in _by_g.items():
        _k = max(1, round(len(_ids) * _quota))
        _sampled += _rnd.sample(_ids, min(_k, len(_ids)))
    return sorted(_sampled)


def _singleton_dominated(n=8527):
    ids = list(range(n))
    group_of = {i: -i for i in ids}  # all singletons (pool-free negatives)
    return ids, group_of


def test_old_sampler_was_a_noop_under_singletons():
    ids, group_of = _singleton_dominated()
    assert len(_old_sampler(ids, group_of, 1500, seed=7)) == 8527  # the bug


def test_sample_capped_honours_total_cap_under_singletons():
    ids, group_of = _singleton_dominated()
    out = sample_capped(ids, group_of, 1500, seed=7)
    assert len(out) == 1500  # the fix: 8527 -> 1500, not 8527 -> 8527
    assert set(out) <= set(ids)


def test_sample_capped_cap_is_hard_for_any_group_shape():
    rnd = random.Random(0)
    ids = list(range(3000))
    # mixed shape: a few big pools + many singletons
    group_of = {i: (i % 7 if i < 700 else -i) for i in ids}
    for cap in (1, 10, 1500, 2999):
        assert len(sample_capped(ids, group_of, cap, seed=1)) == cap
    assert sample_capped(ids, group_of, 5000, seed=1) == sorted(ids)  # under cap: all


def test_sample_capped_deterministic_and_seed_sensitive():
    ids, group_of = _singleton_dominated(4000)
    a = sample_capped(ids, group_of, 100, seed=1)
    b = sample_capped(ids, group_of, 100, seed=1)
    c = sample_capped(ids, group_of, 100, seed=2)
    assert a == b
    assert a != c


def test_sample_capped_preserves_group_diversity():
    # 10 groups x 100 members, cap 50 -> every group represented (round-robin
    # guarantees each group appears before any appears twice)
    ids = list(range(1000))
    group_of = {i: i % 10 for i in ids}
    out = sample_capped(ids, group_of, 50, seed=3)
    assert len({group_of[i] for i in out}) == 10


# --------------------------------------------------------- Step 8: grouping

def test_grouping_pool_rows_group_by_pool():
    rows = [(101, "negative", "human_correction", 55),
            (102, "negative", "human_correction", 55),
            (103, "positive", "human_correction", 66)]
    group_of, prov = build_grouping(rows, [])
    assert group_of[101] == group_of[102] == 55
    assert group_of[103] == 66
    assert prov[101] == "pool"


def test_grouping_pool_free_rows_are_singletons_not_minus_one():
    """bug 1: pool-free GT used to collapse into ONE group (-1), degenerating
    grouped CV. Each pool-free row must be its own -post_id singleton."""
    rows = [(201, "negative", "human_correction", None),
            (202, "negative", "human_correction", None)]
    group_of, prov = build_grouping(rows, [])
    assert group_of[201] == -201
    assert group_of[202] == -202
    assert group_of[201] != group_of[202]
    assert prov[201] == "pool_free"


def test_grouping_recovered_human_adds_are_singletons():
    group_of, prov = build_grouping([], [301, 302])
    assert group_of[301] == -301 and group_of[302] == -302
    assert prov[301] == "recovered_human_add"


# ------------------------------------------------ Step 6: idempotency / ledger

def _key(tag="anilingus", ev=42):
    return IdempotencyKey(tag=tag, lifecycle_event_id=ev)


def test_idempotency_key_roundtrip_and_order():
    k = _key()
    assert IdempotencyKey.from_str(k.as_str()) == k
    items = [_key("b", 1), _key("a", 2), _key("a", 1)]
    assert ordered_work_items(items) == [_key("a", 1), _key("a", 2), _key("b", 1)]


def test_should_noop_only_training_proceeds():
    assert not should_noop("training")
    for stage in (None, "candidate", "approved", "dismissed"):
        assert should_noop(stage), stage


def test_same_key_delivered_twice_measures_once(tmp_path):
    """INV-R11: the broker may redeliver the same dispatch; the measurement
    must run exactly once per (tag, lifecycle_event id)."""
    ledger = AttemptLedger(tmp_path / "ledger.json")
    calls = []
    work = lambda: calls.append(1)  # noqa: E731
    assert run_once(ledger, _key(), "training", work) == "ok"
    assert run_once(ledger, _key(), "training", work) == "noop-done"
    assert len(calls) == 1
    # a DIFFERENT lifecycle event for the same tag is new work
    assert run_once(ledger, _key(ev=43), "training", work) == "ok"
    assert len(calls) == 2


def test_stage_moved_on_is_noop_before_any_bookkeeping(tmp_path):
    ledger = AttemptLedger(tmp_path / "ledger.json")
    assert run_once(ledger, _key(), "approved", lambda: 1 / 0) == "noop-stage"
    assert ledger.attempts(_key()) == 0


def test_ledger_survives_reload(tmp_path):
    path = tmp_path / "ledger.json"
    ledger = AttemptLedger(path)
    ledger.record_attempt(_key(), "failed", detail="boom")
    ledger2 = AttemptLedger(path)  # fresh process
    assert ledger2.attempts(_key()) == 1
    assert not ledger2.is_done(_key())


def test_attempt_cap_dead_letters_and_stays_dead(tmp_path):
    ledger = AttemptLedger(tmp_path / "ledger.json", max_attempts=2)

    def boom():
        raise RuntimeError("measurement exploded")

    for _ in range(2):
        with pytest.raises(RuntimeError):
            run_once(ledger, _key(), "training", boom)
    assert ledger.is_dead(_key())
    assert _key().as_str() in ledger.dead_letters()
    # dead stays dead -- even a now-working work fn must not run
    calls = []
    assert run_once(ledger, _key(), "training", lambda: calls.append(1)) == "dead"
    assert calls == []


def test_ledger_atomic_write_leaves_no_tmp_and_valid_json(tmp_path):
    path = tmp_path / "ledger.json"
    ledger = AttemptLedger(path)
    for ev in range(5):
        ledger.record_attempt(_key(ev=ev), "ok")
        ledger.mark_done(_key(ev=ev))
    leftovers = [p for p in tmp_path.iterdir() if p.name != "ledger.json"]
    assert leftovers == [], f"tmp files left behind: {leftovers}"
    doc = json.loads(path.read_text())
    assert len(doc["items"]) == 5


def test_should_attempt_gate(tmp_path):
    ledger = AttemptLedger(tmp_path / "ledger.json", max_attempts=1)
    assert ledger.should_attempt(_key())
    ledger.record_attempt(_key(), "failed")
    assert not ledger.should_attempt(_key())  # cap reached
    k2 = _key(ev=99)
    ledger.mark_done(k2)
    assert not ledger.should_attempt(k2)  # done


# ── 測定不能を「エラー」ではなく「判定」として扱う (2026-08-03 実例) ──
def test_manifest_error_is_a_hold_not_a_retry_loop():
    """split の層別下限を割るのは「正例が足りない」ことの機械的な現れ。

    実例: deepthroat (正例 11) で外側 fold に pos=0 が出て ManifestError。
    これを error 扱いにすると runner は 5 分ごとに再測定を試み続け、しかも
    「なぜ止まっているか」が task にも registration にも残らない — INV-R3 の
    silent drop と同じ害になる。HOLD として閉じ、binding を添えること。
    """
    import inspect
    from tagger import vnext_runner
    src = inspect.getsource(vnext_runner.process_task)
    assert "except ManifestError" in src, "ManifestError を個別に捕まえること"
    head = src.split("except ManifestError")[1].split("except Exception")[0]
    assert "mark_done" in head, "再試行ループを避けるため ledger を閉じる"
    # 語彙は 4 クラスの定数から取る (リテラルを散らさない)。
    assert "HOLD" in head, "判定は保留 (hold) として記録する"
    assert '"HOLD"' not in head, "旧 2 値のリテラルを書かない — 定数を使う"
    assert "binding" in head, "どの floor 項が binding かを必ず添える (Step 14)"


def test_noop_outcomes_do_not_consume_the_per_tick_limit():
    """空振りが処理枠を食うと、判定済みタグが列頭にいる限り列が進まない。

    実測 2026-08-03: deepthroat が判定済みのまま queue に残り、runner は
    5 分ごとに「skipped」を出して打ち切り、後続 5 タグが 1 件も進まなかった。
    """
    import inspect
    from tagger import vnext_runner
    src = inspect.getsource(vnext_runner.tick)
    assert 'if outcome not in ("skipped", "noop")' in src, \
        "空振りは done を増やさないこと"


def test_already_judged_task_is_closed_not_left_in_queue():
    """判定済み key の task は complete して queue から外す (さもないと毎 tick 再来)。"""
    import inspect
    from tagger import vnext_runner
    src = inspect.getsource(vnext_runner.process_task)
    head = src.split("should_attempt")[1].split("# ── E0")[0]
    assert "/complete" in head, "判定済みでも task は閉じること"


def test_idempotency_key_uses_the_lifecycle_event_id_not_a_constant():
    """鍵がタグ名だけだと、GT が育っても二度と測定されない。

    2026-08-03 実測: 初版は lifecycle_event_id を 0 に固定しており、再 dispatch
    が全部「already-judged」で skip されていた。「GT が育つ → 再測定 → 良くなる」
    というループ自体が成立しなくなる。
    """
    import inspect
    from tagger import vnext_runner
    src = inspect.getsource(vnext_runner.process_task)
    assert "stage, event_id = current_stage(tag)" in src
    assert "lifecycle_event_id=event_id" in src
    tick_src = inspect.getsource(vnext_runner.tick)
    assert "lifecycle_event_id=0" in tick_src and "並び順を決めるためだけ" in tick_src, \
        "tick の鍵は並び順専用であることが読めること"


def test_unchanged_gt_skips_remeasurement_but_still_reports():
    """GT が動いていなければ測り直さない — ただし判定は必ず報告する。

    2026-08-03: 冪等キーを lifecycle イベント id にした結果「再測定できる」
    ようになったが、reopen 周期が緩んだこともあり **同じ GT で 1 時間おきに
    測り直す**状態になった (fellatio/dark_anus/chastity_cage)。結果は同一なので
    GPU/CPU を捨てるだけ。省略しても silent drop にしない (INV-R3) ため、
    前回の判定を result_payload に再掲する。
    """
    import inspect
    from tagger import vnext_runner
    src = inspect.getsource(vnext_runner.process_task)
    assert "prev_gt == cur_gt" in src, "GT の同一性で判断すること"
    assert "gt_stamp" in src, "測定結果の gt (ゲート内件数) と混同しないこと"
    # **"gt_unchanged" は判定 (disposition) ではなく省略の理由**。4 クラスの
    # 語彙に混ぜると「5 つ目のクラス」が生えたように読める。
    assert '"reason": "gt_unchanged"' in src, "省略したことを記録に残すこと"
    assert '"disposition": "gt_unchanged"' not in src, "理由を判定の欄に書かない"
    assert '"verdict": prev_disposition' in src, "判定は前回のものをそのまま名乗る"
    tail = src.split('"reason": "gt_unchanged"')[1][:500]
    assert '"verdict":' in tail, "前回判定を再掲すること"
    assert "prev_doc" in src.split("if prev_gt == cur_gt:")[2][:400], \
        "前回判定は測定 doc の classification から取る (旧 2 値の verdict_absolute ではない)"


def test_unreadable_gt_does_not_skip():
    """GT が読めないときに「変わっていない」と決めつけない (fail-open の禁止)。"""
    import inspect
    from tagger import vnext_runner
    src = inspect.getsource(vnext_runner.process_task)
    assert "cur_gt is not None" in src
