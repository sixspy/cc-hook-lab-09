"""并发投递锁（已为 hook-09 投递配额硬阈接线）。

真正的锁注册表在 app.services.并发锁；此处原样再导出同一个函数对象，
因此 ``app.modules.并发投递锁.同锁`` 与 ``app.services.并发锁.同锁`` 共用一张锁表，
脚本/测试“打同锁”拿到的是同一把锁。
"""
from app.services.并发锁 import (  # noqa: F401
    NAME,
    同锁,
    端点日锁,
    锁键,
    活动锁键,
    清空锁表,
)
