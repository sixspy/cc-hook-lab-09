"""并发投递锁（hook-09 争用救绿服务）。

职责单一：为「端点 × 配额日」提供进程内同一把互斥锁。
投递服务在这把锁内完成「读日配额已用 → 判定 → 写正式尝试 / 超发归档 → 提交」，
由此杜绝满额瞬间两个请求都读到余量 1 而双双成功的超发。

注意：这是同一进程内多线程（Flask dev server / pytest 线程）“打同锁”的那把锁；
投递事务另用 BEGIN IMMEDIATE 兜底，跨连接也不会把配额读穿。
"""
from __future__ import annotations

import threading

NAME = "并发投递锁"

_锁注册表: dict[str, "threading.RLock"] = {}
_注册表守卫 = threading.Lock()


def 锁键(端点键: str, 日: str) -> str:
    return f"投递配额:{端点键}:{日}"


def 同锁(键: str) -> "threading.RLock":
    """同一键永远返回同一把可重入锁（“打同锁”）。"""
    with _注册表守卫:
        lk = _锁注册表.get(键)
        if lk is None:
            lk = threading.RLock()
            _锁注册表[键] = lk
        return lk


def 端点日锁(端点键: str, 日: str) -> "threading.RLock":
    return 同锁(锁键(端点键, 日))


def 活动锁键() -> list[str]:
    """供自检脚本/诊断接口核对当前注册表。"""
    with _注册表守卫:
        return sorted(_锁注册表.keys())


def 清空锁表() -> None:
    """仅测试隔离使用。"""
    with _注册表守卫:
        _锁注册表.clear()
