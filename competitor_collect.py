"""
경쟁사 판매현황 수집 — 알라딘 Open API(TTB) ItemSearch (표준 라이브러리만 사용)

검색어(시험명)마다 판매지수(SalesPoint) 순 상위 100권을 받아 날짜별 스냅숏으로 저장한다.
출판사명을 9개 경쟁 그룹(모아 + 8곳)으로 정규화해 출판사별 점유율·추이를 만든다.

환경변수
  ALADIN_TTB_KEY     알라딘 TTB 키 (https://www.aladin.co.kr/ttb/wblog_manage.aspx)
  COMP_KEYWORDS      쉼표 구분 검색어 목록 (없으면 DEFAULT_KEYWORDS)
알라딘 약관: 일 5,000회 한도, 데이터 출처 '알라딘' 표기. 검색어당 2회 호출(50권×2) → 15개 검색어면 하루 30회.
"""
from __future__ import annotations

import json
import os
import re
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import date
from typing import Iterable

TTB_URL = "https://www.aladin.co.kr/ttb/api/ItemSearch.aspx"
DEFAULT_KEYWORDS = ["산업안전기사", "산업안전산업기사", "전기기사", "전기산업기사", "소방설비기사", "소방설비산업기사",
                    "위험물산업기사", "위험물기능사", "공조냉동기계기사", "건설안전기사", "가스기사", "에너지관리기사",
                    "전기기능장", "소방시설관리사", "소방기술사"]

# 출판사명 → 그룹. 알라딘 표기가 제각각이라 부분 일치로 매핑(순서 중요: 먼저 맞는 것 적용)
PUBLISHER_GROUPS = [
    ("모아", ["모아교육", "모아팩토리", "(주)모아", "모아에듀", "모아"]),
    ("에듀윌", ["에듀윌"]),
    ("해커스", ["해커스"]),
    ("시대에듀", ["시대고시", "시대에듀", "시대교육", "SD에듀", "에스디에듀"]),
    ("배울학", ["배울학"]),
    ("에듀피디", ["에듀피디", "EduPD"]),
    ("엔지니어랩", ["엔지니어랩", "엔지니어 랩"]),
    ("공단기", ["공단기", "에스티유니타스", "ST유니타스"]),
    ("자단기", ["자단기"]),
    ("성안당", ["성안당"]),
    ("예문사", ["예문사"]),
    ("한솔아카데미", ["한솔아카데미", "한솔"]),
    ("구민사", ["구민사"]),
    ("동일출판사", ["동일출판"]),
]


def normalize_publisher(name: str) -> str:
    n = (name or "").replace(" ", "")
    for group, keys in PUBLISHER_GROUPS:
        if any(k.replace(" ", "") in n for k in keys):
            return group
    return "기타"


@dataclass
class CompRow:
    snap_date: str
    keyword: str
    rank: int          # 검색어 내 판매지수 순위
    isbn: str
    title: str
    publisher: str
    pub_group: str
    sales_point: int
    price: int | None
    category: str
    best_rank: int | None   # 알라딘 분야 베스트 순위(있을 때)
    link: str

    def key(self) -> str:
        return f"{self.snap_date}:{self.keyword}:{self.isbn}"


SCHEMA = """
CREATE TABLE IF NOT EXISTS comp_snapshot (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  snap_date TEXT NOT NULL, keyword TEXT NOT NULL, rank INTEGER, isbn TEXT, title TEXT,
  publisher TEXT, pub_group TEXT, sales_point INTEGER, price INTEGER, category TEXT, best_rank INTEGER, link TEXT,
  external_key TEXT UNIQUE, collected_at TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS ix_cs_kw_date ON comp_snapshot(keyword, snap_date);
"""


def keywords() -> list[str]:
    env = os.environ.get("COMP_KEYWORDS", "").strip()
    return [k.strip() for k in env.split(",") if k.strip()] or DEFAULT_KEYWORDS


def _call(params: dict, timeout: int = 20) -> dict:
    url = TTB_URL + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": "MO-DO/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        txt = r.read().decode("utf-8", errors="replace").strip()
    # output=js 는 JSON 이지만 끝에 ';' 가 붙거나 일부 문자가 이스케이프되지 않는 경우가 있어 보정
    txt = txt.rstrip(";").strip()
    try:
        return json.loads(txt)
    except json.JSONDecodeError:
        txt = re.sub(r"[\x00-\x1f]", " ", txt)
        return json.loads(txt)


def parse_items(items: list[dict], keyword: str, snap_date: str, start_rank: int = 1) -> list[CompRow]:
    out = []
    for i, it in enumerate(items):
        isbn = (it.get("isbn13") or it.get("isbn") or "").strip()
        if not isbn:
            continue
        pub = it.get("publisher") or ""
        out.append(CompRow(
            snap_date=snap_date, keyword=keyword, rank=start_rank + i, isbn=isbn,
            title=re.sub(r"\s+", " ", it.get("title") or "").strip(), publisher=pub,
            pub_group=normalize_publisher(pub), sales_point=int(it.get("salesPoint") or 0),
            price=int(it.get("priceStandard") or 0) or None, category=it.get("categoryName") or "",
            best_rank=int(it.get("bestRank") or 0) or None, link=it.get("link") or ""))
    return out


def fetch_keyword(ttb_key: str, keyword: str, snap_date: str, pages: int = 2) -> list[CompRow]:
    rows = []
    for page in range(1, pages + 1):
        res = _call({"ttbkey": ttb_key, "Query": keyword, "QueryType": "Keyword", "SearchTarget": "Book",
                     "Sort": "SalesPoint", "MaxResults": 50, "start": page, "output": "js",
                     "Version": "20131101", "Cover": "None", "OptResult": "bestSellerRank"})
        items = res.get("item") or []
        rows += parse_items(items, keyword, snap_date, start_rank=(page - 1) * 50 + 1)
        if len(items) < 50:
            break
        time.sleep(0.5)
    return rows


def upsert(conn, rows: Iterable[CompRow]) -> int:
    sql = ("INSERT OR REPLACE INTO comp_snapshot (snap_date,keyword,rank,isbn,title,publisher,pub_group,sales_point,"
           "price,category,best_rank,link,external_key) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)")
    batch = [(r.snap_date, r.keyword, r.rank, r.isbn, r.title, r.publisher, r.pub_group, r.sales_point, r.price,
              r.category, r.best_rank, r.link, r.key()) for r in rows]
    for i in range(0, len(batch), 150):
        conn.executemany(sql, batch[i:i + 150])
    conn.commit()
    return len(batch)


def collect(conn, kws: list[str] | None = None) -> dict:
    key = os.environ.get("ALADIN_TTB_KEY", "").strip()
    if not key:
        return {"ok": False, "error": "ALADIN_TTB_KEY 환경변수 없음"}
    today, n, errors = str(date.today()), 0, []
    for kw in (kws or keywords()):
        try:
            n += upsert(conn, fetch_keyword(key, kw, today))
        except Exception as e:  # noqa: BLE001
            errors.append(f"{kw}: {e}")
        time.sleep(0.5)
    conn.execute("INSERT INTO bookstore_collect_log(shop,d_from,d_to,rows,status,message) VALUES (?,?,?,?,?,?)",
                 ("competitors", today, today, n, "ok" if not errors else "error", "; ".join(errors)[:500]))
    conn.commit()
    return {"ok": not errors, "shop": "competitors", "rows": n, "errors": errors}


if __name__ == "__main__":
    import sqlite3, sys
    db = sqlite3.connect(os.environ.get("MODO_DB", "modo.db"))
    db.executescript(SCHEMA)
    db.executescript("""CREATE TABLE IF NOT EXISTS bookstore_collect_log (id INTEGER PRIMARY KEY AUTOINCREMENT, shop TEXT,
        d_from TEXT, d_to TEXT, rows INTEGER, status TEXT, message TEXT, ran_at TEXT DEFAULT (datetime('now')))""")
    print(collect(db, sys.argv[1:] or None))
