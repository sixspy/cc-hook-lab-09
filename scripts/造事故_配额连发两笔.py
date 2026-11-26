"""hook-09 事故复现：用夹具把端点日配额调到 1，再对同一端点连投两笔。

见红前：投递服务不认日配额，两笔都“成功”，正式流水出现 2 行成功 -> 超发事故。
救绿后：第一笔配额内成功，第二笔被硬闸拒绝并双写到只读超发归档，正式成功行=1。

用法：
  cd backend && ../.venv/bin/python ../scripts/造事故_配额连发两笔.py
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_BACKEND = _ROOT / "backend"
sys.path.insert(0, str(_BACKEND))

_TMP = tempfile.mkdtemp(prefix="hook09_accident_")
os.environ["数据目录"] = _TMP
os.environ["夹具目录"] = str(_ROOT / "fixtures")

from app import seed  # noqa: E402
from app.db import 连接  # noqa: E402
from app.services import 入队, 投递, 端点, 夹具, 签名目录  # noqa: E402

配额键约定 = "日配额:{ep}"


def 把配额调到一(ep: str) -> str:
    """优先用救绿后的配额服务；见红前退化为既有签名规键值（空桩不接线，故无人读）。"""
    try:
        from app.services import 配额  # type: ignore

        配额.设日配额(ep, 1)
        return "配额服务.设日配额"
    except Exception:
        签名目录.改签名规({配额键约定.format(ep=ep): "1"})
        return "签名规键值(见红前的名义配额)"


def 成功行数(ep: str) -> int:
    conn = 连接()
    try:
        return conn.execute(
            "SELECT COUNT(*) AS c FROM 投递尝试 WHERE 端点键=? AND 状态='成功'", (ep,)
        ).fetchone()["c"]
    finally:
        conn.close()


def 超发归档行数(ep: str) -> int:
    conn = 连接()
    try:
        has = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='超发归档'"
        ).fetchone()
        if not has:
            return 0
        return conn.execute(
            "SELECT COUNT(*) AS c FROM 超发归档 WHERE 端点键=?", (ep,)
        ).fetchone()["c"]
    finally:
        conn.close()


def main() -> int:
    seed.初始化库()
    fx = 夹具.读取("配额连发两笔")
    ep = fx["配额"]["端点键"]
    阈值 = int(fx["配额"]["日配额"])

    info = fx.get("端点") or {}
    try:
        端点.登记(info)
    except ValueError:
        pass

    via = 把配额调到一(ep)
    print(f"[夹具] 端点={ep} 日配额={阈值} 设置途径={via}")

    ids = []
    for item in fx["事件"]:
        row = 入队.建事件(dict(item))
        ids.append(row["事件号"])

    for i, eid in enumerate(ids, 1):
        try:
            r = 投递.投递一次(eid)
            print(f"[第{i}笔] {eid} -> {r.get('状态')} / 事件状态={r.get('事件状态')}")
        except Exception as e:  # noqa: BLE001
            print(f"[第{i}笔] {eid} -> 拒绝/异常：{e}")

    succ = 成功行数(ep)
    over = 超发归档行数(ep)
    print(f"[核账] 正式流水成功行={succ}  超发归档行={over}  日配额阈={阈值}")
    if succ > 阈值:
        print(f"[事故🔴] 配额已满仍超发：成功 {succ} > 配额 {阈值}（超发 {succ - 阈值} 笔）")
        return 2
    print("[安全🟢] 未超发：正式成功行 <= 日配额阈")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
