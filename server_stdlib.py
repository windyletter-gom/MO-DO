
from __future__ import annotations
import os, re, json, sqlite3, threading, webbrowser, traceback, sys, subprocess, base64, mimetypes, time, socket, hashlib
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlparse, parse_qs, unquote

BASE = Path(__file__).resolve().parent
DB = BASE / "data" / "moa_work.db"
UPLOADS = BASE / "uploads"
UPLOADS.mkdir(exist_ok=True)
# 0.0.0.0 로 열어 같은 네트워크의 다른 PC가 브라우저로 접속할 수 있게 한다(1대만 서버로 실행 권장).
# 클라우드(Render 등)는 PORT 환경변수로 포트를 지정한다 → 있으면 그 값을, 없으면 로컬 기본 8000.
HOST = "0.0.0.0"
PORT = int(os.environ.get("PORT") or 8000)
# 서버 환경(클라우드) 여부: PORT/RENDER 환경변수가 있거나 NO_BROWSER 지정 시 자동 브라우저 열기·정산 안내를 생략한다.
IS_SERVER = bool(os.environ.get("PORT") or os.environ.get("RENDER") or os.environ.get("NO_BROWSER"))

def hash_pw(pw):
    return hashlib.sha256(("modo$salt$"+(pw or "")).encode("utf-8")).hexdigest()

def lan_ip():
    try:
        s=socket.socket(socket.AF_INET,socket.SOCK_DGRAM);s.connect(("8.8.8.8",80))
        ip=s.getsockname()[0];s.close();return ip
    except Exception:
        try:return socket.gethostbyname(socket.gethostname())
        except Exception:return "127.0.0.1"

MAX_BODY_BYTES = 30 * 1024 * 1024  # 요청 본문 최대 30MB (업로드 메모리 폭주 방지)

# 팀 워크스페이스 모듈 공통 수정/삭제 대상 (경로 접두어 → 테이블)
GENERIC_CRUD = {
    "planning/live-events": "planning_live_events",
    "planning/promotions": "planning_promotions",
    "planning/b2b": "planning_b2b",
    "planning/site-ops": "planning_site_ops",
    "meetings": "meetings",
    "book-discussions": "book_discussions",
    "checklists": "checklists",
    "resources": "resources",
    "purchases": "purchases",
}

def _crud_target(p):
    """/api/<prefix>/<id> 형태에서 (테이블, id)를 돌려준다. 대상 아니면 (None,None)."""
    if not p.startswith("/api/"):
        return None, None
    last = p.rsplit("/", 1)[-1]
    if not last.isdigit():
        return None, None
    key = p[len("/api/"):p.rfind("/")]
    return GENERIC_CRUD.get(key), (int(last) if GENERIC_CRUD.get(key) else None)

def db():
    c = sqlite3.connect(DB, timeout=30)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys=ON")
    # 네트워크 공유 폴더(SMB/NAS)에서는 WAL 모드가 동작하지 않아 종료 후 변경분이
    # 유실된다. 네트워크 파일시스템에서 안전한 DELETE(롤백 저널) 모드를 사용한다.
    c.execute("PRAGMA journal_mode=DELETE")
    c.execute("PRAGMA synchronous=FULL")
    c.execute("PRAGMA busy_timeout=30000")
    return c

def safe_join(base: Path, rel: str):
    """base 디렉터리 밖으로 벗어나는 경로(../ 등)를 차단한다."""
    base = base.resolve()
    target = (base / rel.lstrip("/")).resolve()
    if target == base or base in target.parents:
        return target
    return None

def ensure_schema():
    with db() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS task_comments(
            id INTEGER PRIMARY KEY,
            task_id INTEGER NOT NULL,
            user_id INTEGER,
            body TEXT NOT NULL,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS task_assignees(
            id INTEGER PRIMARY KEY,
            task_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            role_label TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(task_id,user_id)
        );
        CREATE TABLE IF NOT EXISTS notifications(
            id INTEGER PRIMARY KEY,
            user_id INTEGER NOT NULL,
            title TEXT NOT NULL,
            body TEXT,
            is_read INTEGER DEFAULT 0,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS directives(
            id INTEGER PRIMARY KEY,
            sender_id INTEGER NOT NULL,
            recipient_id INTEGER NOT NULL,
            title TEXT NOT NULL,
            body TEXT NOT NULL,
            priority TEXT DEFAULT 'NORMAL',
            due_date TEXT,
            status TEXT DEFAULT 'SENT',
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS directive_files(
            id INTEGER PRIMARY KEY,
            directive_id INTEGER NOT NULL,
            original_name TEXT,
            stored_name TEXT,
            mime_type TEXT,
            size_bytes INTEGER DEFAULT 0,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS directive_replies(
            id INTEGER PRIMARY KEY,
            directive_id INTEGER NOT NULL,
            sender_id INTEGER NOT NULL,
            body TEXT NOT NULL,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS request_replies(
            id INTEGER PRIMARY KEY,
            request_id INTEGER NOT NULL,
            sender_id INTEGER NOT NULL,
            body TEXT NOT NULL,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS purchases(
            id INTEGER PRIMARY KEY,
            team_id INTEGER,
            purchaser_id INTEGER,
            item_type TEXT,
            item_name TEXT NOT NULL,
            vendor TEXT,
            amount INTEGER DEFAULT 0,
            purchase_date TEXT,
            payment_method TEXT,
            account_id TEXT,
            password_hint TEXT,
            note TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS refunds(
            id INTEGER PRIMARY KEY,
            team_id INTEGER NOT NULL DEFAULT 1,
            instructor_id INTEGER,
            course_name TEXT NOT NULL,
            refund_reason TEXT,
            refund_amount INTEGER DEFAULT 0,
            incentive_adjustment INTEGER DEFAULT 0,
            improvement_action TEXT,
            status TEXT DEFAULT 'OPEN',
            refund_date TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS book_discussions(
            id INTEGER PRIMARY KEY,
            team_id INTEGER,
            month_label TEXT,
            book_title TEXT NOT NULL,
            selected_reason TEXT,
            category TEXT,
            status TEXT DEFAULT 'PLANNED',
            completed_date TEXT,
            rating INTEGER,
            reflection TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS customers(
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            phone TEXT,
            email TEXT,
            company TEXT,
            member_id TEXT,
            customer_type TEXT DEFAULT '일반',
            onboarding_status TEXT DEFAULT '신규',
            interests TEXT,
            notes TEXT,
            last_contact_at TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS consultation_logs(
            id INTEGER PRIMARY KEY,
            customer_id INTEGER,
            counselor_id INTEGER,
            product_name TEXT,
            inquiry_category TEXT,
            inquiry_text TEXT NOT NULL,
            response_text TEXT,
            channel TEXT DEFAULT '전화',
            status TEXT DEFAULT '완료',
            followup_date TEXT,
            share_to_content INTEGER DEFAULT 0,
            share_to_planning INTEGER DEFAULT 0,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS service_knowledge(
            id INTEGER PRIMARY KEY,
            product_name TEXT,
            category TEXT,
            question TEXT NOT NULL,
            answer TEXT NOT NULL,
            source_type TEXT DEFAULT '상담',
            usage_count INTEGER DEFAULT 0,
            updated_by INTEGER,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS product_studies(
            id INTEGER PRIMARY KEY,
            product_name TEXT NOT NULL,
            title TEXT NOT NULL,
            content TEXT NOT NULL,
            keywords TEXT,
            owner_id INTEGER,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS mcc_reviews(
            id INTEGER PRIMARY KEY,
            review_type TEXT NOT NULL DEFAULT '교재',
            target_name TEXT NOT NULL,
            compare_standard TEXT,
            source_version TEXT,
            owner_id INTEGER,
            team_id INTEGER DEFAULT 1,
            status TEXT DEFAULT '검수중',
            priority TEXT DEFAULT 'NORMAL',
            total_items INTEGER DEFAULT 0,
            changed_items INTEGER DEFAULT 0,
            matched_items INTEGER DEFAULT 0,
            progress INTEGER DEFAULT 0,
            started_at TEXT,
            due_date TEXT,
            completed_at TEXT,
            notes TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS mcc_review_items(
            id INTEGER PRIMARY KEY,
            review_id INTEGER NOT NULL,
            item_no INTEGER,
            category TEXT,
            source_text TEXT,
            reference_text TEXT,
            result_type TEXT DEFAULT '일치',
            change_detail TEXT,
            decision TEXT,
            reviewer_id INTEGER,
            status TEXT DEFAULT '대기',
            page_ref TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS marketing_channels(
            id INTEGER PRIMARY KEY, channel_type TEXT NOT NULL, content_name TEXT NOT NULL,
            content_type TEXT, owner_id INTEGER, status TEXT DEFAULT '기획', planned_date TEXT,
            publish_date TEXT, url TEXT, notes TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS marketing_channel_metrics(
            id INTEGER PRIMARY KEY, channel_id INTEGER NOT NULL, metric_date TEXT,
            views INTEGER DEFAULT 0, watch_hours REAL DEFAULT 0, subscribers_delta INTEGER DEFAULT 0,
            visitors INTEGER DEFAULT 0, posts_count INTEGER DEFAULT 0, friends_delta INTEGER DEFAULT 0,
            clicks INTEGER DEFAULT 0, impressions INTEGER DEFAULT 0, conversions INTEGER DEFAULT 0,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS marketing_campaigns(
            id INTEGER PRIMARY KEY, platform TEXT NOT NULL, campaign_name TEXT NOT NULL, product_name TEXT,
            owner_id INTEGER, start_date TEXT, end_date TEXT, budget INTEGER DEFAULT 0, spend INTEGER DEFAULT 0,
            impressions INTEGER DEFAULT 0, clicks INTEGER DEFAULT 0, conversions INTEGER DEFAULT 0, leads INTEGER DEFAULT 0,
            status TEXT DEFAULT '준비', landing_url TEXT, notes TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS marketing_leads(
            id INTEGER PRIMARY KEY, lead_date TEXT, source_channel TEXT, campaign_id INTEGER, product_name TEXT,
            lead_type TEXT, customer_name TEXT, contact TEXT, status TEXT DEFAULT '신규', owner_id INTEGER,
            notes TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS marketing_events(
            id INTEGER PRIMARY KEY, event_name TEXT NOT NULL, event_type TEXT, owner_id INTEGER, event_date TEXT,
            registration_count INTEGER DEFAULT 0, marketing_inflow_count INTEGER DEFAULT 0,
            promo_material_status TEXT DEFAULT '미착수', landing_status TEXT DEFAULT '미착수', message_status TEXT DEFAULT '미착수',
            notes TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS marketing_collab_requests(
            id INTEGER PRIMARY KEY, requester_team_id INTEGER, requester_id INTEGER, title TEXT NOT NULL,
            request_detail TEXT, owner_id INTEGER, due_date TEXT, status TEXT DEFAULT '대기', feedback_score INTEGER DEFAULT 0,
            result_notes TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS marketing_performance_snapshots(
            id INTEGER PRIMARY KEY, snapshot_month TEXT NOT NULL, data_type TEXT NOT NULL, reference_name TEXT NOT NULL,
            value_numeric REAL DEFAULT 0, value_text TEXT, owner_id INTEGER, notes TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS planning_live_events(
            id INTEGER PRIMARY KEY,
            event_name TEXT NOT NULL,
            event_type TEXT DEFAULT '라이브특강',
            product_name TEXT,
            owner_id INTEGER,
            event_date TEXT,
            registration_count INTEGER DEFAULT 0,
            confirmed_count INTEGER DEFAULT 0,
            paid_conversion_count INTEGER DEFAULT 0,
            satisfaction_score REAL DEFAULT 0,
            review_score REAL DEFAULT 0,
            status TEXT DEFAULT '기획',
            notes TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS planning_promotions(
            id INTEGER PRIMARY KEY,
            promotion_name TEXT NOT NULL,
            season TEXT,
            target_product TEXT,
            owner_id INTEGER,
            start_date TEXT,
            end_date TEXT,
            visitors INTEGER DEFAULT 0,
            cart_count INTEGER DEFAULT 0,
            purchase_count INTEGER DEFAULT 0,
            sales_amount INTEGER DEFAULT 0,
            status TEXT DEFAULT '기획',
            notes TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS planning_products(
            id INTEGER PRIMARY KEY,
            product_name TEXT NOT NULL,
            product_type TEXT,
            owner_id INTEGER,
            launch_date TEXT,
            regular_price INTEGER DEFAULT 0,
            promo_price INTEGER DEFAULT 0,
            avg_selling_price INTEGER DEFAULT 0,
            margin_rate REAL DEFAULT 0,
            season_label TEXT,
            status TEXT DEFAULT '기획',
            notes TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS planning_b2b(
            id INTEGER PRIMARY KEY,
            partner_name TEXT NOT NULL,
            product_name TEXT,
            owner_id INTEGER,
            contract_date TEXT,
            supply_price INTEGER DEFAULT 0,
            sales_amount INTEGER DEFAULT 0,
            settlement_month TEXT,
            settlement_amount INTEGER DEFAULT 0,
            settlement_status TEXT DEFAULT '대기',
            sync_status TEXT DEFAULT '정상',
            issue_notes TEXT,
            notes TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS planning_site_ops(
            id INTEGER PRIMARY KEY,
            work_type TEXT NOT NULL,
            title TEXT NOT NULL,
            owner_id INTEGER,
            request_date TEXT,
            due_date TEXT,
            completed_date TEXT,
            status TEXT DEFAULT '대기',
            page_url TEXT,
            issue_count INTEGER DEFAULT 0,
            complaint_count INTEGER DEFAULT 0,
            change_summary TEXT,
            notes TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS data_center_files(
            id INTEGER PRIMARY KEY,
            team_id INTEGER NOT NULL,
            data_type TEXT NOT NULL,
            title TEXT,
            original_name TEXT,
            stored_name TEXT,
            mime_type TEXT,
            size_bytes INTEGER DEFAULT 0,
            file_hash TEXT,
            source_type TEXT DEFAULT 'FILE',
            external_url TEXT,
            status TEXT DEFAULT '처리완료',
            duplicate_of INTEGER,
            uploader_id INTEGER,
            notes TEXT,
            error_message TEXT,
            version_label TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS data_center_logs(
            id INTEGER PRIMARY KEY,
            file_id INTEGER,
            team_id INTEGER NOT NULL,
            actor_id INTEGER,
            action TEXT NOT NULL,
            detail TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS project_teams(
            id INTEGER PRIMARY KEY,
            project_id INTEGER NOT NULL,
            team_id INTEGER NOT NULL,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(project_id,team_id)
        );
        CREATE TABLE IF NOT EXISTS project_members(
            id INTEGER PRIMARY KEY,
            project_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            member_role TEXT DEFAULT 'TFT',
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(project_id,user_id)
        );
        CREATE TABLE IF NOT EXISTS project_approvals(
            id INTEGER PRIMARY KEY,
            project_id INTEGER NOT NULL,
            approver_id INTEGER NOT NULL,
            approval_order INTEGER DEFAULT 1,
            status TEXT DEFAULT 'WAITING',
            comment TEXT,
            decided_at TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(project_id,approver_id)
        );

        CREATE TABLE IF NOT EXISTS work_templates(
            id INTEGER PRIMARY KEY,
            template_type TEXT NOT NULL,
            name TEXT NOT NULL,
            description TEXT,
            payload_json TEXT NOT NULL,
            team_id INTEGER,
            created_by INTEGER,
            active INTEGER DEFAULT 1,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS task_approvals(
            id INTEGER PRIMARY KEY,
            task_id INTEGER NOT NULL,
            approver_id INTEGER NOT NULL,
            approval_order INTEGER DEFAULT 1,
            status TEXT DEFAULT 'WAITING',
            comment TEXT,
            decided_at TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(task_id,approver_id)
        );
        CREATE TABLE IF NOT EXISTS task_review_requests(
            id INTEGER PRIMARY KEY,
            task_id INTEGER NOT NULL,
            requester_id INTEGER NOT NULL,
            comment TEXT,
            status TEXT DEFAULT 'WAITING',
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            completed_at TEXT
        );
        CREATE TABLE IF NOT EXISTS task_review_files(
            id INTEGER PRIMARY KEY,
            request_id INTEGER NOT NULL,
            original_name TEXT NOT NULL,
            stored_name TEXT NOT NULL,
            mime_type TEXT,
            size_bytes INTEGER DEFAULT 0,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS company_notices(
            id INTEGER PRIMARY KEY,
            title TEXT NOT NULL,
            body TEXT,
            category TEXT DEFAULT '일반',
            author_id INTEGER,
            pinned INTEGER DEFAULT 0,
            active INTEGER DEFAULT 1,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );

        /* ===== 콘텐츠 제작(콘.제작) 강의촬영 스케줄 = TOP EVENT 연동 대상 ===== */
        CREATE TABLE IF NOT EXISTS filming_courses(
            id INTEGER PRIMARY KEY,
            field TEXT,                     -- 과정분야(소방/안전/전기 등)
            name TEXT NOT NULL,             -- 과정명칭
            book TEXT,                      -- 교재
            professor TEXT,                 -- 담당교수
            course_code TEXT,               -- 강좌코드
            ended INTEGER DEFAULT 0,        -- 종강 여부(1=종강)
            hidden INTEGER DEFAULT 0,       -- 숨김(정산 완료 전까지 숨기기 금지)
            note TEXT,
            source TEXT DEFAULT 'manual',   -- manual/api (구글시트 API 동기화 대비)
            sync_status TEXT DEFAULT 'manual',
            external_key TEXT,              -- 구글시트 행/시트 식별자(향후 API 연동)
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS filming_clips(
            id INTEGER PRIMARY KEY,
            course_id INTEGER NOT NULL,
            clip_no INTEGER,                -- clip 번호
            subject TEXT,                   -- 과목명
            shoot_date TEXT,                -- 촬영일자
            room TEXT,                      -- 촬영실
            video_len TEXT,                 -- 영상시간(촬영완료 판단)
            attachment TEXT,                -- 첨부파일(O 등)
            coding_date TEXT,               -- 코딩일자(코딩완료 판단)
            settle_status TEXT DEFAULT '미정산', -- 정산완료/미정산
            note TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );

        /* ===== 마케팅용 영상 제작 현황 (월별 로그) ===== */
        CREATE TABLE IF NOT EXISTS mkt_video_log(
            id INTEGER PRIMARY KEY,
            month TEXT,                     -- YYYY-MM
            ym TEXT,                        -- 원본 표기(25년 1월)
            cat_major TEXT,                 -- 대분류(소방/안전/전기/공통)
            cat_minor TEXT,                 -- 중분류(과정명)
            content_type TEXT,              -- 콘텐츠 구분(강의/롱, 홍보/기획쇼츠 등)
            topic TEXT,                     -- 주제
            shoot_date TEXT,                -- 촬영일
            edit_done TEXT,                 -- 편집완료(예정)
            pm TEXT, instructor TEXT, editor TEXT,
            progress TEXT,                  -- 진행사항(완료/진행중/취소/진행예정)
            url TEXT, note TEXT,
            source TEXT DEFAULT 'manual',
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );

        /* ===== 주간 업무보고 (개인이 주차별로 등록, 팀별 조회) ===== */
        CREATE TABLE IF NOT EXISTS weekly_reports(
            id INTEGER PRIMARY KEY,
            week_start TEXT NOT NULL,       -- 해당 주 월요일 'YYYY-MM-DD'
            week_end TEXT NOT NULL,         -- 해당 주 일요일
            team_id INTEGER,
            user_id INTEGER,               -- 담당자
            title TEXT,                     -- 업무명
            detail TEXT,                    -- 업무내용
            complete_date TEXT,             -- 완료일정(달력)
            status TEXT DEFAULT '미완료',   -- 완료/미완료/진행중
            target_rate INTEGER,            -- 목표 달성률(%)
            actual_rate INTEGER,            -- 실제 달성률(%)
            deliverable TEXT,               -- 최종 산출물(설명)
            deliverable_file TEXT,          -- 저장 파일명
            deliverable_original TEXT,      -- 원본 파일명
            deliverable_mime TEXT,
            collaborators TEXT,             -- 협업 인원 표시용 텍스트
            collaborator_ids TEXT,          -- JSON [user_id]
            carried_from INTEGER,           -- 이월 원본 항목 id
            carried_over INTEGER DEFAULT 0, -- 차주로 이월됨(1)
            sort_order INTEGER DEFAULT 0,
            created_by INTEGER,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP
        );

        /* ===== 마케팅 채널 분석(주간) - 정규화 롱테이블 ===== */
        CREATE TABLE IF NOT EXISTS mkt_weekly_metrics(
            id INTEGER PRIMARY KEY,
            week TEXT,                      -- YYYY-MM-DD(매주 금요일 업데이트)
            category TEXT,                  -- youtube/blog/sns/cafe/search
            channel TEXT,                   -- 모아소방TV, 네이버블로그, 인스타그램, 모아바 등
            metric TEXT,                    -- subscribers/views/revenue_usd/visits/followers/members/search_pc ...
            value REAL,
            source TEXT DEFAULT 'manual',
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        """)
        c.commit()

def ensure_indexes():
    # 컬럼 추가(ensure_task_columns 등) 이후에 호출해야 함 — reviewer_id/created_by 등은 런타임에 추가되는 컬럼이라 그 전에 인덱스를 만들면 실패한다.
    with db() as c:
        c.executescript("""
        CREATE INDEX IF NOT EXISTS idx_tasks_assignee ON tasks(assignee_id);
        CREATE INDEX IF NOT EXISTS idx_tasks_team ON tasks(team_id);
        CREATE INDEX IF NOT EXISTS idx_tasks_project ON tasks(project_id);
        CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
        CREATE INDEX IF NOT EXISTS idx_task_approvals_task ON task_approvals(task_id);
        CREATE INDEX IF NOT EXISTS idx_task_approvals_approver ON task_approvals(approver_id);
        CREATE INDEX IF NOT EXISTS idx_task_comments_task ON task_comments(task_id);
        CREATE INDEX IF NOT EXISTS idx_activity_logs_task ON activity_logs(task_id);
        CREATE INDEX IF NOT EXISTS idx_notifications_user ON notifications(user_id);
        CREATE INDEX IF NOT EXISTS idx_directives_recipient ON directives(recipient_id);
        CREATE INDEX IF NOT EXISTS idx_directives_sender ON directives(sender_id);
        CREATE INDEX IF NOT EXISTS idx_directive_replies_directive ON directive_replies(directive_id);
        CREATE INDEX IF NOT EXISTS idx_requests_target_user ON requests(target_user_id);
        CREATE INDEX IF NOT EXISTS idx_requests_requester ON requests(requester_id);
        CREATE INDEX IF NOT EXISTS idx_requests_target_team ON requests(target_team_id);
        CREATE INDEX IF NOT EXISTS idx_request_replies_request ON request_replies(request_id);
        CREATE INDEX IF NOT EXISTS idx_project_members_project ON project_members(project_id);
        CREATE INDEX IF NOT EXISTS idx_project_members_user ON project_members(user_id);
        CREATE INDEX IF NOT EXISTS idx_project_teams_project ON project_teams(project_id);
        CREATE INDEX IF NOT EXISTS idx_project_approvals_project ON project_approvals(project_id);
        CREATE INDEX IF NOT EXISTS idx_consultation_logs_customer ON consultation_logs(customer_id);
        CREATE INDEX IF NOT EXISTS idx_data_center_files_team ON data_center_files(team_id);
        CREATE INDEX IF NOT EXISTS idx_data_board_values_row ON data_board_values(row_id);
        CREATE INDEX IF NOT EXISTS idx_data_board_columns_board ON data_board_columns(board_id);
        CREATE INDEX IF NOT EXISTS idx_data_board_rows_board ON data_board_rows(board_id);
        CREATE INDEX IF NOT EXISTS idx_users_team ON users(team_id);
        CREATE INDEX IF NOT EXISTS idx_calendar_events_team ON calendar_events(team_id);
        CREATE INDEX IF NOT EXISTS idx_filming_clips_course ON filming_clips(course_id);
        CREATE INDEX IF NOT EXISTS idx_mkt_video_month ON mkt_video_log(month);
        CREATE INDEX IF NOT EXISTS idx_mkt_video_major ON mkt_video_log(cat_major);
        CREATE INDEX IF NOT EXISTS idx_mkt_weekly_week ON mkt_weekly_metrics(week);
        CREATE INDEX IF NOT EXISTS idx_mkt_weekly_cat ON mkt_weekly_metrics(category);
        CREATE INDEX IF NOT EXISTS idx_weekly_reports_wk ON weekly_reports(week_start,team_id);
        CREATE INDEX IF NOT EXISTS idx_weekly_reports_user ON weekly_reports(user_id);
        """)
        # 런타임 추가 컬럼(tasks.reviewer_id/created_by)은 존재할 때만 인덱스 생성
        tcols={r["name"] for r in c.execute("PRAGMA table_info(tasks)")}
        if "reviewer_id" in tcols:
            c.execute("CREATE INDEX IF NOT EXISTS idx_tasks_reviewer ON tasks(reviewer_id)")
        if "created_by" in tcols:
            c.execute("CREATE INDEX IF NOT EXISTS idx_tasks_created_by ON tasks(created_by)")
        c.commit()


def ensure_user_columns():
    with db() as c:
        cols={r["name"] for r in c.execute("PRAGMA table_info(users)")}
        if "active" not in cols:
            c.execute("ALTER TABLE users ADD COLUMN active INTEGER DEFAULT 1")
            c.execute("UPDATE users SET active=1 WHERE active IS NULL")
        if "login_id" not in cols:
            c.execute("ALTER TABLE users ADD COLUMN login_id TEXT")
        if "phone" not in cols:
            c.execute("ALTER TABLE users ADD COLUMN phone TEXT")
        if "account_status" not in cols:
            c.execute("ALTER TABLE users ADD COLUMN account_status TEXT DEFAULT 'ACTIVE'")
        if "password_hash" not in cols:
            c.execute("ALTER TABLE users ADD COLUMN password_hash TEXT")
        # 비밀번호 미설정 계정은 기본 비밀번호 '1234'로 일원화
        c.execute("UPDATE users SET password_hash=? WHERE password_hash IS NULL OR password_hash=''",(hash_pw('1234'),))
        # 아이디(login_id) 미설정 계정은 이메일 @ 앞부분으로 자동 채움 (이메일/아이디 로그인 지원)
        c.execute("""UPDATE users SET login_id=LOWER(SUBSTR(email,1,INSTR(email,'@')-1))
                     WHERE (login_id IS NULL OR login_id='') AND email LIKE '%@%'""")
        c.commit()

def ensure_planning_columns():
    with db() as c:
        cols={r["name"] for r in c.execute("PRAGMA table_info(planning_products)")}
        if "landing_exposed" not in cols:
            c.execute("ALTER TABLE planning_products ADD COLUMN landing_exposed INTEGER DEFAULT 1")
        if "updated_at" not in cols:
            c.execute("ALTER TABLE planning_products ADD COLUMN updated_at TEXT")
        # 체크리스트 완료 상태 컬럼(완료 버튼)
        ck={r["name"] for r in c.execute("PRAGMA table_info(checklists)")}
        if "status" not in ck:
            c.execute("ALTER TABLE checklists ADD COLUMN status TEXT DEFAULT '진행'")
        c.commit()

def ensure_calendar_columns():
    with db() as c:
        cols={r["name"] for r in c.execute("PRAGMA table_info(calendar_events)")}
        if "category" not in cols:
            c.execute("ALTER TABLE calendar_events ADD COLUMN category TEXT DEFAULT 'TEAM'")
            # 기존 일정 분류 보정: 팀 미지정은 전사, 지정은 팀
            c.execute("UPDATE calendar_events SET category=CASE WHEN team_id IS NULL THEN 'DEPARTMENT' ELSE 'TEAM' END WHERE category IS NULL OR category=''")
        c.commit()

def ensure_project_columns():
    with db() as c:
        cols={r["name"] for r in c.execute("PRAGMA table_info(projects)")}
        if "team_id" not in cols:
            c.execute("ALTER TABLE projects ADD COLUMN team_id INTEGER")
        if "created_by" not in cols:
            c.execute("ALTER TABLE projects ADD COLUMN created_by INTEGER")
        if "project_type" not in cols:
            c.execute("ALTER TABLE projects ADD COLUMN project_type TEXT DEFAULT 'APPROVAL_PROJECT'")
        if "approval_status" not in cols:
            c.execute("ALTER TABLE projects ADD COLUMN approval_status TEXT DEFAULT 'DRAFT'")
        if "start_date" not in cols:
            c.execute("ALTER TABLE projects ADD COLUMN start_date TEXT")
        if "risk_status" not in cols:
            c.execute("ALTER TABLE projects ADD COLUMN risk_status TEXT DEFAULT 'NORMAL'")
        c.commit()

def ensure_task_columns():
    with db() as c:
        cols={r["name"] for r in c.execute("PRAGMA table_info(tasks)")}
        if "blocked_reason" not in cols:
            c.execute("ALTER TABLE tasks ADD COLUMN blocked_reason TEXT")
        if "blocked_requested_to" not in cols:
            c.execute("ALTER TABLE tasks ADD COLUMN blocked_requested_to INTEGER")
        if "blocked_since" not in cols:
            c.execute("ALTER TABLE tasks ADD COLUMN blocked_since TEXT")
        if "category" not in cols:
            c.execute("ALTER TABLE tasks ADD COLUMN category TEXT")
        if "visibility_scope" not in cols:
            c.execute("ALTER TABLE tasks ADD COLUMN visibility_scope TEXT DEFAULT 'TEAM'")
        if "related_team_ids" not in cols:
            c.execute("ALTER TABLE tasks ADD COLUMN related_team_ids TEXT")
        if "completion_mode" not in cols:
            c.execute("ALTER TABLE tasks ADD COLUMN completion_mode TEXT DEFAULT 'REPORT'")
        if "reviewer_id" not in cols:
            c.execute("ALTER TABLE tasks ADD COLUMN reviewer_id INTEGER")
        if "created_by" not in cols:
            c.execute("ALTER TABLE tasks ADD COLUMN created_by INTEGER")
        if "work_type" not in cols:
            c.execute("ALTER TABLE tasks ADD COLUMN work_type TEXT DEFAULT 'PROJECT'")
        if "created_at" not in cols:
            c.execute("ALTER TABLE tasks ADD COLUMN created_at TEXT")
            c.execute("UPDATE tasks SET created_at=CURRENT_TIMESTAMP WHERE created_at IS NULL")
        c.commit()

def jdump(obj):
    return json.dumps(obj, ensure_ascii=False).encode("utf-8")

def one(q, default=None):
    if not q: return default
    return q[0]

def status_order(s):
    return {"TODO":1,"IN_PROGRESS":2,"BLOCKED":3,"REVIEW":4,"DONE":5}.get(s,9)

def add_log(c, task_id, actor_id, action, old_value="", new_value=""):
    c.execute("""INSERT INTO activity_logs(task_id,actor_id,action,old_value,new_value)
                 VALUES(?,?,?,?,?)""",(task_id,actor_id,action,old_value,new_value))

def notify(c, user_id, title, body=""):
    if user_id:
        c.execute("INSERT INTO notifications(user_id,title,body) VALUES(?,?,?)",(user_id,title,body))

class App(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        print("[HTTP]", fmt % args, flush=True)

    def send_json(self, obj, status=200):
        b = jdump(obj)
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def send_file(self, path: Path, ctype="text/html; charset=utf-8"):
        if not path.exists():
            self.send_error(404); return
        b = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def body(self):
        n = int(self.headers.get("Content-Length", "0") or 0)
        if n > MAX_BODY_BYTES:
            raise ValueError("요청 본문이 너무 큽니다(최대 30MB).")
        if not n: return {}
        return json.loads(self.rfile.read(n).decode("utf-8"))

    def do_GET(self):
        try:
            u = urlparse(self.path)
            p, q = u.path, parse_qs(u.query)

            if p == "/":
                return self.send_file(BASE/"static"/"index.html")
            if p.startswith("/static/"):
                f = safe_join(BASE/"static", p.split("/static/",1)[1])
                if not f: return self.send_error(403)
                ctype = "text/css" if f.suffix==".css" else "application/javascript" if f.suffix==".js" else "image/png" if f.suffix==".png" else "text/html; charset=utf-8"
                return self.send_file(f, ctype)
            if p.startswith("/uploads/"):
                f = safe_join(UPLOADS, unquote(p.split("/uploads/",1)[1]))
                if not f: return self.send_error(403)
                ctype = mimetypes.guess_type(str(f))[0] or "application/octet-stream"
                return self.send_file(f, ctype)

            with db() as c:
                if p == "/api/users":
                    rows=c.execute("""SELECT u.*,t.name team_name FROM users u LEFT JOIN teams t ON u.team_id=t.id
                    ORDER BY CASE WHEN u.role='DIVISION_ADMIN' THEN 0 ELSE 1 END,t.sort_order,u.is_team_leader DESC,u.id""").fetchall()
                    out=[dict(r) for r in rows]
                    for r in out:r.pop("password_hash",None)
                    return self.send_json(out)

                if p == "/api/teams":
                    out=[]
                    for r in c.execute("SELECT * FROM teams ORDER BY sort_order"):
                        d=dict(r)
                        d["members"]=[dict(x) for x in c.execute("SELECT * FROM users WHERE team_id=? AND COALESCE(active,1)=1 ORDER BY is_team_leader DESC,id",(r["id"],))]
                        out.append(d)
                    return self.send_json(out)

                if p == "/api/projects":
                    viewer_id=int(one(q.get("viewer_id"),0) or 0)
                    sql="""SELECT p.*,u.name pm_name,tm.name team_name,cr.name creator_name,
                    (SELECT COUNT(*) FROM tasks t WHERE t.project_id=p.id) task_count,
                    (SELECT COUNT(*) FROM tasks t WHERE t.project_id=p.id AND t.status='DONE') done_count,
                    (SELECT COUNT(*) FROM project_members pm WHERE pm.project_id=p.id) member_count,
                    (SELECT COUNT(*) FROM project_approvals pa WHERE pa.project_id=p.id AND pa.status='WAITING') waiting_approvals,
                    (CASE WHEN p.pm_user_id=? OR p.created_by=?
                        OR EXISTS(SELECT 1 FROM project_members pm WHERE pm.project_id=p.id AND pm.user_id=?)
                        OR EXISTS(SELECT 1 FROM tasks tx WHERE tx.project_id=p.id AND tx.assignee_id=?)
                        THEN 1 ELSE 0 END) is_member
                    FROM projects p LEFT JOIN users u ON p.pm_user_id=u.id
                    LEFT JOIN users cr ON p.created_by=cr.id
                    LEFT JOIN teams tm ON p.team_id=tm.id WHERE 1=1"""
                    args=[viewer_id,viewer_id,viewer_id,viewer_id]
                    if viewer_id:
                        viewer=c.execute("SELECT * FROM users WHERE id=?",(viewer_id,)).fetchone()
                        if viewer and viewer["role"]!="DIVISION_ADMIN":
                            sql+=""" AND (p.pm_user_id=? OR p.created_by=?
                            OR EXISTS(SELECT 1 FROM project_members pm WHERE pm.project_id=p.id AND pm.user_id=?)
                            OR EXISTS(SELECT 1 FROM project_teams pt WHERE pt.project_id=p.id AND pt.team_id=?)
                            OR EXISTS(SELECT 1 FROM project_approvals pa WHERE pa.project_id=p.id AND pa.approver_id=?)
                            OR EXISTS(SELECT 1 FROM tasks tx WHERE tx.project_id=p.id AND tx.assignee_id=?))"""
                            args += [viewer_id,viewer_id,viewer_id,viewer["team_id"],viewer_id,viewer_id]
                    sql+=" ORDER BY CASE p.approval_status WHEN 'WAITING' THEN 0 WHEN 'REJECTED' THEN 1 ELSE 2 END,p.id DESC"
                    rows=c.execute(sql,args).fetchall()
                    return self.send_json([dict(r) for r in rows])

                if p == "/api/tasks":
                    sql="""SELECT t.*,p.name project_name,u.name assignee_name,tm.name team_name,
                    cr.name creator_name,rv.name reviewer_name
                    FROM tasks t LEFT JOIN projects p ON t.project_id=p.id
                    LEFT JOIN users u ON t.assignee_id=u.id LEFT JOIN teams tm ON t.team_id=tm.id
                    LEFT JOIN users cr ON t.created_by=cr.id LEFT JOIN users rv ON t.reviewer_id=rv.id WHERE 1=1"""
                    args=[]
                    for k,col in [("project_id","t.project_id"),("assignee_id","t.assignee_id"),("team_id","t.team_id")]:
                        if q.get(k):
                            sql+=f" AND {col}=?"; args.append(int(q[k][0]))
                    viewer_id=int(one(q.get("viewer_id"),0) or 0)
                    if viewer_id and not q.get("project_id") and not q.get("assignee_id") and not q.get("team_id"):
                        viewer=c.execute("SELECT * FROM users WHERE id=?",(viewer_id,)).fetchone()
                        if viewer and viewer["role"] not in ("DIVISION_ADMIN","SUPER_ADMIN"):
                            # 프로젝트 업무는 TFT 참여자/참여팀에게만 노출(팀 공개 규칙으로 새어나가지 않도록 분리).
                            # 일반 업무만 팀/부서/관련팀 공개 규칙을 적용한다.
                            sql+=""" AND (
                              t.assignee_id=? OR t.created_by=? OR t.reviewer_id=?
                              OR EXISTS(SELECT 1 FROM task_approvals ta WHERE ta.task_id=t.id AND ta.approver_id=?)
                              OR EXISTS(SELECT 1 FROM task_assignees tas WHERE tas.task_id=t.id AND tas.user_id=?)
                              OR (t.work_type!='PROJECT' AND (
                                t.visibility_scope='DEPARTMENT'
                                OR (COALESCE(t.visibility_scope,'TEAM')='TEAM' AND t.team_id=?)
                                OR (t.visibility_scope='RELATED' AND (t.team_id=? OR (','||COALESCE(t.related_team_ids,'')||',') LIKE ?))
                              ))
                              OR (t.work_type='PROJECT' AND (
                                EXISTS(SELECT 1 FROM project_members pm WHERE pm.project_id=t.project_id AND pm.user_id=?)
                                OR EXISTS(SELECT 1 FROM project_teams pt WHERE pt.project_id=t.project_id AND pt.team_id=?)
                              ))
                            )"""
                            args += [viewer_id,viewer_id,viewer_id,viewer_id,viewer_id,viewer["team_id"],viewer["team_id"],f"%,{viewer['team_id']},%",viewer_id,viewer["team_id"]]
                    sql+=" ORDER BY t.status_order,t.priority_order,t.id"
                    return self.send_json([dict(r) for r in c.execute(sql,args)])

                if p.startswith("/api/tasks/") and p.count("/") == 3:
                    tid=int(p.rsplit("/",1)[1])
                    r=c.execute("""SELECT t.*,p.name project_name,u.name assignee_name,tm.name team_name,
                        rv.name reviewer_name,cr.name creator_name
                        FROM tasks t LEFT JOIN projects p ON t.project_id=p.id
                        LEFT JOIN users u ON t.assignee_id=u.id LEFT JOIN teams tm ON t.team_id=tm.id
                        LEFT JOIN users rv ON t.reviewer_id=rv.id LEFT JOIN users cr ON t.created_by=cr.id
                        WHERE t.id=?""",(tid,)).fetchone()
                    if not r:return self.send_json({"error":"not found"},404)
                    d=dict(r)
                    d["approvals"]=[dict(x) for x in c.execute("""SELECT ta.*,u.name approver_name,u.rank,u.job_title,t.name team_name
                        FROM task_approvals ta JOIN users u ON ta.approver_id=u.id
                        LEFT JOIN teams t ON u.team_id=t.id WHERE ta.task_id=? ORDER BY ta.approval_order""",(tid,))]
                    d["assignees"]=[dict(x) for x in c.execute("""SELECT tas.user_id,tas.role_label,u.name user_name,t.name team_name
                        FROM task_assignees tas JOIN users u ON tas.user_id=u.id LEFT JOIN teams t ON u.team_id=t.id
                        WHERE tas.task_id=? ORDER BY tas.id""",(tid,))]
                    rr=c.execute("""SELECT tr.*,u.name requester_name FROM task_review_requests tr
                        LEFT JOIN users u ON tr.requester_id=u.id WHERE tr.task_id=? ORDER BY tr.id DESC LIMIT 1""",(tid,)).fetchone()
                    if rr:
                        rd=dict(rr);rd["files"]=[dict(x) for x in c.execute("SELECT * FROM task_review_files WHERE request_id=? ORDER BY id",(rr["id"],))]
                        d["latest_review_request"]=rd
                    else:d["latest_review_request"]=None
                    return self.send_json(d)

                if p.startswith("/api/tasks/") and p.endswith("/comments"):
                    tid=int(p.split("/")[3])
                    rows=c.execute("""SELECT tc.*,u.name user_name FROM task_comments tc
                    LEFT JOIN users u ON tc.user_id=u.id WHERE tc.task_id=? ORDER BY tc.id""",(tid,)).fetchall()
                    return self.send_json([dict(r) for r in rows])

                if p.startswith("/api/tasks/") and p.endswith("/activity"):
                    tid=int(p.split("/")[3])
                    rows=c.execute("""SELECT a.*,u.name actor_name FROM activity_logs a
                    LEFT JOIN users u ON a.actor_id=u.id WHERE a.task_id=? ORDER BY a.id DESC""",(tid,)).fetchall()
                    return self.send_json([dict(r) for r in rows])

                if p == "/api/urgent-priority":
                    viewer_id=int(one(q.get("viewer_id"),0) or 0)
                    viewer=c.execute("SELECT * FROM users WHERE id=?",(viewer_id,)).fetchone()
                    if not viewer:return self.send_json({"error":"viewer not found"},404)
                    sql="""SELECT t.*,p.name project_name,u.name assignee_name,tm.name team_name
                           FROM tasks t LEFT JOIN projects p ON t.project_id=p.id
                           LEFT JOIN users u ON t.assignee_id=u.id LEFT JOIN teams tm ON t.team_id=tm.id
                           WHERE t.priority='URGENT' AND t.status!='DONE'"""
                    args=[]
                    if viewer["role"]=="DIVISION_ADMIN":
                        pass
                    elif viewer["role"]=="TEAM_LEADER":
                        sql+=" AND (t.team_id=? OR t.assignee_id=? OR t.visibility_scope='DEPARTMENT' OR (t.visibility_scope='RELATED' AND (','||COALESCE(t.related_team_ids,'')||',') LIKE ?))"
                        args += [viewer["team_id"],viewer_id,f"%,{viewer['team_id']},%"]
                    else:
                        sql+=""" AND (
                          t.assignee_id=? OR t.created_by=? OR t.reviewer_id=?
                          OR EXISTS(SELECT 1 FROM task_approvals ta WHERE ta.task_id=t.id AND ta.approver_id=?)
                          OR t.visibility_scope='DEPARTMENT'
                          OR (COALESCE(t.visibility_scope,'TEAM')='TEAM' AND t.team_id=?)
                          OR (t.visibility_scope='RELATED' AND (t.team_id=? OR (','||COALESCE(t.related_team_ids,'')||',') LIKE ?))
                        )"""
                        args += [viewer_id,viewer_id,viewer_id,viewer_id,viewer["team_id"],viewer["team_id"],f"%,{viewer['team_id']},%"]
                    sql+=" ORDER BY CASE WHEN t.status='BLOCKED' THEN 0 ELSE 1 END,COALESCE(t.due_date,'9999-12-31'),t.priority_order,t.id"
                    return self.send_json([dict(r) for r in c.execute(sql,args)])

                if p == "/api/blocked-priority":
                    viewer_id=int(one(q.get("viewer_id"),0) or 0)
                    viewer=c.execute("SELECT * FROM users WHERE id=?",(viewer_id,)).fetchone()
                    if not viewer:return self.send_json({"error":"viewer not found"},404)
                    sql="""SELECT t.*,p.name project_name,u.name assignee_name,tm.name team_name,
                           tl.name team_leader_name
                           FROM tasks t
                           LEFT JOIN projects p ON t.project_id=p.id
                           LEFT JOIN users u ON t.assignee_id=u.id
                           LEFT JOIN teams tm ON t.team_id=tm.id
                           LEFT JOIN users tl ON tl.team_id=t.team_id AND tl.is_team_leader=1
                           WHERE t.status='BLOCKED'"""
                    args=[]
                    if viewer["role"]=="TEAM_LEADER":
                        sql+=" AND t.team_id=?";args.append(viewer["team_id"])
                    elif viewer["role"]!="DIVISION_ADMIN":
                        sql+=" AND t.assignee_id=?";args.append(viewer_id)
                    sql+=" ORDER BY COALESCE(t.blocked_since,t.due_date,'9999-12-31'),t.priority_order,t.id"
                    return self.send_json([dict(r) for r in c.execute(sql,args)])

                if p == "/api/executive-dashboard":
                    viewer_id=int(one(q.get("viewer_id"),0) or 0)
                    viewer=c.execute("SELECT * FROM users WHERE id=?",(viewer_id,)).fetchone()
                    if not viewer:return self.send_json({"error":"viewer not found"},404)
                    role=viewer["role"]
                    conditions=[];args=[]
                    if role=="TEAM_LEADER":
                        conditions.append("t.team_id=?");args.append(viewer["team_id"])
                    elif role!="DIVISION_ADMIN":
                        conditions.append("t.assignee_id=?");args.append(viewer_id)
                    where=(" WHERE "+" AND ".join(conditions)) if conditions else ""
                    def cnt(extra=""):
                        sql="SELECT COUNT(*) FROM tasks t"+where
                        a=list(args)
                        if extra: sql+=(" AND " if where else " WHERE ")+extra
                        return c.execute(sql,a).fetchone()[0]
                    totals={"all":cnt(),"active":cnt("t.status!='DONE'"),"done":cnt("t.status='DONE'"),
                            "blocked":cnt("t.status='BLOCKED'"),
                            "delayed":cnt("t.due_date<date('now') AND t.status!='DONE'"),
                            "review":cnt("t.status='REVIEW'"),
                            "urgent":cnt("t.priority='URGENT' AND t.status!='DONE'")}
                    team_rows=[]
                    if role=="DIVISION_ADMIN":
                        for tm in c.execute("SELECT * FROM teams WHERE active=1 ORDER BY sort_order"):
                            tid=tm["id"]
                            alln=c.execute("SELECT COUNT(*) FROM tasks WHERE team_id=?",(tid,)).fetchone()[0]
                            done=c.execute("SELECT COUNT(*) FROM tasks WHERE team_id=? AND status='DONE'",(tid,)).fetchone()[0]
                            blocked=c.execute("SELECT COUNT(*) FROM tasks WHERE team_id=? AND status='BLOCKED'",(tid,)).fetchone()[0]
                            delayed=c.execute("SELECT COUNT(*) FROM tasks WHERE team_id=? AND due_date<date('now') AND status!='DONE'",(tid,)).fetchone()[0]
                            team_rows.append({"team_id":tid,"team_name":tm["name"],"all":alln,"done":done,"blocked":blocked,
                                              "delayed":delayed,"progress":round(done/alln*100) if alln else 0})
                    workload=[]
                    if role in ("DIVISION_ADMIN","TEAM_LEADER"):
                        wwhere="" if role=="DIVISION_ADMIN" else "WHERE u.team_id=?"
                        wargs=[] if role=="DIVISION_ADMIN" else [viewer["team_id"]]
                        workload=[dict(r) for r in c.execute("""SELECT u.id user_id,u.name,u.team_id,tm.name team_name,
                            SUM(CASE WHEN t.status!='DONE' THEN 1 ELSE 0 END) active_count,
                            SUM(CASE WHEN t.status='BLOCKED' THEN 1 ELSE 0 END) blocked_count,
                            SUM(CASE WHEN t.priority='URGENT' AND t.status!='DONE' THEN 1 ELSE 0 END) urgent_count
                            FROM users u LEFT JOIN teams tm ON u.team_id=tm.id LEFT JOIN tasks t ON t.assignee_id=u.id
                            """+wwhere+""" GROUP BY u.id ORDER BY active_count DESC,u.name""",wargs)]
                    my_tasks=[dict(r) for r in c.execute("""SELECT t.*,p.name project_name,tm.name team_name
                        FROM tasks t LEFT JOIN projects p ON t.project_id=p.id LEFT JOIN teams tm ON t.team_id=tm.id
                        WHERE t.assignee_id=? AND t.status!='DONE'
                        ORDER BY CASE WHEN t.priority='URGENT' THEN 0 WHEN t.status='BLOCKED' THEN 1 ELSE 2 END,
                        t.due_date,t.priority_order LIMIT 20""",(viewer_id,))]
                    return self.send_json({"role":role,"viewer_name":viewer["name"],"totals":totals,
                                           "teams":team_rows,"workload":workload,"my_tasks":my_tasks})

                if p == "/api/team-dashboard":
                    tid=int(one(q.get("team_id"),0) or 0)
                    if not tid:return self.send_json({"error":"team_id required"},400)
                    totals={
                        "all":c.execute("SELECT COUNT(*) FROM tasks WHERE team_id=?",(tid,)).fetchone()[0],
                        "active":c.execute("SELECT COUNT(*) FROM tasks WHERE team_id=? AND status!='DONE'",(tid,)).fetchone()[0],
                        "done":c.execute("SELECT COUNT(*) FROM tasks WHERE team_id=? AND status='DONE'",(tid,)).fetchone()[0],
                        "blocked":c.execute("SELECT COUNT(*) FROM tasks WHERE team_id=? AND status='BLOCKED'",(tid,)).fetchone()[0],
                        "delayed":c.execute("SELECT COUNT(*) FROM tasks WHERE team_id=? AND due_date<date('now') AND status!='DONE'",(tid,)).fetchone()[0],
                        "review":c.execute("SELECT COUNT(*) FROM tasks WHERE team_id=? AND status='REVIEW'",(tid,)).fetchone()[0]
                    }
                    status_counts={s:c.execute("SELECT COUNT(*) FROM tasks WHERE team_id=? AND status=?",(tid,s)).fetchone()[0]
                                   for s in ["TODO","IN_PROGRESS","BLOCKED","REVIEW","DONE"]}
                    members=[dict(r) for r in c.execute("""SELECT u.id,u.name,u.rank,
                        SUM(CASE WHEN t.status!='DONE' THEN 1 ELSE 0 END) active_count,
                        SUM(CASE WHEN t.status='BLOCKED' THEN 1 ELSE 0 END) blocked_count,
                        SUM(CASE WHEN t.due_date<date('now') AND t.status!='DONE' THEN 1 ELSE 0 END) delayed_count
                        FROM users u LEFT JOIN tasks t ON t.assignee_id=u.id
                        WHERE u.team_id=? GROUP BY u.id ORDER BY active_count DESC,u.name""",(tid,))]
                    urgent=[dict(r) for r in c.execute("""SELECT t.*,p.name project_name,u.name assignee_name
                        FROM tasks t LEFT JOIN projects p ON t.project_id=p.id LEFT JOIN users u ON t.assignee_id=u.id
                        WHERE t.team_id=? AND t.status!='DONE'
                        ORDER BY CASE WHEN t.status='BLOCKED' THEN 0 WHEN t.priority='URGENT' THEN 1 ELSE 2 END,t.due_date
                        LIMIT 10""",(tid,))]
                    return self.send_json({"totals":totals,"status_counts":status_counts,"members":members,"urgent":urgent})

                if p == "/api/dashboard":
                    uid=int(one(q.get("user_id"),0) or 0)
                    d={
                        "project_count":c.execute("SELECT COUNT(*) FROM projects WHERE status='ACTIVE'").fetchone()[0],
                        "task_count":c.execute("SELECT COUNT(*) FROM tasks").fetchone()[0],
                        "delayed_count":c.execute("SELECT COUNT(*) FROM tasks WHERE due_date<date('now') AND status!='DONE'").fetchone()[0],
                        "urgent_count":c.execute("SELECT COUNT(*) FROM tasks WHERE priority='URGENT' AND status!='DONE'").fetchone()[0],
                        "blocked_count":c.execute("SELECT COUNT(*) FROM tasks WHERE status='BLOCKED'").fetchone()[0]
                    }
                    if uid:
                        d["my_tasks"]=c.execute("""SELECT COUNT(DISTINCT t.id) FROM tasks t
                            WHERE t.status!='DONE' AND (
                              t.assignee_id=? OR t.created_by=? OR t.reviewer_id=?
                              OR EXISTS(SELECT 1 FROM task_approvals ta WHERE ta.task_id=t.id AND ta.approver_id=?)
                            )""",(uid,uid,uid,uid)).fetchone()[0]
                        d["incoming_requests"]=c.execute("SELECT COUNT(*) FROM requests WHERE target_user_id=? AND status!='DONE'",(uid,)).fetchone()[0]
                        d["outgoing_requests"]=c.execute("SELECT COUNT(*) FROM requests WHERE requester_id=? AND status!='DONE'",(uid,)).fetchone()[0]
                    return self.send_json(d)

                if p == "/api/requests":
                    uid=int(one(q.get("user_id"),0) or 0)
                    direction=one(q.get("direction"),"incoming")
                    sql="""SELECT r.*,u1.name requester_name,u2.name target_name,t.name target_team_name
                    FROM requests r LEFT JOIN users u1 ON r.requester_id=u1.id LEFT JOIN users u2 ON r.target_user_id=u2.id
                    LEFT JOIN teams t ON r.target_team_id=t.id WHERE 1=1"""
                    args=[]
                    if uid:
                        if direction=="incoming":
                            sql+=" AND (r.target_user_id=? OR (r.target_user_id IS NULL AND r.target_team_id=(SELECT team_id FROM users WHERE id=?)))"
                            args += [uid,uid]
                        else:
                            sql+=" AND r.requester_id=?"; args.append(uid)
                    sql+=" ORDER BY r.id DESC"
                    return self.send_json([dict(r) for r in c.execute(sql,args)])

                if p.startswith("/api/requests/") and p.endswith("/replies"):
                    rid=int(p.split("/")[3])
                    rows=c.execute("""SELECT rr.*,u.name sender_name FROM request_replies rr
                    LEFT JOIN users u ON rr.sender_id=u.id
                    WHERE rr.request_id=? ORDER BY rr.id""",(rid,)).fetchall()
                    return self.send_json([dict(r) for r in rows])

                if p.startswith("/api/requests/") and p.count("/")==3:
                    rid=int(p.rsplit("/",1)[1])
                    r=c.execute("""SELECT r.*,u1.name requester_name,u2.name target_name,t.name target_team_name
                    FROM requests r
                    LEFT JOIN users u1 ON r.requester_id=u1.id
                    LEFT JOIN users u2 ON r.target_user_id=u2.id
                    LEFT JOIN teams t ON r.target_team_id=t.id
                    WHERE r.id=?""",(rid,)).fetchone()
                    return self.send_json(dict(r) if r else {"error":"not found"},200 if r else 404)

                if p.startswith("/api/calendar/") and p.count("/")==3:
                    eid=int(p.rsplit("/",1)[1])
                    r=c.execute("""SELECT ce.*,t.name team_name,u.name owner_name FROM calendar_events ce
                    LEFT JOIN teams t ON ce.team_id=t.id LEFT JOIN users u ON ce.owner_id=u.id WHERE ce.id=?""",(eid,)).fetchone()
                    return self.send_json(dict(r) if r else {"error":"not found"},200 if r else 404)

                if p == "/api/calendar":
                    month=one(q.get("month"))
                    frm=one(q.get("from"));to=one(q.get("to"))
                    team=one(q.get("team_id"))
                    et=one(q.get("event_type"))
                    viewer_id=int(one(q.get("viewer_id"),0) or 0)
                    sql="""SELECT ce.*,t.name team_name,u.name owner_name FROM calendar_events ce
                    LEFT JOIN teams t ON ce.team_id=t.id LEFT JOIN users u ON ce.owner_id=u.id WHERE 1=1"""
                    args=[]
                    # 개인(PERSONAL) 일정은 본인에게만 노출한다.
                    if viewer_id:
                        sql+=" AND (COALESCE(ce.category,'')!='PERSONAL' OR ce.owner_id=?)";args.append(viewer_id)
                    # from/to 우선: 기간과 겹치는 일정(시작~종료)을 모두 포함
                    if frm and to:
                        sql+=" AND COALESCE(ce.end_date,ce.start_date)>=? AND ce.start_date<=?";args+= [frm,to]
                    elif month: sql+=" AND substr(ce.start_date,1,7)=?";args.append(month)
                    if team: sql+=" AND ce.team_id=?";args.append(int(team))
                    if et: sql+=" AND ce.event_type=?";args.append(et)
                    rows=[dict(r) for r in c.execute(sql,args)]
                    tq="""SELECT tsk.id,tsk.title,tsk.due_date,tsk.team_id,tsk.project_id,tsk.work_type,tsk.assignee_id,tm.name team_name,u.name owner_name,p.name project_name
                    FROM tasks tsk LEFT JOIN teams tm ON tsk.team_id=tm.id LEFT JOIN users u ON tsk.assignee_id=u.id LEFT JOIN projects p ON tsk.project_id=p.id WHERE tsk.due_date IS NOT NULL"""
                    ta=[]
                    if frm and to:
                        tq+=" AND tsk.due_date>=? AND tsk.due_date<=?";ta+= [frm,to]
                    elif month: tq+=" AND substr(tsk.due_date,1,7)=?";ta.append(month)
                    if team: tq+=" AND tsk.team_id=?";ta.append(int(team))
                    for r in c.execute(tq,ta):
                        d=dict(r)
                        is_proj=d.get("work_type")=="PROJECT" and d.get("project_id")
                        rows.append({"id":"task-"+str(d["id"]),"title":d["title"],"start_date":d["due_date"],"end_date":d["due_date"],"event_type":"TASK_DUE","category":"PROJECT" if is_proj else "TASK","team_id":d["team_id"],"team_name":d["team_name"],"owner_id":d["assignee_id"],"owner_name":d["owner_name"],"project_name":d.get("project_name"),"description":("프로젝트 업무 마감일" if is_proj else "업무 마감일"),"source":"TASK"})
                    rows.sort(key=lambda x:(x["start_date"],str(x["id"])))
                    return self.send_json(rows)

                if p.startswith("/api/workspace/"):
                    tid=int(p.rsplit("/",1)[1])
                    team=c.execute("SELECT * FROM teams WHERE id=?",(tid,)).fetchone()
                    if not team:return self.send_json({"error":"not found"},404)
                    modules=[dict(r) for r in c.execute("""SELECT m.*,COALESCE(tm.enabled,0) enabled FROM modules m
                    LEFT JOIN team_modules tm ON m.id=tm.module_id AND tm.team_id=? ORDER BY m.sort_order""",(tid,))]
                    return self.send_json({
                        "team":dict(team),"modules":modules,
                        "boards":[dict(r) for r in c.execute("SELECT * FROM data_boards WHERE team_id=? ORDER BY id",(tid,))],
                        "meetings":[dict(r) for r in c.execute("SELECT * FROM meetings WHERE team_id=? ORDER BY meeting_date DESC LIMIT 10",(tid,))],
                        "checklists":[dict(r) for r in c.execute("SELECT * FROM checklists WHERE team_id=? ORDER BY id DESC",(tid,))],
                        "resources":[dict(r) for r in c.execute("SELECT * FROM resources WHERE team_id=? ORDER BY id DESC",(tid,))]
                    })

                if p.startswith("/api/boards/"):
                    bid=int(p.rsplit("/",1)[1])
                    b=c.execute("SELECT * FROM data_boards WHERE id=?",(bid,)).fetchone()
                    if not b:return self.send_json({"error":"not found"},404)
                    cols=[dict(r) for r in c.execute("SELECT * FROM data_board_columns WHERE board_id=? ORDER BY sort_order,id",(bid,))]
                    rows=[]
                    for r in c.execute("SELECT * FROM data_board_rows WHERE board_id=? ORDER BY id DESC",(bid,)):
                        d=dict(r);d["values"]={x["column_key"]:x["value"] for x in c.execute("SELECT * FROM data_board_values WHERE row_id=?",(r["id"],))}
                        rows.append(d)
                    return self.send_json({"board":dict(b),"columns":cols,"rows":rows})

                if p == "/api/management/tasks":
                    viewer_id=int(one(q.get("viewer_id"),0) or 0)
                    team_filter=one(q.get("team_id"))
                    member_filter=one(q.get("member_id"))
                    viewer=c.execute("SELECT * FROM users WHERE id=?",(viewer_id,)).fetchone()
                    if not viewer:return self.send_json({"error":"viewer not found"},404)
                    sql="""SELECT t.*,p.name project_name,u.name assignee_name,tm.name team_name,u.rank assignee_rank
                           FROM tasks t LEFT JOIN projects p ON t.project_id=p.id
                           LEFT JOIN users u ON t.assignee_id=u.id LEFT JOIN teams tm ON t.team_id=tm.id
                           WHERE 1=1"""
                    args=[]
                    if viewer["role"]=="DIVISION_ADMIN":
                        if team_filter:
                            sql+=" AND t.team_id=?";args.append(int(team_filter))
                    elif viewer["role"]=="TEAM_LEADER":
                        sql+=""" AND (
                          t.team_id=? OR t.assignee_id=? OR t.created_by=? OR t.reviewer_id=?
                          OR EXISTS(SELECT 1 FROM task_approvals ta WHERE ta.task_id=t.id AND ta.approver_id=?)
                          OR t.visibility_scope='DEPARTMENT'
                          OR (t.visibility_scope='RELATED' AND (','||COALESCE(t.related_team_ids,'')||',') LIKE ?)
                        )"""
                        args += [viewer["team_id"],viewer_id,viewer_id,viewer_id,viewer_id,f"%,{viewer['team_id']},%"]
                    else:
                        sql+=""" AND (
                          t.assignee_id=? OR t.created_by=? OR t.reviewer_id=?
                          OR EXISTS(SELECT 1 FROM task_approvals ta WHERE ta.task_id=t.id AND ta.approver_id=?)
                          OR t.visibility_scope='DEPARTMENT'
                          OR (COALESCE(t.visibility_scope,'TEAM')='TEAM' AND t.team_id=?)
                          OR (t.visibility_scope='RELATED' AND (t.team_id=? OR (','||COALESCE(t.related_team_ids,'')||',') LIKE ?))
                        )"""
                        args += [viewer_id,viewer_id,viewer_id,viewer_id,viewer["team_id"],viewer["team_id"],f"%,{viewer['team_id']},%"]
                    if member_filter:
                        sql+=" AND t.assignee_id=?";args.append(int(member_filter))
                    sql+=" ORDER BY CASE t.status WHEN 'BLOCKED' THEN 0 WHEN 'IN_PROGRESS' THEN 1 WHEN 'REVIEW' THEN 2 WHEN 'TODO' THEN 3 ELSE 4 END,t.priority_order,t.id"
                    return self.send_json([dict(r) for r in c.execute(sql,args)])

                if p == "/api/directives":
                    uid=int(one(q.get("user_id"),0) or 0)
                    direction=one(q.get("direction"),"incoming")
                    if direction=="outgoing":
                        rows=c.execute("""SELECT d.*,s.name sender_name,r.name recipient_name,tm.name recipient_team
                        FROM directives d LEFT JOIN users s ON d.sender_id=s.id LEFT JOIN users r ON d.recipient_id=r.id
                        LEFT JOIN teams tm ON r.team_id=tm.id WHERE d.sender_id=? ORDER BY d.id DESC""",(uid,)).fetchall()
                    else:
                        rows=c.execute("""SELECT d.*,s.name sender_name,r.name recipient_name,tm.name recipient_team
                        FROM directives d LEFT JOIN users s ON d.sender_id=s.id LEFT JOIN users r ON d.recipient_id=r.id
                        LEFT JOIN teams tm ON r.team_id=tm.id WHERE d.recipient_id=? ORDER BY d.id DESC""",(uid,)).fetchall()
                    return self.send_json([dict(r) for r in rows])

                if p.startswith("/api/directives/") and p.endswith("/replies"):
                    did=int(p.split("/")[3])
                    rows=c.execute("""SELECT dr.*,u.name sender_name FROM directive_replies dr
                    LEFT JOIN users u ON dr.sender_id=u.id WHERE dr.directive_id=? ORDER BY dr.id""",(did,)).fetchall()
                    return self.send_json([dict(r) for r in rows])

                if p.startswith("/api/directives/") and p.count("/")==3:
                    did=int(p.rsplit("/",1)[1])
                    r=c.execute("""SELECT d.*,s.name sender_name,r.name recipient_name,tm.name recipient_team
                    FROM directives d LEFT JOIN users s ON d.sender_id=s.id LEFT JOIN users r ON d.recipient_id=r.id
                    LEFT JOIN teams tm ON r.team_id=tm.id WHERE d.id=?""",(did,)).fetchone()
                    if not r:return self.send_json({"error":"not found"},404)
                    out=dict(r)
                    out["files"]=[dict(f) for f in c.execute("SELECT * FROM directive_files WHERE directive_id=? ORDER BY id",(did,))]
                    return self.send_json(out)

                if p == "/api/purchases":
                    team=one(q.get("team_id"))
                    sql="""SELECT pu.*,tm.name team_name,u.name purchaser_name FROM purchases pu
                    LEFT JOIN teams tm ON pu.team_id=tm.id LEFT JOIN users u ON pu.purchaser_id=u.id WHERE 1=1"""
                    args=[]
                    if team: sql+=" AND pu.team_id=?";args.append(int(team))
                    sql+=" ORDER BY purchase_date DESC,pu.id DESC"
                    return self.send_json([dict(r) for r in c.execute(sql,args)])

                if p == "/api/refunds":
                    rows=c.execute("""SELECT rf.*,u.name instructor_name FROM refunds rf
                    LEFT JOIN users u ON rf.instructor_id=u.id ORDER BY refund_date DESC,rf.id DESC""").fetchall()
                    return self.send_json([dict(r) for r in rows])

                if p == "/api/book-discussions":
                    team=one(q.get("team_id"))
                    sql="""SELECT bd.*,tm.name team_name FROM book_discussions bd LEFT JOIN teams tm ON bd.team_id=tm.id WHERE 1=1"""
                    args=[]
                    if team: sql+=" AND bd.team_id=?";args.append(int(team))
                    sql+=" ORDER BY bd.month_label DESC,bd.id DESC"
                    return self.send_json([dict(r) for r in c.execute(sql,args)])

                if p == "/api/team-summary":
                    tid=int(one(q.get("team_id"),0) or 0)
                    summary={
                        "active_tasks":c.execute("SELECT COUNT(*) FROM tasks WHERE team_id=? AND status!='DONE'",(tid,)).fetchone()[0],
                        "blocked":c.execute("SELECT COUNT(*) FROM tasks WHERE team_id=? AND status='BLOCKED'",(tid,)).fetchone()[0],
                        "review":c.execute("SELECT COUNT(*) FROM tasks WHERE team_id=? AND status='REVIEW'",(tid,)).fetchone()[0],
                        "delayed":c.execute("SELECT COUNT(*) FROM tasks WHERE team_id=? AND due_date<date('now') AND status!='DONE'",(tid,)).fetchone()[0],
                        "meetings":c.execute("SELECT COUNT(*) FROM meetings WHERE team_id=?",(tid,)).fetchone()[0],
                        "purchases":c.execute("SELECT COUNT(*) FROM purchases WHERE team_id=?",(tid,)).fetchone()[0],
                    }
                    return self.send_json(summary)

                if p == "/api/customers":
                    term=(one(q.get("q"),"") or "").strip()
                    sql="""SELECT c.*,
                        (SELECT COUNT(*) FROM consultation_logs cl WHERE cl.customer_id=c.id) consultation_count,
                        (SELECT MAX(created_at) FROM consultation_logs cl WHERE cl.customer_id=c.id) last_consultation
                        FROM customers c WHERE 1=1"""
                    args=[]
                    if term:
                        like="%"+term+"%"
                        sql+=" AND (c.name LIKE ? OR c.phone LIKE ? OR c.email LIKE ? OR c.member_id LIKE ? OR c.company LIKE ?)"
                        args += [like]*5
                    sql+=" ORDER BY COALESCE(last_contact_at,created_at) DESC,c.id DESC"
                    return self.send_json([dict(r) for r in c.execute(sql,args)])

                if p.startswith("/api/customers/") and p.count("/")==3:
                    cid=int(p.rsplit("/",1)[1])
                    cust=c.execute("SELECT * FROM customers WHERE id=?",(cid,)).fetchone()
                    if not cust:return self.send_json({"error":"not found"},404)
                    logs=[dict(r) for r in c.execute("""SELECT cl.*,u.name counselor_name FROM consultation_logs cl
                        LEFT JOIN users u ON cl.counselor_id=u.id WHERE cl.customer_id=? ORDER BY cl.id DESC""",(cid,))]
                    return self.send_json({"customer":dict(cust),"logs":logs})

                if p == "/api/consultations":
                    product=one(q.get("product"))
                    category=one(q.get("category"))
                    sql="""SELECT cl.*,c.name customer_name,c.phone customer_phone,u.name counselor_name
                    FROM consultation_logs cl LEFT JOIN customers c ON cl.customer_id=c.id
                    LEFT JOIN users u ON cl.counselor_id=u.id WHERE 1=1"""
                    args=[]
                    if product: sql+=" AND cl.product_name=?";args.append(product)
                    if category: sql+=" AND cl.inquiry_category=?";args.append(category)
                    sql+=" ORDER BY cl.id DESC"
                    return self.send_json([dict(r) for r in c.execute(sql,args)])

                if p == "/api/service-knowledge":
                    term=(one(q.get("q"),"") or "").strip()
                    product=one(q.get("product"))
                    sql="SELECT sk.*,u.name updated_by_name FROM service_knowledge sk LEFT JOIN users u ON sk.updated_by=u.id WHERE 1=1"
                    args=[]
                    if term:
                        like="%"+term+"%"
                        sql+=" AND (sk.question LIKE ? OR sk.answer LIKE ? OR sk.category LIKE ? OR sk.product_name LIKE ? OR sk.keywords LIKE ?)"
                        # keywords may not exist on old schema; keep to supported cols only
                        sql=sql.replace(" OR sk.keywords LIKE ?","")
                        args += [like]*4
                    if product: sql+=" AND sk.product_name=?";args.append(product)
                    sql+=" ORDER BY sk.usage_count DESC,sk.updated_at DESC"
                    return self.send_json([dict(r) for r in c.execute(sql,args)])

                if p == "/api/product-studies":
                    product=one(q.get("product"))
                    sql="""SELECT ps.*,u.name owner_name FROM product_studies ps
                    LEFT JOIN users u ON ps.owner_id=u.id WHERE 1=1"""
                    args=[]
                    if product: sql+=" AND ps.product_name=?";args.append(product)
                    sql+=" ORDER BY ps.updated_at DESC,ps.id DESC"
                    return self.send_json([dict(r) for r in c.execute(sql,args)])

                if p == "/api/service-analytics":
                    total=c.execute("SELECT COUNT(*) FROM consultation_logs").fetchone()[0]
                    today=c.execute("SELECT COUNT(*) FROM consultation_logs WHERE date(created_at)=date('now')").fetchone()[0]
                    openf=c.execute("SELECT COUNT(*) FROM consultation_logs WHERE status!='완료'").fetchone()[0]
                    customers=c.execute("SELECT COUNT(*) FROM customers").fetchone()[0]
                    categories=[dict(r) for r in c.execute("""SELECT COALESCE(inquiry_category,'미분류') label,COUNT(*) count
                        FROM consultation_logs GROUP BY inquiry_category ORDER BY count DESC""")]
                    products=[dict(r) for r in c.execute("""SELECT COALESCE(product_name,'미지정') label,COUNT(*) count
                        FROM consultation_logs GROUP BY product_name ORDER BY count DESC LIMIT 10""")]
                    frequent=[dict(r) for r in c.execute("""SELECT question,answer,product_name,category,usage_count
                        FROM service_knowledge ORDER BY usage_count DESC,updated_at DESC LIMIT 10""")]
                    return self.send_json({"total":total,"today":today,"open":openf,"customers":customers,
                                           "categories":categories,"products":products,"frequent":frequent})

                if p == "/api/marketing/channels":
                    rows=c.execute("""SELECT mc.*,u.name owner_name FROM marketing_channels mc
                    LEFT JOIN users u ON mc.owner_id=u.id ORDER BY COALESCE(mc.publish_date,mc.planned_date) DESC,mc.id DESC""").fetchall()
                    return self.send_json([dict(r) for r in rows])

                if p.startswith("/api/marketing/channels/") and p.endswith("/metrics"):
                    cid=int(p.split("/")[4])
                    rows=c.execute("SELECT * FROM marketing_channel_metrics WHERE channel_id=? ORDER BY metric_date DESC,id DESC",(cid,)).fetchall()
                    return self.send_json([dict(r) for r in rows])

                if p == "/api/marketing/campaigns":
                    rows=c.execute("""SELECT mc.*,u.name owner_name FROM marketing_campaigns mc
                    LEFT JOIN users u ON mc.owner_id=u.id ORDER BY COALESCE(mc.end_date,mc.start_date) DESC,mc.id DESC""").fetchall()
                    return self.send_json([dict(r) for r in rows])

                if p == "/api/marketing/leads":
                    rows=c.execute("""SELECT ml.*,u.name owner_name,mc.campaign_name FROM marketing_leads ml
                    LEFT JOIN users u ON ml.owner_id=u.id LEFT JOIN marketing_campaigns mc ON ml.campaign_id=mc.id
                    ORDER BY ml.lead_date DESC,ml.id DESC""").fetchall()
                    return self.send_json([dict(r) for r in rows])

                if p == "/api/marketing/events":
                    rows=c.execute("""SELECT me.*,u.name owner_name FROM marketing_events me
                    LEFT JOIN users u ON me.owner_id=u.id ORDER BY me.event_date DESC,me.id DESC""").fetchall()
                    return self.send_json([dict(r) for r in rows])

                if p == "/api/marketing/collab":
                    rows=c.execute("""SELECT mr.*,rt.name requester_team_name,ru.name requester_name,ou.name owner_name
                    FROM marketing_collab_requests mr LEFT JOIN teams rt ON mr.requester_team_id=rt.id
                    LEFT JOIN users ru ON mr.requester_id=ru.id LEFT JOIN users ou ON mr.owner_id=ou.id
                    ORDER BY mr.due_date,mr.id DESC""").fetchall()
                    return self.send_json([dict(r) for r in rows])

                if p == "/api/marketing/performance":
                    rows=c.execute("""SELECT mp.*,u.name owner_name FROM marketing_performance_snapshots mp
                    LEFT JOIN users u ON mp.owner_id=u.id ORDER BY mp.snapshot_month DESC,mp.id DESC""").fetchall()
                    return self.send_json([dict(r) for r in rows])

                if p == "/api/marketing/overview":
                    active_campaigns=c.execute("SELECT COUNT(*) FROM marketing_campaigns WHERE status NOT IN ('완료','중단')").fetchone()[0]
                    scheduled_contents=c.execute("""SELECT COUNT(*) FROM marketing_channels
                        WHERE status!='발행완료' AND planned_date BETWEEN date('now') AND date('now','+7 day')""").fetchone()[0]
                    active_ads=c.execute("SELECT COUNT(*) FROM marketing_campaigns WHERE status='집행중'").fetchone()[0]
                    due_collab=c.execute("""SELECT COUNT(*) FROM marketing_collab_requests
                        WHERE status!='완료' AND due_date<=date('now','+7 day')""").fetchone()[0]
                    leads_month=c.execute("""SELECT COUNT(*) FROM marketing_leads
                        WHERE substr(lead_date,1,7)=strftime('%Y-%m','now')""").fetchone()[0]
                    contents=[dict(r) for r in c.execute("""SELECT mc.*,u.name owner_name FROM marketing_channels mc
                        LEFT JOIN users u ON mc.owner_id=u.id ORDER BY COALESCE(mc.publish_date,mc.planned_date) DESC LIMIT 8""")]
                    campaigns=[dict(r) for r in c.execute("""SELECT mc.*,u.name owner_name FROM marketing_campaigns mc
                        LEFT JOIN users u ON mc.owner_id=u.id ORDER BY COALESCE(mc.end_date,mc.start_date) DESC LIMIT 8""")]
                    return self.send_json({"active_campaigns":active_campaigns,"scheduled_contents":scheduled_contents,
                        "active_ads":active_ads,"due_collab":due_collab,"leads_month":leads_month,
                        "contents":contents,"campaigns":campaigns})

                # ===== 콘텐츠 제작(콘.제작) 강의촬영 스케줄 =====
                if p == "/api/filming/courses":
                    rows=c.execute("SELECT * FROM filming_courses ORDER BY ended,id").fetchall()
                    out=[]
                    for r in rows:
                        d=dict(r)
                        clips=c.execute("SELECT video_len,coding_date,settle_status FROM filming_clips WHERE course_id=?",(r["id"],)).fetchall()
                        total=len(clips)
                        shot=sum(1 for x in clips if (x["video_len"] or "").strip())
                        coded=sum(1 for x in clips if (x["coding_date"] or "").strip())
                        settled=sum(1 for x in clips if (x["settle_status"] or "")=="정산완료")
                        d.update(total_clips=total,shot=shot,coded=coded,settled=settled,
                            shot_rate=round(shot*100/total) if total else 0,
                            coded_rate=round(coded*100/total) if total else 0,
                            settled_rate=round(settled*100/total) if total else 0)
                        out.append(d)
                    return self.send_json(out)

                if p == "/api/filming/overview":
                    courses=c.execute("SELECT id,ended FROM filming_courses").fetchall()
                    clips=c.execute("SELECT video_len,coding_date,settle_status FROM filming_clips").fetchall()
                    total=len(clips)
                    shot=sum(1 for x in clips if (x["video_len"] or "").strip())
                    coded=sum(1 for x in clips if (x["coding_date"] or "").strip())
                    settled=sum(1 for x in clips if (x["settle_status"] or "")=="정산완료")
                    # 담당교수별 작업량(진행 클립 수 = 촬영된 클립)
                    by_prof=[dict(r) for r in c.execute("""
                        SELECT COALESCE(fc.professor,'미지정') professor,
                               COUNT(cl.id) total,
                               SUM(CASE WHEN TRIM(COALESCE(cl.video_len,''))<>'' THEN 1 ELSE 0 END) shot,
                               SUM(CASE WHEN TRIM(COALESCE(cl.coding_date,''))<>'' THEN 1 ELSE 0 END) coded,
                               SUM(CASE WHEN cl.settle_status='정산완료' THEN 1 ELSE 0 END) settled
                        FROM filming_courses fc LEFT JOIN filming_clips cl ON cl.course_id=fc.id
                        GROUP BY COALESCE(fc.professor,'미지정') ORDER BY total DESC""")]
                    active_list=[dict(r) for r in c.execute("SELECT id,field,name,professor FROM filming_courses WHERE COALESCE(ended,0)=0 ORDER BY id")]
                    return self.send_json({
                        "courses":len(courses),"active":sum(1 for x in courses if not x["ended"]),
                        "ended":sum(1 for x in courses if x["ended"]),
                        "total_clips":total,"shot":shot,"coded":coded,"settled":settled,
                        "shot_rate":round(shot*100/total) if total else 0,
                        "coded_rate":round(coded*100/total) if total else 0,
                        "settled_rate":round(settled*100/total) if total else 0,
                        "by_professor":by_prof,"active_courses":active_list})

                if p.startswith("/api/filming/courses/") and not p.endswith("/clips"):
                    fid=int(p.split("/")[4])
                    r=c.execute("SELECT * FROM filming_courses WHERE id=?",(fid,)).fetchone()
                    if not r: return self.send_json({"error":"not found"},404)
                    d=dict(r)
                    clips=[dict(x) for x in c.execute("SELECT * FROM filming_clips WHERE course_id=? ORDER BY clip_no,id",(fid,))]
                    total=len(clips)
                    shot=sum(1 for x in clips if (x["video_len"] or "").strip())
                    coded=sum(1 for x in clips if (x["coding_date"] or "").strip())
                    settled=sum(1 for x in clips if (x["settle_status"] or "")=="정산완료")
                    d.update(clips=clips,total_clips=total,shot=shot,coded=coded,settled=settled,
                        shot_rate=round(shot*100/total) if total else 0,
                        coded_rate=round(coded*100/total) if total else 0,
                        settled_rate=round(settled*100/total) if total else 0)
                    return self.send_json(d)

                # ===== 마케팅 영상 제작 현황 =====
                if p == "/api/marketing/videos":
                    where=["1=1"]; args=[]
                    for key,col in (("month","month"),("major","cat_major"),("content_type","content_type"),("progress","progress"),("pm","pm")):
                        v=one(q.get(key))
                        if v: where.append(f"{col}=?"); args.append(v)
                    kw=one(q.get("q"))
                    if kw:
                        where.append("(topic LIKE ? OR cat_minor LIKE ? OR instructor LIKE ? OR editor LIKE ?)")
                        args+= [f"%{kw}%"]*4
                    limit=int(one(q.get("limit"),500) or 500)
                    sql="SELECT * FROM mkt_video_log WHERE "+" AND ".join(where)+" ORDER BY month DESC,id DESC LIMIT ?"
                    rows=c.execute(sql,args+[limit]).fetchall()
                    return self.send_json([dict(r) for r in rows])

                if p == "/api/marketing/video-stats":
                    def grp(col):
                        return {r[0] or "(미지정)":r[1] for r in c.execute(f"SELECT {col},COUNT(*) FROM mkt_video_log GROUP BY {col} ORDER BY COUNT(*) DESC")}
                    total=c.execute("SELECT COUNT(*) FROM mkt_video_log").fetchone()[0]
                    months=[r[0] for r in c.execute("SELECT DISTINCT month FROM mkt_video_log WHERE month<>'' ORDER BY month")]
                    by_month=[{"month":r[0],"count":r[1]} for r in c.execute("SELECT month,COUNT(*) FROM mkt_video_log WHERE month<>'' GROUP BY month ORDER BY month")]
                    latest_month=months[-1] if months else None
                    this_month=c.execute("SELECT COUNT(*) FROM mkt_video_log WHERE month=?",(latest_month,)).fetchone()[0] if latest_month else 0
                    in_progress=c.execute("SELECT COUNT(*) FROM mkt_video_log WHERE progress IN ('진행중','진행예정')").fetchone()[0]
                    # 담당자별 작업량: PM / 편집자
                    def workload(col):
                        return [{"name":r[0],"count":r[1]} for r in c.execute(f"SELECT {col},COUNT(*) FROM mkt_video_log WHERE TRIM(COALESCE({col},''))<>'' GROUP BY {col} ORDER BY COUNT(*) DESC LIMIT 12")]
                    return self.send_json({"total":total,"months":months,"by_month":by_month,
                        "latest_month":latest_month,"this_month":this_month,"in_progress":in_progress,
                        "by_major":grp("cat_major"),"by_content_type":grp("content_type"),"by_progress":grp("progress"),
                        "by_pm":dict(list(grp("pm").items())[:10]),
                        "workload_pm":workload("pm"),"workload_editor":workload("editor")})

                # ===== 마케팅 채널 분석(주간) =====
                if p == "/api/marketing/weekly":
                    cat=one(q.get("category")); ch=one(q.get("channel")); metric=one(q.get("metric"))
                    where=["1=1"]; args=[]
                    if cat: where.append("category=?"); args.append(cat)
                    if ch: where.append("channel=?"); args.append(ch)
                    if metric: where.append("metric=?"); args.append(metric)
                    rows=c.execute("SELECT week,category,channel,metric,value FROM mkt_weekly_metrics WHERE "+" AND ".join(where)+" ORDER BY week",args).fetchall()
                    return self.send_json([dict(r) for r in rows])

                if p == "/api/marketing/weekly-latest":
                    # 카테고리/채널/지표별 최신 주간 값 + 직전 대비 증감
                    rows=c.execute("SELECT week,category,channel,metric,value FROM mkt_weekly_metrics ORDER BY week").fetchall()
                    series={}
                    for r in rows:
                        series.setdefault((r["category"],r["channel"],r["metric"]),[]).append((r["week"],r["value"]))
                    out=[]
                    for (cat,ch,metric),seq in series.items():
                        seq.sort()
                        last=seq[-1]; prev=seq[-2] if len(seq)>1 else None
                        out.append({"category":cat,"channel":ch,"metric":metric,"week":last[0],"value":last[1],
                            "delta":(last[1]-prev[1]) if prev else None})
                    weeks=[r[0] for r in c.execute("SELECT DISTINCT week FROM mkt_weekly_metrics ORDER BY week")]
                    return self.send_json({"latest":out,"weeks":weeks,"last_week":weeks[-1] if weeks else None})

                # ===== 주간 업무보고 =====
                if p == "/api/weekly":
                    team_id=int(one(q.get("team_id"),0) or 0)
                    week_start=one(q.get("week_start"),"")
                    where=["wr.week_start=?"]; args=[week_start]
                    if team_id: where.append("wr.team_id=?"); args.append(team_id)
                    rows=[dict(r) for r in c.execute(f"""SELECT wr.*, u.name user_name, u.rank user_rank, t.name team_name
                        FROM weekly_reports wr LEFT JOIN users u ON wr.user_id=u.id LEFT JOIN teams t ON wr.team_id=t.id
                        WHERE {' AND '.join(where)} ORDER BY wr.user_id, wr.sort_order, wr.id""",args)]
                    for r in rows:
                        try: r["collaborator_ids"]=json.loads(r.get("collaborator_ids") or "[]")
                        except: r["collaborator_ids"]=[]
                    members=[]
                    if team_id:
                        members=[dict(x) for x in c.execute("""SELECT id,name,rank,job_title FROM users
                            WHERE team_id=? AND COALESCE(active,1)=1 ORDER BY is_team_leader DESC,id""",(team_id,))]
                    team=c.execute("SELECT id,name FROM teams WHERE id=?",(team_id,)).fetchone() if team_id else None
                    return self.send_json({"week_start":week_start,"week_end":one(q.get("week_end"),""),
                        "team":dict(team) if team else None,"members":members,"items":rows})

                if p == "/api/planning/overview":
                    live_active=c.execute("SELECT COUNT(*) FROM planning_live_events WHERE status NOT IN ('완료','취소')").fetchone()[0]
                    promo_active=c.execute("SELECT COUNT(*) FROM planning_promotions WHERE status NOT IN ('완료','취소')").fetchone()[0]
                    products_active=c.execute("SELECT COUNT(*) FROM planning_products WHERE status NOT IN ('판매중','종료')").fetchone()[0]
                    site_due=c.execute("""SELECT COUNT(*) FROM planning_site_ops
                        WHERE status!='완료' AND due_date<=date('now','+7 day')""").fetchone()[0]
                    b2b_pending=c.execute("SELECT COUNT(*) FROM planning_b2b WHERE settlement_status!='완료'").fetchone()[0]
                    live=[dict(r) for r in c.execute("""SELECT le.*,u.name owner_name FROM planning_live_events le
                        LEFT JOIN users u ON le.owner_id=u.id ORDER BY le.event_date DESC,le.id DESC LIMIT 8""")]
                    products=[dict(r) for r in c.execute("""SELECT pp.*,u.name owner_name FROM planning_products pp
                        LEFT JOIN users u ON pp.owner_id=u.id ORDER BY pp.launch_date DESC,pp.id DESC LIMIT 8""")]
                    return self.send_json({"live_active":live_active,"promo_active":promo_active,"products_active":products_active,
                        "site_due":site_due,"b2b_pending":b2b_pending,"live":live,"products":products})

                if p == "/api/planning/live-events":
                    rows=c.execute("""SELECT le.*,u.name owner_name FROM planning_live_events le
                    LEFT JOIN users u ON le.owner_id=u.id ORDER BY le.event_date DESC,le.id DESC""").fetchall()
                    return self.send_json([dict(r) for r in rows])

                if p == "/api/planning/promotions":
                    rows=c.execute("""SELECT pp.*,u.name owner_name FROM planning_promotions pp
                    LEFT JOIN users u ON pp.owner_id=u.id ORDER BY COALESCE(pp.end_date,pp.start_date) DESC,pp.id DESC""").fetchall()
                    return self.send_json([dict(r) for r in rows])

                if p == "/api/planning/products":
                    rows=c.execute("""SELECT pp.*,u.name owner_name FROM planning_products pp
                    LEFT JOIN users u ON pp.owner_id=u.id ORDER BY pp.launch_date DESC,pp.id DESC""").fetchall()
                    return self.send_json([dict(r) for r in rows])

                if p == "/api/planning/b2b":
                    rows=c.execute("""SELECT pb.*,u.name owner_name FROM planning_b2b pb
                    LEFT JOIN users u ON pb.owner_id=u.id ORDER BY pb.settlement_month DESC,pb.id DESC""").fetchall()
                    return self.send_json([dict(r) for r in rows])

                if p == "/api/planning/site-ops":
                    rows=c.execute("""SELECT so.*,u.name owner_name FROM planning_site_ops so
                    LEFT JOIN users u ON so.owner_id=u.id ORDER BY COALESCE(so.due_date,so.request_date) DESC,so.id DESC""").fetchall()
                    return self.send_json([dict(r) for r in rows])

                if p == "/api/mcc/reviews/overview":
                    total=c.execute("SELECT COUNT(*) FROM mcc_reviews WHERE status!='완료'").fetchone()[0]
                    important=c.execute("SELECT COALESCE(SUM(changed_items),0) FROM mcc_reviews WHERE status!='완료'").fetchone()[0]
                    total_items=c.execute("SELECT COALESCE(SUM(total_items),0) FROM mcc_reviews").fetchone()[0]
                    matched=c.execute("SELECT COALESCE(SUM(matched_items),0) FROM mcc_reviews").fetchone()[0]
                    auto_match=round((matched/total_items*100),1) if total_items else 0
                    rows=[dict(r) for r in c.execute("""SELECT mr.*,u.name owner_name FROM mcc_reviews mr
                        LEFT JOIN users u ON mr.owner_id=u.id
                        ORDER BY CASE mr.status WHEN '검수중' THEN 0 WHEN '검토대기' THEN 1 WHEN '중요' THEN 2 ELSE 3 END,
                        COALESCE(mr.due_date,'9999-12-31'),mr.id DESC LIMIT 20""")]
                    return self.send_json({"in_progress":total,"important_changes":important,"auto_match":auto_match,"rows":rows})

                if p == "/api/mcc/reviews":
                    rows=c.execute("""SELECT mr.*,u.name owner_name FROM mcc_reviews mr
                        LEFT JOIN users u ON mr.owner_id=u.id
                        ORDER BY COALESCE(mr.due_date,'9999-12-31'),mr.id DESC""").fetchall()
                    return self.send_json([dict(r) for r in rows])

                if p.startswith("/api/mcc/reviews/") and p.endswith("/items"):
                    rid=int(p.split("/")[4])
                    rows=c.execute("""SELECT ri.*,u.name reviewer_name FROM mcc_review_items ri
                        LEFT JOIN users u ON ri.reviewer_id=u.id
                        WHERE ri.review_id=? ORDER BY ri.item_no,ri.id""",(rid,)).fetchall()
                    return self.send_json([dict(r) for r in rows])

                if p.startswith("/api/mcc/reviews/") and p.count("/")==4 and p.rsplit("/",1)[1].isdigit():
                    rid=int(p.rsplit("/",1)[1])
                    r=c.execute("""SELECT mr.*,u.name owner_name FROM mcc_reviews mr
                        LEFT JOIN users u ON mr.owner_id=u.id WHERE mr.id=?""",(rid,)).fetchone()
                    return self.send_json(dict(r) if r else {"error":"not found"},200 if r else 404)

                if p == "/api/data-center":
                    tid=int(one(q.get("team_id"),0) or 0)
                    tab=one(q.get("tab"),"all")
                    sql="""SELECT dc.*,u.name uploader_name,tm.name team_name,
                        d2.original_name duplicate_name
                        FROM data_center_files dc
                        LEFT JOIN users u ON dc.uploader_id=u.id
                        LEFT JOIN teams tm ON dc.team_id=tm.id
                        LEFT JOIN data_center_files d2 ON dc.duplicate_of=d2.id
                        WHERE dc.team_id=?"""
                    args=[tid]
                    if tab=="duplicates":sql+=" AND dc.duplicate_of IS NOT NULL"
                    elif tab=="errors":sql+=" AND dc.status IN ('오류','재처리대기')"
                    elif tab=="source":sql+=" AND dc.source_type='SOURCE'"
                    elif tab=="external":sql+=" AND dc.source_type='EXTERNAL'"
                    sql+=" ORDER BY dc.id DESC"
                    return self.send_json([dict(r) for r in c.execute(sql,args)])

                if p == "/api/data-center/summary":
                    tid=int(one(q.get("team_id"),0) or 0)
                    out={
                      "total":c.execute("SELECT COUNT(*) FROM data_center_files WHERE team_id=?",(tid,)).fetchone()[0],
                      "files":c.execute("SELECT COUNT(*) FROM data_center_files WHERE team_id=? AND source_type='FILE'",(tid,)).fetchone()[0],
                      "duplicates":c.execute("SELECT COUNT(*) FROM data_center_files WHERE team_id=? AND duplicate_of IS NOT NULL",(tid,)).fetchone()[0],
                      "errors":c.execute("SELECT COUNT(*) FROM data_center_files WHERE team_id=? AND status IN ('오류','재처리대기')",(tid,)).fetchone()[0],
                      "external":c.execute("SELECT COUNT(*) FROM data_center_files WHERE team_id=? AND source_type='EXTERNAL'",(tid,)).fetchone()[0]
                    }
                    return self.send_json(out)

                if p.startswith("/api/data-center/") and p.count("/")==3 and p.rsplit("/",1)[1].isdigit():
                    fid=int(p.rsplit("/",1)[1])
                    r=c.execute("""SELECT dc.*,u.name uploader_name,tm.name team_name FROM data_center_files dc
                        LEFT JOIN users u ON dc.uploader_id=u.id LEFT JOIN teams tm ON dc.team_id=tm.id WHERE dc.id=?""",(fid,)).fetchone()
                    if not r:return self.send_json({"error":"not found"},404)
                    logs=[dict(x) for x in c.execute("""SELECT l.*,u.name actor_name FROM data_center_logs l
                        LEFT JOIN users u ON l.actor_id=u.id WHERE l.file_id=? ORDER BY l.id DESC""",(fid,))]
                    d=dict(r);d["logs"]=logs;return self.send_json(d)

                if p.startswith("/api/projects/") and p.count("/")==3 and p.rsplit("/",1)[1].isdigit():
                    pid=int(p.rsplit("/",1)[1])
                    p0=c.execute("""SELECT p.*,u.name pm_name,cr.name creator_name,tm.name team_name
                        FROM projects p LEFT JOIN users u ON p.pm_user_id=u.id
                        LEFT JOIN users cr ON p.created_by=cr.id LEFT JOIN teams tm ON p.team_id=tm.id WHERE p.id=?""",(pid,)).fetchone()
                    if not p0:return self.send_json({"error":"not found"},404)
                    out=dict(p0)
                    out["teams"]=[dict(r) for r in c.execute("""SELECT t.* FROM project_teams pt JOIN teams t ON pt.team_id=t.id WHERE pt.project_id=? ORDER BY t.sort_order""",(pid,))]
                    out["members"]=[dict(r) for r in c.execute("""SELECT u.id,u.name,u.rank,u.job_title,u.team_id,t.name team_name,pm.member_role
                        FROM project_members pm JOIN users u ON pm.user_id=u.id LEFT JOIN teams t ON u.team_id=t.id
                        WHERE pm.project_id=? ORDER BY t.sort_order,u.is_team_leader DESC,u.name""",(pid,))]
                    out["approvals"]=[dict(r) for r in c.execute("""SELECT pa.*,u.name approver_name,u.job_title,u.rank,t.name team_name
                        FROM project_approvals pa JOIN users u ON pa.approver_id=u.id LEFT JOIN teams t ON u.team_id=t.id
                        WHERE pa.project_id=? ORDER BY pa.approval_order""",(pid,))]
                    return self.send_json(out)

                if p == "/api/work-management":
                    viewer_id=int(one(q.get("viewer_id"),0) or 0)
                    scope=one(q.get("scope"),"mine")
                    viewer=c.execute("SELECT * FROM users WHERE id=?",(viewer_id,)).fetchone()
                    if not viewer:return self.send_json({"error":"viewer not found"},404)
                    sql="""SELECT t.*,p.name project_name,p.approval_status project_approval_status,
                        u.name assignee_name,tm.name team_name,cr.name creator_name,rv.name reviewer_name,
                        (SELECT COUNT(*) FROM task_approvals ta WHERE ta.task_id=t.id AND ta.status='REJECTED') rejected_count,
                        (SELECT COUNT(*) FROM task_approvals ta WHERE ta.task_id=t.id AND ta.status='WAITING') waiting_count
                        FROM tasks t LEFT JOIN projects p ON t.project_id=p.id
                        LEFT JOIN users u ON t.assignee_id=u.id LEFT JOIN teams tm ON t.team_id=tm.id
                        LEFT JOIN users cr ON t.created_by=cr.id LEFT JOIN users rv ON t.reviewer_id=rv.id
                        WHERE 1=1"""
                    args=[]
                    if scope=="mine":
                        # 내 업무 = 담당/작성/현재 승인/승인선 참여 업무
                        sql+=""" AND (
                            t.assignee_id=? OR t.created_by=? OR t.reviewer_id=?
                            OR EXISTS(SELECT 1 FROM task_approvals ta WHERE ta.task_id=t.id AND ta.approver_id=?)
                        )"""
                        args += [viewer_id,viewer_id,viewer_id,viewer_id]
                    elif scope=="team":
                        sql+=" AND t.team_id=?";args.append(viewer["team_id"])
                    elif scope=="related":
                        sql+=""" AND (
                          (t.visibility_scope='RELATED' AND (t.team_id=? OR (','||COALESCE(t.related_team_ids,'')||',') LIKE ?))
                          OR (t.work_type='PROJECT' AND (
                            EXISTS(SELECT 1 FROM project_members pm WHERE pm.project_id=t.project_id AND pm.user_id=?)
                            OR EXISTS(SELECT 1 FROM project_teams pt WHERE pt.project_id=t.project_id AND pt.team_id=?)
                          ))
                        )"""
                        args += [viewer["team_id"],f"%,{viewer['team_id']},%",viewer_id,viewer["team_id"]]
                    elif scope=="department":
                        sql+=" AND t.visibility_scope='DEPARTMENT'"
                    elif scope=="review":
                        # 현재 처리 차례인 검토 업무만 노출. 구버전 데이터는 reviewer_id로 보완.
                        sql+=""" AND t.status='REVIEW' AND (
                          EXISTS(SELECT 1 FROM task_approvals ta
                            WHERE ta.task_id=t.id AND ta.approver_id=? AND ta.status='WAITING'
                            AND ta.approval_order=(SELECT MIN(ta2.approval_order) FROM task_approvals ta2 WHERE ta2.task_id=t.id AND ta2.status='WAITING'))
                          OR (NOT EXISTS(SELECT 1 FROM task_approvals tx WHERE tx.task_id=t.id) AND t.reviewer_id=?)
                        )"""
                        args += [viewer_id,viewer_id]
                    elif scope=="report":
                        sql+=" AND t.completion_mode='REPORT' AND t.assignee_id=? AND t.status!='DONE'";args.append(viewer_id)
                    elif scope=="approval":
                        # 업무 승인 현황: '검토·컨펌형' 업무만 대상(완료보고형 제외). 내가 승인선/검토자/담당/작성으로 관여한 건.
                        sql+=""" AND t.completion_mode='REVIEW' AND (
                          t.assignee_id=? OR t.created_by=? OR t.reviewer_id=?
                          OR EXISTS(SELECT 1 FROM task_approvals ta WHERE ta.task_id=t.id AND ta.approver_id=?)
                        )"""
                        args += [viewer_id,viewer_id,viewer_id,viewer_id]
                    elif scope=="history":
                        sql+=""" AND t.status='DONE' AND (
                          t.assignee_id=? OR t.created_by=? OR t.reviewer_id=?
                          OR EXISTS(SELECT 1 FROM task_approvals ta WHERE ta.task_id=t.id AND ta.approver_id=?)
                        )"""
                        args += [viewer_id,viewer_id,viewer_id,viewer_id]
                    sql+=" ORDER BY t.status_order,t.priority_order,COALESCE(t.due_date,'9999-12-31'),t.id DESC"
                    rows=[dict(r) for r in c.execute(sql,args)]
                    if scope=="approval":
                        for r in rows:
                            ca=c.execute("SELECT approver_id FROM task_approvals WHERE task_id=? AND status='WAITING' ORDER BY approval_order LIMIT 1",(r["id"],)).fetchone()
                            r["current_approver_id"]=(ca["approver_id"] if ca else (r.get("reviewer_id") if r.get("status")=="REVIEW" else None))
                            va=c.execute("SELECT status FROM task_approvals WHERE task_id=? AND approver_id=? LIMIT 1",(r["id"],viewer_id)).fetchone()
                            r["viewer_is_approver"]=1 if va else 0
                            r["viewer_approval_status"]=(va["status"] if va else None)
                    return self.send_json(rows)

                if p == "/api/templates":
                    typ=one(q.get("template_type"),"")
                    tid=int(one(q.get("team_id"),0) or 0)
                    sql="""SELECT wt.*,u.name creator_name,t.name team_name FROM work_templates wt
                           LEFT JOIN users u ON wt.created_by=u.id LEFT JOIN teams t ON wt.team_id=t.id
                           WHERE wt.active=1"""
                    args=[]
                    if typ:sql+=" AND wt.template_type=?";args.append(typ)
                    if tid:sql+=" AND (wt.team_id IS NULL OR wt.team_id=?)";args.append(tid)
                    sql+=" ORDER BY wt.id DESC"
                    rows=[]
                    for r in c.execute(sql,args):
                        d=dict(r)
                        try:d["payload"]=json.loads(d.pop("payload_json"))
                        except:d["payload"]={}
                        rows.append(d)
                    return self.send_json(rows)

                if p == "/api/task-history":
                    viewer_id=int(one(q.get("viewer_id"),0) or 0)
                    team_id=int(one(q.get("team_id"),0) or 0)
                    status=one(q.get("status"),"")
                    keyword=one(q.get("keyword"),"").strip()
                    date_from=one(q.get("date_from"),"");date_to=one(q.get("date_to"),"")
                    viewer=c.execute("SELECT * FROM users WHERE id=?",(viewer_id,)).fetchone()
                    if not viewer:return self.send_json({"error":"viewer not found"},404)
                    sql="""SELECT t.*,p.name project_name,u.name assignee_name,tm.name team_name,
                        cr.name creator_name,rv.name reviewer_name,
                        (SELECT COUNT(*) FROM activity_logs a WHERE a.task_id=t.id) history_count,
                        (SELECT MAX(a.created_at) FROM activity_logs a WHERE a.task_id=t.id) last_activity_at
                        FROM tasks t
                        LEFT JOIN projects p ON t.project_id=p.id
                        LEFT JOIN users u ON t.assignee_id=u.id
                        LEFT JOIN teams tm ON t.team_id=tm.id
                        LEFT JOIN users cr ON t.created_by=cr.id
                        LEFT JOIN users rv ON t.reviewer_id=rv.id
                        WHERE 1=1"""
                    args=[]
                    if viewer["role"]!="DIVISION_ADMIN":
                        sql+=""" AND (
                          t.assignee_id=? OR t.created_by=? OR t.reviewer_id=?
                          OR (t.work_type!='PROJECT' AND (
                            (t.visibility_scope='TEAM' AND t.team_id=?)
                            OR t.visibility_scope='DEPARTMENT'
                            OR (t.visibility_scope='RELATED' AND (t.team_id=? OR (','||COALESCE(t.related_team_ids,'')||',') LIKE ?))
                          ))
                          OR (t.work_type='PROJECT' AND (
                            EXISTS(SELECT 1 FROM project_members pm WHERE pm.project_id=t.project_id AND pm.user_id=?)
                            OR EXISTS(SELECT 1 FROM project_teams pt WHERE pt.project_id=t.project_id AND pt.team_id=?)
                          ))
                        )"""
                        args += [viewer_id,viewer_id,viewer_id,viewer["team_id"],viewer["team_id"],f"%,{viewer['team_id']},%",viewer_id,viewer["team_id"]]
                    if team_id:
                        sql+=" AND t.team_id=?";args.append(team_id)
                    if status:
                        sql+=" AND t.status=?";args.append(status)
                    if keyword:
                        sql+=" AND (t.title LIKE ? OR t.description LIKE ? OR u.name LIKE ? OR tm.name LIKE ?)"
                        kw=f"%{keyword}%";args += [kw,kw,kw,kw]
                    # 날짜 검색: 업무 마감일 또는 생성일 기준으로 해당 일자 범위 포함
                    if date_from:
                        sql+=" AND COALESCE(t.due_date,substr(t.created_at,1,10))>=?";args.append(date_from)
                    if date_to:
                        sql+=" AND COALESCE(t.due_date,substr(t.created_at,1,10))<=?";args.append(date_to)
                    sql+=" ORDER BY COALESCE((SELECT MAX(a2.created_at) FROM activity_logs a2 WHERE a2.task_id=t.id),t.created_at,'1970-01-01') DESC,t.id DESC"
                    rows=[dict(r) for r in c.execute(sql,args)]
                    return self.send_json(rows)

                if p.startswith("/api/task-history/") and p.count("/")==3:
                    tid=int(p.rsplit("/",1)[1])
                    viewer_id=int(one(q.get("viewer_id"),0) or 0)
                    viewer=c.execute("SELECT * FROM users WHERE id=?",(viewer_id,)).fetchone()
                    t=c.execute("""SELECT t.*,p.name project_name,u.name assignee_name,tm.name team_name,
                        cr.name creator_name,rv.name reviewer_name
                        FROM tasks t LEFT JOIN projects p ON t.project_id=p.id
                        LEFT JOIN users u ON t.assignee_id=u.id LEFT JOIN teams tm ON t.team_id=tm.id
                        LEFT JOIN users cr ON t.created_by=cr.id LEFT JOIN users rv ON t.reviewer_id=rv.id
                        WHERE t.id=?""",(tid,)).fetchone()
                    if not t:return self.send_json({"error":"not found"},404)
                    allowed=False
                    if viewer and viewer["role"]=="DIVISION_ADMIN":allowed=True
                    elif viewer:
                        related=[x for x in (t["related_team_ids"] or "").split(",") if x]
                        allowed=(viewer_id in [t["assignee_id"],t["created_by"],t["reviewer_id"]]
                            or ((not t["visibility_scope"] or t["visibility_scope"]=="TEAM") and viewer["team_id"]==t["team_id"])
                            or t["visibility_scope"]=="DEPARTMENT"
                            or (t["visibility_scope"]=="RELATED" and (viewer["team_id"]==t["team_id"] or str(viewer["team_id"]) in related)))
                        if t["work_type"]=="PROJECT" and t["project_id"]:
                            allowed=allowed or bool(c.execute("SELECT 1 FROM project_members WHERE project_id=? AND user_id=?",(t["project_id"],viewer_id)).fetchone())
                            allowed=allowed or bool(c.execute("SELECT 1 FROM project_teams WHERE project_id=? AND team_id=?",(t["project_id"],viewer["team_id"])).fetchone())
                    if not allowed:return self.send_json({"error":"열람 권한이 없습니다."},403)
                    logs=[dict(r) for r in c.execute("""SELECT a.*,u.name actor_name,u.rank actor_rank,u.job_title actor_job_title,t.name actor_team_name
                        FROM activity_logs a LEFT JOIN users u ON a.actor_id=u.id
                        LEFT JOIN teams t ON u.team_id=t.id WHERE a.task_id=? ORDER BY a.id""",(tid,))]
                    comments=[dict(r) for r in c.execute("""SELECT tc.*,u.name user_name FROM task_comments tc
                        LEFT JOIN users u ON tc.user_id=u.id WHERE tc.task_id=? ORDER BY tc.id""",(tid,))]
                    out=dict(t);out["logs"]=logs;out["comments"]=comments
                    rr=c.execute("""SELECT tr.*,u.name requester_name FROM task_review_requests tr
                        LEFT JOIN users u ON tr.requester_id=u.id WHERE tr.task_id=? ORDER BY tr.id DESC LIMIT 1""",(tid,)).fetchone()
                    if rr:
                        rd=dict(rr)
                        rd["files"]=[dict(x) for x in c.execute("SELECT * FROM task_review_files WHERE request_id=? ORDER BY id",(rr["id"],))]
                        rd["approvals"]=[dict(x) for x in c.execute("""SELECT ta.*,u.name approver_name,u.rank,u.job_title,t.name team_name
                            FROM task_approvals ta JOIN users u ON ta.approver_id=u.id
                            LEFT JOIN teams t ON u.team_id=t.id WHERE ta.task_id=? ORDER BY ta.approval_order""",(tid,))]
                        cur=c.execute("""SELECT ta.approver_id,u.name approver_name,u.rank,u.job_title,t.name team_name
                            FROM task_approvals ta JOIN users u ON ta.approver_id=u.id LEFT JOIN teams t ON u.team_id=t.id
                            WHERE ta.task_id=? AND ta.status='WAITING' ORDER BY ta.approval_order LIMIT 1""",(tid,)).fetchone()
                        rd["current_approver"]=dict(cur) if cur else None
                        out["latest_review_request"]=rd
                    else:out["latest_review_request"]=None
                    return self.send_json(out)

                if p == "/api/project-history":
                    viewer_id=int(one(q.get("viewer_id"),0) or 0)
                    team_id=int(one(q.get("team_id"),0) or 0)
                    status=one(q.get("status"),"")
                    keyword=one(q.get("keyword"),"").strip()
                    date_from=one(q.get("date_from"),"");date_to=one(q.get("date_to"),"")
                    viewer=c.execute("SELECT * FROM users WHERE id=?",(viewer_id,)).fetchone()
                    if not viewer:return self.send_json({"error":"viewer not found"},404)
                    sql="""SELECT p.*,u.name pm_name,tm.name team_name,cr.name creator_name,
                        (SELECT COUNT(*) FROM tasks t WHERE t.project_id=p.id) task_count,
                        (SELECT COUNT(*) FROM tasks t WHERE t.project_id=p.id AND t.status='DONE') done_count,
                        (SELECT COUNT(*) FROM project_members pm WHERE pm.project_id=p.id) member_count,
                        (SELECT MAX(x) FROM (
                          SELECT MAX(a.created_at) x FROM activity_logs a JOIN tasks t ON a.task_id=t.id WHERE t.project_id=p.id
                          UNION ALL SELECT MAX(pa.decided_at) FROM project_approvals pa WHERE pa.project_id=p.id
                          UNION ALL SELECT MAX(t2.created_at) FROM tasks t2 WHERE t2.project_id=p.id
                          UNION ALL SELECT MAX(pm2.created_at) FROM project_members pm2 WHERE pm2.project_id=p.id
                        )) last_activity_at
                        FROM projects p LEFT JOIN users u ON p.pm_user_id=u.id
                        LEFT JOIN users cr ON p.created_by=cr.id
                        LEFT JOIN teams tm ON p.team_id=tm.id WHERE 1=1"""
                    args=[]
                    if viewer["role"]!="DIVISION_ADMIN":
                        sql+=""" AND (p.pm_user_id=? OR p.created_by=?
                          OR EXISTS(SELECT 1 FROM project_members pm WHERE pm.project_id=p.id AND pm.user_id=?)
                          OR EXISTS(SELECT 1 FROM project_teams pt WHERE pt.project_id=p.id AND pt.team_id=?)
                          OR EXISTS(SELECT 1 FROM project_approvals pa WHERE pa.project_id=p.id AND pa.approver_id=?)
                          OR EXISTS(SELECT 1 FROM tasks tx WHERE tx.project_id=p.id AND tx.assignee_id=?))"""
                        args += [viewer_id,viewer_id,viewer_id,viewer["team_id"],viewer_id,viewer_id]
                    if team_id:
                        sql+=" AND p.team_id=?";args.append(team_id)
                    if keyword:
                        sql+=" AND (p.name LIKE ? OR u.name LIKE ? OR tm.name LIKE ?)"
                        kw=f"%{keyword}%";args += [kw,kw,kw]
                    if date_from:
                        sql+=" AND COALESCE(p.due_date,substr(p.created_at,1,10))>=?";args.append(date_from)
                    if date_to:
                        sql+=" AND COALESCE(p.due_date,substr(p.created_at,1,10))<=?";args.append(date_to)
                    sql+=" ORDER BY last_activity_at DESC,p.id DESC"
                    rows=[dict(r) for r in c.execute(sql,args)]
                    def proj_state(r):
                        if r.get("status")=="HOLD":return "HOLD"
                        if r.get("approval_status")=="WAITING":return "WAITING"
                        if r.get("approval_status")=="REJECTED":return "REJECTED"
                        if (r.get("task_count",0)>0 and r.get("done_count",0)>=r.get("task_count",0)) or r.get("status") in ("DONE","CLOSED","COMPLETED"):return "DONE"
                        return "IN_PROGRESS"
                    for r in rows:
                        r["proj_state"]=proj_state(r)
                        r["progress"]=round(r["done_count"]/r["task_count"]*100) if r.get("task_count") else 0
                    if status:
                        rows=[r for r in rows if r["proj_state"]==status]
                    return self.send_json(rows)

                if p.startswith("/api/project-history/") and p.count("/")==3:
                    pid=int(p.rsplit("/",1)[1])
                    viewer_id=int(one(q.get("viewer_id"),0) or 0)
                    viewer=c.execute("SELECT * FROM users WHERE id=?",(viewer_id,)).fetchone()
                    pr=c.execute("""SELECT p.*,u.name pm_name,tm.name team_name,cr.name creator_name
                        FROM projects p LEFT JOIN users u ON p.pm_user_id=u.id
                        LEFT JOIN users cr ON p.created_by=cr.id LEFT JOIN teams tm ON p.team_id=tm.id
                        WHERE p.id=?""",(pid,)).fetchone()
                    if not pr:return self.send_json({"error":"not found"},404)
                    allowed=False
                    if viewer and viewer["role"]=="DIVISION_ADMIN":allowed=True
                    elif viewer:
                        allowed=(viewer_id in [pr["pm_user_id"],pr["created_by"]]
                            or bool(c.execute("SELECT 1 FROM project_members WHERE project_id=? AND user_id=?",(pid,viewer_id)).fetchone())
                            or bool(c.execute("SELECT 1 FROM project_teams WHERE project_id=? AND team_id=?",(pid,viewer["team_id"])).fetchone())
                            or bool(c.execute("SELECT 1 FROM project_approvals WHERE project_id=? AND approver_id=?",(pid,viewer_id)).fetchone())
                            or bool(c.execute("SELECT 1 FROM tasks WHERE project_id=? AND assignee_id=?",(pid,viewer_id)).fetchone()))
                    if not allowed:return self.send_json({"error":"열람 권한이 없습니다."},403)
                    ev=[]
                    er=c.execute("""SELECT MIN(x) d FROM (
                        SELECT MIN(created_at) x FROM project_members WHERE project_id=?
                        UNION ALL SELECT MIN(created_at) FROM tasks WHERE project_id=?)""",(pid,pid)).fetchone()
                    reg_at=(er["d"] if er and er["d"] else None)
                    ev.append({"kind":"","title":"프로젝트 등록","detail":f"{pr['creator_name'] or pr['pm_name'] or '-'}님이 프로젝트를 등록했습니다.","actor":pr["creator_name"] or pr["pm_name"] or "시스템","created_at":reg_at})
                    for m in c.execute("""SELECT pm.*,u.name user_name,t.name team_name FROM project_members pm
                        LEFT JOIN users u ON pm.user_id=u.id LEFT JOIN teams t ON u.team_id=t.id
                        WHERE pm.project_id=? ORDER BY pm.id""",(pid,)):
                        role="PM" if m["member_role"]=="PM" else ("리더" if m["member_role"]=="LEADER" else "TFT")
                        ev.append({"kind":"comment","title":"TFT 구성원 합류","detail":f"{m['user_name']} ({m['team_name'] or '-'}) {role} 합류","actor":m["user_name"] or "-","actor_meta":m["team_name"] or "","created_at":m["created_at"]})
                    for a in c.execute("""SELECT pa.*,u.name approver_name,t.name team_name FROM project_approvals pa
                        LEFT JOIN users u ON pa.approver_id=u.id LEFT JOIN teams t ON u.team_id=t.id
                        WHERE pa.project_id=? ORDER BY pa.approval_order""",(pid,)):
                        if a["status"]=="APPROVED":
                            ev.append({"kind":"done","title":"개설 승인","detail":f"{a['approver_name']}님이 프로젝트 개설을 승인했습니다."+(f" — {a['comment']}" if a["comment"] else ""),"actor":a["approver_name"] or "-","actor_meta":a["team_name"] or "","created_at":a["decided_at"] or a["created_at"]})
                        elif a["status"]=="REJECTED":
                            ev.append({"kind":"reject","title":"개설 반려","detail":f"{a['approver_name']}님이 프로젝트를 반려했습니다."+(f" — {a['comment']}" if a["comment"] else ""),"actor":a["approver_name"] or "-","actor_meta":a["team_name"] or "","created_at":a["decided_at"] or a["created_at"]})
                    for t in c.execute("""SELECT t.*,u.name assignee_name FROM tasks t
                        LEFT JOIN users u ON t.assignee_id=u.id WHERE t.project_id=? ORDER BY t.id""",(pid,)):
                        ev.append({"kind":"","title":"세부 업무 등록","detail":f"{t['title']} ("+(t["assignee_name"] or "미배정")+")","actor":t["assignee_name"] or "-","created_at":t["created_at"]})
                        if t["status"]=="DONE":
                            done_log=c.execute("SELECT MAX(created_at) d FROM activity_logs WHERE task_id=? AND (action='MOVE' OR action='APPROVE')",(t["id"],)).fetchone()
                            ev.append({"kind":"done","title":"세부 업무 완료","detail":f"{t['title']} 완료","actor":t["assignee_name"] or "-","created_at":(done_log["d"] if done_log and done_log["d"] else t["created_at"])})
                    ev.sort(key=lambda e:str(e.get("created_at") or ""))
                    prog=0
                    tc=c.execute("SELECT COUNT(*) n FROM tasks WHERE project_id=?",(pid,)).fetchone()["n"]
                    dc=c.execute("SELECT COUNT(*) n FROM tasks WHERE project_id=? AND status='DONE'",(pid,)).fetchone()["n"]
                    if tc:prog=round(dc/tc*100)
                    out=dict(pr);out["events"]=ev;out["progress"]=prog;out["task_count"]=tc;out["done_count"]=dc
                    out["members"]=[dict(x) for x in c.execute("SELECT pm.*,u.name FROM project_members pm LEFT JOIN users u ON pm.user_id=u.id WHERE pm.project_id=?",(pid,))]
                    return self.send_json(out)

                if p == "/api/admin/organization":
                    actor_id=int(one(q.get("actor_id"),0) or 0)
                    actor=c.execute("SELECT * FROM users WHERE id=?",(actor_id,)).fetchone()
                    if not actor or not (actor["role"]=="SUPER_ADMIN" or actor["name"] in ("정해근","이원행")):
                        return self.send_json({"error":"관리자 권한이 없습니다."},403)
                    teams_out=[]
                    for t in c.execute("SELECT * FROM teams ORDER BY sort_order,id"):
                        d=dict(t)
                        d["active_count"]=c.execute("SELECT COUNT(*) FROM users WHERE team_id=? AND COALESCE(active,1)=1",(t["id"],)).fetchone()[0]
                        d["retired_count"]=c.execute("SELECT COUNT(*) FROM users WHERE team_id=? AND COALESCE(active,1)=0",(t["id"],)).fetchone()[0]
                        teams_out.append(d)
                    users_out=[dict(r) for r in c.execute("""SELECT u.*,t.name team_name FROM users u
                        LEFT JOIN teams t ON u.team_id=t.id ORDER BY COALESCE(u.active,1) DESC,t.sort_order,u.is_team_leader DESC,u.name""")]
                    return self.send_json({"teams":teams_out,"users":users_out})

                if p.startswith("/api/tasks/") and p.endswith("/review-request"):
                    tid=int(p.split("/")[3])
                    rr=c.execute("""SELECT tr.*,u.name requester_name FROM task_review_requests tr
                        LEFT JOIN users u ON tr.requester_id=u.id WHERE tr.task_id=? ORDER BY tr.id DESC LIMIT 1""",(tid,)).fetchone()
                    if not rr:return self.send_json({"error":"검토 요청 내역이 없습니다."},404)
                    d=dict(rr)
                    d["files"]=[dict(x) for x in c.execute("SELECT * FROM task_review_files WHERE request_id=? ORDER BY id",(rr["id"],))]
                    d["approvals"]=[dict(x) for x in c.execute("""SELECT ta.*,u.name approver_name,u.rank,u.job_title,t.name team_name
                        FROM task_approvals ta JOIN users u ON ta.approver_id=u.id
                        LEFT JOIN teams t ON u.team_id=t.id WHERE ta.task_id=? ORDER BY ta.approval_order""",(tid,))]
                    cur=c.execute("""SELECT ta.approver_id,u.name approver_name,u.rank,u.job_title,t.name team_name
                        FROM task_approvals ta JOIN users u ON ta.approver_id=u.id LEFT JOIN teams t ON u.team_id=t.id
                        WHERE ta.task_id=? AND ta.status='WAITING' ORDER BY ta.approval_order LIMIT 1""",(tid,)).fetchone()
                    d["current_approver"]=dict(cur) if cur else None
                    return self.send_json(d)

                if p == "/api/export":
                    actor_id=int(one(q.get("actor_id"),0) or 0)
                    actor=c.execute("SELECT * FROM users WHERE id=?",(actor_id,)).fetchone()
                    if not actor or actor["role"] not in ("SUPER_ADMIN","DIVISION_ADMIN"):
                        return self.send_json({"error":"관리자만 데이터를 내보낼 수 있습니다."},403)
                    tables=["divisions","teams","users","projects","tasks","requests","activity_logs","calendar_events","modules","team_modules","data_boards","data_board_columns","data_board_rows","data_board_values","meetings","checklists","resources","task_comments","notifications","directives","directive_replies","request_replies","purchases","refunds","book_discussions","customers","consultation_logs","service_knowledge","product_studies","marketing_channels","marketing_channel_metrics","marketing_campaigns","marketing_leads","marketing_events","marketing_collab_requests","marketing_performance_snapshots","planning_live_events","planning_promotions","planning_products","planning_b2b","planning_site_ops"]
                    redact={"purchases":["password_hint","account_id"]}
                    out={}
                    for table in tables:
                        try:
                            rows=[dict(r) for r in c.execute(f"SELECT * FROM {table}")]
                            for col in redact.get(table,[]):
                                for r in rows:
                                    if col in r and r[col]: r[col]="***"
                            out[table]=rows
                        except Exception:
                            out[table]=[]
                    return self.send_json(out)

                if p == "/api/notifications":
                    uid=int(one(q.get("user_id"),0) or 0)
                    rows=c.execute("SELECT * FROM notifications WHERE user_id=? ORDER BY is_read,id DESC",(uid,)).fetchall()
                    return self.send_json([dict(r) for r in rows])

                if p == "/api/notices":
                    limit=int(one(q.get("limit"),0) or 0)
                    sql="""SELECT n.*,u.name author_name,t.name author_team FROM company_notices n
                        LEFT JOIN users u ON n.author_id=u.id LEFT JOIN teams t ON u.team_id=t.id
                        WHERE n.active=1 ORDER BY n.pinned DESC,n.id DESC"""
                    if limit: sql+=f" LIMIT {limit}"
                    return self.send_json([dict(r) for r in c.execute(sql)])

                if p.startswith("/api/notices/") and p.count("/")==3 and p.rsplit("/",1)[1].isdigit():
                    nid=int(p.rsplit("/",1)[1])
                    r=c.execute("""SELECT n.*,u.name author_name,t.name author_team FROM company_notices n
                        LEFT JOIN users u ON n.author_id=u.id LEFT JOIN teams t ON u.team_id=t.id WHERE n.id=?""",(nid,)).fetchone()
                    return self.send_json(dict(r) if r else {"error":"not found"},200 if r else 404)

            return self.send_json({"error":"not found"},404)
        except Exception as e:
            traceback.print_exc()
            return self.send_json({"error":str(e)},500)

    def do_POST(self):
        try:
            p=urlparse(self.path).path
            x=self.body()
            with db() as c:
                if p=="/api/login":
                    # 이메일 전체 또는 아이디(이메일 @ 앞부분)로 로그인 가능
                    ident=(x.get("email") or "").strip().lower()
                    pw=x.get("password") or ""
                    if not ident or not pw:
                        return self.send_json({"error":"이메일(또는 아이디)과 비밀번호를 입력해주세요."},400)
                    # 아이디만 입력한 경우 @moaeg.com 붙인 이메일도 후보로 대조
                    ident_email=ident if "@" in ident else (ident+"@moaeg.com")
                    u=c.execute("""SELECT u.*,t.name team_name FROM users u LEFT JOIN teams t ON u.team_id=t.id
                        WHERE (LOWER(u.email)=? OR LOWER(u.email)=? OR LOWER(COALESCE(u.login_id,''))=?
                               OR LOWER(SUBSTR(u.email,1,INSTR(u.email,'@')-1))=?) AND COALESCE(u.active,1)=1""",
                        (ident,ident_email,ident,ident)).fetchone()
                    if not u:
                        return self.send_json({"error":"등록되지 않은 이메일/아이디이거나 비활성 계정입니다."},401)
                    if (u["password_hash"] or "")!=hash_pw(pw):
                        return self.send_json({"error":"비밀번호가 올바르지 않습니다."},401)
                    out=dict(u);out.pop("password_hash",None)
                    return self.send_json({"ok":True,"user":out})

                if p=="/api/change-password":
                    uid=int(x.get("user_id") or 0)
                    cur_pw=x.get("current_password") or "";new_pw=x.get("new_password") or ""
                    u=c.execute("SELECT * FROM users WHERE id=?",(uid,)).fetchone()
                    if not u:return self.send_json({"error":"사용자를 찾을 수 없습니다."},404)
                    if (u["password_hash"] or "")!=hash_pw(cur_pw):
                        return self.send_json({"error":"현재 비밀번호가 올바르지 않습니다."},403)
                    if len(new_pw)<4:
                        return self.send_json({"error":"새 비밀번호는 4자 이상이어야 합니다."},400)
                    c.execute("UPDATE users SET password_hash=? WHERE id=?",(hash_pw(new_pw),uid));c.commit()
                    return self.send_json({"ok":True})

                if p=="/api/reset-password":
                    # 관리자가 특정 계정 비밀번호를 1234로 초기화
                    actor=c.execute("SELECT * FROM users WHERE id=?",(x.get("actor_id"),)).fetchone()
                    if not actor or not (actor["role"] in ("SUPER_ADMIN","DIVISION_ADMIN") or actor["name"] in ("정해근","이원행")):
                        return self.send_json({"error":"관리자만 초기화할 수 있습니다."},403)
                    uid=int(x.get("user_id") or 0)
                    c.execute("UPDATE users SET password_hash=? WHERE id=?",(hash_pw('1234'),uid));c.commit()
                    return self.send_json({"ok":True})

                if p=="/api/projects":
                    creator=c.execute("SELECT * FROM users WHERE id=?",(x.get("created_by"),)).fetchone()
                    if not creator:
                        return self.send_json({"error":"작성자 정보를 확인할 수 없습니다."},400)
                    if not (x.get("name") or "").strip():
                        return self.send_json({"error":"프로젝트명을 입력해주세요."},400)
                    cur=c.execute("""INSERT INTO projects(name,pm_user_id,start_date,due_date,description,status,team_id,created_by,project_type,approval_status,risk_status)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?)""",(x["name"],x.get("pm_user_id") or x.get("created_by"),x.get("start_date"),x.get("due_date"),x.get("description",""),
                    "ACTIVE",x.get("team_id"),x.get("created_by"),"APPROVAL_PROJECT","WAITING",x.get("risk_status","NORMAL")))
                    pid=cur.lastrowid
                    team_ids=[int(v) for v in x.get("team_ids",[]) if v]
                    member_ids=[int(v) for v in x.get("member_ids",[]) if v]
                    approver_ids=[int(v) for v in x.get("approver_ids",[]) if v]
                    if x.get("team_id") and x.get("team_id") not in team_ids:team_ids.insert(0,int(x["team_id"]))
                    pm=x.get("pm_user_id") or x.get("created_by")
                    if pm and pm not in member_ids:member_ids.insert(0,int(pm))
                    for tid in dict.fromkeys(team_ids):
                        c.execute("INSERT OR IGNORE INTO project_teams(project_id,team_id) VALUES(?,?)",(pid,tid))
                    for uid in dict.fromkeys(member_ids):
                        c.execute("INSERT OR IGNORE INTO project_members(project_id,user_id,member_role) VALUES(?,?,?)",(pid,uid,"PM" if uid==pm else "TFT"))
                        if uid!=x.get("created_by"):notify(c,uid,"프로젝트 TFT 배정",x["name"])
                    # 승인선: 명시 지정이 없으면 '사원 등록건은 주관팀·관련팀 팀장 승인' 규칙 자동 적용
                    creator_is_leader=creator["role"] in ("TEAM_LEADER","DIVISION_ADMIN","SUPER_ADMIN") or creator["is_team_leader"] or creator["name"] in ("정해근","이원행")
                    if not approver_ids and not creator_is_leader:
                        # 사원 등록 건은 주관팀 팀장이 개설 승인
                        host_team=x.get("team_id")
                        leaders=[r["id"] for r in c.execute(
                            "SELECT id FROM users WHERE COALESCE(active,1)=1 AND is_team_leader=1 AND team_id=?",(host_team,))] if host_team else []
                        approver_ids=[uid for uid in dict.fromkeys(leaders) if uid!=x.get("created_by")]
                        if not approver_ids:
                            div=c.execute("SELECT id FROM users WHERE COALESCE(active,1)=1 AND role IN ('DIVISION_ADMIN','SUPER_ADMIN') AND id!=? ORDER BY id LIMIT 1",(x.get("created_by"),)).fetchone()
                            if div:approver_ids=[div["id"]]
                    approver_ids=[uid for uid in dict.fromkeys(approver_ids) if uid!=x.get("created_by")]
                    for order,uid in enumerate(approver_ids,1):
                        c.execute("INSERT OR IGNORE INTO project_approvals(project_id,approver_id,approval_order,status) VALUES(?,?,?,'WAITING')",(pid,uid,order))
                        notify(c,uid,"프로젝트 개설 승인 요청",x["name"])
                    if not approver_ids:
                        c.execute("UPDATE projects SET approval_status='APPROVED' WHERE id=?",(pid,))
                    c.commit()
                    return self.send_json({"id":pid,"approval_status":"WAITING" if approver_ids else "APPROVED"})

                if p=="/api/tasks":
                    if x.get("assignee_id") and x.get("team_id"):
                        au=c.execute("SELECT team_id FROM users WHERE id=?",(x["assignee_id"],)).fetchone()
                        if not au or au["team_id"]!=x["team_id"]:
                            return self.send_json({"error":"담당자는 선택한 팀의 구성원이어야 합니다."},400)
                    st=x.get("status","TODO")
                    pid=x.get("project_id")
                    maxo=c.execute("SELECT COALESCE(MAX(priority_order),0)+1 FROM tasks WHERE COALESCE(project_id,0)=COALESCE(?,0) AND status=?",(pid,st)).fetchone()[0]
                    cur=c.execute("""INSERT INTO tasks(project_id,title,team_id,assignee_id,priority,status,due_date,description,priority_order,status_order,blocked_reason,blocked_requested_to,blocked_since,category,visibility_scope,related_team_ids,completion_mode,reviewer_id,created_by,work_type,created_at)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,CASE WHEN ?='BLOCKED' THEN datetime('now') ELSE NULL END,?,?,?,?,?,?,?,CURRENT_TIMESTAMP)""",
                    (x.get("project_id"),x["title"],x.get("team_id"),x.get("assignee_id"),x.get("priority","NORMAL"),st,x.get("due_date"),x.get("description",""),maxo,status_order(st),x.get("blocked_reason"),x.get("blocked_requested_to"),st,
                     x.get("category",""),x.get("visibility_scope","TEAM"),x.get("related_team_ids",""),x.get("completion_mode","REPORT"),x.get("reviewer_id"),x.get("created_by"),x.get("work_type","PROJECT")))
                    add_log(c,cur.lastrowid,x.get("actor_id") or x.get("created_by"),"CREATE","",x["title"])
                    if x.get("assignee_id"):
                        au=c.execute("SELECT name FROM users WHERE id=?",(x.get("assignee_id"),)).fetchone()
                        add_log(c,cur.lastrowid,x.get("actor_id") or x.get("created_by"),"ASSIGNEE","",au["name"] if au else str(x.get("assignee_id")))
                    add_log(c,cur.lastrowid,x.get("actor_id") or x.get("created_by"),"STATUS","",st)
                    raw_approver_ids=[int(v) for v in x.get("approver_ids",[]) if v]
                    # 담당자만 승인선에서 제외(자기 업무 자기승인 방지). 작성자는 승인자가 될 수 있음.
                    excluded={int(x.get("assignee_id") or 0)}
                    approver_ids=[uid for uid in dict.fromkeys(raw_approver_ids) if uid not in excluded]
                    for order,uid in enumerate(approver_ids,1):
                        c.execute("INSERT OR IGNORE INTO task_approvals(task_id,approver_id,approval_order,status) VALUES(?,?,?,'WAITING')",(cur.lastrowid,uid,order))
                    if approver_ids:c.execute("UPDATE tasks SET reviewer_id=? WHERE id=?",(approver_ids[0],cur.lastrowid))
                    # 다중 담당자(assignees: [{user_id, role_label}]) 저장 — 주 담당자(assignee_id)는 유지
                    for a in x.get("assignees",[]):
                        uid=a.get("user_id") if isinstance(a,dict) else a
                        if not uid:continue
                        c.execute("INSERT OR IGNORE INTO task_assignees(task_id,user_id,role_label) VALUES(?,?,?)",(cur.lastrowid,int(uid),(a.get("role_label","") if isinstance(a,dict) else "")))
                    notify(c,x.get("assignee_id"),"새 업무가 배정되었습니다",x["title"])
                    c.commit()
                    return self.send_json({"id":cur.lastrowid})

                if p=="/api/notices":
                    actor=c.execute("SELECT * FROM users WHERE id=?",(x.get("author_id"),)).fetchone()
                    if not actor or not (actor["role"] in ("SUPER_ADMIN","DIVISION_ADMIN") or actor["name"] in ("정해근","이원행")):
                        return self.send_json({"error":"공지사항은 관리자만 등록할 수 있습니다."},403)
                    title=(x.get("title") or "").strip();body=(x.get("body") or "").strip()
                    if not title:return self.send_json({"error":"공지 제목을 입력해주세요."},400)
                    cur=c.execute("""INSERT INTO company_notices(title,body,category,author_id,pinned)
                        VALUES(?,?,?,?,?)""",(title,body,x.get("category","일반"),x.get("author_id"),1 if x.get("pinned") else 0))
                    c.commit();return self.send_json({"id":cur.lastrowid})

                if p=="/api/requests":
                    cur=c.execute("""INSERT INTO requests(requester_id,target_user_id,target_team_id,type,title,content,status,priority,due_date)
                    VALUES(?,?,?,?,?,?,?,?,?)""",(x["requester_id"],x.get("target_user_id"),x.get("target_team_id"),x.get("type","TASK"),x["title"],x.get("content",""),"WAITING",x.get("priority","NORMAL"),x.get("due_date")))
                    notify(c,x.get("target_user_id"),"새 요청/문의가 도착했습니다",x["title"])
                    c.commit()
                    return self.send_json({"id":cur.lastrowid})

                if p=="/api/directives":
                    sender_id=int(x.get("sender_id") or 0)
                    sender=c.execute("SELECT * FROM users WHERE id=?",(sender_id,)).fetchone()
                    if not sender:return self.send_json({"error":"발신자 정보를 확인할 수 없습니다."},400)
                    # 수신자: 다중(recipient_ids) 우선, 없으면 단일(recipient_id)
                    rids=x.get("recipient_ids") or ([x.get("recipient_id")] if x.get("recipient_id") else [])
                    rids=[int(r) for r in rids if r]
                    if not rids:return self.send_json({"error":"수신자를 1명 이상 선택해주세요."},400)
                    title=(x.get("title") or "").strip();body=(x.get("body") or "").strip()
                    if not title or not body:return self.send_json({"error":"제목과 지시 내용을 입력해주세요."},400)
                    files=x.get("files",[])[:5]
                    created=[]
                    for rid in rids:
                        recipient=c.execute("SELECT * FROM users WHERE id=? AND COALESCE(active,1)=1",(rid,)).fetchone()
                        if not recipient:continue
                        cur=c.execute("""INSERT INTO directives(sender_id,recipient_id,title,body,priority,due_date,status)
                        VALUES(?,?,?,?,?,?,?)""",(sender_id,rid,title,body,x.get("priority","NORMAL"),x.get("due_date"),"SENT"))
                        did=cur.lastrowid
                        if files:
                            fdir=UPLOADS/"directive_files"/str(did);fdir.mkdir(parents=True,exist_ok=True)
                            for i,f in enumerate(files):
                                raw=base64.b64decode(f.get("data_base64","") or "")
                                if not raw:continue
                                original=f.get("name") or f"file_{i+1}"
                                safe="".join(ch for ch in original if ch.isalnum() or ch in "._-") or f"file_{i+1}"
                                stored=f"{int(time.time()*1000)}_{i}_{safe}";(fdir/stored).write_bytes(raw)
                                c.execute("INSERT INTO directive_files(directive_id,original_name,stored_name,mime_type,size_bytes) VALUES(?,?,?,?,?)",(did,original,stored,f.get("mime_type","application/octet-stream"),len(raw)))
                        notify(c,rid,"업무지시: "+title,(sender["name"] if sender else "관리자")+": "+body[:100])
                        created.append(did)
                    if not created:return self.send_json({"error":"유효한 수신자가 없습니다."},400)
                    c.commit()
                    return self.send_json({"ids":created,"id":created[0],"count":len(created),"notification_created":True})

                if p.startswith("/api/directives/") and p.endswith("/replies"):
                    did=int(p.split("/")[3])
                    cur=c.execute("INSERT INTO directive_replies(directive_id,sender_id,body) VALUES(?,?,?)",(did,x["sender_id"],x["body"]))
                    d=c.execute("SELECT sender_id,recipient_id,title FROM directives WHERE id=?",(did,)).fetchone()
                    if d:
                        target=d["sender_id"] if x["sender_id"]==d["recipient_id"] else d["recipient_id"]
                        sender=c.execute("SELECT name FROM users WHERE id=?",(x["sender_id"],)).fetchone()
                        notify(c,target,"업무지시 답변: "+d["title"],(sender["name"] if sender else "사용자")+": "+x["body"][:100])
                        c.execute("UPDATE directives SET status='REPLIED' WHERE id=?",(did,))
                    c.commit();return self.send_json({"id":cur.lastrowid})

                if p=="/api/purchases":
                    cur=c.execute("""INSERT INTO purchases(team_id,purchaser_id,item_type,item_name,vendor,amount,purchase_date,payment_method,account_id,password_hint,note)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?)""",(x.get("team_id"),x.get("purchaser_id"),x.get("item_type","기타"),x["item_name"],x.get("vendor",""),int(x.get("amount") or 0),x.get("purchase_date"),x.get("payment_method",""),x.get("account_id",""),x.get("password_hint",""),x.get("note","")))
                    c.commit();return self.send_json({"id":cur.lastrowid})

                if p=="/api/refunds":
                    cur=c.execute("""INSERT INTO refunds(team_id,instructor_id,course_name,refund_reason,refund_amount,incentive_adjustment,improvement_action,status,refund_date)
                    VALUES(1,?,?,?,?,?,?,?,?)""",(x.get("instructor_id"),x["course_name"],x.get("refund_reason",""),int(x.get("refund_amount") or 0),int(x.get("incentive_adjustment") or 0),x.get("improvement_action",""),x.get("status","OPEN"),x.get("refund_date")))
                    c.commit();return self.send_json({"id":cur.lastrowid})

                if p=="/api/book-discussions":
                    cur=c.execute("""INSERT INTO book_discussions(team_id,month_label,book_title,selected_reason,category,status,completed_date,rating,reflection)
                    VALUES(?,?,?,?,?,?,?,?,?)""",(x.get("team_id"),x.get("month_label",""),x["book_title"],x.get("selected_reason",""),x.get("category","자기계발"),x.get("status","PLANNED"),x.get("completed_date"),int(x.get("rating") or 0),x.get("reflection","")))
                    c.commit();return self.send_json({"id":cur.lastrowid})

                if p.startswith("/api/requests/") and p.endswith("/replies"):
                    rid=int(p.split("/")[3])
                    cur=c.execute("INSERT INTO request_replies(request_id,sender_id,body) VALUES(?,?,?)",
                                  (rid,x["sender_id"],x["body"]))
                    req=c.execute("SELECT requester_id,target_user_id,target_team_id,title FROM requests WHERE id=?",(rid,)).fetchone()
                    if req:
                        sender=c.execute("SELECT name FROM users WHERE id=?",(x["sender_id"],)).fetchone()
                        msg=(sender["name"] if sender else "사용자")+": "+x["body"][:100]
                        if x["sender_id"]==req["requester_id"]:
                            if req["target_user_id"]:
                                notify(c,req["target_user_id"],"요청/문의 답변: "+req["title"],msg)
                            elif req["target_team_id"]:
                                for m in c.execute("SELECT id FROM users WHERE team_id=?",(req["target_team_id"],)):
                                    if m["id"]!=x["sender_id"]: notify(c,m["id"],"요청/문의 답변: "+req["title"],msg)
                        else:
                            notify(c,req["requester_id"],"요청/문의 답변: "+req["title"],msg)
                        c.execute("UPDATE requests SET status='REPLIED' WHERE id=?",(rid,))
                    c.commit()
                    return self.send_json({"id":cur.lastrowid})

                if p=="/api/customers":
                    cur=c.execute("""INSERT INTO customers(name,phone,email,company,member_id,customer_type,onboarding_status,interests,notes,last_contact_at)
                    VALUES(?,?,?,?,?,?,?,?,?,datetime('now'))""",(x["name"],x.get("phone",""),x.get("email",""),x.get("company",""),
                    x.get("member_id",""),x.get("customer_type","일반"),x.get("onboarding_status","신규"),x.get("interests",""),x.get("notes","")))
                    c.commit();return self.send_json({"id":cur.lastrowid})

                if p=="/api/consultations":
                    cur=c.execute("""INSERT INTO consultation_logs(customer_id,counselor_id,product_name,inquiry_category,inquiry_text,response_text,channel,status,followup_date,share_to_content,share_to_planning)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?)""",(x.get("customer_id"),x.get("counselor_id"),x.get("product_name",""),x.get("inquiry_category","기타"),
                    x["inquiry_text"],x.get("response_text",""),x.get("channel","전화"),x.get("status","완료"),x.get("followup_date"),
                    1 if x.get("share_to_content") else 0,1 if x.get("share_to_planning") else 0))
                    if x.get("customer_id"):
                        c.execute("UPDATE customers SET last_contact_at=datetime('now'),onboarding_status='상담중' WHERE id=?",(x["customer_id"],))
                    c.commit();return self.send_json({"id":cur.lastrowid})

                if p=="/api/service-knowledge":
                    cur=c.execute("""INSERT INTO service_knowledge(product_name,category,question,answer,source_type,usage_count,updated_by)
                    VALUES(?,?,?,?,?,?,?)""",(x.get("product_name",""),x.get("category","기타"),x["question"],x["answer"],
                    x.get("source_type","상담"),int(x.get("usage_count") or 0),x.get("updated_by")))
                    c.commit();return self.send_json({"id":cur.lastrowid})

                if p.startswith("/api/service-knowledge/") and p.endswith("/use"):
                    kid=int(p.split("/")[3])
                    c.execute("UPDATE service_knowledge SET usage_count=usage_count+1,updated_at=datetime('now') WHERE id=?",(kid,))
                    c.commit();return self.send_json({"ok":True})

                if p=="/api/product-studies":
                    cur=c.execute("""INSERT INTO product_studies(product_name,title,content,keywords,owner_id)
                    VALUES(?,?,?,?,?)""",(x["product_name"],x["title"],x["content"],x.get("keywords",""),x.get("owner_id")))
                    c.commit();return self.send_json({"id":cur.lastrowid})

                if p=="/api/marketing/channels":
                    cur=c.execute("""INSERT INTO marketing_channels(channel_type,content_name,content_type,owner_id,status,planned_date,publish_date,url,notes)
                    VALUES(?,?,?,?,?,?,?,?,?)""",(x["channel_type"],x["content_name"],x.get("content_type",""),x.get("owner_id"),x.get("status","기획"),x.get("planned_date"),x.get("publish_date"),x.get("url",""),x.get("notes","")))
                    c.commit();return self.send_json({"id":cur.lastrowid})

                if p.startswith("/api/marketing/channels/") and p.endswith("/metrics"):
                    cid=int(p.split("/")[4])
                    cur=c.execute("""INSERT INTO marketing_channel_metrics(channel_id,metric_date,views,watch_hours,subscribers_delta,visitors,posts_count,friends_delta,clicks,impressions,conversions)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?)""",(cid,x.get("metric_date"),int(x.get("views") or 0),float(x.get("watch_hours") or 0),int(x.get("subscribers_delta") or 0),int(x.get("visitors") or 0),int(x.get("posts_count") or 0),int(x.get("friends_delta") or 0),int(x.get("clicks") or 0),int(x.get("impressions") or 0),int(x.get("conversions") or 0)))
                    c.commit();return self.send_json({"id":cur.lastrowid})

                if p=="/api/marketing/campaigns":
                    cur=c.execute("""INSERT INTO marketing_campaigns(platform,campaign_name,product_name,owner_id,start_date,end_date,budget,spend,impressions,clicks,conversions,leads,status,landing_url,notes)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",(x["platform"],x["campaign_name"],x.get("product_name",""),x.get("owner_id"),x.get("start_date"),x.get("end_date"),int(x.get("budget") or 0),int(x.get("spend") or 0),int(x.get("impressions") or 0),int(x.get("clicks") or 0),int(x.get("conversions") or 0),int(x.get("leads") or 0),x.get("status","준비"),x.get("landing_url",""),x.get("notes","")))
                    c.commit();return self.send_json({"id":cur.lastrowid})

                if p=="/api/marketing/leads":
                    cur=c.execute("""INSERT INTO marketing_leads(lead_date,source_channel,campaign_id,product_name,lead_type,customer_name,contact,status,owner_id,notes)
                    VALUES(?,?,?,?,?,?,?,?,?,?)""",(x.get("lead_date"),x.get("source_channel",""),x.get("campaign_id"),x.get("product_name",""),x.get("lead_type","문의"),x.get("customer_name",""),x.get("contact",""),x.get("status","신규"),x.get("owner_id"),x.get("notes","")))
                    c.commit();return self.send_json({"id":cur.lastrowid})

                if p=="/api/marketing/events":
                    cur=c.execute("""INSERT INTO marketing_events(event_name,event_type,owner_id,event_date,registration_count,marketing_inflow_count,promo_material_status,landing_status,message_status,notes)
                    VALUES(?,?,?,?,?,?,?,?,?,?)""",(x["event_name"],x.get("event_type",""),x.get("owner_id"),x.get("event_date"),int(x.get("registration_count") or 0),int(x.get("marketing_inflow_count") or 0),x.get("promo_material_status","미착수"),x.get("landing_status","미착수"),x.get("message_status","미착수"),x.get("notes","")))
                    c.commit();return self.send_json({"id":cur.lastrowid})

                if p=="/api/marketing/collab":
                    cur=c.execute("""INSERT INTO marketing_collab_requests(requester_team_id,requester_id,title,request_detail,owner_id,due_date,status,feedback_score,result_notes)
                    VALUES(?,?,?,?,?,?,?,?,?)""",(x.get("requester_team_id"),x.get("requester_id"),x["title"],x.get("request_detail",""),x.get("owner_id"),x.get("due_date"),x.get("status","대기"),int(x.get("feedback_score") or 0),x.get("result_notes","")))
                    c.commit();return self.send_json({"id":cur.lastrowid})

                if p=="/api/marketing/performance":
                    cur=c.execute("""INSERT INTO marketing_performance_snapshots(snapshot_month,data_type,reference_name,value_numeric,value_text,owner_id,notes)
                    VALUES(?,?,?,?,?,?,?)""",(x["snapshot_month"],x["data_type"],x["reference_name"],float(x.get("value_numeric") or 0),x.get("value_text",""),x.get("owner_id"),x.get("notes","")))
                    c.commit();return self.send_json({"id":cur.lastrowid})

                # ===== 콘.제작 강의촬영: 과정/클립 등록·수정 =====
                if p=="/api/filming/courses":
                    cur=c.execute("""INSERT INTO filming_courses(field,name,book,professor,course_code,ended,hidden,note,source,sync_status)
                    VALUES(?,?,?,?,?,?,?,?,?,?)""",(x.get("field"),x.get("name"),x.get("book"),x.get("professor"),
                    x.get("course_code"),int(x.get("ended") or 0),int(x.get("hidden") or 0),x.get("note",""),"manual","manual"))
                    c.commit();return self.send_json({"id":cur.lastrowid})

                if p=="/api/filming/clips":
                    cur=c.execute("""INSERT INTO filming_clips(course_id,clip_no,subject,shoot_date,room,video_len,attachment,coding_date,settle_status,note)
                    VALUES(?,?,?,?,?,?,?,?,?,?)""",(x["course_id"],x.get("clip_no"),x.get("subject",""),x.get("shoot_date",""),
                    x.get("room",""),x.get("video_len",""),x.get("attachment",""),x.get("coding_date",""),x.get("settle_status","미정산"),x.get("note","")))
                    c.execute("UPDATE filming_courses SET updated_at=CURRENT_TIMESTAMP WHERE id=?",(x["course_id"],))
                    c.commit();return self.send_json({"id":cur.lastrowid})

                # ===== 마케팅 영상 제작 로그 등록 =====
                if p=="/api/marketing/videos":
                    cur=c.execute("""INSERT INTO mkt_video_log(month,ym,cat_major,cat_minor,content_type,topic,shoot_date,edit_done,pm,instructor,editor,progress,url,note,source)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,'manual')""",(x.get("month",""),x.get("ym",""),x.get("cat_major",""),
                    x.get("cat_minor",""),x.get("content_type",""),x.get("topic",""),x.get("shoot_date",""),x.get("edit_done",""),
                    x.get("pm",""),x.get("instructor",""),x.get("editor",""),x.get("progress","진행예정"),x.get("url",""),x.get("note","")))
                    c.commit();return self.send_json({"id":cur.lastrowid})

                # ===== 주간 업무보고: 등록 / 이월 =====
                if p=="/api/weekly":
                    def save_deliv(item_id,b64,orig,mime):
                        if not b64: return None,None,None
                        raw=base64.b64decode(b64)
                        wdir=UPLOADS/"weekly"/str(item_id); wdir.mkdir(parents=True,exist_ok=True)
                        safe=re.sub(r'[^\w.\-]','_',orig or 'file')
                        stored=f"{int(time.time())}_{safe}"
                        (wdir/stored).write_bytes(raw)
                        return stored,orig,(mime or "application/octet-stream")
                    collabs=x.get("collaborator_ids") or []
                    cur=c.execute("""INSERT INTO weekly_reports(week_start,week_end,team_id,user_id,title,detail,complete_date,status,
                        target_rate,actual_rate,deliverable,collaborators,collaborator_ids,sort_order,created_by)
                        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (x["week_start"],x.get("week_end",""),x.get("team_id"),x.get("user_id"),x.get("title",""),x.get("detail",""),
                         x.get("complete_date"),x.get("status","미완료"),
                         (int(x["target_rate"]) if str(x.get("target_rate","")).strip()!="" else None),
                         (int(x["actual_rate"]) if str(x.get("actual_rate","")).strip()!="" else None),
                         x.get("deliverable",""),x.get("collaborators",""),json.dumps(collabs,ensure_ascii=False),
                         int(x.get("sort_order") or 0),x.get("created_by")))
                    wid=cur.lastrowid
                    if x.get("deliverable_base64"):
                        stored,orig,mime=save_deliv(wid,x.get("deliverable_base64"),x.get("deliverable_name"),x.get("deliverable_mime"))
                        c.execute("UPDATE weekly_reports SET deliverable_file=?,deliverable_original=?,deliverable_mime=? WHERE id=?",(stored,orig,mime,wid))
                    c.commit();return self.send_json({"id":wid})

                if p.startswith("/api/weekly/") and p.endswith("/carry"):
                    src_id=int(p.split("/")[3])
                    src=c.execute("SELECT * FROM weekly_reports WHERE id=?",(src_id,)).fetchone()
                    if not src: return self.send_json({"error":"원본을 찾을 수 없습니다."},404)
                    from datetime import datetime,timedelta
                    ws=datetime.strptime(src["week_start"],"%Y-%m-%d")+timedelta(days=7)
                    we=datetime.strptime(src["week_end"],"%Y-%m-%d")+timedelta(days=7) if src["week_end"] else ws+timedelta(days=6)
                    # 이미 이월된 항목 중복 방지
                    dup=c.execute("SELECT id FROM weekly_reports WHERE carried_from=?",(src_id,)).fetchone()
                    if dup:
                        return self.send_json({"error":"이미 차주로 이월된 업무입니다.","id":dup["id"]},409)
                    cur=c.execute("""INSERT INTO weekly_reports(week_start,week_end,team_id,user_id,title,detail,complete_date,status,
                        target_rate,actual_rate,deliverable,collaborators,collaborator_ids,carried_from,created_by)
                        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (ws.strftime("%Y-%m-%d"),we.strftime("%Y-%m-%d"),src["team_id"],src["user_id"],src["title"],src["detail"],
                         None,"미완료",src["target_rate"],None,src["deliverable"],src["collaborators"],src["collaborator_ids"],
                         src_id,x.get("actor_id")))
                    c.execute("UPDATE weekly_reports SET carried_over=1 WHERE id=?",(src_id,))
                    c.commit();return self.send_json({"id":cur.lastrowid,"ok":True})

                if p=="/api/planning/live-events":
                    cur=c.execute("""INSERT INTO planning_live_events(event_name,event_type,product_name,owner_id,event_date,registration_count,confirmed_count,paid_conversion_count,satisfaction_score,review_score,status,notes)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",(x["event_name"],x.get("event_type","라이브특강"),x.get("product_name",""),x.get("owner_id"),
                    x.get("event_date"),int(x.get("registration_count") or 0),int(x.get("confirmed_count") or 0),int(x.get("paid_conversion_count") or 0),
                    float(x.get("satisfaction_score") or 0),float(x.get("review_score") or 0),x.get("status","기획"),x.get("notes","")))
                    c.commit();return self.send_json({"id":cur.lastrowid})

                if p=="/api/planning/promotions":
                    cur=c.execute("""INSERT INTO planning_promotions(promotion_name,season,target_product,owner_id,start_date,end_date,visitors,cart_count,purchase_count,sales_amount,status,notes)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",(x["promotion_name"],x.get("season",""),x.get("target_product",""),x.get("owner_id"),
                    x.get("start_date"),x.get("end_date"),int(x.get("visitors") or 0),int(x.get("cart_count") or 0),
                    int(x.get("purchase_count") or 0),int(x.get("sales_amount") or 0),x.get("status","기획"),x.get("notes","")))
                    c.commit();return self.send_json({"id":cur.lastrowid})

                if p=="/api/planning/products":
                    cur=c.execute("""INSERT INTO planning_products(product_name,product_type,owner_id,launch_date,regular_price,promo_price,avg_selling_price,margin_rate,season_label,status,notes)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?)""",(x["product_name"],x.get("product_type",""),x.get("owner_id"),x.get("launch_date"),
                    int(x.get("regular_price") or 0),int(x.get("promo_price") or 0),int(x.get("avg_selling_price") or 0),
                    float(x.get("margin_rate") or 0),x.get("season_label",""),x.get("status","기획"),x.get("notes","")))
                    c.commit();return self.send_json({"id":cur.lastrowid})

                if p=="/api/planning/b2b":
                    cur=c.execute("""INSERT INTO planning_b2b(partner_name,product_name,owner_id,contract_date,supply_price,sales_amount,settlement_month,settlement_amount,settlement_status,sync_status,issue_notes,notes)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",(x["partner_name"],x.get("product_name",""),x.get("owner_id"),x.get("contract_date"),
                    int(x.get("supply_price") or 0),int(x.get("sales_amount") or 0),x.get("settlement_month",""),
                    int(x.get("settlement_amount") or 0),x.get("settlement_status","대기"),x.get("sync_status","정상"),x.get("issue_notes",""),x.get("notes","")))
                    c.commit();return self.send_json({"id":cur.lastrowid})

                if p=="/api/planning/site-ops":
                    cur=c.execute("""INSERT INTO planning_site_ops(work_type,title,owner_id,request_date,due_date,completed_date,status,page_url,issue_count,complaint_count,change_summary,notes)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",(x["work_type"],x["title"],x.get("owner_id"),x.get("request_date"),x.get("due_date"),
                    x.get("completed_date"),x.get("status","대기"),x.get("page_url",""),int(x.get("issue_count") or 0),
                    int(x.get("complaint_count") or 0),x.get("change_summary",""),x.get("notes","")))
                    c.commit();return self.send_json({"id":cur.lastrowid})

                if p=="/api/mcc/reviews":
                    cur=c.execute("""INSERT INTO mcc_reviews(review_type,target_name,compare_standard,source_version,owner_id,team_id,status,priority,total_items,changed_items,matched_items,progress,started_at,due_date,notes)
                    VALUES(?,?,?,?,?,1,?,?,?,?,?,?,?,?,?)""",(
                        x.get("review_type","교재"),x["target_name"],x.get("compare_standard",""),x.get("source_version",""),
                        x.get("owner_id"),x.get("status","검수중"),x.get("priority","NORMAL"),
                        int(x.get("total_items") or 0),int(x.get("changed_items") or 0),int(x.get("matched_items") or 0),
                        int(x.get("progress") or 0),x.get("started_at"),x.get("due_date"),x.get("notes","")))
                    c.commit();return self.send_json({"id":cur.lastrowid})

                if p.startswith("/api/mcc/reviews/") and p.endswith("/items"):
                    rid=int(p.split("/")[4])
                    cur=c.execute("""INSERT INTO mcc_review_items(review_id,item_no,category,source_text,reference_text,result_type,change_detail,decision,reviewer_id,status,page_ref)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?)""",(rid,int(x.get("item_no") or 0),x.get("category",""),x.get("source_text",""),
                    x.get("reference_text",""),x.get("result_type","일치"),x.get("change_detail",""),x.get("decision",""),
                    x.get("reviewer_id"),x.get("status","대기"),x.get("page_ref","")))
                    if x.get("result_type")!="일치":
                        c.execute("UPDATE mcc_reviews SET changed_items=changed_items+1 WHERE id=?",(rid,))
                    else:
                        c.execute("UPDATE mcc_reviews SET matched_items=matched_items+1 WHERE id=?",(rid,))
                    done=c.execute("SELECT COUNT(*) FROM mcc_review_items WHERE review_id=?",(rid,)).fetchone()[0]
                    total=c.execute("SELECT total_items FROM mcc_reviews WHERE id=?",(rid,)).fetchone()[0] or done
                    progress=min(100,round(done/total*100)) if total else 0
                    c.execute("UPDATE mcc_reviews SET progress=? WHERE id=?",(progress,rid))
                    c.commit();return self.send_json({"id":cur.lastrowid,"progress":progress})

                if p=="/api/data-center/upload":
                    tid=int(x["team_id"]);uploader=x.get("uploader_id")
                    raw=base64.b64decode(x.get("data_base64","") or "")
                    original=x.get("original_name","upload.bin")
                    safe="".join(ch for ch in original if ch.isalnum() or ch in "._-") or "upload.bin"
                    digest=__import__("hashlib").sha256(raw).hexdigest() if raw else None
                    teamdir=UPLOADS/"data_center"/str(tid);teamdir.mkdir(parents=True,exist_ok=True)
                    stored=f"{int(time.time()*1000)}_{safe}"
                    if raw:(teamdir/stored).write_bytes(raw)
                    dup=None
                    if digest:
                        r=c.execute("SELECT id FROM data_center_files WHERE team_id=? AND file_hash=? ORDER BY id LIMIT 1",(tid,digest)).fetchone()
                        dup=r["id"] if r else None
                    status="중복확인" if dup else x.get("status","처리완료")
                    cur=c.execute("""INSERT INTO data_center_files(team_id,data_type,title,original_name,stored_name,mime_type,size_bytes,file_hash,source_type,status,duplicate_of,uploader_id,notes,version_label)
                        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",(tid,x.get("data_type","기타"),x.get("title") or original,original,stored,
                        x.get("mime_type","application/octet-stream"),len(raw),digest,"FILE",status,dup,uploader,x.get("notes",""),x.get("version_label","")))
                    fid=cur.lastrowid
                    c.execute("INSERT INTO data_center_logs(file_id,team_id,actor_id,action,detail) VALUES(?,?,?,?,?)",(fid,tid,uploader,"UPLOAD",f"{original} 업로드"))
                    c.commit();return self.send_json({"id":fid,"duplicate_of":dup,"status":status})

                if p=="/api/data-center/external":
                    tid=int(x["team_id"]);uploader=x.get("uploader_id")
                    cur=c.execute("""INSERT INTO data_center_files(team_id,data_type,title,source_type,external_url,status,uploader_id,notes,version_label)
                        VALUES(?,?,?,?,?,?,?,?,?)""",(tid,x.get("data_type","외부연동"),x["title"],"EXTERNAL",x.get("external_url",""),x.get("status","연결됨"),uploader,x.get("notes",""),x.get("version_label","")))
                    fid=cur.lastrowid;c.execute("INSERT INTO data_center_logs(file_id,team_id,actor_id,action,detail) VALUES(?,?,?,?,?)",(fid,tid,uploader,"EXTERNAL_LINK","외부 데이터 연결 등록"))
                    c.commit();return self.send_json({"id":fid})

                if p=="/api/data-center/source":
                    tid=int(x["team_id"]);uploader=x.get("uploader_id")
                    cur=c.execute("""INSERT INTO data_center_files(team_id,data_type,title,source_type,status,uploader_id,notes,version_label)
                        VALUES(?,?,?,?,?,?,?,?)""",(tid,x.get("data_type","원천데이터"),x["title"],"SOURCE",x.get("status","사용중"),uploader,x.get("notes",""),x.get("version_label","")))
                    fid=cur.lastrowid;c.execute("INSERT INTO data_center_logs(file_id,team_id,actor_id,action,detail) VALUES(?,?,?,?,?)",(fid,tid,uploader,"SOURCE_CREATE","원천 데이터 등록"))
                    c.commit();return self.send_json({"id":fid})

                if p.startswith("/api/data-center/") and p.endswith("/reprocess"):
                    fid=int(p.split("/")[3]);r=c.execute("SELECT * FROM data_center_files WHERE id=?",(fid,)).fetchone()
                    if not r:return self.send_json({"error":"not found"},404)
                    c.execute("UPDATE data_center_files SET status='처리완료',error_message='',updated_at=datetime('now') WHERE id=?",(fid,))
                    c.execute("INSERT INTO data_center_logs(file_id,team_id,actor_id,action,detail) VALUES(?,?,?,?,?)",(fid,r["team_id"],x.get("actor_id"),"REPROCESS","수동 재처리 완료"))
                    c.commit();return self.send_json({"ok":True})

                if p=="/api/templates":
                    cur=c.execute("""INSERT INTO work_templates(template_type,name,description,payload_json,team_id,created_by)
                        VALUES(?,?,?,?,?,?)""",(x["template_type"],x["name"],x.get("description",""),json.dumps(x.get("payload",{}),ensure_ascii=False),x.get("team_id"),x.get("created_by")))
                    c.commit();return self.send_json({"id":cur.lastrowid})

                if p=="/api/admin/users":
                    actor=c.execute("SELECT * FROM users WHERE id=?",(x.get("actor_id"),)).fetchone()
                    if not actor or not (actor["role"]=="SUPER_ADMIN" or actor["name"] in ("정해근","이원행")):
                        return self.send_json({"error":"관리자 권한이 없습니다."},403)
                    # 신규 사용자는 최초 비밀번호를 1234로 설정(로그인 후 개인설정에서 변경).
                    # login_id 미입력 시 이메일 @ 앞부분을 아이디로 자동 지정한다.
                    email_v=(x.get("email") or "").strip()
                    login_v=(x.get("login_id") or "").strip() or (email_v.split("@")[0].lower() if "@" in email_v else "")
                    cur=c.execute("""INSERT INTO users(team_id,name,rank,job_title,extension,email,role,is_team_leader,active,login_id,phone,account_status,password_hash)
                        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",(x.get("team_id"),x["name"],x.get("rank",""),x.get("job_title",""),x.get("extension",""),email_v,
                        x.get("role","MEMBER"),1 if x.get("is_team_leader") else 0,1,login_v,x.get("phone",""),"ACTIVE",hash_pw("1234")))
                    c.commit();return self.send_json({"id":cur.lastrowid,"login_id":login_v})

                if p=="/api/admin/teams":
                    actor=c.execute("SELECT * FROM users WHERE id=?",(x.get("actor_id"),)).fetchone()
                    if not actor or not (actor["role"]=="SUPER_ADMIN" or actor["name"] in ("정해근","이원행")):
                        return self.send_json({"error":"관리자 권한이 없습니다."},403)
                    cur=c.execute("INSERT INTO teams(division_id,name,active,sort_order) VALUES(?,?,1,?)",(1,x["name"],x.get("sort_order",99)))
                    c.commit();return self.send_json({"id":cur.lastrowid})

                if p.startswith("/api/tasks/") and p.endswith("/review-request"):
                    tid=int(p.split("/")[3]);t=c.execute("SELECT * FROM tasks WHERE id=?",(tid,)).fetchone()
                    if not t:return self.send_json({"error":"not found"},404)
                    actor=int(x.get("requester_id") or 0)
                    admin=c.execute("SELECT 1 FROM users WHERE id=? AND role IN ('SUPER_ADMIN','DIVISION_ADMIN')",(actor,)).fetchone()
                    if actor not in (t["assignee_id"],t["created_by"]) and not admin:return self.send_json({"error":"담당자만 검토를 요청할 수 있습니다."},403)
                    if t["completion_mode"]!="REVIEW":return self.send_json({"error":"검토·컨펌형 업무가 아닙니다."},400)
                    approvals=c.execute("SELECT * FROM task_approvals WHERE task_id=? ORDER BY approval_order",(tid,)).fetchall()
                    if not approvals and t["reviewer_id"]:
                        c.execute("INSERT OR IGNORE INTO task_approvals(task_id,approver_id,approval_order,status) VALUES(?,?,1,'WAITING')",(tid,t["reviewer_id"]))
                    c.execute("UPDATE task_approvals SET status='WAITING',comment=NULL,decided_at=NULL WHERE task_id=?",(tid,))
                    # 요청자 또는 현재 담당자가 승인선에 포함되어 있으면 자기 승인 방지를 위해 자동 건너뜀
                    c.execute("""UPDATE task_approvals SET status='SKIPPED',comment='요청자/담당자 자기 승인 제외',decided_at=datetime('now')
                        WHERE task_id=? AND approver_id IN (?,?)""",(tid,actor,t["assignee_id"] or 0))
                    first=c.execute("""SELECT approver_id FROM task_approvals WHERE task_id=? AND status='WAITING'
                        ORDER BY approval_order LIMIT 1""",(tid,)).fetchone()
                    if not first:return self.send_json({"error":"요청자 본인을 제외한 검토 승인자를 지정해주세요."},400)
                    c.execute("UPDATE task_review_requests SET status='SUPERSEDED',completed_at=datetime('now') WHERE task_id=? AND status='WAITING'",(tid,))
                    cur=c.execute("INSERT INTO task_review_requests(task_id,requester_id,comment,status) VALUES(?,?,?,'WAITING')",(tid,actor,x.get("comment","")))
                    rid=cur.lastrowid
                    reviewdir=UPLOADS/"task_reviews"/str(rid);reviewdir.mkdir(parents=True,exist_ok=True)
                    for i,f in enumerate(x.get("files",[])[:5]):
                        raw=base64.b64decode(f.get("data_base64","") or "")
                        if not raw:continue
                        original=f.get("name") or f"file_{i+1}"
                        safe="".join(ch for ch in original if ch.isalnum() or ch in "._-") or f"file_{i+1}"
                        stored=f"{int(time.time()*1000)}_{i}_{safe}";(reviewdir/stored).write_bytes(raw)
                        c.execute("INSERT INTO task_review_files(request_id,original_name,stored_name,mime_type,size_bytes) VALUES(?,?,?,?,?)",(rid,original,stored,f.get("mime_type","application/octet-stream"),len(raw)))
                    c.execute("UPDATE tasks SET status='REVIEW',status_order=?,reviewer_id=? WHERE id=?",(status_order("REVIEW"),first["approver_id"],tid))
                    add_log(c,tid,actor,"REVIEW_REQUEST","",x.get("comment",""));notify(c,first["approver_id"],"업무 검토 요청",t["title"])
                    c.commit();return self.send_json({"id":rid,"status":"REVIEW"})

                if p.startswith("/api/tasks/") and p.endswith("/complete-report"):
                    tid=int(p.split("/")[3]);t=c.execute("SELECT * FROM tasks WHERE id=?",(tid,)).fetchone()
                    if not t:return self.send_json({"error":"not found"},404)
                    actor=int(x.get("actor_id") or 0)
                    admin=c.execute("SELECT 1 FROM users WHERE id=? AND role IN ('SUPER_ADMIN','DIVISION_ADMIN')",(actor,)).fetchone()
                    if actor not in (t["assignee_id"],t["created_by"]) and not admin:return self.send_json({"error":"담당자만 완료보고를 할 수 있습니다."},403)
                    comment=(x.get("comment") or "").strip()
                    # 완료보고도 검토처럼 코멘트+파일을 기록 (task_review_requests/files 재사용, 상태 REPORTED)
                    cur=c.execute("INSERT INTO task_review_requests(task_id,requester_id,comment,status,completed_at) VALUES(?,?,?,'REPORTED',datetime('now'))",(tid,actor,comment))
                    rid=cur.lastrowid
                    reportdir=UPLOADS/"task_reviews"/str(rid);reportdir.mkdir(parents=True,exist_ok=True)
                    nfiles=0
                    for i,f in enumerate(x.get("files",[])[:5]):
                        raw=base64.b64decode(f.get("data_base64","") or "")
                        if not raw:continue
                        original=f.get("name") or f"file_{i+1}"
                        safe="".join(ch for ch in original if ch.isalnum() or ch in "._-") or f"file_{i+1}"
                        stored=f"{int(time.time()*1000)}_{i}_{safe}";(reportdir/stored).write_bytes(raw)
                        c.execute("INSERT INTO task_review_files(request_id,original_name,stored_name,mime_type,size_bytes) VALUES(?,?,?,?,?)",(rid,original,stored,f.get("mime_type","application/octet-stream"),len(raw)))
                        nfiles+=1
                    if comment:
                        c.execute("INSERT INTO task_comments(task_id,user_id,body) VALUES(?,?,?)",(tid,actor,"[완료보고] "+comment))
                    c.execute("UPDATE tasks SET status='DONE',status_order=? WHERE id=?",(status_order("DONE"),tid))
                    add_log(c,tid,actor,"COMPLETE_REPORT","",comment or (f"첨부 {nfiles}건" if nfiles else "완료보고"))
                    c.commit();return self.send_json({"id":rid,"status":"DONE","files":nfiles})

                if p=="/api/calendar":
                    cur=c.execute("""INSERT INTO calendar_events(title,start_date,end_date,event_type,team_id,owner_id,description,category) VALUES(?,?,?,?,?,?,?,?)""",
                    (x["title"],x["start_date"],x.get("end_date") or x["start_date"],x.get("event_type","GENERAL"),x.get("team_id"),x.get("owner_id"),x.get("description",""),x.get("category","TEAM")));c.commit()
                    return self.send_json({"id":cur.lastrowid})

                if p=="/api/meetings":
                    cur=c.execute("INSERT INTO meetings(team_id,title,meeting_date,attendees,decisions,action_items) VALUES(?,?,?,?,?,?)",(x["team_id"],x["title"],x["meeting_date"],x.get("attendees",""),x.get("decisions",""),x.get("action_items","")));c.commit()
                    return self.send_json({"id":cur.lastrowid})

                if p=="/api/checklists":
                    cur=c.execute("INSERT INTO checklists(team_id,title,recurrence,items) VALUES(?,?,?,?)",(x["team_id"],x["title"],x.get("recurrence","NONE"),x.get("items","")));c.commit()
                    return self.send_json({"id":cur.lastrowid})

                if p=="/api/resources/upload":
                    original=x.get("file_name","upload.bin")
                    safe="".join(ch for ch in original if ch.isalnum() or ch in "._- ()[]")[:120] or "upload.bin"
                    raw=base64.b64decode(x.get("file_base64",""))
                    stored=f"{int(time.time()*1000)}_{safe}"
                    (UPLOADS/stored).write_bytes(raw)
                    cur=c.execute("INSERT INTO resources(team_id,title,resource_type,url,description) VALUES(?,?,?,?,?)",
                                  (x["team_id"],x["title"],"FILE",f"/uploads/{stored}",x.get("description","")))
                    c.commit()
                    return self.send_json({"id":cur.lastrowid,"url":f"/uploads/{stored}","file_name":original,"size_bytes":len(raw)})

                if p=="/api/resources":
                    cur=c.execute("INSERT INTO resources(team_id,title,resource_type,url,description) VALUES(?,?,?,?,?)",(x["team_id"],x["title"],x.get("resource_type","LINK"),x.get("url",""),x.get("description","")));c.commit()
                    return self.send_json({"id":cur.lastrowid})

                if p=="/api/boards":
                    cur=c.execute("INSERT INTO data_boards(team_id,name,description,board_type) VALUES(?,?,?,?)",(x["team_id"],x["name"],x.get("description",""),"TABLE"))
                    bid=cur.lastrowid
                    # First title column is implicit; custom columns are stored.
                    for i,col in enumerate(x.get("columns",[]), start=1):
                        key=col.get("column_key") or f"col_{i}"
                        c.execute("""INSERT INTO data_board_columns(board_id,column_key,label,data_type,sort_order)
                        VALUES(?,?,?,?,?)""",(bid,key,col["label"],col.get("data_type","TEXT"),i))
                    c.commit(); return self.send_json({"id":bid})

                if p.startswith("/api/boards/") and p.endswith("/columns"):
                    bid=int(p.split("/")[3])
                    maxo=c.execute("SELECT COALESCE(MAX(sort_order),0)+1 FROM data_board_columns WHERE board_id=?",(bid,)).fetchone()[0]
                    key=x.get("column_key") or f"col_{maxo}"
                    cur=c.execute("""INSERT INTO data_board_columns(board_id,column_key,label,data_type,sort_order)
                    VALUES(?,?,?,?,?)""",(bid,key,x["label"],x.get("data_type","TEXT"),maxo));c.commit()
                    return self.send_json({"id":cur.lastrowid})

                if p.startswith("/api/boards/") and p.endswith("/rows"):
                    bid=int(p.split("/")[3])
                    cur=c.execute("INSERT INTO data_board_rows(board_id,title) VALUES(?,?)",(bid,x["title"]))
                    rid=cur.lastrowid
                    for k,v in x.get("values",{}).items():
                        c.execute("INSERT INTO data_board_values(row_id,column_key,value) VALUES(?,?,?)",(rid,k,str(v)))
                    c.commit(); return self.send_json({"id":rid})

                if p.startswith("/api/tasks/") and p.endswith("/comments"):
                    tid=int(p.split("/")[3])
                    cur=c.execute("INSERT INTO task_comments(task_id,user_id,body) VALUES(?,?,?)",(tid,x.get("user_id"),x["body"]))
                    add_log(c,tid,x.get("user_id"),"COMMENT","",x["body"][:100])
                    task=c.execute("SELECT assignee_id,title FROM tasks WHERE id=?",(tid,)).fetchone()
                    if task and task["assignee_id"] and task["assignee_id"] != x.get("user_id"):
                        notify(c,task["assignee_id"],"업무에 새 댓글이 등록되었습니다",task["title"])
                    c.commit(); return self.send_json({"id":cur.lastrowid})

            return self.send_json({"error":"not found"},404)
        except Exception as e:
            traceback.print_exc();return self.send_json({"error":str(e)},500)

    def do_PATCH(self):
        try:
            p=urlparse(self.path).path;x=self.body()
            with db() as c:
                # 클립 촬영/코딩/정산 상태 갱신
                if p.startswith("/api/filming/clips/") and p.rsplit("/",1)[1].isdigit():
                    clip_id=int(p.rsplit("/",1)[1])
                    fields=[]; args=[]
                    for k in ("shoot_date","room","video_len","attachment","coding_date","settle_status","subject","note"):
                        if k in x: fields.append(f"{k}=?"); args.append(x[k])
                    if not fields: return self.send_json({"error":"변경할 항목이 없습니다."},400)
                    args.append(clip_id)
                    c.execute(f"UPDATE filming_clips SET {','.join(fields)} WHERE id=?",args)
                    c.commit();return self.send_json({"ok":True})

                # 과정 정보/종강/숨김 갱신
                if p.startswith("/api/filming/courses/") and p.rsplit("/",1)[1].isdigit():
                    fid=int(p.rsplit("/",1)[1])
                    fields=[]; args=[]
                    for k in ("field","name","book","professor","course_code","ended","hidden","note"):
                        if k in x: fields.append(f"{k}=?"); args.append(x[k])
                    if not fields: return self.send_json({"error":"변경할 항목이 없습니다."},400)
                    fields.append("updated_at=CURRENT_TIMESTAMP")
                    args.append(fid)
                    c.execute(f"UPDATE filming_courses SET {','.join(fields)} WHERE id=?",args)
                    c.commit();return self.send_json({"ok":True})

                # 주간 업무보고 항목 수정
                if p.startswith("/api/weekly/") and p.rsplit("/",1)[1].isdigit():
                    wid=int(p.rsplit("/",1)[1])
                    item=c.execute("SELECT * FROM weekly_reports WHERE id=?",(wid,)).fetchone()
                    if not item: return self.send_json({"error":"항목을 찾을 수 없습니다."},404)
                    actor=c.execute("SELECT * FROM users WHERE id=?",(x.get("actor_id"),)).fetchone()
                    is_admin=actor and (actor["role"] in ("SUPER_ADMIN","DIVISION_ADMIN") or actor["is_team_leader"] or actor["name"] in ("정해근","이원행"))
                    if not (actor and (actor["id"]==item["user_id"] or is_admin)):
                        return self.send_json({"error":"본인 또는 팀장/관리자만 수정할 수 있습니다."},403)
                    fields=[]; args=[]
                    for k in ("title","detail","complete_date","status","deliverable","collaborators"):
                        if k in x: fields.append(f"{k}=?"); args.append(x[k])
                    for k in ("target_rate","actual_rate"):
                        if k in x:
                            v=x[k]; fields.append(f"{k}=?"); args.append(int(v) if str(v).strip()!="" else None)
                    if "collaborator_ids" in x:
                        fields.append("collaborator_ids=?"); args.append(json.dumps(x["collaborator_ids"] or [],ensure_ascii=False))
                    if x.get("deliverable_base64"):
                        raw=base64.b64decode(x["deliverable_base64"])
                        wdir=UPLOADS/"weekly"/str(wid); wdir.mkdir(parents=True,exist_ok=True)
                        safe=re.sub(r'[^\w.\-]','_',x.get("deliverable_name") or 'file')
                        stored=f"{int(time.time())}_{safe}"; (wdir/stored).write_bytes(raw)
                        fields+=["deliverable_file=?","deliverable_original=?","deliverable_mime=?"]
                        args+=[stored,x.get("deliverable_name"),x.get("deliverable_mime") or "application/octet-stream"]
                    if not fields: return self.send_json({"error":"변경할 항목이 없습니다."},400)
                    fields.append("updated_at=CURRENT_TIMESTAMP"); args.append(wid)
                    c.execute(f"UPDATE weekly_reports SET {','.join(fields)} WHERE id=?",args)
                    c.commit();return self.send_json({"ok":True})

                if p.startswith("/api/requests/") and p.count("/")==3 and p.rsplit("/",1)[1].isdigit():
                    rid=int(p.rsplit("/",1)[1])
                    old=c.execute("SELECT * FROM requests WHERE id=?",(rid,)).fetchone()
                    if not old:return self.send_json({"error":"not found"},404)
                    status=x.get("status","DONE")
                    if status not in ("WAITING","IN_PROGRESS","REPLIED","DONE"):
                        return self.send_json({"error":"허용되지 않은 상태입니다."},400)
                    c.execute("UPDATE requests SET status=? WHERE id=?",(status,rid))
                    if status=="DONE":
                        actor=x.get("actor_id")
                        target=old["requester_id"] if actor==old["target_user_id"] else old["target_user_id"]
                        if target and target!=actor:
                            notify(c,target,"요청/문의 처리 완료",old["title"])
                    c.commit();return self.send_json({"ok":True,"status":status})

                if p.startswith("/api/projects/") and p.endswith("/approve"):
                    pid=int(p.split("/")[3]);actor=x.get("actor_id");action=x.get("action","APPROVE")
                    pa=c.execute("SELECT * FROM project_approvals WHERE project_id=? AND approver_id=?",(pid,actor)).fetchone()
                    if not pa:return self.send_json({"error":"승인 권한이 없습니다."},403)
                    st="APPROVED" if action=="APPROVE" else "REJECTED"
                    c.execute("UPDATE project_approvals SET status=?,comment=?,decided_at=datetime('now') WHERE id=?",(st,x.get("comment",""),pa["id"]))
                    if action=="REJECT":
                        c.execute("UPDATE projects SET approval_status='REJECTED' WHERE id=?",(pid,))
                    else:
                        waiting=c.execute("SELECT COUNT(*) FROM project_approvals WHERE project_id=? AND status='WAITING'",(pid,)).fetchone()[0]
                        rejected=c.execute("SELECT COUNT(*) FROM project_approvals WHERE project_id=? AND status='REJECTED'",(pid,)).fetchone()[0]
                        if waiting==0 and rejected==0:c.execute("UPDATE projects SET approval_status='APPROVED' WHERE id=?",(pid,))
                    p0=c.execute("SELECT name,created_by FROM projects WHERE id=?",(pid,)).fetchone()
                    if p0:notify(c,p0["created_by"],"프로젝트 승인 결과",p0["name"]+" · "+st)
                    c.commit();return self.send_json({"ok":True,"status":st})

                if p.startswith("/api/tasks/") and p.endswith("/approve"):
                    tid=int(p.split("/")[3]);t=c.execute("SELECT * FROM tasks WHERE id=?",(tid,)).fetchone()
                    if not t:return self.send_json({"error":"not found"},404)
                    actor=int(x.get("actor_id") or 0)
                    current=c.execute("SELECT * FROM task_approvals WHERE task_id=? AND status='WAITING' ORDER BY approval_order LIMIT 1",(tid,)).fetchone()
                    if not current or actor!=current["approver_id"]:return self.send_json({"error":"현재 단계의 지정 승인자만 처리할 수 있습니다."},403)
                    action=x.get("action","APPROVE");comment=x.get("comment","")
                    if action=="APPROVE":
                        c.execute("UPDATE task_approvals SET status='APPROVED',comment=?,decided_at=datetime('now') WHERE id=?",(comment,current["id"]))
                        add_log(c,tid,actor,"APPROVE",str(current["approval_order"]),comment or "승인")
                        nxt=c.execute("SELECT * FROM task_approvals WHERE task_id=? AND status='WAITING' ORDER BY approval_order LIMIT 1",(tid,)).fetchone()
                        if nxt:
                            c.execute("UPDATE tasks SET status='REVIEW',status_order=?,reviewer_id=? WHERE id=?",(status_order("REVIEW"),nxt["approver_id"],tid))
                            notify(c,nxt["approver_id"],"다음 단계 업무 검토 요청",t["title"]);result="REVIEW"
                        else:
                            c.execute("UPDATE tasks SET status='DONE',status_order=? WHERE id=?",(status_order("DONE"),tid))
                            c.execute("UPDATE task_review_requests SET status='APPROVED',completed_at=datetime('now') WHERE task_id=? AND status='WAITING'",(tid,))
                            notify(c,t["assignee_id"],"업무 최종 승인 완료",t["title"]);result="DONE"
                    else:
                        c.execute("UPDATE task_approvals SET status='REJECTED',comment=?,decided_at=datetime('now') WHERE id=?",(comment,current["id"]))
                        c.execute("UPDATE tasks SET status='IN_PROGRESS',status_order=? WHERE id=?",(status_order("IN_PROGRESS"),tid))
                        c.execute("UPDATE task_review_requests SET status='REJECTED',completed_at=datetime('now') WHERE task_id=? AND status='WAITING'",(tid,))
                        add_log(c,tid,actor,"REJECT",str(current["approval_order"]),comment or "반려");notify(c,t["assignee_id"],"업무 검토 반려",t["title"]);result="IN_PROGRESS"
                    c.commit();return self.send_json({"ok":True,"status":result})


                if p.startswith("/api/tasks/") and p.endswith("/move"):
                    tid=int(p.split("/")[3])
                    old=c.execute("SELECT status,assignee_id,title FROM tasks WHERE id=?",(tid,)).fetchone()
                    if x["status"]=="BLOCKED":
                        c.execute("UPDATE tasks SET status=?,status_order=?,priority_order=?,blocked_since=COALESCE(blocked_since,datetime('now')) WHERE id=?",(x["status"],status_order(x["status"]),x.get("order",0),tid))
                    else:
                        c.execute("UPDATE tasks SET status=?,status_order=?,priority_order=?,blocked_reason=NULL,blocked_requested_to=NULL,blocked_since=NULL WHERE id=?",(x["status"],status_order(x["status"]),x.get("order",0),tid))
                    add_log(c,tid,x.get("actor_id"),"MOVE",old["status"] if old else "",x["status"])
                    if old and old["assignee_id"] and old["assignee_id"] != x.get("actor_id"):
                        notify(c,old["assignee_id"],"업무 상태가 변경되었습니다",f'{old["title"]}: {x["status"]}')
                    c.commit();return self.send_json({"ok":True})

                if p.startswith("/api/tasks/") and p.endswith("/priority"):
                    tid=int(p.split("/")[3])
                    old=c.execute("SELECT priority FROM tasks WHERE id=?",(tid,)).fetchone()
                    c.execute("UPDATE tasks SET priority=?,priority_order=? WHERE id=?",(x.get("priority","NORMAL"),x.get("order",0),tid))
                    add_log(c,tid,x.get("actor_id"),"PRIORITY",old["priority"] if old else "",x.get("priority","NORMAL"))
                    c.commit();return self.send_json({"ok":True})

                if p.startswith("/api/projects/") and p.count("/")==3 and p.rsplit("/",1)[1].isdigit():
                    pid=int(p.rsplit("/",1)[1])
                    pr=c.execute("SELECT * FROM projects WHERE id=?",(pid,)).fetchone()
                    if not pr:return self.send_json({"error":"not found"},404)
                    actor=int(x.get("actor_id") or 0)
                    admin=c.execute("SELECT 1 FROM users WHERE id=? AND role IN ('SUPER_ADMIN','DIVISION_ADMIN')",(actor,)).fetchone()
                    if actor not in (pr["pm_user_id"],pr["created_by"]) and not admin:
                        return self.send_json({"error":"PM·작성자 또는 관리자만 상태를 변경할 수 있습니다."},403)
                    st=x.get("status")
                    if st in ("HOLD","ACTIVE"):
                        c.execute("UPDATE projects SET status=? WHERE id=?",(st,pid))
                    c.commit();return self.send_json({"ok":True,"status":st})

                if p.startswith("/api/tasks/") and p.count("/") == 3:
                    tid=int(p.rsplit("/",1)[1])
                    old=c.execute("SELECT * FROM tasks WHERE id=?",(tid,)).fetchone()
                    if not old:return self.send_json({"error":"not found"},404)
                    fields=["title","description","team_id","assignee_id","priority","status","due_date","completion_mode"]
                    vals={k:x[k] for k in fields if k in x}
                    if "status" in vals:
                        vals["status_order"]=status_order(vals["status"])
                        if vals["status"]=="BLOCKED":
                            vals["blocked_since"]=old["blocked_since"] if "blocked_since" in old.keys() and old["blocked_since"] else __import__("datetime").datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                        else:
                            vals["blocked_reason"]=None
                            vals["blocked_requested_to"]=None
                            vals["blocked_since"]=None
                    if vals:
                        setq=",".join(f"{k}=?" for k in vals)
                        c.execute(f"UPDATE tasks SET {setq} WHERE id=?",list(vals.values())+[tid])
                        for k,v in vals.items():
                            if k=="status_order": continue
                            add_log(c,tid,x.get("actor_id"),"UPDATE_"+k.upper(),str(old[k]) if k in old.keys() else "",str(v))
                    if "assignee_id" in vals and vals["assignee_id"] != old["assignee_id"]:
                        notify(c,vals["assignee_id"],"업무 담당자로 지정되었습니다",vals.get("title") or old["title"])
                    # 다중 담당자 재설정 (assignees 전달 시 전체 교체)
                    if "assignees" in x:
                        c.execute("DELETE FROM task_assignees WHERE task_id=?",(tid,))
                        for a in x.get("assignees",[]):
                            uid=a.get("user_id") if isinstance(a,dict) else a
                            if not uid:continue
                            c.execute("INSERT OR IGNORE INTO task_assignees(task_id,user_id,role_label) VALUES(?,?,?)",(tid,int(uid),(a.get("role_label","") if isinstance(a,dict) else "")))
                    # 검토·승인선 재설정 (approver_ids 전달 시 전체 교체). 진행 중 승인 이력이 없을 때만 안전 교체.
                    if "approver_ids" in x:
                        assignee_now=x.get("assignee_id",old["assignee_id"])
                        raw=[int(v) for v in x.get("approver_ids",[]) if v]
                        appr=[uid for uid in dict.fromkeys(raw) if uid!=int(assignee_now or 0)]
                        c.execute("DELETE FROM task_approvals WHERE task_id=?",(tid,))
                        for order,uid in enumerate(appr,1):
                            c.execute("INSERT OR IGNORE INTO task_approvals(task_id,approver_id,approval_order,status) VALUES(?,?,?,'WAITING')",(tid,uid,order))
                        c.execute("UPDATE tasks SET reviewer_id=? WHERE id=?",(appr[0] if appr else None,tid))
                        add_log(c,tid,x.get("actor_id"),"UPDATE_APPROVERS","",",".join(str(a) for a in appr))
                    c.commit();return self.send_json({"ok":True})

                if p.startswith("/api/mcc/reviews/") and p.count("/")==4:
                    rid=int(p.rsplit("/",1)[1])
                    old=c.execute("SELECT * FROM mcc_reviews WHERE id=?",(rid,)).fetchone()
                    if not old:return self.send_json({"error":"not found"},404)
                    fields=["review_type","target_name","compare_standard","source_version","owner_id","status","priority","total_items","changed_items","matched_items","progress","started_at","due_date","completed_at","notes"]
                    vals={k:x[k] for k in fields if k in x}
                    if vals:
                        sql=",".join(f"{k}=?" for k in vals)
                        c.execute(f"UPDATE mcc_reviews SET {sql} WHERE id=?",list(vals.values())+[rid])
                    c.commit();return self.send_json({"ok":True})

                if p.startswith("/api/data-center/") and p.count("/")==3 and p.rsplit("/",1)[1].isdigit():
                    fid=int(p.rsplit("/",1)[1]);old=c.execute("SELECT * FROM data_center_files WHERE id=?",(fid,)).fetchone()
                    if not old:return self.send_json({"error":"not found"},404)
                    fields=["data_type","title","external_url","status","notes","version_label","error_message","duplicate_of"]
                    vals={k:x[k] for k in fields if k in x}
                    if vals:
                        sql=",".join(f"{k}=?" for k in vals)+",updated_at=datetime('now')"
                        c.execute(f"UPDATE data_center_files SET {sql} WHERE id=?",list(vals.values())+[fid])
                    c.execute("INSERT INTO data_center_logs(file_id,team_id,actor_id,action,detail) VALUES(?,?,?,?,?)",(fid,old["team_id"],x.get("actor_id"),"UPDATE","데이터 정보 수정"))
                    c.commit();return self.send_json({"ok":True})

                if p.startswith("/api/admin/teams/") and p.count("/")==4:
                    tid=int(p.rsplit("/",1)[1])
                    actor=c.execute("SELECT * FROM users WHERE id=?",(x.get("actor_id"),)).fetchone()
                    if not actor or not (actor["role"]=="SUPER_ADMIN" or actor["name"] in ("정해근","이원행")):
                        return self.send_json({"error":"관리자 권한이 없습니다."},403)
                    vals={k:x[k] for k in ["name","active","sort_order"] if k in x}
                    if vals:
                        sql=",".join(f"{k}=?" for k in vals)
                        c.execute(f"UPDATE teams SET {sql} WHERE id=?",list(vals.values())+[tid])
                    c.commit();return self.send_json({"ok":True})

                if p.startswith("/api/projects/") and p.endswith("/tft"):
                    pid=int(p.split("/")[3])
                    actor=c.execute("SELECT * FROM users WHERE id=?",(x.get("actor_id"),)).fetchone()
                    p0=c.execute("SELECT * FROM projects WHERE id=?",(pid,)).fetchone()
                    if not p0:return self.send_json({"error":"not found"},404)
                    if not actor or not (actor["role"] in ("SUPER_ADMIN","DIVISION_ADMIN") or actor["is_team_leader"] or actor["id"] in (p0["created_by"],p0["pm_user_id"])):
                        return self.send_json({"error":"TFT 편집 권한이 없습니다."},403)
                    member_ids=[int(v) for v in x.get("member_ids",[]) if v]
                    leader_id=int(x.get("leader_id") or 0)
                    tft_name=x.get("tft_name","")
                    c.execute("DELETE FROM project_members WHERE project_id=?",(pid,))
                    team_ids=[]
                    for uid in dict.fromkeys(member_ids):
                        u=c.execute("SELECT team_id FROM users WHERE id=?",(uid,)).fetchone()
                        if not u:continue
                        if u["team_id"] and u["team_id"] not in team_ids:team_ids.append(u["team_id"])
                        role="LEADER" if uid==leader_id else "TFT"
                        c.execute("INSERT INTO project_members(project_id,user_id,member_role) VALUES(?,?,?)",(pid,uid,role))
                        notify(c,uid,"프로젝트 TFT 구성 변경",p0["name"])
                    c.execute("DELETE FROM project_teams WHERE project_id=?",(pid,))
                    for tid in team_ids:c.execute("INSERT INTO project_teams(project_id,team_id) VALUES(?,?)",(pid,tid))
                    if leader_id:
                        c.execute("UPDATE projects SET pm_user_id=? WHERE id=?",(leader_id,pid))
                    if tft_name:
                        c.execute("UPDATE projects SET description=CASE WHEN description IS NULL OR description='' THEN ? ELSE description END WHERE id=?",(tft_name,pid))
                    c.commit();return self.send_json({"ok":True,"member_count":len(member_ids)})

                if p.startswith("/api/users/") and p.count("/")==3:
                    uid=int(p.rsplit("/",1)[1])
                    actor_id=x.get("actor_id")
                    actor=c.execute("SELECT * FROM users WHERE id=?",(actor_id,)).fetchone()
                    if not actor or not (actor["role"]=="SUPER_ADMIN" or actor["name"] in ("정해근","이원행")):
                        return self.send_json({"error":"관리자만 구성원 정보를 수정할 수 있습니다."},403)
                    old=c.execute("SELECT * FROM users WHERE id=?",(uid,)).fetchone()
                    if not old:return self.send_json({"error":"not found"},404)
                    fields=["name","rank","job_title","extension","email","team_id","active","role","is_team_leader","login_id","phone","account_status"]
                    vals={k:x[k] for k in fields if k in x}
                    if vals:
                        sql=",".join(f"{k}=?" for k in vals)
                        c.execute(f"UPDATE users SET {sql} WHERE id=?",list(vals.values())+[uid])
                    c.commit();return self.send_json({"ok":True})

                if p.startswith("/api/calendar/") and p.count("/")==3:
                    eid=int(p.rsplit("/",1)[1])
                    old=c.execute("SELECT * FROM calendar_events WHERE id=?",(eid,)).fetchone()
                    if not old:return self.send_json({"error":"not found"},404)
                    actor=x.get("actor_id")
                    if actor!=old["owner_id"]:
                        return self.send_json({"error":"작성자만 수정할 수 있습니다."},403)
                    fields=["title","start_date","end_date","event_type","team_id","description","category"]
                    vals={k:x[k] for k in fields if k in x}
                    if vals:
                        sql=",".join(f"{k}=?" for k in vals)
                        c.execute(f"UPDATE calendar_events SET {sql} WHERE id=?",list(vals.values())+[eid])
                    c.commit();return self.send_json({"ok":True})

                if p.startswith("/api/notifications/") and p.endswith("/read"):
                    nid=int(p.split("/")[3]);c.execute("UPDATE notifications SET is_read=1 WHERE id=?",(nid,));c.commit();return self.send_json({"ok":True})

                if p.startswith("/api/planning/products/") and p.count("/")==4 and p.rsplit("/",1)[1].isdigit():
                    pid=int(p.rsplit("/",1)[1])
                    old=c.execute("SELECT * FROM planning_products WHERE id=?",(pid,)).fetchone()
                    if not old:return self.send_json({"error":"not found"},404)
                    fields=["product_name","product_type","owner_id","launch_date","regular_price","promo_price","avg_selling_price","margin_rate","season_label","status","notes","landing_exposed"]
                    vals={k:x[k] for k in fields if k in x}
                    if vals:
                        setq=",".join(f"{k}=?" for k in vals)+",updated_at=datetime('now')"
                        c.execute(f"UPDATE planning_products SET {setq} WHERE id=?",list(vals.values())+[pid])
                    c.commit();return self.send_json({"ok":True})

                # 팀 워크스페이스 모듈 공통 수정(PATCH)
                gtable,gid=_crud_target(p)
                if gtable:
                    old=c.execute(f"SELECT * FROM {gtable} WHERE id=?",(gid,)).fetchone()
                    if not old:return self.send_json({"error":"not found"},404)
                    cols={r["name"] for r in c.execute(f"PRAGMA table_info({gtable})")}
                    vals={k:v for k,v in x.items() if k in cols and k not in ("id","created_at","team_id")}
                    if vals:
                        setq=",".join(f"{k}=?" for k in vals)
                        c.execute(f"UPDATE {gtable} SET {setq} WHERE id=?",list(vals.values())+[gid])
                    c.commit();return self.send_json({"ok":True})

            return self.send_json({"error":"not found"},404)
        except Exception as e:
            traceback.print_exc();return self.send_json({"error":str(e)},500)

    def do_DELETE(self):
        try:
            p=urlparse(self.path).path
            x=self.body()
            with db() as c:
                if p.startswith("/api/weekly/") and p.rsplit("/",1)[1].isdigit():
                    wid=int(p.rsplit("/",1)[1])
                    item=c.execute("SELECT * FROM weekly_reports WHERE id=?",(wid,)).fetchone()
                    if not item: return self.send_json({"error":"not found"},404)
                    actor=c.execute("SELECT * FROM users WHERE id=?",(x.get("actor_id"),)).fetchone()
                    is_admin=actor and (actor["role"] in ("SUPER_ADMIN","DIVISION_ADMIN") or actor["is_team_leader"] or actor["name"] in ("정해근","이원행"))
                    if not (actor and (actor["id"]==item["user_id"] or is_admin)):
                        return self.send_json({"error":"본인 또는 팀장/관리자만 삭제할 수 있습니다."},403)
                    c.execute("UPDATE weekly_reports SET carried_over=0 WHERE carried_from=?",(wid,))
                    c.execute("DELETE FROM weekly_reports WHERE id=?",(wid,))
                    c.commit();return self.send_json({"ok":True})

                if p.startswith("/api/data-center/") and p.count("/")==3 and p.rsplit("/",1)[1].isdigit():
                    fid=int(p.rsplit("/",1)[1]);old=c.execute("SELECT * FROM data_center_files WHERE id=?",(fid,)).fetchone()
                    if not old:return self.send_json({"error":"not found"},404)
                    if old["stored_name"]:
                        f=UPLOADS/"data_center"/str(old["team_id"])/old["stored_name"]
                        if f.exists():
                            try:f.unlink()
                            except:pass
                    c.execute("DELETE FROM data_center_logs WHERE file_id=?",(fid,))
                    c.execute("UPDATE data_center_files SET duplicate_of=NULL WHERE duplicate_of=?",(fid,))
                    c.execute("DELETE FROM data_center_files WHERE id=?",(fid,))
                    c.commit();return self.send_json({"ok":True})

                if p.startswith("/api/calendar/") and p.count("/")==3:
                    eid=int(p.rsplit("/",1)[1])
                    old=c.execute("SELECT * FROM calendar_events WHERE id=?",(eid,)).fetchone()
                    if not old:return self.send_json({"error":"not found"},404)
                    actor=x.get("actor_id")
                    if actor!=old["owner_id"]:
                        return self.send_json({"error":"작성자만 삭제할 수 있습니다."},403)
                    c.execute("DELETE FROM calendar_events WHERE id=?",(eid,))
                    c.commit();return self.send_json({"ok":True})

                if p.startswith("/api/notices/") and p.count("/")==3 and p.rsplit("/",1)[1].isdigit():
                    nid=int(p.rsplit("/",1)[1])
                    actor=c.execute("SELECT * FROM users WHERE id=?",(x.get("actor_id"),)).fetchone()
                    if not actor or not (actor["role"] in ("SUPER_ADMIN","DIVISION_ADMIN") or actor["name"] in ("정해근","이원행")):
                        return self.send_json({"error":"관리자만 공지를 삭제할 수 있습니다."},403)
                    c.execute("DELETE FROM company_notices WHERE id=?",(nid,))
                    c.commit();return self.send_json({"ok":True})

                if p.startswith("/api/planning/products/") and p.count("/")==4 and p.rsplit("/",1)[1].isdigit():
                    pid=int(p.rsplit("/",1)[1])
                    if not c.execute("SELECT 1 FROM planning_products WHERE id=?",(pid,)).fetchone():
                        return self.send_json({"error":"not found"},404)
                    c.execute("DELETE FROM planning_products WHERE id=?",(pid,))
                    c.commit();return self.send_json({"ok":True})

                if p.startswith("/api/tasks/") and p.count("/")==3 and p.rsplit("/",1)[1].isdigit():
                    tid=int(p.rsplit("/",1)[1])
                    t=c.execute("SELECT * FROM tasks WHERE id=?",(tid,)).fetchone()
                    if not t:return self.send_json({"error":"not found"},404)
                    actor=int(x.get("actor_id") or 0)
                    admin=c.execute("SELECT 1 FROM users WHERE id=? AND role IN ('SUPER_ADMIN','DIVISION_ADMIN')",(actor,)).fetchone()
                    if actor not in (t["assignee_id"],t["created_by"]) and not admin:
                        return self.send_json({"error":"담당자·작성자 또는 관리자만 삭제할 수 있습니다."},403)
                    for rr in c.execute("SELECT id FROM task_review_requests WHERE task_id=?",(tid,)):
                        c.execute("DELETE FROM task_review_files WHERE request_id=?",(rr["id"],))
                    c.execute("DELETE FROM task_review_requests WHERE task_id=?",(tid,))
                    c.execute("DELETE FROM task_approvals WHERE task_id=?",(tid,))
                    c.execute("DELETE FROM task_comments WHERE task_id=?",(tid,))
                    c.execute("DELETE FROM activity_logs WHERE task_id=?",(tid,))
                    c.execute("DELETE FROM tasks WHERE id=?",(tid,))
                    c.commit();return self.send_json({"ok":True})

                if p.startswith("/api/projects/") and p.count("/")==3 and p.rsplit("/",1)[1].isdigit():
                    pid=int(p.rsplit("/",1)[1])
                    pr=c.execute("SELECT * FROM projects WHERE id=?",(pid,)).fetchone()
                    if not pr:return self.send_json({"error":"not found"},404)
                    actor=int(x.get("actor_id") or 0)
                    admin=c.execute("SELECT 1 FROM users WHERE id=? AND role IN ('SUPER_ADMIN','DIVISION_ADMIN')",(actor,)).fetchone()
                    if actor not in (pr["pm_user_id"],pr["created_by"]) and not admin:
                        return self.send_json({"error":"PM·작성자 또는 관리자만 삭제할 수 있습니다."},403)
                    # 프로젝트 소속 세부 업무와 그 연관 데이터까지 완전 삭제(히스토리 X)
                    for t in c.execute("SELECT id FROM tasks WHERE project_id=?",(pid,)):
                        tid=t["id"]
                        for rr in c.execute("SELECT id FROM task_review_requests WHERE task_id=?",(tid,)):
                            c.execute("DELETE FROM task_review_files WHERE request_id=?",(rr["id"],))
                        c.execute("DELETE FROM task_review_requests WHERE task_id=?",(tid,))
                        c.execute("DELETE FROM task_approvals WHERE task_id=?",(tid,))
                        c.execute("DELETE FROM task_comments WHERE task_id=?",(tid,))
                        c.execute("DELETE FROM activity_logs WHERE task_id=?",(tid,))
                    c.execute("DELETE FROM tasks WHERE project_id=?",(pid,))
                    c.execute("DELETE FROM project_approvals WHERE project_id=?",(pid,))
                    c.execute("DELETE FROM project_members WHERE project_id=?",(pid,))
                    c.execute("DELETE FROM project_teams WHERE project_id=?",(pid,))
                    c.execute("DELETE FROM projects WHERE id=?",(pid,))
                    c.commit();return self.send_json({"ok":True})

                # 팀 워크스페이스 모듈 공통 삭제(DELETE)
                gtable,gid=_crud_target(p)
                if gtable:
                    if not c.execute(f"SELECT 1 FROM {gtable} WHERE id=?",(gid,)).fetchone():
                        return self.send_json({"error":"not found"},404)
                    c.execute(f"DELETE FROM {gtable} WHERE id=?",(gid,))
                    c.commit();return self.send_json({"ok":True})
            return self.send_json({"error":"not found"},404)
        except Exception as e:
            traceback.print_exc();return self.send_json({"error":str(e)},500)

def seed_new_datasets():
    """콘.제작 강의촬영 / 마케팅 영상 제작 현황 / 채널 분석(주간) 최초 1회 시드.
    번들된 data/seed_*.json 을 읽어 신규 테이블을 채운다. 기존 운영 데이터는 건드리지 않는다.
    각 데이터셋별 app_meta 플래그로 1회만 실행. (구글시트 API 연동 전 '개발 검토용' 데이터)"""
    def load(name):
        # data/ 가 클라우드 영구디스크로 마운트되면 저장소의 seed 파일이 가려질 수 있어
        # 여러 위치(BASE, BASE/data, BASE/seeds)를 순서대로 탐색한다.
        for f in (BASE/name, BASE/"data"/name, BASE/"seeds"/name):
            if f.exists():
                try: return json.loads(f.read_text(encoding="utf-8"))
                except Exception as e:
                    print("[SEED] load fail", f, e); return None
        print("[SEED] not found:", name); return None
    with db() as c:
        c.execute("CREATE TABLE IF NOT EXISTS app_meta(key TEXT PRIMARY KEY, value TEXT)")
        def done(flag): return bool(c.execute("SELECT 1 FROM app_meta WHERE key=?",(flag,)).fetchone())
        def mark(flag): c.execute("INSERT OR REPLACE INTO app_meta(key,value) VALUES(?, '1')",(flag,))

        # 1) 강의촬영(콘.제작)
        data=load("seed_filming.json")
        if data and not done("seed_filming_v1") and c.execute("SELECT COUNT(*) FROM filming_courses").fetchone()[0]==0:
            for co in data:
                cur=c.execute("""INSERT INTO filming_courses(field,name,book,professor,course_code,ended,hidden,note,source,sync_status)
                    VALUES(?,?,?,?,?,?,?,?,?,?)""",
                    (co.get("field"),co.get("name"),co.get("book"),co.get("professor"),co.get("course_code"),
                     int(co.get("ended",0) or 0),0,co.get("note"),co.get("source","sample"),"sample"))
                cid=cur.lastrowid
                for cl in co.get("clips",[]):
                    c.execute("""INSERT INTO filming_clips(course_id,clip_no,subject,shoot_date,room,video_len,attachment,coding_date,settle_status)
                        VALUES(?,?,?,?,?,?,?,?,?)""",
                        (cid,cl.get("clip_no"),cl.get("subject"),cl.get("shoot_date"),cl.get("room"),
                         cl.get("video_len"),cl.get("attachment"),cl.get("coding_date"),cl.get("settle_status","미정산")))
            mark("seed_filming_v1")
            print(f"[SEED] filming courses={len(data)}")

        # 2) 마케팅 영상 제작 현황
        data=load("seed_mkt_video.json")
        if data and not done("seed_mkt_video_v1") and c.execute("SELECT COUNT(*) FROM mkt_video_log").fetchone()[0]==0:
            c.executemany("""INSERT INTO mkt_video_log(month,ym,cat_major,cat_minor,content_type,topic,shoot_date,edit_done,pm,instructor,editor,progress,url,note,source)
                VALUES(:month,:ym,:cat_major,:cat_minor,:content_type,:topic,:shoot_date,:edit_done,:pm,:instructor,:editor,:progress,:url,:note,'sheet')""",
                [{**{k:r.get(k,'') for k in ('month','ym','cat_major','cat_minor','content_type','topic','shoot_date','edit_done','pm','instructor','editor','progress','url','note')}} for r in data])
            mark("seed_mkt_video_v1")
            print(f"[SEED] mkt_video_log rows={len(data)}")

        # 3) 마케팅 채널 분석(주간)
        data=load("seed_mkt_weekly.json")
        if data and not done("seed_mkt_weekly_v1") and c.execute("SELECT COUNT(*) FROM mkt_weekly_metrics").fetchone()[0]==0:
            c.executemany("""INSERT INTO mkt_weekly_metrics(week,category,channel,metric,value,source)
                VALUES(:week,:category,:channel,:metric,:value,'sheet')""",
                [{k:r.get(k) for k in ('week','category','channel','metric','value')} for r in data])
            mark("seed_mkt_weekly_v1")
            print(f"[SEED] mkt_weekly_metrics rows={len(data)}")
        c.commit()

def main():
    print("="*56)
    print("모두 (MO DO) v3.0 - Standard Python Edition")
    print("="*56)
    print(f"Python: {sys.version.split()[0]}")
    print(f"DB: {DB}")
    if not DB.exists():
        print("DB not found. Creating...")
        subprocess.check_call([sys.executable, str(BASE/"init_db.py")], cwd=BASE)
    ensure_schema()
    ensure_user_columns()
    ensure_calendar_columns()
    ensure_planning_columns()
    with db() as c:
        c.execute("UPDATE users SET role='SUPER_ADMIN' WHERE name IN ('정해근','이원행')")
        c.execute("UPDATE users SET account_status=CASE WHEN COALESCE(active,1)=1 THEN 'ACTIVE' ELSE 'RETIRED' END WHERE account_status IS NULL OR account_status=''")
        c.commit()
    ensure_project_columns()
    ensure_task_columns()
    ensure_indexes()
    seed_new_datasets()
    with db() as c:
        c.execute("UPDATE tasks SET work_type='PROJECT' WHERE work_type IS NULL OR work_type=''")
        c.execute("UPDATE projects SET project_type='APPROVAL_PROJECT' WHERE project_type IS NULL OR project_type=''")
        c.execute("UPDATE projects SET approval_status='APPROVED' WHERE approval_status IS NULL OR approval_status=''")
        # 데모/예시 데이터는 '최초 1회'만 생성. 운영 초기화(reset_db.py operational) 후에는
        # seed_done 플래그가 남아 있어 다시 채워지지 않는다. (완전 초기화 시 DB가 삭제되어 플래그도 사라짐)
        c.execute("CREATE TABLE IF NOT EXISTS app_meta(key TEXT PRIMARY KEY, value TEXT)")
        seeded=bool(c.execute("SELECT 1 FROM app_meta WHERE key='seed_done'").fetchone())
        # 공지사항 초기 예시 (최초 1회 생성)
        if not seeded and c.execute("SELECT COUNT(*) FROM company_notices").fetchone()[0]==0:
            admin=c.execute("SELECT id FROM users WHERE name='이원행'").fetchone()
            aid=admin["id"] if admin else None
            c.executemany("INSERT INTO company_notices(title,body,category,author_id,pinned) VALUES(?,?,?,?,?)",[
                ("완료 업무 결과물 첨부 기준 안내","검토·컨펌형 업무는 결과물 파일을 반드시 첨부한 뒤 검토 요청해 주세요. 첨부가 없으면 승인이 반려될 수 있습니다.","기준",aid,1),
                ("파일 업로드 테스트 자료 사용 안내","데이터센터 업로드 기능 점검 중에는 실제 기밀자료 대신 테스트 자료를 사용해 주세요.","안내",aid,0),
                ("직원 피드백 수렴 안내","협업비서 개선을 위한 의견은 원격기획팀으로 전달해 주시면 반영 검토하겠습니다.","안내",aid,0),
            ])
        # MCC 교재·법령 검수 예시 (비어 있을 때 1회 생성)
        if not seeded and c.execute("SELECT COUNT(*) FROM mcc_reviews").fetchone()[0]==0:
            def uid(name):
                r=c.execute("SELECT id FROM users WHERE name=?",(name,)).fetchone();return r["id"] if r else None
            reviews=[
                ("교재","2026 가스 KGS 핵심정리집","KGS AA917 (2026.06.12 개정)","2026 개정판",uid("김영연"),"검수중","HIGH",120,14,86,72,"2026-07-15","2026-08-12","계산형 문항 비중 증가 반영 필요"),
                ("법령","소방시설관리사 필기 교재","NFTC 2026 개정 고시","2026 상반기판",uid("천은지"),"검수중","URGENT",90,9,58,64,"2026-07-20","2026-08-10","소화설비 기준 개정 대조"),
                ("KGS CODE","가스산업기사 실기 교안","KGS FU551 (2026.05)","2026 촬영본",uid("남유진"),"검토대기","NORMAL",60,3,52,88,"2026-07-05","2026-07-30","용어 표기 통일 확인"),
            ]
            for rv in reviews:
                cur=c.execute("""INSERT INTO mcc_reviews(review_type,target_name,compare_standard,source_version,owner_id,team_id,status,priority,total_items,changed_items,matched_items,progress,started_at,due_date,notes)
                    VALUES(?,?,?,?,?,1,?,?,?,?,?,?,?,?,?)""",rv)
                rid=cur.lastrowid
                # (번호, 구분, 오류내용(교재원문), 최신기준·시행일, 판정, 근거, 위치)
                items=[
                    (1,"수치","안전밸브 작동압력 1.5배","KGS 기준 1.6배 이하 (2026.06.12 시행)","승인","개정 수치와 불일치 — 수정 확정","p.152 표3"),
                    (2,"용어","고압가스","고압가스(임계온도 기준 명시)","승인","정의 문구 보강 확정","p.33 2문단"),
                    (3,"법령","시행규칙 제12조","제12조의2 신설 조항","보류","신설 조항 적용 범위 담당 협의 필요","p.88 각주"),
                    (4,"표·수식","유량 계산식 Q=1.5D²","원문 표 확인 필요","보류","표·수식 원문 대조 필요","p.121 식(4)"),
                ]
                for it in items:
                    c.execute("""INSERT INTO mcc_review_items(review_id,item_no,category,source_text,reference_text,result_type,change_detail,decision,reviewer_id,status,page_ref)
                        VALUES(?,?,?,?,?,?,?,?,?, '검수완료',?)""",(rid,it[0],it[1],it[2],it[3],it[4],it[5],it[4],rv[4],it[6]))
        # 캘린더 카테고리(개인/전사) 색상 시연용 예시 (1회 생성)
        if not seeded and c.execute("SELECT COUNT(*) FROM calendar_events WHERE category='PERSONAL'").fetchone()[0]==0:
            u=lambda n:(c.execute("SELECT id FROM users WHERE name=?",(n,)).fetchone() or {"id":None})["id"]
            c.executemany("INSERT INTO calendar_events(title,start_date,end_date,event_type,team_id,owner_id,description,category) VALUES(?,?,?,?,?,?,?,?)",[
                ("연차 휴가","2026-07-17","2026-07-17","GENERAL",None,u("유홍곤"),"개인 일정","PERSONAL"),
                ("전사 월례회의","2026-07-31","2026-07-31","MEETING",None,u("이원행"),"전사 공용 일정","DEPARTMENT"),
                ("상반기 성과 공유회","2026-07-10","2026-07-10","MEETING",None,u("이원행"),"전사 공용 일정","DEPARTMENT"),
            ])
        # 상품 구성 변경(SB 슬라이드2) 반영 — 기획 상품 데이터. 가격은 미정(추후 확정).
        if not seeded and c.execute("SELECT COUNT(*) FROM planning_products WHERE season_label='상품구성변경'").fetchone()[0]==0:
            owner=(c.execute("SELECT id FROM users WHERE name='윤영환'").fetchone() or {"id":None})["id"]
            c.executemany("INSERT INTO planning_products(product_name,product_type,owner_id,status,season_label,notes,landing_exposed) VALUES(?,?,?,?, '상품구성변경',?,?)",[
                ("평생 상품(단독 운영)","평생형",owner,"판매중","평생 상품만 운영으로 정리",1),
                ("정규 첨삭반","첨삭반",owner,"기획","기본/심화 첨삭반 → 정규 첨삭반으로 네이밍 통합. 가격 수정 예정",1),
                ("전병호 기술사 강의(운영 제외)","단과",owner,"종료","평생 상품·랜딩에서 제외(금액 인하 반영)",0),
            ])
        # 검토·컨펌형 예시 업무(결재선 포함) — REVIEW 업무가 하나도 없을 때 1회 생성
        if not seeded and c.execute("SELECT COUNT(*) FROM tasks WHERE completion_mode='REVIEW'").fetchone()[0]==0:
            leader=c.execute("SELECT * FROM users WHERE is_team_leader=1 AND COALESCE(active,1)=1 LIMIT 1").fetchone()
            execu=c.execute("SELECT * FROM users WHERE name='이원행' LIMIT 1").fetchone()
            if leader and execu:
                staff=c.execute("SELECT * FROM users WHERE COALESCE(active,1)=1 AND (is_team_leader IS NULL OR is_team_leader=0) AND team_id=? AND id NOT IN (?,?) LIMIT 1",(leader["team_id"],leader["id"],execu["id"])).fetchone()
                staff=staff or c.execute("SELECT * FROM users WHERE COALESCE(active,1)=1 AND id NOT IN (?,?) LIMIT 1",(leader["id"],execu["id"])).fetchone()
                tteam=leader["team_id"]
                def mk_review(title,desc,priority,cur_stage):
                    # cur_stage: 1=팀장 차례, 2=이사 차례(팀장 승인 완료)
                    rev=leader["id"] if cur_stage==1 else execu["id"]
                    cur=c.execute("""INSERT INTO tasks(project_id,title,team_id,assignee_id,priority,status,due_date,description,priority_order,status_order,category,visibility_scope,completion_mode,reviewer_id,created_by,work_type,created_at)
                        VALUES(NULL,?,?,?,?,'REVIEW',?,?,1,?,?,'TEAM','REVIEW',?,?,'GENERAL',datetime('now'))""",
                        (title,tteam,staff["id"],priority,'2026-08-20',desc,status_order("REVIEW"),"업무",rev,staff["id"]))
                    tid=cur.lastrowid
                    c.execute("INSERT INTO task_approvals(task_id,approver_id,approval_order,status) VALUES(?,?,1,?)",(tid,leader["id"],'APPROVED' if cur_stage==2 else 'WAITING'))
                    if cur_stage==2:
                        c.execute("UPDATE task_approvals SET decided_at=datetime('now'),comment='1단계 승인' WHERE task_id=? AND approver_id=?",(tid,leader["id"]))
                    c.execute("INSERT INTO task_approvals(task_id,approver_id,approval_order,status) VALUES(?,?,2,'WAITING')",(tid,execu["id"]))
                    rc=c.execute("INSERT INTO task_review_requests(task_id,requester_id,comment,status) VALUES(?,?,?,'WAITING')",(tid,staff["id"],"검토 부탁드립니다. 결과물 확인 후 승인 요청드립니다."))
                    add_log(c,tid,staff["id"],"REVIEW_REQUEST","","검토 요청")
                    if cur_stage==2:add_log(c,tid,leader["id"],"APPROVE","1","1단계 승인")
                    return tid
                mk_review("[예시] 소방 신규과정 상세페이지 검수","상세페이지 문구·가격 표기 검수 후 승인 요청","HIGH",1)
                mk_review("[예시] 하반기 국비 홍보안 최종 컨펌","홍보 카피·배너 시안 최종 컨펌 요청(팀장 승인 완료, 이사 컨펌 대기)","URGENT",2)
        if not seeded:
            c.execute("INSERT OR REPLACE INTO app_meta(key,value) VALUES('seed_done','1')")
        c.commit()
    httpd=ThreadingHTTPServer((HOST,PORT),App)
    ip=lan_ip()
    local_url=f"http://127.0.0.1:{PORT}"
    print("-"*56)
    print(f"이 PC(서버)에서:   {local_url}")
    print(f"같은 네트워크의 다른 PC에서:  http://{ip}:{PORT}")
    print("→ 여러 명이 함께 쓰려면 '한 대'에서만 이 프로그램을 실행하고,")
    print("  나머지 인원은 브라우저에서 위 주소로 접속하세요.")
    print(f"DB 저장 위치: {DB}")
    print("-"*56)
    if not IS_SERVER:
        threading.Timer(1.0, lambda: webbrowser.open(local_url,new=2)).start()
        print("브라우저가 자동으로 열립니다. 창을 닫거나 Ctrl+C 로 종료합니다.")
    else:
        print(f"[SERVER] 클라우드 모드로 기동합니다. PORT={PORT}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping server...")
    finally:
        httpd.server_close()

if __name__=="__main__":
    try:
        main()
    except Exception:
        traceback.print_exc()
        input("\nAn error occurred. Press Enter to close...")
