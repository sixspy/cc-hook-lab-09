from __future__ import annotations
from app.db import 连接
from app.services import 校验, 配额
from app.services.时间 import 此刻


def 登记(body: dict) -> dict:
    端点键 = 校验.非空("端点键", body.get("端点键"))
    名称 = 校验.非空("名称", body.get("名称"))
    url = 校验.非空("回调URL", body.get("回调URL"))
    版本 = str(body.get("密钥版本") or "v1")
    说明 = str(body.get("说明") or "")
    conn = 连接()
    try:
        if conn.execute("SELECT 1 FROM 投递端点 WHERE 端点键=?", (端点键,)).fetchone():
            raise ValueError("端点键已存在")
        conn.execute(
            "INSERT INTO 投递端点(端点键,名称,回调URL,密钥版本,启用,说明) VALUES (?,?,?,?,1,?)",
            (端点键, 名称, url, 版本, 说明),
        )
        conn.execute("INSERT INTO 操作留痕(时刻,操作,事件号,详情) VALUES (?,?,?,?)",
                     (此刻(), "登记端点", "", 端点键))
        conn.commit()
        return dict(conn.execute("SELECT * FROM 投递端点 WHERE 端点键=?", (端点键,)).fetchone())
    finally:
        conn.close()


def 改启用(端点键: str, 启用: bool) -> dict:
    conn = 连接()
    try:
        if not conn.execute("SELECT 1 FROM 投递端点 WHERE 端点键=?", (端点键,)).fetchone():
            raise ValueError("无此端点")
        conn.execute("UPDATE 投递端点 SET 启用=? WHERE 端点键=?", (1 if 启用 else 0, 端点键))
        conn.commit()
        return dict(conn.execute("SELECT * FROM 投递端点 WHERE 端点键=?", (端点键,)).fetchone())
    finally:
        conn.close()


def 设配额(端点键: str, 日配额: int) -> dict:
    """端点名册策略：设置端点日配额硬阈。"""
    return 配额.设日配额(端点键, 日配额)


def 配额对账(端点键: str | None = None, 日: str | None = None):
    """端点名册 × 正式流水 × 只读超发归档 三账联查对账（救乙红的专属服务）。

    传 端点键 -> 返回该端点对账 dict；不传 -> 返回全部端点对账 list。
    “同票”要求联查口径与配额视图口径完全一致，且余量=阈-配额内成功。
    """
    日 = 日 or 配额.本日()
    conn = 连接()
    try:
        if 端点键:
            sql = """
                SELECT e.*,
                       COALESCE(s.配额内成功, 0) AS 联查配额内成功,
                       COALESCE(o.超发数, 0)     AS 联查超发数
                FROM 投递端点 e
                LEFT JOIN (
                    SELECT 端点键, COUNT(*) AS 配额内成功
                    FROM 投递尝试
                    WHERE 状态='成功' AND COALESCE(轨道,'配额内')='配额内'
                      AND substr(投递时,1,10)=?
                    GROUP BY 端点键
                ) s ON s.端点键 = e.端点键
                LEFT JOIN (
                    SELECT 端点键, COUNT(*) AS 超发数
                    FROM 超发归档 WHERE 日=? GROUP BY 端点键
                ) o ON o.端点键 = e.端点键
                WHERE e.端点键 = ?
            """
            row = conn.execute(sql, (日, 日, 端点键)).fetchone()
            if not row:
                raise ValueError("无此端点")
            rows = [row]
        else:
            sql = """
                SELECT e.*,
                       COALESCE(s.配额内成功, 0) AS 联查配额内成功,
                       COALESCE(o.超发数, 0)     AS 联查超发数
                FROM 投递端点 e
                LEFT JOIN (
                    SELECT 端点键, COUNT(*) AS 配额内成功
                    FROM 投递尝试
                    WHERE 状态='成功' AND COALESCE(轨道,'配额内')='配额内'
                      AND substr(投递时,1,10)=?
                    GROUP BY 端点键
                ) s ON s.端点键 = e.端点键
                LEFT JOIN (
                    SELECT 端点键, COUNT(*) AS 超发数
                    FROM 超发归档 WHERE 日=? GROUP BY 端点键
                ) o ON o.端点键 = e.端点键
                ORDER BY e.端点键
            """
            rows = conn.execute(sql, (日, 日)).fetchall()

        out: list[dict] = []
        for r in rows:
            d = dict(r)
            ep = d["端点键"]
            view = 配额.配额视图(ep, conn=conn, 时刻串=日 + "T23:59:59Z")
            联内 = int(d.pop("联查配额内成功", 0))
            联超 = int(d.pop("联查超发数", 0))
            d.update(view)  # 以统一配额视图覆盖/补齐阈值、余量、中文闸口等
            余量恒等 = int(view["日配额余量"]) == max(0, int(view["日配额"]) - int(view["配额内成功"]))
            d["联查配额内成功"] = 联内
            d["联查超发数"] = 联超
            d["同票"] = bool(
                联内 == int(view["配额内成功"])
                and 联超 == int(view["超发数"])
                and 余量恒等
            )
            d["对账结论"] = (
                "名册阈值、正式流水、超发归档同票对齐"
                if d["同票"] else
                "名册与流水/归档口径撕裂，需核账"
            )
            out.append(d)

        if 端点键:
            return out[0]
        return out
    finally:
        conn.close()
