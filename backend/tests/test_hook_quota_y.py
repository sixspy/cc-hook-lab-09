# -*- coding: utf-8 -*-
"""失败测乙（hook-09 / 端点服务·名册对账）：端点名册不得把超发当成配额内。

必须先 RED：配额=1 连投两笔后，旧名册只是端点原表，既不联查流水也不分离超发，
朴素口径会把两笔都算作“配额内”。救绿由端点服务的配额对账 + 名册联查读模型完成。
"""
from __future__ import annotations

import os
import tempfile
import uuid
from pathlib import Path

_TMP = tempfile.mkdtemp(prefix="hook09_y_")
os.environ.setdefault("数据目录", _TMP)
os.environ["夹具目录"] = str(Path(__file__).resolve().parents[2] / "fixtures")

import pytest  # noqa: E402

from app import db, seed  # noqa: E402
from app.services import 入队, 投递, 端点, 签名目录  # noqa: E402


@pytest.fixture()
def 独立库(tmp_path, monkeypatch):
    库文件 = tmp_path / "hook_quota_y.db"
    monkeypatch.setattr(db, "库路径", 库文件)
    monkeypatch.setattr(db, "数据目录", tmp_path)
    seed.初始化库()
    return 库文件


def _配额一端点() -> str:
    ep = f"ep-qy-{uuid.uuid4().hex[:8]}"
    端点.登记({
        "端点键": ep,
        "名称": "乙测名册端点",
        "回调URL": "https://example.local/hooks/qy",
        "密钥版本": "v2",
    })
    签名目录.改签名规({f"日配额:{ep}": "1"})
    for i in (1, 2):
        ev = 入队.建事件({
            "事件号": f"EVT-QY-{uuid.uuid4().hex[:10]}",
            "事件类型": "invoice.paid",
            "端点键": ep,
            "载荷摘要": f"qy-{i}",
        })
        try:
            投递.投递一次(ev["事件号"])
        except Exception:
            pass  # 救绿后第二笔应被闸口拒绝；本测只核对账口径
    return ep


def test_乙_名册联查把超发与配额内分开(独立库):
    ep = _配额一端点()

    roster = {r["端点键"]: r for r in 签名目录.端点列表()}
    assert ep in roster
    row = roster[ep]

    # —— 核心红断言：旧名册没有这两个分离口径，.get 为 None -> RED ——
    # 正确口径：配额内成功恰为 1，超发恰为 1，而不是把超发也算进配额内（2）。
    assert row.get("配额内成功") == 1, (
        f"名册把配额内成功算成 {row.get('配额内成功')!r}，疑似把超发当成配额内"
    )
    assert row.get("超发数") == 1, f"名册超发数应为 1，实际 {row.get('超发数')!r}"
    assert int(row.get("日配额余量", -1)) == 0
    assert row.get("日配额") == 1


def test_乙_端点服务配额对账同票(独立库):
    ep = _配额一端点()

    # 专司乙绿的服务：端点服务必须提供配额对账（联查正式流水 + 影子归档）。
    assert hasattr(端点, "配额对账"), "端点服务缺少 配额对账，无法分服务救乙红"
    rep = 端点.配额对账(ep)
    if isinstance(rep, list):  # 允许无参/批量形态返回单行列表
        rep = next(x for x in rep if x["端点键"] == ep)

    assert rep["日配额"] == 1
    assert rep["配额内成功"] == 1
    assert rep["超发数"] == 1
    assert int(rep["日配额余量"]) == 0
    # 名册阈值 / 正式流水 / 超发归档 三账同票
    assert rep.get("同票") is True, f"名册与流水/归档不同票：{rep}"
    assert "配额" in str(rep.get("闸口文案", ""))
