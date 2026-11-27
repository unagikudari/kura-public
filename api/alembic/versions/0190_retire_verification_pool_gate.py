"""Retire the verification waiting queue without changing historical pools or GT.

Apply with ops/install/database.py upgrade (owner-only function replacement).
The explicit tagger_auto_exclude_pool preference remains enforced.
"""
from alembic import op
from api.alembic.owner import as_owner

revision = "0190"
down_revision = "0189"
branch_labels = None
depends_on = None
REPLICA_DDL = None

CLAIM_SQL = r"""CREATE OR REPLACE FUNCTION public.tagger_governed_claim_auto_tag(p_limit integer, p_lease_seconds integer)
 RETURNS TABLE(id integer, tagging_attempts integer, tagging_locked_until timestamp with time zone, mime_type character varying, post_type character varying)
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO 'pg_catalog', 'public'
AS $function$
        BEGIN
            IF session_user NOT IN (SELECT db_role FROM public.tagger_allowed_executor WHERE scope = 'tagging-pipeline') THEN
                RAISE EXCEPTION 'tagger_governed_claim_auto_tag: session_user % is not an allowed executor for scope tagging-pipeline',
                    session_user;
            END IF;
            -- Stage 3 (docs/notes/tagging-work-table-plan.md): the queue is public.post_tagging_work.
            -- Legacy verification status no longer gates tagging; explicit auto-exclusion still does.
            -- Only the work
            -- row is locked and written, so a claim no longer rewrites the post row.
            RETURN QUERY
            WITH picked AS (
                SELECT w.post_id AS pid
                FROM public.post_tagging_work w
                JOIN public.post p ON p.id = w.post_id
                WHERE w.status = 'pending'
                  AND (w.lease_until IS NULL OR w.lease_until < now())
                  AND p."type" IN ('image', 'animation', 'video')
                  AND p.status = 'ok'
                  AND NOT EXISTS (
                      SELECT 1 FROM public.pool_post pp
                      JOIN public.tagger_auto_exclude_pool ex ON ex.pool_id = pp.pool_id
                      WHERE pp.post_id = p.id AND ex.excluded
                  )
                ORDER BY w.lease_until NULLS FIRST, w.post_id
                LIMIT p_limit
                FOR UPDATE OF w SKIP LOCKED
            )
            UPDATE public.post_tagging_work w
            SET lease_until = now() + make_interval(secs => p_lease_seconds)
            FROM picked, public.post p
            WHERE w.post_id = picked.pid AND p.id = w.post_id
            RETURNING w.post_id, w.attempts, w.lease_until, p."mime-type", p."type";
        END;
        $function$
"""

PREVIOUS_SQL = r"""CREATE OR REPLACE FUNCTION public.tagger_governed_claim_auto_tag(p_limit integer, p_lease_seconds integer)
 RETURNS TABLE(id integer, tagging_attempts integer, tagging_locked_until timestamp with time zone, mime_type character varying, post_type character varying)
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO 'pg_catalog', 'public'
AS $function$
        BEGIN
            IF session_user NOT IN (SELECT db_role FROM public.tagger_allowed_executor WHERE scope = 'tagging-pipeline') THEN
                RAISE EXCEPTION 'tagger_governed_claim_auto_tag: session_user % is not an allowed executor for scope tagging-pipeline',
                    session_user;
            END IF;
            -- Stage 3 (docs/notes/tagging-work-table-plan.md): the queue is public.post_tagging_work.
            -- The eligibility conditions on post and the pools are unchanged; only the work
            -- row is locked and written, so a claim no longer rewrites the post row.
            RETURN QUERY
            WITH picked AS (
                SELECT w.post_id AS pid
                FROM public.post_tagging_work w
                JOIN public.post p ON p.id = w.post_id
                WHERE w.status = 'pending'
                  AND (w.lease_until IS NULL OR w.lease_until < now())
                  AND p."type" IN ('image', 'animation', 'video')
                  AND p.status = 'ok'
                  AND NOT EXISTS (
                      SELECT 1 FROM public.pool_post pp
                      JOIN public.pool po ON po.id = pp.pool_id
                      WHERE pp.post_id = p.id
                        AND po.tagger_source IS NOT NULL
                        AND po.verify_status = 'pending'
                  )
                  AND NOT EXISTS (
                      SELECT 1 FROM public.pool_post pp
                      JOIN public.tagger_auto_exclude_pool ex ON ex.pool_id = pp.pool_id
                      WHERE pp.post_id = p.id AND ex.excluded
                  )
                ORDER BY w.lease_until NULLS FIRST, w.post_id
                LIMIT p_limit
                FOR UPDATE OF w SKIP LOCKED
            )
            UPDATE public.post_tagging_work w
            SET lease_until = now() + make_interval(secs => p_lease_seconds)
            FROM picked, public.post p
            WHERE w.post_id = picked.pid AND p.id = w.post_id
            RETURNING w.post_id, w.attempts, w.lease_until, p."mime-type", p."type";
        END;
        $function$
"""

GT_SQL = (
r"""CREATE OR REPLACE FUNCTION public.tagger_governed_sync_neg_gt_from_pins()
 RETURNS integer
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO 'pg_catalog', 'public'
AS $function$
        DECLARE v_count integer;
        BEGIN
            IF session_user NOT IN (SELECT db_role FROM public.tagger_allowed_executor
                                    WHERE scope = 'gt-sync') THEN
                RAISE EXCEPTION 'tagger_governed_sync_neg_gt_from_pins: session_user % is not an allowed executor for scope gt-sync',
                    session_user;
            END IF;
            INSERT INTO public.tagger_gt_label
                (tag, tag_id, post_id, verdict, source_pool_id, derived_from, verified_at)
            SELECT tn.name, ptp.tag_id, ptp.post_id, 'negative', NULL, 'human_correction', now()
            FROM public.post_tag_pin ptp
            JOIN public.tag_name tn ON tn.tag_id = ptp.tag_id AND tn.ord = 0
            LEFT JOIN public.post_tag pt ON pt.post_id = ptp.post_id AND pt.tag_id = ptp.tag_id
            JOIN public.post po ON po.id = ptp.post_id AND po.status = 'ok'
            -- 事前判定 (0163): 既に negative/human_correction になっている（出典poolは保持）
            -- 行は INSERT まで運ばない。ON CONFLICT の衝突判定は 73,803 行ぶん
            -- 一意 index を叩くので、DO UPDATE の WHERE だけでは 688ms 残る。
            LEFT JOIN public.tagger_gt_label g
                   ON g.tag_id = ptp.tag_id AND g.post_id = ptp.post_id
            WHERE ptp.state = 'absent'
              AND NOT COALESCE(pt.locked, false)
              AND (g.tag_id IS NULL
                   OR g.verdict        IS DISTINCT FROM 'negative'
                   OR g.derived_from   IS DISTINCT FROM 'human_correction')
            -- Unconditional in VERDICT: an absent pin is the human's LATEST
            -- explicit decision for this (tag, post) -- it always wins as
            -- negative. But a row that ALREADY says so needs no write: the
            -- unconditional form rewrote all 73,803 rows every 14 minutes
            -- (0163). The WHERE below does not weaken the rule, it only
            -- skips writes that would not change the row.
            ON CONFLICT (tag_id, post_id) DO UPDATE SET
                verdict = 'negative', source_pool_id = COALESCE(tagger_gt_label.source_pool_id, EXCLUDED.source_pool_id),
                derived_from = 'human_correction', verified_at = EXCLUDED.verified_at
            WHERE tagger_gt_label.verdict        IS DISTINCT FROM EXCLUDED.verdict
               OR tagger_gt_label.derived_from   IS DISTINCT FROM EXCLUDED.derived_from;
            GET DIAGNOSTICS v_count = ROW_COUNT;
            RETURN v_count;
        END;
        $function$
""",
r"""CREATE OR REPLACE FUNCTION public.tagger_governed_sync_pos_gt_from_pins()
 RETURNS integer
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO 'pg_catalog', 'public'
AS $function$
        DECLARE v_count integer;
        BEGIN
            IF session_user NOT IN (SELECT db_role FROM public.tagger_allowed_executor
                                    WHERE scope = 'gt-sync') THEN
                RAISE EXCEPTION 'tagger_governed_sync_pos_gt_from_pins: session_user % is not an allowed executor for scope gt-sync',
                    session_user;
            END IF;
            INSERT INTO public.tagger_gt_label
                (tag, tag_id, post_id, verdict, source_pool_id, derived_from, verified_at)
            SELECT tn.name, ptp.tag_id, ptp.post_id, 'positive', NULL, 'human_correction', now()
            FROM public.post_tag_pin ptp
            JOIN public.tag_name tn ON tn.tag_id = ptp.tag_id AND tn.ord = 0
            -- INNER: a positive assertion only makes sense while the tag is
            -- actually on the post (pin flips to absent when a human removes
            -- it, so a divergence here means a machine bypass -- emit nothing).
            JOIN public.post_tag pt ON pt.post_id = ptp.post_id AND pt.tag_id = ptp.tag_id
            JOIN public.post po ON po.id = ptp.post_id AND po.status = 'ok'
            -- 事前判定 (0163) — neg 側と同じ理由。
            LEFT JOIN public.tagger_gt_label g
                   ON g.tag_id = ptp.tag_id AND g.post_id = ptp.post_id
            WHERE ptp.state = 'present'
              AND NOT COALESCE(pt.locked, false)
              AND (g.tag_id IS NULL
                   OR g.verdict        IS DISTINCT FROM 'positive'
                   OR g.derived_from   IS DISTINCT FROM 'human_correction')
            ON CONFLICT (tag_id, post_id) DO UPDATE SET
                verdict = 'positive', source_pool_id = COALESCE(tagger_gt_label.source_pool_id, EXCLUDED.source_pool_id),
                derived_from = 'human_correction', verified_at = EXCLUDED.verified_at
            WHERE tagger_gt_label.verdict        IS DISTINCT FROM EXCLUDED.verdict
               OR tagger_gt_label.derived_from   IS DISTINCT FROM EXCLUDED.derived_from;
            GET DIAGNOSTICS v_count = ROW_COUNT;
            RETURN v_count;
        END;
        $function$
""",
)

PREVIOUS_GT_SQL = (
r"""CREATE OR REPLACE FUNCTION public.tagger_governed_sync_neg_gt_from_pins()
 RETURNS integer
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO 'pg_catalog', 'public'
AS $function$
        DECLARE v_count integer;
        BEGIN
            IF session_user NOT IN (SELECT db_role FROM public.tagger_allowed_executor
                                    WHERE scope = 'gt-sync') THEN
                RAISE EXCEPTION 'tagger_governed_sync_neg_gt_from_pins: session_user % is not an allowed executor for scope gt-sync',
                    session_user;
            END IF;
            INSERT INTO public.tagger_gt_label
                (tag, tag_id, post_id, verdict, source_pool_id, derived_from, verified_at)
            SELECT tn.name, ptp.tag_id, ptp.post_id, 'negative', NULL, 'human_correction', now()
            FROM public.post_tag_pin ptp
            JOIN public.tag_name tn ON tn.tag_id = ptp.tag_id AND tn.ord = 0
            LEFT JOIN public.post_tag pt ON pt.post_id = ptp.post_id AND pt.tag_id = ptp.tag_id
            JOIN public.post po ON po.id = ptp.post_id AND po.status = 'ok'
            -- 事前判定 (0163): 既に negative/human_correction/NULL になっている
            -- 行は INSERT まで運ばない。ON CONFLICT の衝突判定は 73,803 行ぶん
            -- 一意 index を叩くので、DO UPDATE の WHERE だけでは 688ms 残る。
            LEFT JOIN public.tagger_gt_label g
                   ON g.tag_id = ptp.tag_id AND g.post_id = ptp.post_id
            WHERE ptp.state = 'absent'
              AND NOT COALESCE(pt.locked, false)
              AND (g.tag_id IS NULL
                   OR g.verdict        IS DISTINCT FROM 'negative'
                   OR g.derived_from   IS DISTINCT FROM 'human_correction'
                   OR g.source_pool_id IS NOT NULL)
            -- Unconditional in VERDICT: an absent pin is the human's LATEST
            -- explicit decision for this (tag, post) -- it always wins as
            -- negative. But a row that ALREADY says so needs no write: the
            -- unconditional form rewrote all 73,803 rows every 14 minutes
            -- (0163). The WHERE below does not weaken the rule, it only
            -- skips writes that would not change the row.
            ON CONFLICT (tag_id, post_id) DO UPDATE SET
                verdict = 'negative', source_pool_id = EXCLUDED.source_pool_id,
                derived_from = 'human_correction', verified_at = EXCLUDED.verified_at
            WHERE tagger_gt_label.verdict        IS DISTINCT FROM EXCLUDED.verdict
               OR tagger_gt_label.derived_from   IS DISTINCT FROM EXCLUDED.derived_from
               OR tagger_gt_label.source_pool_id IS DISTINCT FROM EXCLUDED.source_pool_id;
            GET DIAGNOSTICS v_count = ROW_COUNT;
            RETURN v_count;
        END;
        $function$
""",
r"""CREATE OR REPLACE FUNCTION public.tagger_governed_sync_pos_gt_from_pins()
 RETURNS integer
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO 'pg_catalog', 'public'
AS $function$
        DECLARE v_count integer;
        BEGIN
            IF session_user NOT IN (SELECT db_role FROM public.tagger_allowed_executor
                                    WHERE scope = 'gt-sync') THEN
                RAISE EXCEPTION 'tagger_governed_sync_pos_gt_from_pins: session_user % is not an allowed executor for scope gt-sync',
                    session_user;
            END IF;
            INSERT INTO public.tagger_gt_label
                (tag, tag_id, post_id, verdict, source_pool_id, derived_from, verified_at)
            SELECT tn.name, ptp.tag_id, ptp.post_id, 'positive', NULL, 'human_correction', now()
            FROM public.post_tag_pin ptp
            JOIN public.tag_name tn ON tn.tag_id = ptp.tag_id AND tn.ord = 0
            -- INNER: a positive assertion only makes sense while the tag is
            -- actually on the post (pin flips to absent when a human removes
            -- it, so a divergence here means a machine bypass -- emit nothing).
            JOIN public.post_tag pt ON pt.post_id = ptp.post_id AND pt.tag_id = ptp.tag_id
            JOIN public.post po ON po.id = ptp.post_id AND po.status = 'ok'
            -- 事前判定 (0163) — neg 側と同じ理由。
            LEFT JOIN public.tagger_gt_label g
                   ON g.tag_id = ptp.tag_id AND g.post_id = ptp.post_id
            WHERE ptp.state = 'present'
              AND NOT COALESCE(pt.locked, false)
              AND (g.tag_id IS NULL
                   OR g.verdict        IS DISTINCT FROM 'positive'
                   OR g.derived_from   IS DISTINCT FROM 'human_correction'
                   OR g.source_pool_id IS NOT NULL)
            ON CONFLICT (tag_id, post_id) DO UPDATE SET
                verdict = 'positive', source_pool_id = EXCLUDED.source_pool_id,
                derived_from = 'human_correction', verified_at = EXCLUDED.verified_at
            WHERE tagger_gt_label.verdict        IS DISTINCT FROM EXCLUDED.verdict
               OR tagger_gt_label.derived_from   IS DISTINCT FROM EXCLUDED.derived_from
               OR tagger_gt_label.source_pool_id IS DISTINCT FROM EXCLUDED.source_pool_id;
            GET DIAGNOSTICS v_count = ROW_COUNT;
            RETURN v_count;
        END;
        $function$
""",
)

def upgrade():
    as_owner(op, "kura_reconciler", CLAIM_SQL)
    for sql in GT_SQL:
        as_owner(op, "kura_reconciler", sql)

def downgrade():
    as_owner(op, "kura_reconciler", PREVIOUS_SQL)
    for sql in PREVIOUS_GT_SQL:
        as_owner(op, "kura_reconciler", sql)
