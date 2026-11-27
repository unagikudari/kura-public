"""The lifecycle view leaves out retired tag identities.

A tag merge keeps the source's row, without a name, when the tagger's append-only history refers
to it (api/routers/tags.py). That row is history, not live state: `tagger_lifecycle` (and
`tagger_tag_state` on top of it) showed it as a row with a NULL tag. The view is 0135's, with one
WHERE added; its columns are unchanged. The history itself is still read from the event tables by
tag_id.
"""
from alembic import op

revision = '0197'
down_revision = '0196'
branch_labels = None
depends_on = None
# publisher-only: a view; replicas carry no tagger lifecycle of their own to read through it
REPLICA_DDL: str | None = None

VIEW = """
        CREATE OR REPLACE VIEW tagger_lifecycle AS
        WITH latest_event AS (
            SELECT DISTINCT ON (tag_id) tag_id, tag, to_stage, actor_db_role,
                   occurred_at, reason
            FROM tagger_lifecycle_event
            ORDER BY tag_id, occurred_at DESC, id DESC
        ), latest_candidate AS (
            SELECT DISTINCT ON (tag_id) tag_id, id AS candidate_id,
                   status AS candidate_status, promoted_patch_id
            FROM tagger_head_candidate
            ORDER BY tag_id, added_at DESC, id DESC
        ), latest_patch AS (
            SELECT DISTINCT ON (tag_id) tag_id, id AS patch_id, version AS patch_version,
                   status AS patch_status, head_type, first_release_id,
                   created_at AS patch_created_at
            FROM tagger_patch
            ORDER BY tag_id, version DESC
        )
        SELECT public.tagger_tag_name_of(COALESCE(le.tag_id, lc.tag_id, lp.tag_id)) AS tag,
               le.to_stage AS lifecycle_stage,
               le.occurred_at AS lifecycle_updated_at,
               lc.candidate_id,
               lc.candidate_status,
               lp.patch_id,
               lp.patch_version,
               lp.patch_status,
               lp.head_type,
               lp.first_release_id,
               lp.first_release_id IS NOT NULL
                 AND lp.first_release_id = (SELECT release_id FROM tagger_release_active
                                            WHERE id = 1) AS on_active_release,
               CASE
                 WHEN lp.patch_status = 'approved' AND le.to_stage IS NULL
                   THEN 'patch approved but no lifecycle_event recorded -- possible bypass of governed path'
                 WHEN lp.patch_status = 'approved' AND NOT EXISTS (
                        SELECT 1 FROM tagger_lifecycle_event e
                        WHERE e.tag_id = lp.tag_id AND e.to_stage = 'approved')
                   THEN 'patch approved but no lifecycle_event ever reached approved -- possible bypass of governed path'
                 WHEN lp.patch_status = 'approved'
                      AND (SELECT rank FROM tagger_stage WHERE stage = le.to_stage)
                        < (SELECT rank FROM tagger_stage WHERE stage = 'approved')
                      AND le.occurred_at < lp.patch_created_at
                   THEN 'patch approved but latest lifecycle_event predates the approved patch -- stale ledger or bypass'
                 WHEN lc.candidate_status = 'promoted' AND lc.promoted_patch_id IS NULL
                   THEN 'candidate marked promoted but no promoted_patch_id link -- inconsistent'
                 ELSE NULL
               END AS consistency,
               -- **末尾に足す**。`CREATE OR REPLACE VIEW` は既存の出力列の
               -- 名前・順序の変更を許さない (実適用 5 回目:
               -- InvalidTableDefinitionError「ビューの列名を tag から tag_id に
               -- 変更できません」)。DROP VIEW は依存する tagger_tag_state ごと
               -- CASCADE が要るので採らない — 既存の読み手を 1 つも壊さずに
               -- 列を増やせる末尾追加が正しい。
               COALESCE(le.tag_id, lc.tag_id, lp.tag_id) AS tag_id
        FROM latest_event le
        FULL JOIN latest_candidate lc ON lc.tag_id = le.tag_id
        FULL JOIN latest_patch lp ON lp.tag_id = COALESCE(le.tag_id, lc.tag_id)
        -- a retired identity (the nameless source a tag merge leaves behind for its history) is not live state
        WHERE public.tagger_tag_name_of(COALESCE(le.tag_id, lc.tag_id, lp.tag_id)) IS NOT NULL
"""


def upgrade():
    op.execute(VIEW)


def downgrade():
    op.execute(VIEW.replace("WHERE public.tagger_tag_name_of(COALESCE(le.tag_id, lc.tag_id, lp.tag_id)) IS NOT NULL", ""))
