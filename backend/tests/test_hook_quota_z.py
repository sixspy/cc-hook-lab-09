# -*- coding: utf-8 -*-
"""争用失败测（hook-09 / 并发投递锁）：满额瞬间双请求并发，不得超发。

必须先 RED：没有同锁 + 原子阈检查时，两个并发请求都会看到“尚余 1 笔”而双双成功。
救绿由独立的并发锁服务（端点日同锁）配合投递事务完成，与甲（投递闸）、乙（名册对账）分属不同服务。
"""
from __future__ import annotations

import os
import tempfile
import threading
import uuid
from pathlib import Path

_TMP = tempfile.mkdtemp(prefix="hook09_z_")
os.environ.setdefault("数据目录", _TMP)
os.environ["夹具目录"] = str(Path(__file__).resolve().parents[2] / "fixtures")

import pytest  # noqa: E402

from app import db, seed  # noqa: E402
from app.db import 连接  # noqa: E402
from app.services import 入队, 端点, 签名目录  # noqa: E402


@pytest.fixture()
def 独立库(tmp_path, monkeypatch):
    库文件 = tmp_path / "hook_quota_z.db"
    monkeypatch.setattr(db, "库路径", 库文件)
    monkeypatch.setattr(db, "数据目录", tmp_path)
    seed.初始化库()
    return 库文件


def _成功行数(ep: str) -> int:
    conn = 连接()
    try:
        return conn.execute(
            "SELECT COUNT(*) AS c FROM 投递尝试 WHERE 端点键=? AND 状态='成功'",
            (ep,),
        ).fetchone()["c"]
    finally:
        conn.close()


def test_争用_满额瞬间双请求只成一笔(独立库):
    from app.services import 投递  # 延迟导入，确保走到打了补丁的库

    ep = f"ep-qz-{uuid.uuid4().hex[:8]}"
    端点.登记({
        "端点键": ep,
        "名称": "争用测锁端点",
        "回调URL": "https://example.local/hooks/qz",
        "密钥版本": "v1",
    })
    签名目录.改签名规({f"日配额:{ep}": "1"})

    eids = []
    for i in (1, 2):
        ev = 入队.建事件({
            "事件号": f"EVT-QZ-{uuid.uuid4().hex[:10]}",
            "事件类型": "invoice.paid",
            "端点键": ep,
            "载荷摘要": f"qz-{i}",
        })
        eids.append(ev["事件号"])

    起跑 = threading.Barrier(2)
    结果: dict[str, dict] = {}

    def worker(tag: str, eid: str):
        起跑.wait()  # 尽量同时冲进临界区，制造“满额瞬间”
        try:
            r = 投递.投递一次(eid)
            结果[tag] = {"ok": True, "状态": r.get("状态")}
        except Exception as e:  # 配额闸口拒绝
            结果[tag] = {"ok": False, "错误": str(e)}

    t1 = threading.Thread(target=worker, args=("a", eids[0]))
    t2 = threading.Thread(target=worker, args=("b", eids[1]))
    t1.start(); t2.start()
    t1.join(15); t2.join(15)
    assert not t1.is_alive() and not t2.is_alive(), "并发投递线程超时未结束（疑似死锁）"

    成功 = _成功行数(ep)
    成功返回 = [v for v in 结果.values() if v.get("ok") and v.get("状态") == "成功"]
    拒绝 = [v for v in 结果.values() if not v.get("ok")]

    # —— 核心争用红断言：现状无锁，这里经常为 2（超发）-> RED ——
    assert 成功 == 1, f"满额瞬间双请求后成功行={成功}，应恰为 1（争用超发）"
    assert len(成功返回) == 1, f"应恰有一个请求成功，实际 {len(成功返回)}：{结果}"
    assert len(拒绝) == 1, f"应恰有一个请求被闸口拒绝，实际：{结果}"
    assert "配额" in 拒绝[0].get("错误", ""), f"拒绝因应含中文配额说明：{拒绝[0]}"


def test_争用_两端请求打的是同一把端点日锁(独立库):
    # 专司 z 绿的并发锁服务：同端点同日必须返回同一把锁对象（“打同锁”）。
    from app.services import 并发锁

    l1 = 并发锁.端点日锁("ep-same-lock", "2026-11-26")
    l2 = 并发锁.端点日锁("ep-same-lock", "2026-11-26")
    assert l1 is l2, "同端点同日必须是同一把锁，否则满额瞬间仍会超发"

    l3 = 并发锁.端点日锁("ep-other", "2026-11-26")
    assert l3 is not l1, "不同端点应分锁，避免无谓互相阻塞"

    # 既有空桩「并发投递锁」也必须接到同一锁表，脚本/页面口径一致。
    from app.modules.并发投递锁 import 同锁 as 桩同锁  # type: ignore

    assert 桩同锁(并发锁.锁键("ep-same-lock", "2026-11-26")) is l1
