"""Worker execution receipts, canonical-only; audit refs are replicated.
Heartbeat is local (not published). Freeze auto-pull BEFORE merge; stage subscriber
columns first. See docs/notes/cl2-execution-receipt-rollout.md.
Apply REPLICA_DDL to every subscriber and verify ledger parity BEFORE canonical
upgrade/API deploy. No release publication in this migration. No rollback once
receipt-backed audit history exists. Seven-argument heartbeat delegates NULL.
"""
from alembic import op
from api.alembic.owner import as_owner
revision = '0192'
down_revision = '0191'
branch_labels = None
depends_on = None
# No heap scan in the ACCESS EXCLUSIVE transaction. NOT VALID still checks
# new writes. Run VALIDATION_DDL in a separate transaction AFTER upgrade commits.
REPLICA_DDL = """
SET LOCAL lock_timeout='2s';
ALTER TABLE public.tagger_worker_heartbeat ADD COLUMN IF NOT EXISTS execution_receipt_sha256 text;
ALTER TABLE public.post_tag_audit ADD COLUMN IF NOT EXISTS execution_receipt_sha256 text;
DO $$ BEGIN
 IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conrelid='public.tagger_worker_heartbeat'::regclass AND conname='heartbeat_execution_receipt_hex') THEN
  ALTER TABLE public.tagger_worker_heartbeat ADD CONSTRAINT heartbeat_execution_receipt_hex CHECK (execution_receipt_sha256 ~ '^[0-9a-f]{64}$') NOT VALID;
 END IF;
 IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conrelid='public.post_tag_audit'::regclass AND conname='audit_execution_receipt_hex') THEN
  ALTER TABLE public.post_tag_audit ADD CONSTRAINT audit_execution_receipt_hex CHECK (execution_receipt_sha256 ~ '^[0-9a-f]{64}$') NOT VALID;
 END IF;
END $$;
"""
VALIDATION_DDL = """
SET LOCAL lock_timeout='2s';
SET LOCAL statement_timeout='5min';
ALTER TABLE public.tagger_worker_heartbeat VALIDATE CONSTRAINT heartbeat_execution_receipt_hex;
ALTER TABLE public.post_tag_audit VALIDATE CONSTRAINT audit_execution_receipt_hex;
"""
TABLE_DDL = """
CREATE TABLE tagger_execution_receipt (
 receipt_sha256 text PRIMARY KEY CHECK (receipt_sha256 ~ '^[0-9a-f]{64}$'),
 canonical_body text NOT NULL CHECK (octet_length(canonical_body) <= 4096),
 body jsonb NOT NULL CHECK (jsonb_typeof(body) = 'object'),
 first_seen_at timestamptz NOT NULL DEFAULT now(),
 CHECK (canonical_body::jsonb = body),
 CHECK (receipt_sha256 = encode(sha256(convert_to(canonical_body,'UTF8')), 'hex'))
);
COMMENT ON TABLE tagger_execution_receipt IS 'Canonical-only immutable worker self-report, not attestation. API validates restricted canonical JSON and context. Replicated references have no FK; NULL means unknown.';
CREATE FUNCTION tagger_execution_receipt_immutable() RETURNS trigger LANGUAGE plpgsql SET search_path=pg_catalog,public AS $$
BEGIN RAISE EXCEPTION 'execution_receipt_immutable'; END $$;
CREATE TRIGGER tagger_execution_receipt_immutable BEFORE UPDATE OR DELETE OR TRUNCATE
 ON tagger_execution_receipt FOR EACH STATEMENT EXECUTE FUNCTION tagger_execution_receipt_immutable();
GRANT SELECT, INSERT ON tagger_execution_receipt TO kura;
GRANT SELECT ON tagger_execution_receipt TO kura_reconciler;
"""
HEARTBEAT_DDL = """
        CREATE OR REPLACE FUNCTION tagger_upsert_heartbeat(
            p_worker_id text, p_host text, p_profile text, p_generation bigint,
            p_release_id integer, p_provider text, p_model_sha256 text, p_receipt text
        )
        RETURNS void
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $$
        BEGIN
            IF session_user NOT IN (SELECT db_role FROM public.tagger_allowed_executor WHERE scope = 'worker-heartbeat') THEN
                RAISE EXCEPTION 'tagger_upsert_heartbeat: session_user % is not an allowed executor for scope worker-heartbeat',
                    session_user;
            END IF;
            IF p_receipt IS NOT NULL AND NOT EXISTS (
                SELECT 1 FROM public.tagger_execution_receipt WHERE receipt_sha256=p_receipt
                AND body->>'host'=p_host AND body->>'profile'=p_profile
                AND body->>'release_id'=p_release_id::text) THEN
                RAISE EXCEPTION 'execution_receipt_context';
            END IF;
            INSERT INTO public.tagger_worker_heartbeat
                (worker_id, host, profile, generation, release_id, provider, model_sha256, reported_at, execution_receipt_sha256)
            VALUES (p_worker_id, p_host, p_profile, p_generation, p_release_id, p_provider, p_model_sha256, now(), p_receipt)
            ON CONFLICT (worker_id) DO UPDATE SET
                host = EXCLUDED.host, profile = EXCLUDED.profile,
                generation = EXCLUDED.generation, release_id = EXCLUDED.release_id,
                provider = EXCLUDED.provider, model_sha256 = EXCLUDED.model_sha256,
                reported_at = EXCLUDED.reported_at, execution_receipt_sha256 = EXCLUDED.execution_receipt_sha256;
        END;
        $$

"""
WRAPPER_DDL = """
CREATE OR REPLACE FUNCTION tagger_upsert_heartbeat(p_worker_id text, p_host text, p_profile text, p_generation bigint, p_release_id integer, p_provider text, p_model_sha256 text)
RETURNS void LANGUAGE sql SECURITY DEFINER SET search_path=pg_catalog,public AS $$
 SELECT public.tagger_upsert_heartbeat($1,$2,$3,$4,$5,$6,$7,NULL::text)
$$;
"""

def statements(block):
    """The block's statements, one each: asyncpg prepares what alembic executes and refuses several commands in one
    (the first live upgrade stopped there, 2026-11-15). Split at a line-ending `;` outside a $$ body."""
    out, current, inside = [], [], False
    for line in block.strip().splitlines():
        current.append(line)
        if line.count('$$') % 2:
            inside = not inside
        if not inside and line.rstrip().endswith(';'):
            out.append('\n'.join(current).rstrip().rstrip(';'))
            current = []
    if current:
        raise ValueError('unterminated statement in a DDL block')
    return out


def upgrade():
    op.execute("SET LOCAL lock_timeout='2s'")
    for statement in statements(TABLE_DDL):
        op.execute(statement)
    op.execute("""DO $$ BEGIN
 IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname='replicator') THEN
  REVOKE ALL ON public.tagger_execution_receipt FROM replicator;
 END IF;
END $$""")
    for statement in statements(REPLICA_DDL):
        op.execute(statement)
    # The heartbeat functions belong to kura_reconciler, which `kura` is not a member of: they are replaced and
    # created as their owner (api/alembic/owner.py), so this revision needs the superuser-authenticated upgrade
    # (ops/install/database.py upgrade) before the API that carries it starts; a plain startup refuses with that remedy.
    as_owner(op, 'kura_reconciler', HEARTBEAT_DDL)
    as_owner(op, 'kura_reconciler', WRAPPER_DDL)
    for sig in ('text,text,text,bigint,integer,text,text', 'text,text,text,bigint,integer,text,text,text'):
        as_owner(op, 'kura_reconciler', f'REVOKE ALL ON FUNCTION tagger_upsert_heartbeat({sig}) FROM PUBLIC')
        as_owner(op, 'kura_reconciler', f'GRANT EXECUTE ON FUNCTION tagger_upsert_heartbeat({sig}) TO kura')

def downgrade():
    raise RuntimeError('execution_receipt_history_requires_forward_migration')
