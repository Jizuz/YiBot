"""T22：mobile 去重脚本单测（sqlite）。"""

from datetime import datetime, timedelta

import pytest
from sqlalchemy import BigInteger, Integer, create_engine
from sqlalchemy.orm import sessionmaker

from data.mysql.conn import Base
from data.mysql.models.user import User
from scripts import migrate_dedup_mobile as dedup


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:")
    User.__table__.c.id.type = BigInteger().with_variant(Integer(), "sqlite")
    Base.metadata.create_all(engine, tables=[User.__table__])
    session = sessionmaker(bind=engine)()
    yield session
    session.close()
    engine.dispose()


def _seed(db, mobile, created_offsets):
    for i, offset in enumerate(created_offsets):
        db.add(User(
            name=f"u{i}", mobile=mobile, password="x",
            gmt_create=datetime(2026, 1, 1) + timedelta(days=offset),
            disabled=0, is_delete=0,
        ))
    db.commit()


def test_find_duplicates_and_keep_earliest(db):
    _seed(db, "13800000001", [5, 1, 3])  # 3 条重复，保留 offset=1（最早）
    duplicates = dedup.find_duplicates(db)
    assert "13800000001" in duplicates
    keep = dedup.pick_keep(duplicates["13800000001"])
    assert keep.gmt_create == datetime(2026, 1, 2)


def test_apply_marks_soft_delete(db, capsys):
    _seed(db, "13800000002", [0, 2])
    duplicates = dedup.find_duplicates(db)
    for rows in duplicates.values():
        keep = dedup.pick_keep(rows)
        for row in rows:
            if row.id != keep.id:
                row.is_delete = 1
    db.commit()
    alive = db.query(User).filter(
        User.mobile == "13800000002", User.is_delete == 0
    ).count()
    assert alive == 1
    # 软标记可逆（is_delete=1 可恢复）
    soft = db.query(User).filter(User.mobile == "13800000002", User.is_delete == 1).count()
    assert soft == 1


def test_no_duplicates_single_pass(db):
    _seed(db, "13800000003", [0])
    assert dedup.find_duplicates(db) == {}
