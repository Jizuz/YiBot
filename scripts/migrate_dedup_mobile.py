"""mobile 去重脚本（T22，design: 数据迁移 & 灰度策略）。

前置条件：Alembic 001（uk_user_mobile 唯一索引）执行前必须先去重。
规则：同手机号保留 gmt_create 最早且未删除的记录，其余标记 is_delete=1。

用法：
  .venv/bin/python scripts/migrate_dedup_mobile.py --dry-run   # 仅输出清单人工复核
  .venv/bin/python scripts/migrate_dedup_mobile.py --apply     # 执行软标记
"""

import argparse
import logging
import sys
from collections import defaultdict
from datetime import datetime

from data.mysql.conn import SessionLocal
from data.mysql.models.user import User

logger = logging.getLogger("yibot.scripts.dedup_mobile")


def find_duplicates(db) -> dict[str, list[User]]:
    users = db.query(User).filter(User.is_delete == 0).all()
    by_mobile: dict[str, list[User]] = defaultdict(list)
    for user in users:
        if user.mobile:
            by_mobile[user.mobile].append(user)
    return {m: rows for m, rows in by_mobile.items() if len(rows) > 1}


def pick_keep(rows: list[User]) -> User:
    def sort_key(u: User):
        created = u.gmt_create or datetime.min
        return (created, u.id or 0)

    return sorted(rows, key=sort_key)[0]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="仅输出清单不修改")
    parser.add_argument("--apply", action="store_true", help="执行软标记")
    args = parser.parse_args()
    if not args.dry_run and not args.apply:
        parser.error("必须指定 --dry-run 或 --apply")

    db = SessionLocal()
    try:
        duplicates = find_duplicates(db)
        if not duplicates:
            logger.info("无重复手机号")
            print("✓ 无重复手机号，可执行 Alembic 001")
            return 0

        total_drop = 0
        print(f"发现 {len(duplicates)} 个重复手机号：")
        for mobile, rows in sorted(duplicates.items()):
            keep = pick_keep(rows)
            drops = [r for r in rows if r.id != keep.id]
            total_drop += len(drops)
            print(
                f"  mobile={mobile} 保留 id={keep.id}"
                f"(gmt_create={keep.gmt_create}) 软删除 ids={[r.id for r in drops]}"
            )

        if args.apply:
            for mobile, rows in duplicates.items():
                keep = pick_keep(rows)
                for row in rows:
                    if row.id != keep.id:
                        row.is_delete = 1
            db.commit()
            logger.info(
                "dedup applied",
                extra={"event": "dedup_applied", "groups": len(duplicates), "dropped": total_drop},
            )
            print(f"✓ 已软标记 {total_drop} 条（is_delete=1，可逆恢复）")
        else:
            print(f"dry-run：共需软标记 {total_drop} 条（复核后加 --apply 执行）")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    sys.exit(main())
