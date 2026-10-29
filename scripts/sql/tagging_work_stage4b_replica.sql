-- Stage 4b on the replica (trainer): drop post.tagging_* there, AFTER primary has applied 0185
-- and after this replica has APPLIED everything primary wrote before that.
--
-- Why the wait: changes primary made before 0185 still carry the four columns. If the replica
-- drops them while such a change is still unapplied, applying it fails and the
-- subscription stops (every published table). "primary is at 0185" or "the apply worker is
-- running" proves nothing about what was applied; the subscription's replication origin
-- (remote_lsn) does: it is the publisher position up to which changes are applied here.
--
-- 1. On primary, once its ledger reads 0185 (kura may run this):
--      psql -h 127.0.0.1 -U kura -d kura -tAc "SELECT pg_current_wal_lsn()"
--    Any position taken after 0185 committed is past every change with the old columns.
-- 2. On trainer, as kura (post's owner there), with that position:
--      psql -X -h 127.0.0.1 -U kura -d kura -v target_lsn=<lsn> -f scripts/sql/tagging_work_stage4b_replica.sql
--    (kura must be able to read pg_replication_origin_status: on trainer it can, 2026-10-25.)
--    It waits (up to wait_seconds, default 300) until the subscription's remote_lsn reaches
--    target_lsn, then drops in one transaction with the ledger update, under a short
--    lock_timeout. remote_lsn moves when a replicated transaction commits here, so the wait
--    ends with primary's next write to a published table after the target (constant in practice).
--    The last line says DROPPED, or STOPPED with the reason (nothing changed;
--    rerun). Rerunning after DROPPED does nothing.
--
-- The DDL is 0185's REPLICA_DDL (tests/test_tagging_work_stage4b.py checks they match).

\set ON_ERROR_STOP 1
\if :{?target_lsn}
\else
\echo 'STOPPED: pass -v target_lsn=<pg_current_wal_lsn() on primary, taken after 0185>'
\quit
\endif
\if :{?wait_seconds}
\else
\set wait_seconds 300
\endif
\if :{?subscription}
\else
\set subscription kura_sub_trainer_from_primary
\endif

SELECT version_num = '0185' AS done, version_num = '0184' AS ready FROM kura_alembic_version
\gset
\if :done
\echo 'DROPPED (already)'
\quit
\endif
\if :ready
\else
\echo 'STOPPED: the ledger is neither 0184 nor 0185'
\quit
\endif

-- Wait for apply to pass the target. Outside the DDL transaction, so nothing is held.
-- (psql does not substitute variables inside $$, so the values go in as settings.)
SELECT set_config('stage4b.origin', (SELECT 'pg_' || oid FROM pg_subscription WHERE subname = :'subscription'), false),
       set_config('stage4b.target', :'target_lsn', false),
       set_config('stage4b.wait', :'wait_seconds', false)
\gset
DO $$
DECLARE
    deadline timestamptz := clock_timestamp() + make_interval(secs => current_setting('stage4b.wait')::int);
BEGIN
    WHILE clock_timestamp() < deadline LOOP
        EXIT WHEN (SELECT remote_lsn FROM pg_replication_origin_status
                    WHERE external_id = current_setting('stage4b.origin'))
                  >= current_setting('stage4b.target')::pg_lsn;
        PERFORM pg_sleep(0.5);
    END LOOP;
END
$$;
SELECT coalesce((SELECT remote_lsn FROM pg_replication_origin_status
                  WHERE external_id = current_setting('stage4b.origin'))
                >= :'target_lsn'::pg_lsn, false) AS reached,
       (SELECT remote_lsn FROM pg_replication_origin_status
         WHERE external_id = current_setting('stage4b.origin')) AS applied
\gset
\if :reached
\echo 'applied up to' :target_lsn '(at' :applied ')'
\else
\echo 'STOPPED: apply has not reached' :target_lsn 'within' :wait_seconds 's (applied:' :applied '); nothing changed'
\quit
\endif

-- From here an error must not leave psql mid-transaction: it rolls back and says so.
\set ON_ERROR_STOP 0
BEGIN;
SET LOCAL lock_timeout = '2s';
DROP TRIGGER IF EXISTS trg_post_tagging_columns_governed ON public.post;
DROP FUNCTION IF EXISTS public.trg_post_tagging_columns_governed();
ALTER TABLE public.post DROP COLUMN tagging_status, DROP COLUMN tagging_locked_until,
  DROP COLUMN tagging_attempts, DROP COLUMN tagging_last_error;
UPDATE kura_alembic_version SET version_num = '0185' WHERE version_num = '0184';
COMMIT;
SELECT version_num = '0185' AS dropped FROM kura_alembic_version
\gset
\if :dropped
\echo 'DROPPED'
\else
\echo 'STOPPED: the drop did not commit (see the error above; often lock_timeout); nothing changed, rerun'
\endif
