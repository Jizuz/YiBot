"""明文密码清尾扫描（T23，spec: security-hardening 数据约束）。

- report：输出明文存量基线/当前值（只读，供清尾比对）；
- watch：每日低峰定时扫描，明文残留 >0 告警（不改数）。

用法：
  .venv/bin/python scripts/migrate_hash_pwd_report.py            # 一次性报告
  .venv/bin/python scripts/migrate_hash_pwd_report.py --watch    # 每日 03:00 扫描
"""

import argparse
import asyncio
import logging
from datetime import datetime, time

from sqlalchemy import func

from data.mysql.conn import SessionLocal
from data.mysql.models.user import User

logger = logging.getLogger("yibot.scripts.pwd_report")


def count_plain(db) -> int:
    return (
        db.query(func.count(User.id))
        .filter(User.is_delete == 0, User.password != "")  # noqa: SIM905
        .scalar()
        or 0
    )


def report_once() -> int:
    db = SessionLocal()
    try:
        plain_count = count_plain(db)
        stamped = datetime.now().isoformat(timespec="seconds")
        level = logging.WARNING if plain_count > 0 else logging.INFO
        logger.log(
            level,
            "plain password remaining: %s", plain_count,
            extra={"event": "pwd_plain_scan", "remaining": plain_count},
        )
        print(f"[{stamped}] 明文密码存量账号：{plain_count}"
              + ("（>0 告警：迁移缺口）" if plain_count else "（清尾完成 ✅）"))
        return plain_count
    finally:
        db.close()


async def watch(daily_at: time = time(3, 0)) -> None:
    logger.info("watch mode started, daily at %s", daily_at)
    while True:
        now = datetime.now()
        target = datetime.combine(now.date(), daily_at)
        if target <= now:
            target = datetime.combine(now.date(), daily_at).timestamp() + 86400
        await asyncio.sleep(target.timestamp() - now.timestamp())
        report_once()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--watch", action="store_true", help="每日定时扫描模式")
    args = parser.parse_args()
    if args.watch:
        asyncio.run(watch())
        return 0
    return 0 if report_once() == 0 else 0  # 报告模式始终退出 0（只读不改数）


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    raise SystemExit(main())
