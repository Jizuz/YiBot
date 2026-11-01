"""user 表 mobile 唯一索引（T22 迁移 001）。

前置：scripts/migrate_dedup_mobile.py --dry-run 复核 + --apply 去重完成
（同手机号软标记），否则唯一索引创建失败。
"""

from alembic import op

revision = "0001_unique_mobile"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index("uk_user_mobile", "user", ["mobile"], unique=True)


def downgrade() -> None:
    op.drop_index("uk_user_mobile", table_name="user")
