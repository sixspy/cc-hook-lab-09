from __future__ import annotations
from app.db import 连接
from app.services import 校验, 配额


def 签名规() -> dict:
    conn = 连接()
    try:
        rows = conn.execute("SELECT 键,值 FROM 签名规").fetchall()
        return {r["键"]: r["值"] for r in rows}
    finally:
        conn.close()


def 改签名规(body: dict) -> dict:
    conn = 连接()
    try:
        for k, v in body.items():
            键 = 校验.非空("键", k)
            值 = 校验.非空("值", v)
            conn.execute(
                "INSERT INTO 签名规(键,值) VALUES (?,?) ON CONFLICT(键) DO UPDATE SET 值=excluded.值",
                (键, 值),
            )
        conn.commit()
        return 签名规()
    finally:
        conn.close()


def 概览() -> dict:
    conn = 连接()
    try:
        def c(sql):
            return conn.execute(sql).fetchone()[0]
        return {
            "端点数": c("SELECT COUNT(*) FROM 投递端点"),
            "启用端点": c("SELECT COUNT(*) FROM 投递端点 WHERE 启用=1"),
            "事件总数": c("SELECT COUNT(*) FROM 事件"),
            "待投": c("SELECT COUNT(*) FROM 事件 WHERE 状态='待投'"),
            "投递中": c("SELECT COUNT(*) FROM 事件 WHERE 状态='投递中'"),
            "已投递": c("SELECT COUNT(*) FROM 事件 WHERE 状态='已投递'"),
            "死信": c("SELECT COUNT(*) FROM 事件 WHERE 状态='死信'"),
            "尝试数": c("SELECT COUNT(*) FROM 投递尝试"),
            "死信匣": c("SELECT COUNT(*) FROM 死信 WHERE 状态='待处置'"),
            "签名规": 签名规(),
        }
    finally:
        conn.close()


def 事件列表() -> list:
    conn = 连接()
    try:
        return [dict(r) for r in conn.execute("SELECT * FROM 事件 ORDER BY 入队时 DESC").fetchall()]
    finally:
        conn.close()


def 事件一条(事件号: str) -> dict | None:
    conn = 连接()
    try:
        row = conn.execute("SELECT * FROM 事件 WHERE 事件号=?", (事件号,)).fetchone()
        if not row:
            return None
        d = dict(row)
        d["尝试"] = [dict(x) for x in conn.execute(
            "SELECT * FROM 投递尝试 WHERE 事件号=? ORDER BY 投递时 DESC", (事件号,)
        ).fetchall()]
        dl = conn.execute("SELECT * FROM 死信 WHERE 事件号=?", (事件号,)).fetchone()
        d["死信"] = dict(dl) if dl else None
        return d
    finally:
        conn.close()


def 投递列表() -> list:
    conn = 连接()
    try:
        # 双写两轨形态：现行流水只读「配额内」集；超配额请求在 超发归档 轨，读集互斥。
        return [dict(r) for r in conn.execute(
            "SELECT * FROM 投递尝试 "
            "WHERE COALESCE(轨道,'配额内')='配额内' "
            "ORDER BY 投递时 DESC LIMIT 200"
        ).fetchall()]
    finally:
        conn.close()


def 死信列表() -> list:
    conn = 连接()
    try:
        return [dict(r) for r in conn.execute("SELECT * FROM 死信 ORDER BY 入匣时 DESC").fetchall()]
    finally:
        conn.close()


def 端点列表() -> list:
    """端点名册：端点原档 LEFT JOIN 当日配额/流水/超发口径（联查同票）。"""
    conn = 连接()
    try:
        rows = [dict(r) for r in conn.execute(
            "SELECT * FROM 投递端点 ORDER BY 端点键"
        ).fetchall()]
        for d in rows:
            try:
                view = 配额.配额视图(d["端点键"], conn=conn)
                d.update(view)  # 用生效阈值/余量/中文闸口覆盖可空原始列
            except Exception:
                # 读模型不应因配额口径异常拖垮名册；原始端点档仍返回。
                pass
        return rows
    finally:
        conn.close()


def 超发归档列表(端点键: str | None = None) -> list:
    """只读第二轨：超配额请求归档。现行 /api/投递 流水绝不读这张表。"""
    conn = 连接()
    try:
        has = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='超发归档'"
        ).fetchone()
        if not has:
            return []
        if 端点键:
            rows = conn.execute(
                "SELECT * FROM 超发归档 WHERE 端点键=? ORDER BY 时刻 DESC, 归档号 DESC LIMIT 200",
                (端点键,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM 超发归档 ORDER BY 时刻 DESC, 归档号 DESC LIMIT 200"
            ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def 互证列表() -> list:
    conn = 连接()
    try:
        return [dict(r) for r in conn.execute("SELECT * FROM 互证批次 ORDER BY 批次号 DESC LIMIT 50").fetchall()]
    finally:
        conn.close()
