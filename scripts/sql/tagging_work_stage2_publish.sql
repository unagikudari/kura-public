-- Stage 2 of docs/notes/tagging-work-table-plan.md: publish post_tagging_work to the replica.
--
-- Run as postgres on primary, and only AFTER trainer has the table (0182's REPLICA_DDL):
-- once the table is in the publication its changes are sent, and a subscriber that
-- lacks the table stops applying everything. Feed it on stdin (postgres cannot read
-- under the kura user's home):
--   sudo -u postgres psql -X -d kura -v ON_ERROR_STOP=1 < scripts/sql/tagging_work_stage2_publish.sql
-- Then on trainer, as kura: ALTER SUBSCRIPTION kura_sub_trainer_from_primary REFRESH PUBLICATION
-- and wait for pg_subscription_rel.srsubstate = 'r' for the table.
--
-- Why postgres: kura owns kura_mesh_pub but not the table (kura_reconciler does), and
-- adding a table to a publication requires owning both. Nothing is granted to kura.
--
-- Undo (before stage 3 only):
--   ALTER PUBLICATION kura_mesh_pub DROP TABLE public.post_tagging_work;   (as postgres)
-- ALTER PUBLICATION ... ADD TABLE takes SHARE UPDATE EXCLUSIVE, which does not wait
-- for the mirror's row writes. Rerunning is harmless.

\set ON_ERROR_STOP 1
SET lock_timeout = '2s';

DO $do$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_publication_tables WHERE pubname = 'kura_mesh_pub'
                 AND schemaname = 'public' AND tablename = 'post_tagging_work') THEN
    ALTER PUBLICATION kura_mesh_pub ADD TABLE public.post_tagging_work;
  END IF;
END
$do$;

SELECT count(*) AS published FROM pg_publication_tables
 WHERE pubname = 'kura_mesh_pub' AND schemaname = 'public' AND tablename = 'post_tagging_work';
