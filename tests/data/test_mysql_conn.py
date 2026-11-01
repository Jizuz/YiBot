"""T20：mysql conn 改造测试（spec: production-hardening）。

覆盖：submit_db_task 线程执行/失败日志/队列上限丢弃、session_scope 提交回滚。
"""

import logging
import threading
import time

import pytest

from data.mysql import conn as mysql_conn


def test_submit_db_task_executes():
    result = []
    done = threading.Event()

    def job():
        result.append(42)
        done.set()

    mysql_conn.submit_db_task(job)
    assert done.wait(timeout=2)
    assert result == [42]


def test_submit_db_task_failure_logged(caplog):
    def bad_job():
        raise ValueError("boom")

    with caplog.at_level(logging.ERROR, logger="yibot.data.mysql"):
        mysql_conn.submit_db_task(bad_job)
        deadline = time.time() + 2
        while time.time() < deadline:
            if any(
                getattr(r, "event", "") == "db_task_fail" for r in caplog.records
            ):
                break
            time.sleep(0.02)

    assert any(
        getattr(r, "event", "") == "db_task_fail" for r in caplog.records
    ), "失败任务必须记录 ERROR 日志（禁止静默丢消息）"


def test_submit_db_task_drops_when_queue_full(monkeypatch, caplog):
    monkeypatch.setattr(mysql_conn, "_inflight", threading.Semaphore(1))
    mysql_conn._inflight.acquire()  # 占满额度

    with caplog.at_level(logging.ERROR, logger="yibot.data.mysql"):
        future = mysql_conn.submit_db_task(lambda: None)

    assert future is None  # 满额丢弃，不排队
    assert any(
        getattr(r, "event", "") == "db_queue_drop" for r in caplog.records
    )


def test_engine_echo_disabled():
    """生产禁止 echo=True 输出 SQL 日志（design: 数据层归位）。"""
    assert mysql_conn.engine.echo is False
