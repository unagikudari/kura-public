"""0195: post_tag_audit.source に pin_absent:<tag> を許す

「付いていないタグを『無い』と確定する」(tag-pin の state='absent', 1.329.0) の来歴。
1.329.0 はこの行を `bulk_absent:<tag>` / `assert_absent:<tag>` として書こうとしたが、
`ck_post_tag_audit_source` が許す値は `auto_tag` / `manual` / `bulk_lock:%` /
`bulk_remove:%` だけで、**本番で最初の 1 件から 500 になった** (CheckViolation、
何も書かれていない)。test は deploy 前の API にしか当てておらず、制約を見ていなかった。

## なぜ既存の値に寄せないか

0161 と同じ理由。`bulk_lock:<tag>` に寄せれば通るが、施錠 (present) と区別が付かなく
なる。この操作は 1 回で最大 1000 件を負例 GT にするので、「あの確定はどれか」を
audit 1 クエリで引けることが、施錠・解除と同じだけ要る。単発と一括で操作は同じ
(pin を absent にする) なので、名前は 1 つ: `pin_absent:<tag>`。

## replica

CHECK は apply が強制するクラス (docs/notes/replica-ddl-propagation.md): replica に
先に REPLICA_DDL を当て、台帳を 0195 に進めてから canonical に適用する。

制約の張り直しは既存行を検査する (post_tag_audit は約 400 万行)。lock_timeout を
置き、取れなければ失敗させる — 待ち行列を作って書き込みを止めないため。
"""
from alembic import op

revision = "0195"
down_revision = "0194"
branch_labels = None
depends_on = None

_DDL_DROP = "ALTER TABLE post_tag_audit DROP CONSTRAINT IF EXISTS ck_post_tag_audit_source"
_DDL_ADD = (
    "ALTER TABLE post_tag_audit ADD CONSTRAINT ck_post_tag_audit_source CHECK ("
    "source = ANY (ARRAY['auto_tag'::text, 'manual'::text]) "
    "OR source LIKE 'bulk_lock:%' OR source LIKE 'bulk_remove:%' OR source LIKE 'pin_absent:%')"
)
_DDL_OLD = (
    "ALTER TABLE post_tag_audit ADD CONSTRAINT ck_post_tag_audit_source CHECK ("
    "source = ANY (ARRAY['auto_tag'::text, 'manual'::text]) "
    "OR source LIKE 'bulk_lock:%' OR source LIKE 'bulk_remove:%')"
)

REPLICA_DDL = f"SET LOCAL lock_timeout='5s'; {_DDL_DROP}; {_DDL_ADD}"


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout='5s'")
    op.execute(_DDL_DROP)
    op.execute(_DDL_ADD)


def downgrade() -> None:
    # 戻すと pin_absent の行が制約違反になる。既存行を消さずに戻せないので、
    # 呼ぶ側が意図してやること。
    op.execute(_DDL_DROP)
    op.execute(_DDL_OLD)
