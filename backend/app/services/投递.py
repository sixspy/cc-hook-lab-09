from __future__ import annotations
import hashlib
import hmac
import uuid

from app.db import 连接
from app.services.时间 import 此刻
from app.services import 配额, 并发锁


def _签名(密钥版本: str, 事件号: str, 摘要: str) -> str:
    raw = f"{密钥版本}|{事件号}|{摘要}".encode("utf-8")
    key = f"hook-secret-{密钥版本}".encode("utf-8")
    return hmac.new(key, raw, hashlib.sha256).hexdigest()[:24]


def _最大重试(conn) -> int:
    row = conn.execute("SELECT 值 FROM 签名规 WHERE 键='最大重试'").fetchone()
    try:
        return int(row["值"]) if row else 3
    except (TypeError, ValueError):
        return 3


def 投递一次(事件号: str, 强制失败: bool = False) -> dict:
    """对事件执行一次签名投递。

    hook-09 起接入投递配额硬阈（双写两轨形态）：
    - 同一「端点 × 日」先打同一把进程锁，再用 BEGIN IMMEDIATE 把
      「读日配额已用 → 闸口判定 → 写尝试/归档 → 提交」封成一个原子临界区；
    - 配额内：写正式 投递尝试（轨道=配额内），成功则事件转已投递；
    - 满额：不写正式成功行，双写只读 超发归档 并抛 配额已满（中文拒因）。
    """
    t = 此刻()
    尝试号 = f"TRY-{uuid.uuid4().hex[:10]}"
    conn = 连接()
    # 显式事务模式：由我们自己发 BEGIN IMMEDIATE，避免驱动隐式事务与“读穿配额”。
    conn.isolation_level = None
    拒绝: 配额.配额已满 | None = None
    try:
        ev0 = conn.execute("SELECT * FROM 事件 WHERE 事件号=?", (事件号,)).fetchone()
        if not ev0:
            raise ValueError("无此事件")
        端点键 = ev0["端点键"]
        日 = t[:10]

        with 并发锁.端点日锁(端点键, 日):
            try:
                conn.execute("BEGIN IMMEDIATE")
                ev = conn.execute("SELECT * FROM 事件 WHERE 事件号=?", (事件号,)).fetchone()
                if not ev:
                    raise ValueError("无此事件")
                if ev["状态"] in ("已投递", "死信"):
                    raise ValueError(f"事件状态为{ev['状态']}，不可再投")
                ep = conn.execute(
                    "SELECT * FROM 投递端点 WHERE 端点键=?", (ev["端点键"],)
                ).fetchone()
                if not ep:
                    raise ValueError("端点缺失")
                if int(ep["启用"]) != 1:
                    raise ValueError("端点停用")

                sig = _签名(ep["密钥版本"], 事件号, ev["载荷摘要"])
                视图 = 配额.配额视图(端点键, conn=conn, 时刻串=t)

                # —— 硬阈闸口：满额只走超发归档轨，绝不写正式成功行 ——
                if 视图["已满"]:
                    原因 = 视图["闸口文案"]
                    配额.写超发归档(
                        conn,
                        事件号=事件号,
                        端点键=端点键,
                        尝试号=尝试号,
                        签名摘要=sig,
                        视图=视图,
                        拒因=原因,
                        时刻=t,
                    )
                    conn.execute(
                        "INSERT INTO 操作留痕(时刻,操作,事件号,详情) VALUES (?,?,?,?)",
                        (t, "配额拒投", 事件号, f"{尝试号} {原因}"),
                    )
                    conn.commit()
                    拒绝 = 配额.配额已满(原因, 视图)
                else:
                    最大 = _最大重试(conn)
                    失败次数 = conn.execute(
                        "SELECT COUNT(*) AS c FROM 投递尝试 "
                        "WHERE 事件号=? AND 状态='失败' AND COALESCE(轨道,'配额内')='配额内'",
                        (事件号,),
                    ).fetchone()["c"]
                    成功 = (not 强制失败) and (失败次数 < 1000)
                    # 模拟：事件类型含 .fail 或 body 强制 → 失败
                    if 强制失败 or str(ev["事件类型"]).endswith(".fail"):
                        成功 = False

                    if 成功:
                        conn.execute(
                            "INSERT INTO 投递尝试(尝试号,事件号,端点键,签名摘要,状态,轨道,响应码,说明,投递时) "
                            "VALUES (?,?,?,?,?,?,?,?,?)",
                            (尝试号, 事件号, ev["端点键"], sig, "成功", "配额内", 200, "投递成功（计入日配额）", t),
                        )
                        conn.execute(
                            "UPDATE 事件 SET 状态=?, 更新时=? WHERE 事件号=?",
                            ("已投递", t, 事件号),
                        )
                        conn.execute(
                            "INSERT INTO 操作留痕(时刻,操作,事件号,详情) VALUES (?,?,?,?)",
                            (t, "投递成功", 事件号, 尝试号),
                        )
                        conn.commit()
                        return {
                            "尝试号": 尝试号,
                            "状态": "成功",
                            "事件状态": "已投递",
                            "签名摘要": sig,
                            "配额": 视图,
                        }

                    # 失败（技术失败不占“成功配额”，事件保持可重试）
                    conn.execute(
                        "INSERT INTO 投递尝试(尝试号,事件号,端点键,签名摘要,状态,轨道,响应码,说明,投递时) "
                        "VALUES (?,?,?,?,?,?,?,?,?)",
                        (尝试号, 事件号, ev["端点键"], sig, "失败", "配额内", 500, "投递失败", t),
                    )
                    新失败 = 失败次数 + 1
                    if 新失败 >= 最大:
                        dl = f"DL-{uuid.uuid4().hex[:10]}"
                        if not conn.execute(
                            "SELECT 1 FROM 死信 WHERE 事件号=?", (事件号,)
                        ).fetchone():
                            conn.execute(
                                "INSERT INTO 死信(死信号,事件号,端点键,失败次数,末次原因,状态,入匣时) "
                                "VALUES (?,?,?,?,?,?,?)",
                                (dl, 事件号, ev["端点键"], 新失败, "重试耗尽", "待处置", t),
                            )
                        conn.execute(
                            "UPDATE 事件 SET 状态=?, 更新时=? WHERE 事件号=?",
                            ("死信", t, 事件号),
                        )
                        事件状态 = "死信"
                    else:
                        conn.execute(
                            "UPDATE 事件 SET 状态=?, 更新时=? WHERE 事件号=?",
                            ("投递中", t, 事件号),
                        )
                        事件状态 = "投递中"
                    conn.execute(
                        "INSERT INTO 操作留痕(时刻,操作,事件号,详情) VALUES (?,?,?,?)",
                        (t, "投递失败", 事件号, f"失败{新失败}/{最大}"),
                    )
                    conn.commit()
                    return {
                        "尝试号": 尝试号,
                        "状态": "失败",
                        "事件状态": 事件状态,
                        "失败次数": 新失败,
                        "签名摘要": sig,
                        "配额": 视图,
                    }
            except Exception:
                conn.rollback()
                raise

            # 满额拒绝：归档已提交，此刻在事务保护之外抛中文闸口异常。
            if 拒绝 is not None:
                raise 拒绝
    finally:
        conn.close()
