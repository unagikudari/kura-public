# Description: kura の LLM 向け read-only MCP サーバ — タガーライフサイクル全状態を要約済み構造化データで公開
# Kind: server
# Effects: read-only
# Requires: mcp (tagger/requirements.txt には未反映、api-venv に別途 pip install 済み)
"""kura MCP server — plan:kura-tagger-lifecycle-redesign Step 20.

user指示「kuraMCPを確認すればタガーを含めやったことが全てLLMフレンドリーに
理解できるようにしろ」への対応。他のLLMエージェントがHTMLダッシュボードを
スクレイピングすることなく、構造化JSONでkuraの現在状態(特にタガーライフ
サイクル)を把握できるようにする。

read-only 限定(MISSION要求): 生の5テーブル行や書き込みツールは一切公開しない
— 0078の統一ビュー(tagger_lifecycle / tagger_fleet)を経由した要約済み
canonical view のみ。書き込みは全てprimary canonical経由のAPI/governed function
であり、MCP はそれを迂回する経路にしない。

iPhone経由のbroker MCPとは別サーバ(kura固有、mount先も別)。
"""
from __future__ import annotations

import json

from mcp.server.fastmcp import FastMCP
from sqlalchemy import text

from api.db.session import async_session
from api.services import tagging_queue

mcp = FastMCP("kura", streamable_http_path="/", stateless_http=True, json_response=True)


async def _fetch_all(query: str, **params) -> list[dict]:
    async with async_session() as db:
        rows = (await db.execute(text(query), params)).mappings().all()
        return [dict(r) for r in rows]


def _json_default(o):
    # datetimes -> isoformat, everything else -> str fallback (read-only
    # tool output, never round-tripped, so lossy stringification is fine).
    if hasattr(o, "isoformat"):
        return o.isoformat()
    return str(o)


def _dumps(obj) -> str:
    return json.dumps(obj, default=_json_default, ensure_ascii=False)


@mcp.tool()
async def tagger_lifecycle(tag: str | None = None) -> str:
    """タガーの概念/head 単位のライフサイクル状態を返す(candidate→...→deployed)。

    tag を指定すればその1件、省略すれば consistency に懸念があるものを優先し
    最大50件。生のtagger_patch/tagger_head_candidate行ではなく、0078の
    統一ビュー(tagger_lifecycle)を経由した要約のみを返す。
    """
    if tag:
        rows = await _fetch_all("SELECT * FROM tagger_lifecycle WHERE tag = :tag", tag=tag)
    else:
        rows = await _fetch_all(
            "SELECT * FROM tagger_lifecycle ORDER BY (consistency IS NOT NULL) DESC, "
            "lifecycle_updated_at DESC NULLS LAST LIMIT 50"
        )
    return _dumps({"tags": rows, "count": len(rows)})


@mcp.tool()
async def tagger_fleet() -> str:
    """タガーworkerフリートの状態(期待profile vs 実際の稼働状態)を返す。

    0078の統一ビュー(tagger_fleet)経由。degraded_reasonが非NULLのノードは
    要注意(例: full profileノードがCPU providerを報告=GPU退行の疑い)。
    """
    rows = await _fetch_all("SELECT * FROM tagger_fleet ORDER BY host")
    return _dumps({"nodes": rows, "count": len(rows)})


@mcp.tool()
async def tagger_releases(limit: int = 10) -> str:
    """直近のtagger_releaseとactive pointerの状態を返す(モデル本体のBLOB列は含まない)。"""
    releases = await _fetch_all(
        "SELECT id, commit_sha, base_model_repo, patch_set_digest, n_out, "
        "signed_digest, created_at, created_by FROM tagger_release "
        "ORDER BY id DESC LIMIT :limit", limit=limit
    )
    active = await _fetch_all(
        "SELECT release_id, generation, updated_at, updated_by FROM tagger_release_active WHERE id = 1"
    )
    return _dumps({"releases": releases, "active": active[0] if active else None})


@mcp.tool()
async def kura_whats_happening() -> str:
    """kura全体の「今何が起きているか」を一望できるサマリを返す(タガーライフ
    サイクル + フリート状態 + 検証待ちpool + head candidate + queue状況)を
    1回の呼び出しで要約する — 複数ツールを個別に叩かなくて済むエントリポイント。
    """
    lifecycle_issues = await _fetch_all(
        "SELECT tag, lifecycle_stage, consistency FROM tagger_lifecycle "
        "WHERE consistency IS NOT NULL LIMIT 20"
    )
    fleet = await _fetch_all(
        "SELECT host, liveness, degraded_reason FROM tagger_fleet "
        "WHERE liveness != 'active' OR degraded_reason IS NOT NULL"
    )
    pending_pools = await _fetch_all(
        "SELECT count(*) AS n FROM pool WHERE verify_status = 'pending'"
    )
    open_candidates = await _fetch_all(
        "SELECT tag, reason, added_at FROM tagger_head_candidate "
        "WHERE status = 'candidate' ORDER BY added_at DESC LIMIT 10"
    )
    async with async_session() as db:
        queue = await tagging_queue.by_status(db)
    reconciler = await _fetch_all(
        "SELECT paused, halt_reason, updated_at FROM tagger_reconciler_state WHERE id = true"
    )
    return _dumps({
        "lifecycle_consistency_issues": lifecycle_issues,
        "fleet_attention_needed": fleet,
        "pending_verify_pools": pending_pools[0]["n"] if pending_pools else 0,
        "open_head_candidates": open_candidates,
        "tagging_queue": queue,
        "reconciler": reconciler[0] if reconciler else None,
    })
