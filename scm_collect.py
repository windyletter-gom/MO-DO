"""
MO-DO 서점 SCM 발주내역 직접 수집 모듈 (표준 라이브러리만 사용)

구조
  SCMClient            쿠키 세션 + GET/POST 헬퍼 (urllib)
  KyoboAdapter         교보 SCM  (자동)
  AladinAdapter        알라딘 공급사 포털 (자동)
  Yes24Adapter         YES24 SCM (휴대폰 인증번호 2단계 → 담당자 입력 필요)
  upsert_orders()      bookstore_orders 테이블 적재 (external_key 기준 중복 제거)
  collect()            어댑터 실행 → 적재, server_stdlib.py 에서 호출

교보·알라딘 어댑터는 2026-10-01 HAR 캡처로 확인한 실제 요청/응답 구조에 맞춰 작성됨.
  교보  : WebSquare JSON API (scmLogin.action → findPlorDate → findRdpCode → findTotalPlorList)
  알라딘: ASP.NET 폼. 발주내역(wbackorder, 당일) + 판매 도서 통계(wStatSalesBook, 기간별 실제 판매권수)
YES24 어댑터는 휴대폰 인증 캡처가 아직 없어 TODO 상태.
자격증명은 환경변수에서만 읽고, 로그에 남기지 않는다.

환경변수
  KYOBO_SCM_ID / KYOBO_SCM_PW
  ALADIN_SCM_ID / ALADIN_SCM_PW
  YES24_SCM_ID / YES24_SCM_PW
"""
from __future__ import annotations

import gzip
import http.cookiejar
import json
import os
import re
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass, asdict
from datetime import date, timedelta
from typing import Iterable

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/128.0 Safari/537.36")


# ---------------------------------------------------------------- 데이터 모델
@dataclass
class OrderRow:
    shop: str            # kyobo | yes24 | aladin
    order_date: str      # YYYY-MM-DD
    isbn: str
    title: str
    store: str           # 교보: 점포명/인터넷교보, YES24: 센터, 알라딘: 물류/중고매장
    qty: int
    supply_rate: float | None = None   # 공급율 %
    ship_rate: float | None = None     # 출고율 % (알라딘)
    price: int | None = None           # 정가
    amount: int | None = None          # 발주금액
    external_key: str = ""             # 서점 측 발주번호 등 행 식별자 (없으면 해시)
    vendor: str = ""                   # 교보: 거래처명(모아교육그룹 / 모아팩토리)
    flag: str = ""                     # 교보: '고객'=고객주문 포함

    def key(self) -> str:
        if self.external_key:
            return f"{self.shop}:{self.external_key}"
        return f"{self.shop}:{self.order_date}:{self.isbn}:{self.store}:{self.qty}"


@dataclass
class SaleRow:
    """알라딘 '판매 도서 통계' — 서점이 실제로 판매한 권수 (발주가 아닌 판매)"""
    shop: str
    sale_from: str      # YYYY-MM-DD (기간 시작)
    sale_to: str        # YYYY-MM-DD (기간 끝, 일 단위 수집이면 from==to)
    isbn: str
    title: str
    qty: int
    price: int | None = None

    def key(self) -> str:
        return f"{self.shop}:{self.sale_from}:{self.sale_to}:{self.isbn}"


# ---------------------------------------------------------------- HTTP 세션
class SCMClient:
    """쿠키를 유지하는 최소 HTTP 클라이언트. 요청 간 지연을 두어 서버에 부담을 주지 않는다."""

    def __init__(self, base: str, delay: float = 1.0):
        self.base = base.rstrip("/")
        self.delay = delay
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.jar))
        self._last = 0.0

    def _throttle(self):
        wait = self.delay - (time.time() - self._last)
        if wait > 0:
            time.sleep(wait)
        self._last = time.time()

    def request(self, path: str, data: dict | str | bytes | None = None,
                json_body: dict | None = None, headers: dict | None = None,
                method: str | None = None, timeout: int = 30) -> tuple[int, bytes, dict]:
        self._throttle()
        url = path if path.startswith("http") else self.base + path
        h = {"User-Agent": UA, "Accept": "*/*", "Accept-Encoding": "gzip"}
        if headers:
            h.update(headers)
        body = None
        if json_body is not None:
            body = json.dumps(json_body).encode()
            h.setdefault("Content-Type", "application/json")
        elif isinstance(data, dict):
            body = urllib.parse.urlencode(data).encode()
            h.setdefault("Content-Type", "application/x-www-form-urlencoded")
        elif isinstance(data, str):
            body = data.encode()
        elif isinstance(data, bytes):
            body = data
        req = urllib.request.Request(url, data=body, headers=h,
                                     method=method or ("POST" if body is not None else "GET"))
        try:
            with self.opener.open(req, timeout=timeout) as r:
                raw = r.read()
                if r.headers.get("Content-Encoding") == "gzip":
                    raw = gzip.decompress(raw)
                return r.status, raw, dict(r.headers)
        except urllib.error.HTTPError as e:
            raw = e.read()
            if e.headers.get("Content-Encoding") == "gzip":
                raw = gzip.decompress(raw)
            return e.code, raw, dict(e.headers)

    def get(self, path, **kw):
        return self.request(path, method="GET", **kw)

    def post(self, path, **kw):
        return self.request(path, method="POST", **kw)

    def text(self, raw: bytes, encoding: str = "utf-8") -> str:
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            return raw.decode("cp949", errors="replace")


class NeedsAuthCode(Exception):
    """YES24: 인증번호가 필요한 상태. 세션을 보관해두고 담당자 입력을 기다린다."""


class LoginFailed(Exception):
    pass


# ---------------------------------------------------------------- 어댑터 공통
class BaseAdapter:
    shop = ""
    base = ""

    def __init__(self):
        self.client = SCMClient(self.base)
        self.user = os.environ.get(f"{self.shop.upper()}_SCM_ID", "")
        self.pw = os.environ.get(f"{self.shop.upper()}_SCM_PW", "")
        if not self.user or not self.pw:
            raise LoginFailed(f"{self.shop}: 환경변수 {self.shop.upper()}_SCM_ID/PW 없음")

    def login(self) -> None:
        raise NotImplementedError

    def fetch(self, d_from: date, d_to: date) -> Iterable[OrderRow]:
        raise NotImplementedError

    # 공통 유틸
    @staticmethod
    def _num(s) -> int:
        return int(re.sub(r"[^\d-]", "", str(s) or "0") or 0)

    @staticmethod
    def _pct(s) -> float | None:
        s = re.sub(r"[^\d.]", "", str(s) or "")
        return float(s) if s else None

    @staticmethod
    def _date(s) -> str:
        s = re.sub(r"[^\d]", "", str(s))[:8]
        return f"{s[:4]}-{s[4:6]}-{s[6:8]}" if len(s) == 8 else ""


# ---------------------------------------------------------------- 교보문고
class KyoboAdapter(BaseAdapter):
    """
    https://scm.kyobobook.co.kr/scm  (WebSquare + JSON .action API, 세션 쿠키)
    로그인 ID = 사업자번호 10자리. 로그인 응답 dl_vndr 에 거래처가 여러 개면(모아교육그룹/모아팩토리) 전부 수집.
    발주는 '발주일자' 단위로만 조회된다. findPlorDate 가 발주가 있는 날짜 목록을 주므로 그 날짜만 돈다.
    """
    shop = "kyobo"
    base = "https://scm.kyobobook.co.kr"
    JSON_HDR = {"Accept": "application/json", "Content-Type": 'application/json; charset="UTF-8"'}
    RDP_LABEL = {"009": "점포", "050": "인터넷교보", "106": "기타(106)", "008": "납품"}

    def __init__(self):
        super().__init__()
        self.vendors: list[dict] = []

    def _json(self, path: str, payload: dict | None, referer: str = "plorInfo") -> dict:
        st, raw, _ = self.client.request(path, json_body=payload if payload is not None else None,
                                         data=b"" if payload is None else None, method="POST",
                                         headers={**self.JSON_HDR,
                                                  "Referer": f"{self.base}/scm/page.action?pageID={referer}"})
        if st != 200:
            raise RuntimeError(f"kyobo {path}: HTTP {st}")
        try:
            return json.loads(raw.decode("utf-8"))
        except Exception as e:  # noqa: BLE001
            raise RuntimeError(f"kyobo {path}: JSON 아님 ({e})")

    def login(self):
        self.client.get("/scm/login.action")  # 세션 쿠키 발급
        res = self._json("/scm/scmLogin.action",
                         {"dm_login": {"userId": self.user, "password": self.pw, "loginYn": ""}}, "login")
        if res.get("dm_login", {}).get("loginYn") != "Y":
            raise LoginFailed("kyobo: 로그인 실패 (loginYn != Y)")
        self.vendors = res.get("dl_vndr") or [res.get("dm_user", {})]
        self.client.get("/scm/page.action?pageID=main")

    def order_dates(self, vndr_code: str) -> list[str]:
        """해당 거래처의 발주 존재 일자(YYYYMMDD) 목록 — 서버가 최근 일자만 돌려준다."""
        self._json("/scm/scmMain.action", {"dm_vndrInfo": {"vndrCode": vndr_code}}, "main")
        self.client.get("/scm/page.action?pageID=plorInfo")
        res = self._json("/scm/order/findPlorDate.action", None)
        return [d["code"] for d in res.get("ds_FindPlorDate", [])]

    def rdp_codes(self, vndr_code: str, plor_date: str) -> list[dict]:
        res = self._json("/scm/order/findRdpCode.action", {"dm_SearchMap": {
            "vndrCode": vndr_code, "plorDate": plor_date, "findRdpCode": "", "plorGb": ""}})
        return res.get("ds_rdpCode", [])

    def order_list(self, vndr_code: str, plor_date: str, rdp_code: str) -> list[dict]:
        res = self._json("/scm/order/findTotalPlorList.action", {"dm_SearchMap": {
            "vndrCode": vndr_code, "plorDate": plor_date, "findRdpCode": rdp_code, "plorGb": "001"}})
        return res.get("ds_TotalExcelList", [])

    @classmethod
    def parse_rows(cls, rows: list[dict], vendor_name: str) -> list[OrderRow]:
        """한 행에 점포(009)/인터넷(050)/106 수량이 열로 들어있어 채널별로 분리한다. 중복 호출은 key로 흡수."""
        out = []
        for r in rows:
            for col in ("009", "050", "106"):
                q = int(r.get(f"plorQntt{col}") or 0)
                if q <= 0:
                    continue
                out.append(OrderRow(
                    shop="kyobo", order_date=cls._date(r.get("plorDate")),
                    isbn=str(r.get("cmdtCode", "")).strip(), title=r.get("cmdtName", ""),
                    store=cls.RDP_LABEL.get(col, col), qty=q,
                    supply_rate=float(r.get(f"byngRate{col}") or r.get("plorByngRate") or 0) or None,
                    price=int(r.get("cmdtPrce") or 0) or None,
                    external_key=f"{r.get('vndrCode','')}:{r.get('keyData','')}:{col}",
                    vendor=vendor_name, flag=(r.get("plorDvsn008Ysno") or "").strip()))
        return out

    def fetch(self, d_from, d_to):
        lo, hi = d_from.strftime("%Y%m%d"), d_to.strftime("%Y%m%d")
        for v in self.vendors:
            code, name = v.get("vndr_code", ""), v.get("vndr_name", "")
            for pd in self.order_dates(code):
                if not (lo <= pd <= hi):
                    continue
                for rdp in self.rdp_codes(code, pd):
                    yield from self.parse_rows(self.order_list(code, pd, rdp["rdpCode"]), name)


# ---------------------------------------------------------------- 알라딘
class AladinAdapter(BaseAdapter):
    """
    https://www.aladin.co.kr/supplier/  (ASP.NET WebForms, 세션 쿠키)
    - 발주 내역  wbackorder.aspx      : 당일 발주만 표시 → 매일 수집 (OrderRow)
    - 판매 통계  wStatSalesBook.aspx  : 기간별 도서별 실제 판매권수 → 전일 1일치 수집 (SaleRow). 월 단위 백필 가능
    """
    shop = "aladin"
    base = "https://www.aladin.co.kr"

    def login(self):
        self.client.get("/supplier/wmain.aspx?start=we")
        st, raw, _ = self.client.post("/supplier/wmain.aspx?start=we", data={
            "SupplierId": self.user, "x": "19", "y": "19", "Password": self.pw, "ActionType": "login"})
        html = self.client.text(raw)
        if st != 200 or "로그아웃" not in html:
            raise LoginFailed("aladin: 로그인 실패")

    # ----- 발주(당일)
    @staticmethod
    def parse_backorder(html: str, order_date: str) -> list[OrderRow]:
        strip = lambda x: re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", x.replace("&nbsp;", " "))).strip()
        out = []
        for t in re.findall(r"<table.*?</table>", html, flags=re.S | re.I):
            rows = [[strip(c) for c in re.findall(r"<t[hd][^>]*>(.*?)</t[hd]>", tr, flags=re.S | re.I)]
                    for tr in re.findall(r"<tr.*?</tr>", t, flags=re.S | re.I)]
            rows = [[c for c in r if c] for r in rows]
            rows = [r for r in rows if r]
            if not rows or "발주 부수" not in rows[0][0]:
                continue
            hdr = rows[0]
            for r in rows[1:]:
                if len(r) < len(hdr):
                    continue
                d = dict(zip(hdr, r))
                isbn = re.sub(r"\D", "", d.get("ISBN", ""))
                if len(isbn) < 10:
                    continue
                out.append(OrderRow(shop="aladin", order_date=order_date, isbn=isbn,
                                    title=d.get("제 목", d.get("제목", "")), store="알라딘 물류",
                                    qty=BaseAdapter._num(d.get("발주 부수")),
                                    price=BaseAdapter._num(d.get("정 가 (원)", d.get("정가"))) or None,
                                    external_key=f"{order_date}:{isbn}"))
        return out

    def fetch(self, d_from, d_to):
        st, raw, _ = self.client.get("/supplier/wbackorder.aspx")
        html = self.client.text(raw)
        m = re.search(r"(\d{4})년\s*(\d{1,2})월\s*(\d{1,2})일", html)
        od = f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}" if m else str(date.today())
        yield from self.parse_backorder(html, od)

    # ----- 판매 통계(기간)
    @staticmethod
    def parse_sales(html: str, d_from: date, d_to: date) -> list[SaleRow]:
        strip = lambda x: re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", x.replace("&nbsp;", " "))).strip()
        out = []
        for t in re.findall(r"<table.*?</table>", html, flags=re.S | re.I):
            if "판매권수" not in t:
                continue
            rows = [[strip(c) for c in re.findall(r"<t[hd][^>]*>(.*?)</t[hd]>", tr, flags=re.S | re.I)]
                    for tr in re.findall(r"<tr.*?</tr>", t, flags=re.S | re.I)]
            hdr_i = next((i for i, r in enumerate(rows) if r and r[0] == "출판사"), None)
            if hdr_i is None:
                continue
            hdr = rows[hdr_i]
            for r in rows[hdr_i + 1:]:
                if len(r) < 6 or r[0].startswith("총"):
                    continue
                d = dict(zip(hdr, r))
                isbn = re.sub(r"\D", "", d.get("ISBN", ""))
                if len(isbn) < 10:
                    continue
                out.append(SaleRow(shop="aladin", sale_from=str(d_from), sale_to=str(d_to), isbn=isbn,
                                   title=d.get("도서명", ""), qty=BaseAdapter._num(d.get("판매권수")),
                                   price=BaseAdapter._num(d.get("정가")) or None))
            break
        return out

    def fetch_sales(self, d_from: date, d_to: date) -> list[SaleRow]:
        st, raw, _ = self.client.get("/supplier/wStatSalesBook.aspx")
        html = self.client.text(raw)
        form = dict(re.findall(r'name="(__[A-Z]+)"[^>]*value="([^"]*)"', html))
        form.update({"cboStartYear": d_from.year, "cboStartMonth": d_from.month, "cboStartDay": d_from.day,
                     "cboEndYear": d_to.year, "cboEndMonth": d_to.month, "cboEndDay": d_to.day,
                     "cmdGetStat": "조회"})
        st, raw, _ = self.client.post("/supplier/wStatSalesBook.aspx", data=form)
        if st != 200:
            raise RuntimeError(f"aladin 판매통계: HTTP {st}")
        return self.parse_sales(self.client.text(raw), d_from, d_to)


# ---------------------------------------------------------------- YES24 (2단계 인증)
class Yes24Adapter(BaseAdapter):
    """
    http://scm.yes24.com — 로그인 후 휴대폰 인증번호 입력 단계가 있다.
    흐름: begin_login() → NeedsAuthCode → (담당자가 MO-DO에 코드 입력) → complete_login(code) → fetch()
    세션 객체는 PENDING_YES24 에 보관해 두 요청 사이에 쿠키를 유지한다.
    """
    shop = "yes24"
    base = "http://scm.yes24.com"

    def login(self):
        self.begin_login()

    def begin_login(self):
        # TODO(캡처 필요): 1차 로그인 요청. 응답에서 인증번호 입력 화면/상태를 판별.
        st, raw, _ = self.client.post("/TODO_LOGIN_PATH",
                                      data={"TODO_ID_FIELD": self.user, "TODO_PW_FIELD": self.pw})
        html = self.client.text(raw)
        if "TODO_AUTHCODE_PAGE_MARK" in html:
            PENDING_YES24["adapter"] = self
            PENDING_YES24["at"] = time.time()
            raise NeedsAuthCode("yes24: 휴대폰 인증번호 입력 필요")
        if "TODO_LOGIN_SUCCESS_MARK" not in html:
            raise LoginFailed("yes24: 로그인 실패")

    def complete_login(self, code: str):
        # TODO(캡처 필요): 인증번호 제출 요청
        st, raw, _ = self.client.post("/TODO_AUTHCODE_PATH", data={"TODO_CODE_FIELD": code})
        if "TODO_LOGIN_SUCCESS_MARK" not in self.client.text(raw):
            raise LoginFailed("yes24: 인증번호 확인 실패")
        PENDING_YES24.clear()

    def fetch(self, d_from, d_to):
        # TODO(캡처 필요): 발주내역(센터별) 조회 요청/응답
        st, raw, hdr = self.client.post("/TODO_ORDER_PATH", data={
            "TODO_FROM": d_from.strftime("%Y-%m-%d"), "TODO_TO": d_to.strftime("%Y-%m-%d")})
        for r in parse_html_table(self.client.text(raw)):
            yield OrderRow(
                shop="yes24", order_date=self._date(r.get("발주일")),
                isbn=r.get("ISBN", "").strip(), title=r.get("도서명", ""),
                store=r.get("센터", ""), qty=self._num(r.get("발주수량")),
                supply_rate=self._pct(r.get("공급율")), amount=self._num(r.get("발주금액")),
                price=self._num(r.get("정가")), external_key=r.get("발주번호", ""))


PENDING_YES24: dict = {}   # 인증번호 대기 중인 YES24 세션 (단일 프로세스 가정, 10분 만료)


# ---------------------------------------------------------------- HTML 표 파서 (라이브러리 없이)
def parse_html_table(html: str, table_index: int = 0) -> list[dict]:
    """첫 번째(또는 지정) <table>을 헤더 기준 dict 목록으로. 간단한 표만 대상."""
    tables = re.findall(r"<table.*?</table>", html, flags=re.S | re.I)
    if len(tables) <= table_index:
        return []
    t = tables[table_index]
    rows = re.findall(r"<tr.*?</tr>", t, flags=re.S | re.I)
    strip = lambda s: re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", s)).strip()
    out, header = [], None
    for tr in rows:
        cells = re.findall(r"<t[hd][^>]*>(.*?)</t[hd]>", tr, flags=re.S | re.I)
        cells = [strip(c) for c in cells]
        if not cells:
            continue
        if header is None:
            header = cells
            continue
        if len(cells) == len(header):
            out.append(dict(zip(header, cells)))
    return out


# ---------------------------------------------------------------- DB 적재
SCHEMA = """
CREATE TABLE IF NOT EXISTS bookstore_orders (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  shop TEXT NOT NULL, order_date TEXT NOT NULL, isbn TEXT, title TEXT, store TEXT,
  qty INTEGER NOT NULL, supply_rate REAL, ship_rate REAL, price INTEGER, amount INTEGER,
  external_key TEXT UNIQUE, vendor TEXT, flag TEXT, collected_at TEXT DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS bookstore_sales (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  shop TEXT NOT NULL, sale_from TEXT NOT NULL, sale_to TEXT NOT NULL, isbn TEXT, title TEXT,
  qty INTEGER NOT NULL, price INTEGER, external_key TEXT UNIQUE, collected_at TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS ix_bs_shop_date ON bookstore_sales(shop, sale_from);
CREATE INDEX IF NOT EXISTS ix_bo_shop_date ON bookstore_orders(shop, order_date);
CREATE INDEX IF NOT EXISTS ix_bo_isbn ON bookstore_orders(isbn);
CREATE TABLE IF NOT EXISTS bookstore_collect_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT, shop TEXT, d_from TEXT, d_to TEXT,
  rows INTEGER, status TEXT, message TEXT, ran_at TEXT DEFAULT (datetime('now'))
);
"""


def upsert_orders(conn, rows: Iterable[OrderRow]) -> int:
    """external_key 기준 INSERT OR REPLACE. server_stdlib의 libsql/sqlite 커넥션 모두 사용 가능."""
    n = 0
    sql = ("INSERT OR REPLACE INTO bookstore_orders "
           "(shop,order_date,isbn,title,store,qty,supply_rate,ship_rate,price,amount,external_key,vendor,flag) "
           "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)")
    batch = []
    for r in rows:
        batch.append((r.shop, r.order_date, r.isbn, r.title, r.store, r.qty, r.supply_rate,
                      r.ship_rate, r.price, r.amount, r.key(), r.vendor, r.flag))
        if len(batch) >= 150:
            conn.executemany(sql, batch); n += len(batch); batch = []
    if batch:
        conn.executemany(sql, batch); n += len(batch)
    conn.commit()
    return n


def upsert_sales(conn, rows: Iterable[SaleRow]) -> int:
    sql = ("INSERT OR REPLACE INTO bookstore_sales (shop,sale_from,sale_to,isbn,title,qty,price,external_key) "
           "VALUES (?,?,?,?,?,?,?,?)")
    batch = [(r.shop, r.sale_from, r.sale_to, r.isbn, r.title, r.qty, r.price, r.key()) for r in rows]
    for i in range(0, len(batch), 150):
        conn.executemany(sql, batch[i:i + 150])
    conn.commit()
    return len(batch)


def collect_aladin_sales(conn, d_from: date, d_to: date, adapter: AladinAdapter | None = None,
                         daily: bool = True) -> dict:
    """알라딘 실제 판매권수. daily=True 면 날짜별로 1일씩 조회해 일 단위 시계열을 만든다(백필 시 날짜 수만큼 호출)."""
    try:
        ad = adapter or AladinAdapter()
        if adapter is None:
            ad.login()
        n = 0
        if daily:
            d = d_from
            while d <= d_to:
                n += upsert_sales(conn, ad.fetch_sales(d, d)); d += timedelta(days=1)
        else:
            n = upsert_sales(conn, ad.fetch_sales(d_from, d_to))
        conn.execute("INSERT INTO bookstore_collect_log(shop,d_from,d_to,rows,status) VALUES (?,?,?,?,?)",
                     ("aladin_sales", str(d_from), str(d_to), n, "ok")); conn.commit()
        return {"ok": True, "shop": "aladin_sales", "rows": n}
    except Exception as e:  # noqa: BLE001
        conn.execute("INSERT INTO bookstore_collect_log(shop,d_from,d_to,rows,status,message) VALUES (?,?,?,?,?,?)",
                     ("aladin_sales", str(d_from), str(d_to), 0, "error", str(e)[:500])); conn.commit()
        return {"ok": False, "shop": "aladin_sales", "error": str(e)}


ADAPTERS = {"kyobo": KyoboAdapter, "aladin": AladinAdapter, "yes24": Yes24Adapter}


def collect(conn, shop: str, days: int = 7, adapter: BaseAdapter | None = None) -> dict:
    """한 서점 수집. 매일 새벽 실행 시 최근 7일을 다시 가져와 누락/정정분을 덮어쓴다."""
    d_to, d_from = date.today(), date.today() - timedelta(days=days)
    try:
        ad = adapter or ADAPTERS[shop]()
        if adapter is None:
            ad.login()
        n = upsert_orders(conn, ad.fetch(d_from, d_to))
        conn.execute("INSERT INTO bookstore_collect_log(shop,d_from,d_to,rows,status) VALUES (?,?,?,?,?)",
                     (shop, str(d_from), str(d_to), n, "ok")); conn.commit()
        return {"ok": True, "shop": shop, "rows": n}
    except NeedsAuthCode as e:
        conn.execute("INSERT INTO bookstore_collect_log(shop,d_from,d_to,rows,status,message) VALUES (?,?,?,?,?,?)",
                     (shop, str(d_from), str(d_to), 0, "needs_code", str(e))); conn.commit()
        return {"ok": False, "shop": shop, "needs_code": True}
    except Exception as e:  # noqa: BLE001
        conn.execute("INSERT INTO bookstore_collect_log(shop,d_from,d_to,rows,status,message) VALUES (?,?,?,?,?,?)",
                     (shop, str(d_from), str(d_to), 0, "error", str(e)[:500])); conn.commit()
        return {"ok": False, "shop": shop, "error": str(e)}


# ---------------------------------------------------------------- server_stdlib.py 연결 예시
"""
ensure_schema() 안에서:        for stmt in SCHEMA.split(";"): if stmt.strip(): cur.execute(stmt)

엔드포인트(관리자 또는 team_id==6):
  POST /api/bookstore/collect        {"shop":"kyobo"|"aladin"}          → collect(conn, shop) (+ aladin이면 collect_aladin_sales 전일)
  POST /api/bookstore/collect/aladin-sales {"from":"2026-01-01","to":"2026-09-30"} → 백필
  GET  /api/bookstore/sales?from=&to=&isbn=                              → 알라딘 실제 판매(일 단위)
  POST /api/bookstore/collect/yes24  {}                                  → begin: needs_code 반환
  POST /api/bookstore/collect/yes24  {"code":"123456"}                   → PENDING_YES24["adapter"].complete_login(code) 후 collect(conn,"yes24",adapter=...)
  GET  /api/bookstore/orders?shop=&from=&to=&isbn=                       → 대시보드 데이터
  GET  /api/bookstore/collect-log                                        → 마지막 수집 상태

새벽 자동 수집: Render Cron Job 으로  python scm_collect.py kyobo aladin
"""

if __name__ == "__main__":
    # 사용:  python scm_collect.py kyobo aladin            (최근 7일 발주 + 알라딘 전일 판매)
    #        python scm_collect.py aladin-sales 2026-01-01 2026-09-30   (알라딘 판매 백필, 일 단위)
    import sqlite3, sys
    db = sqlite3.connect(os.environ.get("MODO_DB", "modo.db"))
    db.executescript(SCHEMA)
    args = sys.argv[1:] or ["kyobo", "aladin"]
    if args[0] == "aladin-sales":
        f, t = date.fromisoformat(args[1]), date.fromisoformat(args[2])
        print(collect_aladin_sales(db, f, t))
    else:
        for shop in args:
            print(collect(db, shop))
            if shop == "aladin":
                y = date.today() - timedelta(days=1)
                print(collect_aladin_sales(db, y, y))
