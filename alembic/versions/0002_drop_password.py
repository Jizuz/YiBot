"""删除明文密码列（T22 迁移 002）——备而不发！

执行前置条件（design: 风险表）：
  1. scripts/migrate_hash_pwd_report.py 连续观测明文存量 = 0；
  2. 观测一个发布周期无回退（旧版本不再写明文）；
  3. 经独立变更窗口评审后方可执行 alembic upgrade +1。
"""

from alembic import op
import sqlalchemy as sa

revision = "0002_drop_password"
down_revision = "0001_unique_mobile"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_column("user", "password")


def downgrade() -> None:
    op.add_column("user", sa.Column("password", sa.String(200), nullable=True))