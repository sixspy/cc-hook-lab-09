# -*- coding: utf-8 -*-
"""失败测甲（hook-09 / 投递服务）：端点日配额满后，成功尝试不得再增。

工序要求：本测试必须先在未接线的业务服务上跑出 RED（配额=1 连投两笔 -> 2 行成功），
随后由「投递配额闸 + 投递服务」救绿。它与 y（名册对账）、z（争用同锁）分属不同服务。
"""
from __future__ import annotations

import os
import tempfile
import uuid
from pathlib import Path

_TMP = tempfile.mkdtemp(prefix="hook09_x_")
os.environ.setdefault("数据目录", _TMP)
os.environ["夹具目录"] = str(Path(__file__).resolve().parents[2] / "fixtures")

import pytest  # noqa: E402

from app import db, seed  # noqa: E402
from app.db import 连接  # noqa: E402
from app.services import 入队, 投递, 端点, 签名目录  # noqa: E402


@pytest.fixture()
def 独立库(tmp_path, monkeypatch):
    库文件 = tmp_path / "hook_quota_x.db"
    monkeypatch.setattr(db, "库路径", 库文件)
    monkeypatch.setattr(db, "数据目录", tmp_path)
    seed.初始化库()
    return 库文件


def _新端点() -> str:
    ep = f"ep-qx-{uuid.uuid4().hex[:8]}"
    端点.登记({
        "端点键": ep,
        "名称": "甲测配额端点",
        "回调URL": "https://example.local/hooks/qx",
        "密钥版本": "v1",
    })
    # 见红前就存在的设置面：签名规键值。救绿后的配额服务必须认得这条端点日配额。
    签名目录.改签名规({f"日配额:{ep}": "1"})
    return ep


def _连投两笔(ep: str):
    outcomes = []
    for i in (1, 2):
        ev = 入队.建事件({
            "事件号": f"EVT-QX-{uuid.uuid4().hex[:10]}",
            "事件类型": "invoice.paid",
            "端点键": ep,
            "载荷摘要": f"qx-{i}",
        })
        try:
            r = 投递.投递一次(ev["事件号"])
            outcomes.append({"受理": True, "返回": r})
        except Exception as e:  # 配额闸口允许以中文 ValueError 拒绝
            outcomes.append({"受理": False, "错误": str(e)})
    return outcomes


def _成功行数(ep: str) -> int:
    conn = 连接()
    try:
        return conn.execute(
            "SELECT COUNT(*) AS c FROM 投递尝试 WHERE 端点键=? AND 状态='成功'",
            (ep,),
        ).fetchone()["c"]
    finally:
        conn.close()


def _超发归档行(ep: str) -> list[dict]:
    conn = 连接()
    try:
        has = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='超发归档'"
        ).fetchone()
        if not has:
            return []
        return [dict(r) for r in conn.execute(
            "SELECT * FROM 超发归档 WHERE 端点键=?", (ep,)
        ).fetchall()]
    finally:
        conn.close()


def test_甲_配额满后成功尝试不再增(独立库):
    ep = _新端点()
    outcomes = _连投两笔(ep)

    成功 = _成功行数(ep)
    # —— 核心红断言之所在：现状代码忽略日配额，这里实际为 2，断言失败（RED）——
    assert 成功 == 1, f"日配额=1 却出现 {成功} 行成功尝试，配额满后仍增 = 超发事故"

    # 第一笔必须成功
    assert outcomes[0]["受理"] is True
    assert outcomes[0]["返回"]["状态"] == "成功"

    # 第二笔不得是“成功”：要么被拒，要么返回非成功状态
    second = outcomes[1]
    if second["受理"]:
        assert second["返回"].get("状态") != "成功", "满额后第二笔仍回报成功"

    # 必须留下可读的中文超发拒因（异常文案 / 返回体 / 归档轨 至少一处）
    文案 = " ".join([
        second.get("错误", ""),
        str((second.get("返回") or {}).get("错误", "")),
        str((second.get("返回") or {}).get("拒因", "")),
        " ".join(r.get("拒因", "") for r in _超发归档行(ep)),
    ])
    assert "配额" in 文案, f"缺少中文配额/超发拒因，实际：{文案!r}"

    # 双写两轨：超配额请求必须落到只读超发归档轨，且不污染正式流水
    arch = _超发归档行(ep)
    assert len(arch) == 1, f"超配额请求应归档 1 笔，实际 {len(arch)}"
    assert arch[0].get("状态") in ("超发拦截", "拒绝超发", "配额已满")
