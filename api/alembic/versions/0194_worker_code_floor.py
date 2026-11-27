"""Separate the publication epoch from worker-code compatibility.

Apply REPLICA_DDL to every subscriber BEFORE canonical migration/first active
row update. Initialization preserves the old effective floor under an exclusive
lock; ALTER's constant fast-default fills the single existing row without a
DML write or bypass of its governed-write trigger. No publication occurs here.
"""
from alembic import op
revision="0194"
down_revision="0193"
branch_labels=None
depends_on=None
REPLICA_DDL = r"""
DO $migration$
DECLARE old_floor integer;
BEGIN
 SET LOCAL lock_timeout='2s';
 LOCK TABLE public.tagger_release_active IN ACCESS EXCLUSIVE MODE;
 IF NOT EXISTS (SELECT 1 FROM pg_attribute WHERE attrelid='public.tagger_release_active'::regclass AND attname='required_worker_generation' AND NOT attisdropped) THEN
  SELECT generation INTO STRICT old_floor FROM public.tagger_release_active WHERE id=1;
  IF old_floor IS NULL OR old_floor<1 THEN RAISE EXCEPTION 'worker_floor_initialization_invalid'; END IF;
  EXECUTE format('ALTER TABLE public.tagger_release_active ADD COLUMN required_worker_generation integer NOT NULL DEFAULT %s CHECK (required_worker_generation >= 1)', old_floor);
  ALTER TABLE public.tagger_release_active ALTER COLUMN required_worker_generation DROP DEFAULT;
 END IF;
END $migration$;
"""
def upgrade():
 op.execute(REPLICA_DDL)
 op.execute("COMMENT ON COLUMN tagger_release_active.required_worker_generation IS 'Minimum worker CODE generation; unchanged by ordinary model publication. Raise only with a reviewed compatibility change.'")
 op.execute("COMMENT ON COLUMN tagger_release_active.generation IS 'Publication epoch: increments on every active release change; sweep TOCTOU token, never worker code compatibility.'")
def downgrade():
 raise RuntimeError('worker compatibility floor cannot be collapsed back into the publication epoch')
