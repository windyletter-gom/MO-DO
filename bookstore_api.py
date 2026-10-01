"""
MO-DO 서점 발주·판매 API (server_stdlib.py 에서 import)

연결 방법 (server_stdlib.py 수정 3곳):
  1) import 줄:                       import bookstore_api
  2) main() 의 ensure_schema() 뒤:    bookstore_api.ensure_schema(db); bookstore_api.start_scheduler(db)
  3) do_GET  의 `with db() as c:` 안 첫 줄:   if bookstore_api.handle_get(self, c, p, q): return
     do_POST 의 `with db() as c:` 안 첫 줄:   if bookstore_api.handle_post(self, c, p, x): return

엔드포인트
  GET  /api/bookstore/status                          수집 상태(서점별 마지막 수집, 건수, 날짜 범위)
  GET  /api/bookstore/orders?from=&to=&shop=          발주 행 (교보/알라딘)
  GET  /api/bookstore/sales?from=&to=                 알라딘 실제 판매 행 (일 단위)
  POST /api/bookstore/collect            {actor_id, shop:"kyobo"|"aladin"}            즉시 수집 (관리자/서비스운영)
  POST /api/bookstore/collect/aladin-sales {actor_id, from, to}                        알라딘 판매 백필
  POST /api/bookstore/collect/yes24      {actor_id} → needs_code / {actor_id, code}    YES24 2단계 (어댑터 완성 후)

자동 수집: 환경변수 MODO_BOOKSTORE_AUTOCOLLECT=1 이면 매일 MODO_BOOKSTORE_HOUR(기본 5, KST)시에
교보 발주(최근 7일) + 알라딘 당일 발주 + 알라딘 전일 판매를 백그라운드 스레드로 수집한다.
자격증명 환경변수: KYOBO_SCM_ID/PW, ALADIN_SCM_ID/PW (Render → Environment 에 등록)
"""
from __future__ import annotations

import os
import threading
import time
import traceback
from datetime import date, datetime, timedelta, timezone

import scm_collect as sc

KST = timezone(timedelta(hours=9))
_lock = threading.Lock()          # 수집 동시 실행 방지


# ------------------------------------------------------------------ 스키마
def ensure_schema(db):
    with db() as c:
        c.executescript(sc.SCHEMA)


# ------------------------------------------------------------------ 권한
def _can_collect(actor) -> bool:
    if not actor:
        return False
    return (actor["role"] in ("SUPER_ADMIN", "DIVISION_ADMIN") or actor["name"] in ("정해근", "이원행")
            or actor["team_id"] == 6)


# ------------------------------------------------------------------ GET
def handle_get(h, c, p: str, q: dict) -> bool:
    one = lambda k, d="": (q.get(k) or [d])[0]
    if p == "/api/bookstore/status":
        out = {"shops": {}, "auto": os.environ.get("MODO_BOOKSTORE_AUTOCOLLECT") == "1",
               "credentials": {s: bool(os.environ.get(f"{s.upper()}_SCM_ID") and os.environ.get(f"{s.upper()}_SCM_PW"))
                               for s in ("kyobo", "aladin", "yes24")}}
        for r in c.execute("SELECT shop, COUNT(*) n, SUM(qty) qty, MIN(order_date) d0, MAX(order_date) d1 "
                           "FROM bookstore_orders GROUP BY shop"):
            out["shops"][r["shop"]] = {"kind": "orders", "rows": r["n"], "qty": r["qty"], "from": r["d0"], "to": r["d1"]}
        r = c.execute("SELECT COUNT(*) n, SUM(qty) qty, MIN(sale_from) d0, MAX(sale_from) d1 FROM bookstore_sales").fetchone()
        if r and r["n"]:
            out["shops"]["aladin_sales"] = {"kind": "sales", "rows": r["n"], "qty": r["qty"], "from": r["d0"], "to": r["d1"]}
        out["log"] = [dict(x) for x in c.execute(
            "SELECT shop, d_from, d_to, rows, status, message, ran_at FROM bookstore_collect_log ORDER BY id DESC LIMIT 30")]
        h.send_json(out); return True

    if p == "/api/bookstore/orders":
        f, t, shop = one("from", "2000-01-01"), one("to", "2999-12-31"), one("shop")
        sql = ("SELECT shop, order_date, isbn, title, store, qty, supply_rate, ship_rate, price, amount, vendor, flag "
               "FROM bookstore_orders WHERE order_date BETWEEN ? AND ?")
        args = [f, t]
        if shop:
            sql += " AND shop=?"; args.append(shop)
        rows = [dict(r) for r in c.execute(sql + " ORDER BY order_date, shop, isbn", args)]
        h.send_json({"rows": rows, "count": len(rows)}); return True

    if p == "/api/bookstore/sales":
        f, t = one("from", "2000-01-01"), one("to", "2999-12-31")
        rows = [dict(r) for r in c.execute(
            "SELECT shop, sale_from, sale_to, isbn, title, qty, price FROM bookstore_sales "
            "WHERE sale_from BETWEEN ? AND ? ORDER BY sale_from, isbn", (f, t))]
        h.send_json({"rows": rows, "count": len(rows)}); return True
    return False


# ------------------------------------------------------------------ POST
def handle_post(h, c, p: str, x: dict) -> bool:
    if not p.startswith("/api/bookstore/collect"):
        return False
    actor = c.execute("SELECT * FROM users WHERE id=?", (x.get("actor_id"),)).fetchone()
    if not _can_collect(actor):
        h.send_json({"error": "관리자 또는 서비스운영팀만 수집을 실행할 수 있습니다."}, 403); return True
    if not _lock.acquire(blocking=False):
        h.send_json({"error": "다른 수집이 진행 중입니다. 잠시 후 다시 시도하세요."}, 409); return True
    try:
        if p == "/api/bookstore/collect":
            shop = x.get("shop")
            if shop not in ("kyobo", "aladin"):
                h.send_json({"error": "shop 은 kyobo 또는 aladin"}, 400); return True
            res = sc.collect(c, shop, days=int(x.get("days") or 7))
            if shop == "aladin" and res.get("ok"):
                y = date.today() - timedelta(days=1)
                res["sales"] = sc.collect_aladin_sales(c, y, y)
            h.send_json(res); return True

        if p == "/api/bookstore/collect/aladin-sales":
            f, t = date.fromisoformat(x["from"]), date.fromisoformat(x["to"])
            if (t - f).days > 400:
                h.send_json({"error": "한 번에 최대 400일까지"}, 400); return True
            h.send_json(sc.collect_aladin_sales(c, f, t)); return True

        if p == "/api/bookstore/collect/yes24":
            code = (x.get("code") or "").strip()
            if code and sc.PENDING_YES24.get("adapter"):
                ad = sc.PENDING_YES24["adapter"]
                try:
                    ad.complete_login(code)
                except sc.LoginFailed as e:
                    h.send_json({"error": str(e)}, 400); return True
                h.send_json(sc.collect(c, "yes24", adapter=ad)); return True
            h.send_json(sc.collect(c, "yes24")); return True   # needs_code:true 를 돌려주면 프론트가 인증번호 입력창을 띄움
        return False
    finally:
        _lock.release()


# ------------------------------------------------------------------ 자동 수집
def _run_daily(db):
    with _lock:
        with db() as c:
            print("[BOOKSTORE] 일일 수집 시작", flush=True)
            print("[BOOKSTORE] kyobo", sc.collect(c, "kyobo", days=7), flush=True)
            r = sc.collect(c, "aladin"); print("[BOOKSTORE] aladin", r, flush=True)
            y = date.today() - timedelta(days=1)
            print("[BOOKSTORE] aladin_sales", sc.collect_aladin_sales(c, y, y), flush=True)


def start_scheduler(db):
    if os.environ.get("MODO_BOOKSTORE_AUTOCOLLECT") != "1":
        return
    hour = int(os.environ.get("MODO_BOOKSTORE_HOUR", "5"))

    def loop():
        while True:
            now = datetime.now(KST)
            nxt = now.replace(hour=hour, minute=0, second=0, microsecond=0)
            if nxt <= now:
                nxt += timedelta(days=1)
            time.sleep((nxt - now).total_seconds())
            try:
                _run_daily(db)
            except Exception:  # noqa: BLE001
                traceback.print_exc()
    threading.Thread(target=loop, daemon=True, name="bookstore-collect").start()
    print(f"[BOOKSTORE] 자동 수집 예약: 매일 {hour:02d}:00 KST", flush=True)
