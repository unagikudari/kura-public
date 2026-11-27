"""Read-only lifecycle health for the tagger dashboard (owner ruling 2026-11-23:
alerts live on the kura dashboard, nothing is sent elsewhere).

It answers the questions an operator would otherwise ask an agent:
when did the loop last publish, does every expected worker actually run each
published head (and if not, why), what is each pending candidate waiting on,
and why did recent candidates stop.
"""
from sqlalchemy import text

HEARTBEAT_WINDOW_MINUTES = 5


def worker_reason(worker):
    """Why a worker has not confirmed a published head. None when confirmed."""
    if worker["confirmed"]:
        return None
    if worker["reported_at"] is None or not worker["fresh"]:
        return "heartbeat_missing"
    if worker["expected_provider"] and worker["provider"] != worker["expected_provider"]:
        return "provider_mismatch"
    if worker["release_id"] != worker["active_release_id"]:
        return "release_mismatch"
    return "not_executed_since_publication"


REASON_TEXT = {
    "heartbeat_missing": "ワーカーの報告が {window} 分以上ありません（停止中か不調）",
    "provider_mismatch": "期待する実行環境 {expected} に対して {provider} で動いています",
    "release_mismatch": "ワーカーが別の release {release} で動いています",
    "not_executed_since_publication": ("公開後、現在の受領・閾値・適用方法でこの head を実行した記録がありません"
                                       "（未実行か、ワーカー再起動後の受領がまだ）"),
}


async def published_head_health(db):
    from api.services.execution_receipts import published_candidate_coverage

    coverage = await published_candidate_coverage(db)
    if not coverage:
        return []
    state = (await db.execute(text("""
        SELECT e.host, e.profile, e.expected_provider,
               h.provider, h.release_id, h.reported_at,
               coalesce(h.reported_at >= clock_timestamp() - make_interval(mins => :window), false) AS fresh,
               (SELECT release_id FROM tagger_release_active WHERE id = 1) AS active_release_id
        FROM tagger_expected_worker e
        LEFT JOIN LATERAL (
            SELECT provider, release_id, reported_at FROM tagger_worker_heartbeat hb
            WHERE hb.host = e.host AND hb.profile = e.profile
            ORDER BY reported_at DESC LIMIT 1
        ) h ON true
        WHERE NOT e.opportunistic
    """), {"window": HEARTBEAT_WINDOW_MINUTES})).mappings().all()
    by_worker = {(row["host"], row["profile"]): dict(row) for row in state}
    heads = []
    for tag, result in sorted(coverage.items()):
        workers = []
        for worker in result["workers"]:
            row = by_worker.get((worker["host"], worker["profile"]), {})
            merged = {**row, **worker}
            merged.setdefault("reported_at", None)
            merged.setdefault("fresh", False)
            workers.append({"host": worker["host"], "profile": worker["profile"],
                            "confirmed": worker["confirmed"], "reason": worker_reason(merged),
                            "expected_provider": merged.get("expected_provider"),
                            "provider": merged.get("provider"),
                            "release_id": merged.get("release_id"),
                            "reported_at": merged.get("reported_at")})
        heads.append({"tag": tag, "complete": result["complete"], "workers": workers})
    return heads


PENDING_LIMIT = 50


async def pending_candidates(db):
    """One query for every pending candidate's latest recorded evaluation; the
    worker-trial count is computed only where the floors were met."""
    from api.services.qualification_evidence import runtime_report_coverage

    rows = (await db.execute(text("""
        SELECT r.id, r.tag, r.requested_at, r.metrics->'production_review'->>'scope' AS scope,
               (SELECT convert_from(ce.document, 'UTF8')::jsonb->'report'->>'state'
                FROM tagger_candidate_evaluation ce
                WHERE ce.registration_id = r.id
                ORDER BY ce.recorded_at DESC LIMIT 1) AS evaluation,
               count(*) OVER () AS total
        FROM tagger_registration_request r
        WHERE r.status = 'pending'
          AND r.id = (SELECT newer.id FROM tagger_registration_request newer
                      WHERE newer.tag_id = r.tag_id ORDER BY requested_at DESC, id DESC LIMIT 1)
        ORDER BY r.requested_at
        LIMIT :limit
    """), {"limit": PENDING_LIMIT})).mappings().all()
    result = []
    for row in rows:
        entry = {key: row[key] for key in ("id", "tag", "requested_at", "scope", "evaluation")}
        if row["evaluation"] == "point_floors_met_review_required":
            try:
                coverage = await runtime_report_coverage(db, row["id"])
                entry["workers_passed"] = sum(w["state"] == "reported_pass" for w in coverage["workers"])
                entry["workers_expected"] = len(coverage["workers"])
            except (ValueError, KeyError, TypeError):
                entry["evaluation"] = "unreadable"
        entry["waiting_on"] = waiting_on(entry)
        result.append(entry)
    total = rows[0]["total"] if rows else 0
    return {"candidates": result, "hidden": max(0, total - len(result))}


def waiting_on(entry):
    """The next step a pending candidate needs, from its latest recorded evaluation."""
    scope = entry.get("scope")
    if scope is None:
        return "legacy_without_production_review"
    if scope != "catalog_sample_outside_frozen_gt_groups":
        return "publication_scope_ineligible"
    evaluation = entry.get("evaluation")
    if evaluation is None:
        return "no_evaluation_recorded"
    if evaluation == "unreadable":
        return "evidence_unreadable"
    if evaluation == "awaiting_human_judgments":
        return "human_review_answers"
    if evaluation == "measured_floor_not_met":
        return "quality_floor_not_met"
    if evaluation == "insufficient_observed_support":
        return "more_review_support"
    if evaluation != "point_floors_met_review_required":
        return "evaluation_unresolved"
    if entry.get("workers_passed", 0) < entry.get("workers_expected", 0):
        return "worker_trials"
    return "verification_and_publication"


WAITING_TEXT = {
    "legacy_without_production_review": "旧形式の登録（本番審査の範囲がない）",
    "publication_scope_ineligible": "評価専用の範囲なので公開対象外",
    "no_evaluation_recorded": "評価の記録がまだない（審査キュー未作成の可能性）",
    "evidence_unreadable": "評価や試験の記録が読めない（要調査）",
    "human_review_answers": "人の審査の回答待ち",
    "quality_floor_not_met": "精度の基準に届いていない",
    "more_review_support": "審査件数が足りない",
    "evaluation_unresolved": "評価が未確定",
    "worker_trials": "ワーカー試験待ち",
    "verification_and_publication": "検証と公開の手前（次の tick で進むはず）",
}


async def recent_stops(db, days=7):
    rows = (await db.execute(text("""
        SELECT coalesce(review_class, '-') AS review_class,
               split_part(coalesce(rejected_reason, ''), ' gt(', 1) AS reason,
               count(*) AS n, count(DISTINCT tag) AS tags
        FROM tagger_registration_request
        WHERE status = 'rejected' AND requested_at >= now() - make_interval(days => :days)
        GROUP BY 1, 2 ORDER BY 3 DESC LIMIT 12
    """), {"days": days})).mappings().all()
    return [dict(row) for row in rows]


async def last_publication(db):
    row = (await db.execute(text("""
        SELECT max(created_at) AS at, count(*) FILTER (WHERE created_at >= now() - interval '30 days') AS last_30d
        FROM tagger_patch WHERE status = 'approved'
    """))).mappings().one()
    return dict(row)


async def snapshot(db, *, timeout_ms=3000):
    """Bounded: each statement here may take at most ``timeout_ms``; the caller's
    statement_timeout is restored afterwards (SET LOCAL would otherwise outlive
    a released savepoint and constrain the rest of the dashboard)."""
    previous = (await db.execute(text("SELECT current_setting('statement_timeout')"))).scalar_one()
    await db.execute(text("SELECT set_config('statement_timeout', :v, true)"), {"v": f"{int(timeout_ms)}ms"})
    try:
        return {"last_publication": await last_publication(db),
                "published_heads": await published_head_health(db),
                "pending": await pending_candidates(db),
                "recent_stops": await recent_stops(db)}
    finally:
        try:
            await db.execute(text("SELECT set_config('statement_timeout', :v, true)"), {"v": previous})
        except Exception:  # noqa: BLE001 - aborted transaction; the savepoint rollback restores it
            pass


def render(report, e):
    """HTML panel for the dashboard iframe; ``e`` is the caller's escape function."""
    last = report["last_publication"]
    rows = []
    for head in report["published_heads"]:
        for worker in head["workers"]:
            if worker["reason"] is None:
                note, cls = "本番で実行を確認", "perf-ok"
            else:
                note = REASON_TEXT[worker["reason"]].format(
                    window=HEARTBEAT_WINDOW_MINUTES, expected=worker["expected_provider"],
                    provider=worker["provider"], release=worker["release_id"])
                cls = "perf-warn"
            rows.append(f"<tr><td>{e(head['tag'])}<td>{e(worker['host'])}/{e(worker['profile'])}"
                        f"<td class={cls}>{e(note)}</tr>")
    pending = "".join(
        f"<tr><td>{e(p['tag'])}<td>#{e(p['id'])}<td>{e(WAITING_TEXT.get(p['waiting_on'], p['waiting_on']))}"
        f"<td>{e(p.get('workers_passed', '-'))}/{e(p.get('workers_expected', '-'))}"
        f"<td class=mut>{e(p.get('evaluation'))}</tr>" for p in report["pending"]["candidates"])
    if report["pending"]["hidden"]:
        pending += f"<tr><td colspan=5 class=mut>ほかに {e(report['pending']['hidden'])} 件（古い順に {PENDING_LIMIT} 件まで表示）</tr>"
    stops = "".join(
        f"<tr><td>{e(s['review_class'])}<td>{e(s['reason'])}<td>{e(s['n'])}<td>{e(s['tags'])}</tr>"
        for s in report["recent_stops"])
    unhealthy = sum(1 for head in report["published_heads"] if not head["complete"])
    headline = ("すべての公開 head が全ワーカーで実行を確認済み" if not unhealthy
                else f"{unhealthy} 個の公開 head で、実行を確認できていないワーカーがあります")
    return f"""<div class=panel data-tagger-category=overview><h1>ライフサイクルの健康状態</h1>
<p>最後の自動公開: <b>{e(last['at'] or 'なし')}</b>（直近 30 日の公開 {e(last['last_30d'])} 件） · <span class={'perf-ok' if not unhealthy else 'perf-warn'}>{e(headline)}</span></p>
<details open><summary>公開した head の本番実行</summary><div class=wrap><table><tr><th>tag<th>worker<th>状態</tr>{''.join(rows) or '<tr><td colspan=3 class=mut>審査を経て公開した head はまだありません</tr>'}</table></div></details>
<details open><summary>保留中の候補が待っているもの</summary><div class=wrap><table><tr><th>tag<th>登録<th>待っているもの<th>試験合格<th>最新の評価記録</tr>{pending or '<tr><td colspan=5 class=mut>保留中の候補はありません</tr>'}</table></div></details>
<details><summary>直近 7 日に止まった候補の理由</summary><div class=wrap><table><tr><th>分類<th>理由<th>件数<th>タグ数</tr>{stops or '<tr><td colspan=4 class=mut>なし</tr>'}</table></div></details>
</div>"""
