"""A tag merge carries the governed tagger rows through a governed function.

`POST /api/tag/{source}/merge-into/{target}` switched to `kura_reconciler` with SET LOCAL ROLE to
touch `tagger_gt_label` and `tagger_head_candidate` (their row triggers accept only that role).
`kura` is not, and is never made, a member of that role (api/alembic/owner.py), so on an installed
database every merge failed with "permission denied to set role" (seen 2026-11-15 merging
dog_penis into knotted_penis). The two statements move into `tagger_governed_merge_tag`, owned by
`kura_reconciler` like every other governed writer; the API calls it.

Needs the privileged upgrade (ops/install/database.py upgrade): the function is created as its owner.
"""
from alembic import op

from api.alembic.owner import as_owner

revision = '0196'
down_revision = '0195'
branch_labels = None
depends_on = None
# publisher-only: a function, no replicated table shape; merges are made on the canonical database only
REPLICA_DDL: str | None = None

FUNCTION = """
CREATE OR REPLACE FUNCTION tagger_governed_merge_tag(p_source integer, p_target integer)
RETURNS void
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $$
BEGIN
    IF session_user NOT IN (SELECT db_role FROM public.tagger_allowed_executor WHERE scope = 'gt-sync') THEN
        RAISE EXCEPTION 'tagger_governed_merge_tag: session_user % is not an allowed executor for scope gt-sync',
            session_user;
    END IF;
    IF p_source = p_target THEN
        RAISE EXCEPTION 'tagger_governed_merge_tag: source and target are the same tag';
    END IF;
    -- derived ground truth of the source is dropped: it is re-derived from the pins, which the merge moves
    DELETE FROM public.tagger_gt_label WHERE tag_id = p_source;
    -- head candidates keep their history under the target's canonical name; a duplicate is dropped
    UPDATE public.tagger_head_candidate c SET tag_id = p_target,
           tag = (SELECT name FROM public.tag_name WHERE tag_id = p_target AND ord = 0)
    WHERE c.tag_id = p_source AND NOT EXISTS (
        SELECT 1 FROM public.tagger_head_candidate x
        WHERE x.tag_id = p_target
          AND x.source_pool_id IS NOT DISTINCT FROM c.source_pool_id
          AND x.id <> c.id);
    DELETE FROM public.tagger_head_candidate WHERE tag_id = p_source;
END;
$$
"""


def upgrade():
    as_owner(op, 'kura_reconciler', FUNCTION)
    as_owner(op, 'kura_reconciler', "REVOKE ALL ON FUNCTION tagger_governed_merge_tag(integer, integer) FROM PUBLIC")
    as_owner(op, 'kura_reconciler', "GRANT EXECUTE ON FUNCTION tagger_governed_merge_tag(integer, integer) TO kura")


def downgrade():
    as_owner(op, 'kura_reconciler', "DROP FUNCTION IF EXISTS tagger_governed_merge_tag(integer, integer)")
