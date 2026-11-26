from __future__ import annotations
from app.db import 连接
from app.services.时间 import 此刻
from app.services import 投递, 配额


def 作废(事件号: str, 原因: str = "人工作废") -> dict:
    t = 此刻()
    conn = 连接()
    try:
        dl = conn.execute("SELECT * FROM 死信 WHERE 事件号=?", (事件号,)).fetchone()
        if not dl:
            raise ValueError("不在死信匣")
        if dl["状态"] != "待处置":
            raise ValueError("死信已处置")
        conn.execute("UPDATE 死信 SET 状态=? WHERE 事件号=?", ("已作废", 事件号))
        conn.execute("UPDATE 事件 SET 状态=?, 更新时=? WHERE 事件号=?", ("已作废", t, 事件号))
        conn.execute("INSERT INTO 操作留痕(时刻,操作,事件号,详情) VALUES (?,?,?,?)",
                     (t, "死信作废", 事件号, 原因))
        conn.commit()
        return dict(conn.execute("SELECT * FROM 死信 WHERE 事件号=?", (事件号,)).fetchone())
    finally:
        conn.close()


def 重投(事件号: str) -> dict:
    t = 此刻()
    conn = 连接()
    try:
        dl = conn.execute("SELECT * FROM 死信 WHERE 事件号=?", (事件号,)).fetchone()
        if not dl:
            raise ValueError("不在死信匣")
        if dl["状态"] != "待处置":
            raise ValueError("死信已处置")
        conn.execute("UPDATE 死信 SET 状态=? WHERE 事件号=?", ("已重投", 事件号))
        conn.execute("UPDATE 事件 SET 状态=?, 更新时=? WHERE 事件号=?", ("待投", t, 事件号))
        conn.execute("INSERT INTO 操作留痕(时刻,操作,事件号,详情) VALUES (?,?,?,?)",
                     (t, "死信重投", 事件号, "回待投"))
        conn.commit()
    finally:
        conn.close()
    try:
        return 投递.投递一次(事件号)
    except 配额.配额已满 as e:
        # 撞日配额硬阈：重投未被放行，把死信匣卡片回补为待处置并写明配额拒因，避免丢卡。
        t2 = 此刻()
        c2 = 连接()
        try:
            c2.execute(
                "UPDATE 死信 SET 状态='待处置', 末次原因=? WHERE 事件号=?",
                (f"配额闸口未放行：{e}", 事件号),
            )
            c2.execute(
                "UPDATE 事件 SET 状态='死信', 更新时=? WHERE 事件号=?",
                (t2, 事件号),
            )
            c2.execute(
                "INSERT INTO 操作留痕(时刻,操作,事件号,详情) VALUES (?,?,?,?)",
                (t2, "重投回退", 事件号, "日配额已满，死信卡回匣待处置"),
            )
            c2.commit()
        finally:
            c2.close()
        raise


def 列表():
    from app.services import 签名目录
    return 签名目录.死信列表()
