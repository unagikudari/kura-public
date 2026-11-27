"""Canonical-only explicit authenticated review responses, separate from GT/pins."""
from alembic import op
revision='0200'
down_revision='0199'
branch_labels=None
depends_on=None
REPLICA_DDL=None
STATEMENTS=(
 "DO $$ BEGIN IF current_user <> 'kura' THEN RAISE EXCEPTION 'human_review_requires_kura'; END IF; END $$",
 """CREATE TABLE public.tagger_human_review_event (
 id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
 actor_id integer NOT NULL CHECK(actor_id>0), actor_name text NOT NULL CHECK(length(actor_name) BETWEEN 1 AND 500),
 request_id uuid NOT NULL, request_sha256 text NOT NULL CHECK(request_sha256~'^[0-9a-f]{64}$'),
 queue_sha256 text NOT NULL CHECK(queue_sha256~'^[0-9a-f]{64}$'), post_id integer NOT NULL CHECK(post_id>0),
 decision text NOT NULL CHECK(decision IN ('present','absent','uncertain')),
 source_context jsonb NOT NULL CHECK(jsonb_typeof(source_context)='object' AND octet_length(source_context::text)<=262144),
 previous_event_id bigint, audit_id bigint,
 received_at timestamptz NOT NULL DEFAULT transaction_timestamp(),
 UNIQUE(actor_id,request_id))""",
 "CREATE INDEX tagger_human_review_latest ON public.tagger_human_review_event(queue_sha256,post_id,id DESC)",
 """COMMENT ON TABLE public.tagger_human_review_event IS 'Canonical-only authenticated explicit user responses. Uncertain leaves archive/pin/GT unchanged. Review completion is not quality or independent data. Historical source context immutable. API derives actor; not cryptographic human attestation.'""",
 """CREATE FUNCTION public.tagger_human_review_immutable() RETURNS trigger LANGUAGE plpgsql SET search_path=pg_catalog AS $$ BEGIN RAISE EXCEPTION 'human_review_event_immutable'; END $$""",
 """CREATE TRIGGER tagger_human_review_immutable BEFORE UPDATE OR DELETE OR TRUNCATE ON public.tagger_human_review_event FOR EACH STATEMENT EXECUTE FUNCTION public.tagger_human_review_immutable()""",
 "REVOKE ALL ON public.tagger_human_review_event FROM PUBLIC",
 "DO $$ BEGIN IF EXISTS(SELECT 1 FROM pg_roles WHERE rolname='replicator') THEN REVOKE ALL ON public.tagger_human_review_event FROM replicator; END IF; END $$",
)
def upgrade():
 op.execute("SET LOCAL lock_timeout='2s'")
 for statement in STATEMENTS:op.execute(statement)
def downgrade():raise RuntimeError('human_review_history_is_not_destructively_downgraded')
