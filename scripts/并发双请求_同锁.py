#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""hook-09 收尾争用脚本：对「日配额=1」的同一端点，满额瞬间并发双 POST /api/投递。

验证“打同锁”：
- 两个请求在 Barrier 上同时冲出，竞争同一把 端点×日 锁（锁键见配额视图）；
- 结果必须恰有一笔 200 成功、一笔 400 配额硬闸拒绝（中文拒因）；
- 配额视图：配额内成功=1、日配额余量=0；超发归档=1；名册配额对账 同票=true。

用法：
  python scripts/并发双请求_同锁.py [base_url]
默认 base_url=http://127.0.0.1:8509 （与 docker compose 宿主端口一致）
"""
from __future__ import annotations

import json
import sys
import threading
import time
import urllib.error
import urllib.request
import urllib.parse
import uuid

BASE = (sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8509").rstrip("/")


def _req(method: str, path: str, body: dict | None = None, cookie: str = "", timeout: int = 20):
    data = None
    headers = {"Content-Type": "application/json; charset=utf-8"}
    if cookie:
        headers["Cookie"] = cookie
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    # urllib 要求 URL 为 ASCII，中文路由/查询键需百分号编码（保留 / ? = &）
    url = BASE + urllib.parse.quote(path, safe="/?=&%")
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode("utf-8")
            sc = r.headers.get_all("Set-Cookie") or []
            return r.status, (json.loads(raw) if raw else {}), "; ".join(c.split(";")[0] for c in sc)
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        try:
            payload = json.loads(raw)
        except Exception:
            payload = {"错误": raw}
        return e.code, payload, ""


def 等服务就绪(seconds: float = 30.0) -> None:
    deadline = time.time() + seconds
    last = None
    while time.time() < deadline:
        try:
            urllib.request.urlopen(BASE + "/login", timeout=3)
            return
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(0.5)
    raise SystemExit(f"服务未就绪：{BASE} ({last})")


def main() -> int:
    等服务就绪()
    st, login, setcookie = _req("POST", "/api/登录", {"账号": "admin", "密码": "123456"})
    cookie = setcookie or ""
    if st != 200:
        print("登录失败", st, login)
        return 3
    print(f"[登录] {st} cookie={cookie[:24]}...")

    tag = uuid.uuid4().hex[:8]
    ep = f"ep-http-lock-{tag}"
    st, rep, _ = _req("POST", "/api/端点", {
        "端点键": ep, "名称": "HTTP争用同锁端点",
        "回调URL": "https://example.local/hooks/lock", "密钥版本": "v1",
    }, cookie)
    assert st == 200, (st, rep)
    st, q, _ = _req("POST", "/api/端点/配额", {"端点键": ep, "日配额": 1}, cookie)
    assert st == 200, (st, q)
    print(f"[配额] {ep} 日配额=1 锁键={q.get('锁键')}")

    eids = []
    for i in (1, 2):
        eid = f"EVT-LOCK-{tag}-{i}"
        st, ev, _ = _req("POST", "/api/事件/入队", {
            "事件号": eid, "事件类型": "invoice.paid", "端点键": ep,
            "载荷摘要": f"lock-{i}", "优先级": "普通",
        }, cookie)
        assert st == 200, (st, ev)
        eids.append(eid)

    barrier = threading.Barrier(2)
    outcomes: dict[str, tuple[int, dict]] = {}

    def worker(name: str, eid: str):
        barrier.wait()  # 满额瞬间：两请求同时冲出，打同一把端点日锁
        outcomes[name] = _req("POST", "/api/投递", {"事件号": eid}, cookie)[:2]

    t1 = threading.Thread(target=worker, args=("甲", eids[0]))
    t2 = threading.Thread(target=worker, args=("乙", eids[1]))
    t1.start(); t2.start(); t1.join(25); t2.join(25)

    for name in ("甲", "乙"):
        code, payload = outcomes[name]
        print(f"[并发-{name}] HTTP {code} 状态={payload.get('状态')} "
              f"错误/拒因={payload.get('错误') or payload.get('拒因') or ''}")

    codes = sorted(outcomes[n][0] for n in outcomes)
    ok200 = [n for n in outcomes if outcomes[n][0] == 200]
    reject = [outcomes[n][1] for n in outcomes if outcomes[n][0] == 400]

    st, view, _ = _req("GET", f"/api/配额/{ep}", cookie=cookie)
    print(f"[余量] 配额内成功={view.get('配额内成功')} 日配额余量={view.get('日配额余量')} "
          f"已满={view.get('已满')} 锁键={view.get('锁键')}")
    print(f"[闸口] {view.get('闸口文案')}")

    st, arch, _ = _req("GET", f"/api/超发归档?端点键={ep}", cookie=cookie)
    print(f"[归档] 超发归档 {len(arch)} 笔；首条拒因={arch[0]['拒因'] if arch else '—'}")

    st, tie, _ = _req("GET", f"/api/端点/配额对账?端点键={ep}", cookie=cookie)
    print(f"[对账] 同票={tie.get('同票')} 结论={tie.get('对账结论')} "
          f"(联查内 {tie.get('联查配额内成功')}/视图片 {tie.get('配额内成功')}, "
          f"联查超 {tie.get('联查超发数')}/视图片 {tie.get('超发数')})")

    failures = []
    if codes != [200, 400]:
        failures.append(f"应恰为 200+400，实际状态码 {codes}")
    if len(ok200) != 1:
        failures.append(f"成功请求应恰 1 个，实际 {len(ok200)}")
    if len(reject) != 1 or "配额" not in str(reject[0].get("错误", "")):
        failures.append("应有 1 个含中文配额拒因的 400")
    if view.get("配额内成功") != 1 or int(view.get("日配额余量", -1)) != 0:
        failures.append("配额视图应为 已用1/余量0")
    if len(arch) != 1:
        failures.append(f"超发归档应 1 笔，实际 {len(arch)}")
    if tie.get("同票") is not True:
        failures.append("名册配额对账必须同票=true")

    if failures:
        print("\n[争用结果🔴] " + " ; ".join(failures))
        return 1
    print("\n[争用结果🟢] 同锁生效：满额瞬间双请求仅一成一拒，无超发，名册/流水/归档同票。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
