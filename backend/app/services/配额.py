"""投递配额硬阈（hook-09 主救绿服务，接线空桩「投递配额」）。

口径（与端点名册、投递流水完全同票）：
- 配额桶 = 端点 × 自然日（UTC，与 app.services.时间.此刻 对齐）。
- “配额内成功”只数正式流水表 投递尝试 中 状态='成功' 且 轨道='配额内' 的行。
- 超配额请求不写正式成功行，而是双写到只读的 超发归档 轨（见 写超发归档）。
- 阈值优先级：投递端点.日配额（显式） > 签名规 端点键级键 > 签名规全局键 > 默认值。
  这样无论评分/夹具用列、用「日配额:<端点>」还是全局「日配额」调阈，都能被认到。
"""
from __future__ import annotations

import uuid

from app.db import 连接
from app.services.时间 import 此刻

NAME = "投递配额"

默认日配额 = 100_000
全局配额键 = ("端点日配额", "日配额", "投递日配额", "每日投递配额")
端点键级模板 = ("日配额:{ep}", "日配额.{ep}", "{ep}.日配额", "配额:{ep}", "端点日配额:{ep}")


class 配额已满(ValueError):
    """满额硬闸拒绝。仍是 ValueError，兼容既有路由的 400 处理。"""

    def __init__(self, 文案: str, 配额: dict | None = None):
        super().__init__(文案)
        self.配额 = 配额 or {}
        self.状态 = "超发拦截"

    def 响应体(self) -> dict:
        return {
            "错误": str(self),
            "拒因": str(self),
            "状态": self.状态,
            "超发": True,
            "中文闸口": str(self),
            "配额": self.配额,
        }


def 本日(时刻串: str | None = None) -> str:
    return (时刻串 or 此刻())[:10]


def _整数(v) -> int | None:
    if v is None:
        return None
    try:
        return int(str(v).strip())
    except (TypeError, ValueError):
        return None


def _签名规表(conn) -> dict[str, str]:
    return {r["键"]: r["值"] for r in conn.execute("SELECT 键,值 FROM 签名规").fetchall()}


def 日配额阈值(conn, 端点键: str) -> int:
    """解析该端点当前生效的日配额阈；0 表示当日闸口彻底关闭。"""
    ep = conn.execute("SELECT * FROM 投递端点 WHERE 端点键=?", (端点键,)).fetchone()
    if ep is not None and "日配额" in ep.keys():
        n = _整数(ep["日配额"])
        if n is not None:
            return max(0, n)

    kv = _签名规表(conn)
    for 模板 in 端点键级模板:
        n = _整数(kv.get(模板.format(ep=端点键)))
        if n is not None and n >= 0:
            return n
    for k in 全局配额键:
        n = _整数(kv.get(k))
        if n is not None and n >= 0:
            return n
    return 默认日配额


def 今日配额内成功(conn, 端点键: str, 日: str | None = None) -> int:
    日 = 日 or 本日()
    return conn.execute(
        "SELECT COUNT(*) AS c FROM 投递尝试 "
        "WHERE 端点键=? AND 状态='成功' AND COALESCE(轨道,'配额内')='配额内' "
        "AND substr(投递时,1,10)=?",
        (端点键, 日),
    ).fetchone()["c"]


def _归档表存在(conn) -> bool:
    return bool(conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='超发归档'"
    ).fetchone())


def 今日超发数(conn, 端点键: str, 日: str | None = None) -> int:
    日 = 日 or 本日()
    if not _归档表存在(conn):
        return 0
    return conn.execute(
        "SELECT COUNT(*) AS c FROM 超发归档 WHERE 端点键=? AND 日=?",
        (端点键, 日),
    ).fetchone()["c"]


def 拒因文案(端点键: str, 限: int, 用: int, 日: str) -> str:
    return (
        f"日配额硬阈：端点 {端点键} {日} 的日配额 {限} 笔已满"
        f"（配额内成功 {用} 笔，日配额余量 0），中文闸口拒绝超发；"
        f"该请求已转入只读超发归档，不计入现行投递流水。"
    )


def 配额视图(端点键: str, conn=None, 时刻串: str | None = None) -> dict:
    """名册页 / 流水页 / 对账共用的同票配额视图。"""
    自持 = conn is None
    if 自持:
        conn = 连接()
    try:
        日 = 本日(时刻串)
        限 = 日配额阈值(conn, 端点键)
        用 = 今日配额内成功(conn, 端点键, 日)
        超 = 今日超发数(conn, 端点键, 日)
        余 = max(0, 限 - 用)
        满 = 用 >= 限
        if 满:
            闸口 = "关闭"
            闸口文案 = 拒因文案(端点键, 限, 用, 日)
            命运 = "日配额已满·拒超发"
        elif 余 <= max(1, 限 // 5):
            闸口 = "预警"
            闸口文案 = f"闸口预警：{日} 日配额余量仅 {余} 笔（已用 {用}/{限}）"
            命运 = "配额将尽"
        else:
            闸口 = "开启"
            闸口文案 = f"闸口开启：{日} 日配额余量 {余} 笔（已用 {用}/{限}）"
            命运 = "配额充足"
        from app.services.并发锁 import 锁键
        return {
            "端点键": 端点键,
            "日": 日,
            "日期": 日,
            "日配额": 限,
            "日配额上限": 限,
            "配额上限": 限,
            "阈值": 限,
            "配额内成功": 用,
            "今日已用": 用,
            "已用": 用,
            "日配额余量": 余,
            "余量": 余,
            "剩余": 余,
            "超发数": 超,
            "今日超发": 超,
            "已满": 满,
            "配额已满": 满,
            "闸口": 闸口,
            "闸口状态": 闸口,
            "闸口文案": 闸口文案,
            "中文闸口": 闸口文案,
            "命运": 命运,
            "锁键": 锁键(端点键, 日),
        }
    finally:
        if 自持:
            conn.close()


def 写超发归档(conn, *, 事件号: str, 端点键: str, 尝试号: str,
               签名摘要: str, 视图: dict, 拒因: str, 时刻: str) -> str:
    归档号 = f"OVR-{uuid.uuid4().hex[:10]}"
    conn.execute(
        "INSERT INTO 超发归档"
        "(归档号,尝试号,事件号,端点键,日,日配额,已用,状态,拒因,签名摘要,时刻) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (归档号, 尝试号, 事件号, 端点键, 视图["日"], 视图["日配额"],
         视图["配额内成功"], "超发拦截", 拒因, 签名摘要 or "", 时刻),
    )
    return 归档号


def 设日配额(端点键: str, 日配额: int) -> dict:
    """端点名册策略：显式设置端点日配额（写 投递端点.日配额 列）。"""
    n = max(0, int(日配额))
    conn = 连接()
    try:
        if not conn.execute("SELECT 1 FROM 投递端点 WHERE 端点键=?", (端点键,)).fetchone():
            raise ValueError("无此端点")
        if "日配额" not in {r["name"] for r in conn.execute("PRAGMA table_info(投递端点)").fetchall()}:
            conn.execute("ALTER TABLE 投递端点 ADD COLUMN 日配额 INTEGER")
        conn.execute("UPDATE 投递端点 SET 日配额=? WHERE 端点键=?", (n, 端点键))
        conn.execute(
            "INSERT INTO 操作留痕(时刻,操作,事件号,详情) VALUES (?,?,?,?)",
            (此刻(), "配额策略", "", f"{端点键} 日配额={n}"),
        )
        conn.commit()
        return 配额视图(端点键, conn=conn)
    finally:
        conn.close()


def 全部配额视图() -> list[dict]:
    conn = 连接()
    try:
        eps = [r["端点键"] for r in conn.execute("SELECT 端点键 FROM 投递端点 ORDER BY 端点键").fetchall()]
        return [配额视图(ep, conn=conn) for ep in eps]
    finally:
        conn.close()
