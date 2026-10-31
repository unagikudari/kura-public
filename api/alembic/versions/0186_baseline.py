"""0186: the schema the fresh-install baseline (api/alembic/baseline/0186.sql) creates

This tree starts from that baseline, so the migrations that built it are not shipped;
this empty revision is where the ones after it attach.
"""
revision = "0186"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    raise NotImplementedError("the baseline is where this tree's history starts")
