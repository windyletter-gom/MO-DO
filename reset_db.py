# -*- coding: utf-8 -*-
"""MOA WORK DB 초기화 도구.

사용법(프로그램을 완전히 종료한 상태에서 실행):
  python reset_db.py operational   # 조직/계정 유지, 테스트로 쌓인 활동 데이터만 삭제 (운영 시작용)
  python reset_db.py factory        # 완전 초기화 - DB 삭제 후 다음 실행 시 기본 데이터로 새로 생성

인자가 없으면 안내 후 종료합니다. 삭제 전 자동으로 백업 파일을 만듭니다.
"""
import sys, sqlite3, shutil, time
from pathlib import Path

BASE = Path(__file__).resolve().parent
DB = BASE / "data" / "moa_work.db"
UPLOADS = BASE / "uploads"

# 초기화해도 유지할 '조직/구성' 테이블 (부서·팀·계정·워크스페이스 구성)
KEEP = {"divisions", "teams", "users", "modules", "team_modules", "app_meta", "sqlite_sequence"}


def backup():
    if DB.exists():
        stamp = time.strftime("%Y%m%d_%H%M%S")
        dest = DB.with_name(f"moa_work_backup_{stamp}.db")
        shutil.copy(DB, dest)
        print(f"[백업] 기존 DB를 백업했습니다: {dest.name}")
        return dest
    return None


def operational():
    if not DB.exists():
        print("DB 파일이 없습니다. 프로그램을 한 번 실행하면 생성됩니다.")
        return
    backup()
    c = sqlite3.connect(DB)  # foreign_keys 기본 OFF → 삭제 순서 무관
    tables = [r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")]
    wiped = []
    for t in tables:
        if t in KEEP:
            continue
        c.execute(f"DELETE FROM {t}")
        wiped.append(t)
    # 자동증가 카운터도 유지 테이블만 남기고 초기화
    try:
        c.execute("DELETE FROM sqlite_sequence WHERE name NOT IN ('divisions','teams','users','modules','team_modules')")
    except Exception:
        pass
    # 데모 재생성 방지 플래그는 유지(운영 시작 후 예시 데이터가 다시 생기지 않도록)
    c.execute("INSERT OR REPLACE INTO app_meta(key,value) VALUES('seed_done','1')")
    c.commit()
    c.execute("VACUUM")
    c.commit()
    c.close()
    if UPLOADS.exists():
        shutil.rmtree(UPLOADS, ignore_errors=True)
    print(f"[운영 시작 초기화 완료] 조직/계정은 유지하고 활동 데이터 {len(wiped)}개 테이블을 비웠습니다.")
    print("이제 프로그램을 실행하면 실제 업무용 빈 상태로 시작합니다.")


def factory():
    backup()
    for f in [DB, DB.with_name(DB.name + "-wal"), DB.with_name(DB.name + "-shm")]:
        if f.exists():
            f.unlink()
    if UPLOADS.exists():
        shutil.rmtree(UPLOADS, ignore_errors=True)
    print("[완전 초기화 완료] DB를 삭제했습니다. 다음 실행 시 기본(데모 포함) 데이터로 새로 생성됩니다.")


def main():
    mode = sys.argv[1].lower() if len(sys.argv) > 1 else ""
    if mode == "operational":
        print("== 운영 시작 초기화: 조직/계정 유지 · 활동 데이터 전체 삭제 ==")
        ans = input("정말 진행할까요? 되돌릴 수 없습니다. (YES 입력 시 진행): ").strip()
        if ans == "YES":
            operational()
        else:
            print("취소했습니다.")
    elif mode == "factory":
        print("== 완전 초기화: DB 삭제 후 기본 데이터로 재생성 ==")
        ans = input("정말 진행할까요? 되돌릴 수 없습니다. (YES 입력 시 진행): ").strip()
        if ans == "YES":
            factory()
        else:
            print("취소했습니다.")
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
