
import sqlite3
from pathlib import Path
BASE=Path(__file__).resolve().parent
DB=BASE/"data"/"moa_work.db"
if DB.exists(): DB.unlink()
c=sqlite3.connect(DB)
c.execute("PRAGMA foreign_keys=ON")
c.executescript("""
CREATE TABLE divisions(id INTEGER PRIMARY KEY,name TEXT);
CREATE TABLE teams(id INTEGER PRIMARY KEY,division_id INTEGER,name TEXT,active INTEGER DEFAULT 1,sort_order INTEGER,FOREIGN KEY(division_id) REFERENCES divisions(id));
CREATE TABLE users(id INTEGER PRIMARY KEY,team_id INTEGER,name TEXT,rank TEXT,job_title TEXT,extension TEXT,email TEXT,role TEXT DEFAULT 'MEMBER',is_team_leader INTEGER DEFAULT 0,active INTEGER DEFAULT 1,FOREIGN KEY(team_id) REFERENCES teams(id));
CREATE TABLE projects(id INTEGER PRIMARY KEY,name TEXT,description TEXT,pm_user_id INTEGER,due_date TEXT,status TEXT,FOREIGN KEY(pm_user_id) REFERENCES users(id));
CREATE TABLE tasks(id INTEGER PRIMARY KEY,project_id INTEGER,title TEXT,description TEXT,team_id INTEGER,assignee_id INTEGER,priority TEXT,status TEXT,due_date TEXT,priority_order INTEGER DEFAULT 0,status_order INTEGER DEFAULT 0,blocked_reason TEXT,blocked_requested_to INTEGER,blocked_since TEXT,FOREIGN KEY(project_id) REFERENCES projects(id),FOREIGN KEY(team_id) REFERENCES teams(id),FOREIGN KEY(assignee_id) REFERENCES users(id));
CREATE TABLE requests(id INTEGER PRIMARY KEY,requester_id INTEGER,target_user_id INTEGER,target_team_id INTEGER,type TEXT,title TEXT,content TEXT,status TEXT,priority TEXT,due_date TEXT,linked_task_id INTEGER,FOREIGN KEY(requester_id) REFERENCES users(id),FOREIGN KEY(target_user_id) REFERENCES users(id),FOREIGN KEY(target_team_id) REFERENCES teams(id));
CREATE TABLE activity_logs(id INTEGER PRIMARY KEY,task_id INTEGER,actor_id INTEGER,action TEXT,old_value TEXT,new_value TEXT,created_at TEXT DEFAULT CURRENT_TIMESTAMP,FOREIGN KEY(task_id) REFERENCES tasks(id),FOREIGN KEY(actor_id) REFERENCES users(id));
CREATE TABLE calendar_events(id INTEGER PRIMARY KEY,title TEXT,start_date TEXT,end_date TEXT,event_type TEXT,team_id INTEGER,owner_id INTEGER,description TEXT);
CREATE TABLE modules(id INTEGER PRIMARY KEY,name TEXT,code TEXT,sort_order INTEGER);
CREATE TABLE team_modules(team_id INTEGER,module_id INTEGER,enabled INTEGER DEFAULT 1,PRIMARY KEY(team_id,module_id));
CREATE TABLE data_boards(id INTEGER PRIMARY KEY,team_id INTEGER,name TEXT,description TEXT,board_type TEXT);
CREATE TABLE data_board_columns(id INTEGER PRIMARY KEY,board_id INTEGER,column_key TEXT,label TEXT,data_type TEXT,sort_order INTEGER);
CREATE TABLE data_board_rows(id INTEGER PRIMARY KEY,board_id INTEGER,title TEXT,created_at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE data_board_values(id INTEGER PRIMARY KEY,row_id INTEGER,column_key TEXT,value TEXT);
CREATE TABLE meetings(id INTEGER PRIMARY KEY,team_id INTEGER,title TEXT,meeting_date TEXT,attendees TEXT,decisions TEXT,action_items TEXT);
CREATE TABLE checklists(id INTEGER PRIMARY KEY,team_id INTEGER,title TEXT,recurrence TEXT,items TEXT);
CREATE TABLE resources(id INTEGER PRIMARY KEY,team_id INTEGER,title TEXT,resource_type TEXT,url TEXT,description TEXT);
CREATE TABLE task_comments(id INTEGER PRIMARY KEY,task_id INTEGER NOT NULL,user_id INTEGER,body TEXT NOT NULL,created_at TEXT DEFAULT CURRENT_TIMESTAMP,FOREIGN KEY(task_id) REFERENCES tasks(id),FOREIGN KEY(user_id) REFERENCES users(id));
CREATE TABLE notifications(id INTEGER PRIMARY KEY,user_id INTEGER NOT NULL,title TEXT NOT NULL,body TEXT,is_read INTEGER DEFAULT 0,created_at TEXT DEFAULT CURRENT_TIMESTAMP,FOREIGN KEY(user_id) REFERENCES users(id));
CREATE TABLE directives(id INTEGER PRIMARY KEY,sender_id INTEGER NOT NULL,recipient_id INTEGER NOT NULL,title TEXT NOT NULL,body TEXT NOT NULL,priority TEXT DEFAULT 'NORMAL',due_date TEXT,status TEXT DEFAULT 'SENT',created_at TEXT DEFAULT CURRENT_TIMESTAMP,FOREIGN KEY(sender_id) REFERENCES users(id),FOREIGN KEY(recipient_id) REFERENCES users(id));
CREATE TABLE directive_replies(id INTEGER PRIMARY KEY,directive_id INTEGER NOT NULL,sender_id INTEGER NOT NULL,body TEXT NOT NULL,created_at TEXT DEFAULT CURRENT_TIMESTAMP,FOREIGN KEY(directive_id) REFERENCES directives(id),FOREIGN KEY(sender_id) REFERENCES users(id));
CREATE TABLE request_replies(id INTEGER PRIMARY KEY,request_id INTEGER NOT NULL,sender_id INTEGER NOT NULL,body TEXT NOT NULL,created_at TEXT DEFAULT CURRENT_TIMESTAMP,FOREIGN KEY(request_id) REFERENCES requests(id),FOREIGN KEY(sender_id) REFERENCES users(id));
CREATE TABLE purchases(id INTEGER PRIMARY KEY,team_id INTEGER,purchaser_id INTEGER,item_type TEXT,item_name TEXT NOT NULL,vendor TEXT,amount INTEGER DEFAULT 0,purchase_date TEXT,payment_method TEXT,account_id TEXT,password_hint TEXT,note TEXT,created_at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE refunds(id INTEGER PRIMARY KEY,team_id INTEGER NOT NULL DEFAULT 1,instructor_id INTEGER,course_name TEXT NOT NULL,refund_reason TEXT,refund_amount INTEGER DEFAULT 0,incentive_adjustment INTEGER DEFAULT 0,improvement_action TEXT,status TEXT DEFAULT 'OPEN',refund_date TEXT,created_at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE book_discussions(id INTEGER PRIMARY KEY,team_id INTEGER,month_label TEXT,book_title TEXT NOT NULL,selected_reason TEXT,category TEXT,status TEXT DEFAULT 'PLANNED',completed_date TEXT,rating INTEGER,reflection TEXT,created_at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE customers(id INTEGER PRIMARY KEY,name TEXT NOT NULL,phone TEXT,email TEXT,company TEXT,member_id TEXT,customer_type TEXT DEFAULT '일반',onboarding_status TEXT DEFAULT '신규',interests TEXT,notes TEXT,last_contact_at TEXT,created_at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE consultation_logs(id INTEGER PRIMARY KEY,customer_id INTEGER,counselor_id INTEGER,product_name TEXT,inquiry_category TEXT,inquiry_text TEXT NOT NULL,response_text TEXT,channel TEXT DEFAULT '전화',status TEXT DEFAULT '완료',followup_date TEXT,share_to_content INTEGER DEFAULT 0,share_to_planning INTEGER DEFAULT 0,created_at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE service_knowledge(id INTEGER PRIMARY KEY,product_name TEXT,category TEXT,question TEXT NOT NULL,answer TEXT NOT NULL,source_type TEXT DEFAULT '상담',usage_count INTEGER DEFAULT 0,updated_by INTEGER,updated_at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE product_studies(id INTEGER PRIMARY KEY,product_name TEXT NOT NULL,title TEXT NOT NULL,content TEXT NOT NULL,keywords TEXT,owner_id INTEGER,updated_at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE marketing_channels(id INTEGER PRIMARY KEY,channel_type TEXT NOT NULL,content_name TEXT NOT NULL,content_type TEXT,owner_id INTEGER,status TEXT DEFAULT '기획',planned_date TEXT,publish_date TEXT,url TEXT,notes TEXT,created_at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE marketing_channel_metrics(id INTEGER PRIMARY KEY,channel_id INTEGER NOT NULL,metric_date TEXT,views INTEGER DEFAULT 0,watch_hours REAL DEFAULT 0,subscribers_delta INTEGER DEFAULT 0,visitors INTEGER DEFAULT 0,posts_count INTEGER DEFAULT 0,friends_delta INTEGER DEFAULT 0,clicks INTEGER DEFAULT 0,impressions INTEGER DEFAULT 0,conversions INTEGER DEFAULT 0,created_at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE marketing_campaigns(id INTEGER PRIMARY KEY,platform TEXT NOT NULL,campaign_name TEXT NOT NULL,product_name TEXT,owner_id INTEGER,start_date TEXT,end_date TEXT,budget INTEGER DEFAULT 0,spend INTEGER DEFAULT 0,impressions INTEGER DEFAULT 0,clicks INTEGER DEFAULT 0,conversions INTEGER DEFAULT 0,leads INTEGER DEFAULT 0,status TEXT DEFAULT '준비',landing_url TEXT,notes TEXT,created_at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE marketing_leads(id INTEGER PRIMARY KEY,lead_date TEXT,source_channel TEXT,campaign_id INTEGER,product_name TEXT,lead_type TEXT,customer_name TEXT,contact TEXT,status TEXT DEFAULT '신규',owner_id INTEGER,notes TEXT,created_at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE marketing_events(id INTEGER PRIMARY KEY,event_name TEXT NOT NULL,event_type TEXT,owner_id INTEGER,event_date TEXT,registration_count INTEGER DEFAULT 0,marketing_inflow_count INTEGER DEFAULT 0,promo_material_status TEXT DEFAULT '미착수',landing_status TEXT DEFAULT '미착수',message_status TEXT DEFAULT '미착수',notes TEXT,created_at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE marketing_collab_requests(id INTEGER PRIMARY KEY,requester_team_id INTEGER,requester_id INTEGER,title TEXT NOT NULL,request_detail TEXT,owner_id INTEGER,due_date TEXT,status TEXT DEFAULT '대기',feedback_score INTEGER DEFAULT 0,result_notes TEXT,created_at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE marketing_performance_snapshots(id INTEGER PRIMARY KEY,snapshot_month TEXT NOT NULL,data_type TEXT NOT NULL,reference_name TEXT NOT NULL,value_numeric REAL DEFAULT 0,value_text TEXT,owner_id INTEGER,notes TEXT,created_at TEXT DEFAULT CURRENT_TIMESTAMP);
""")
c.executescript("""
CREATE TABLE planning_live_events(id INTEGER PRIMARY KEY,event_name TEXT NOT NULL,event_type TEXT DEFAULT '라이브특강',product_name TEXT,owner_id INTEGER,event_date TEXT,registration_count INTEGER DEFAULT 0,confirmed_count INTEGER DEFAULT 0,paid_conversion_count INTEGER DEFAULT 0,satisfaction_score REAL DEFAULT 0,review_score REAL DEFAULT 0,status TEXT DEFAULT '기획',notes TEXT,created_at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE planning_promotions(id INTEGER PRIMARY KEY,promotion_name TEXT NOT NULL,season TEXT,target_product TEXT,owner_id INTEGER,start_date TEXT,end_date TEXT,visitors INTEGER DEFAULT 0,cart_count INTEGER DEFAULT 0,purchase_count INTEGER DEFAULT 0,sales_amount INTEGER DEFAULT 0,status TEXT DEFAULT '기획',notes TEXT,created_at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE planning_products(id INTEGER PRIMARY KEY,product_name TEXT NOT NULL,product_type TEXT,owner_id INTEGER,launch_date TEXT,regular_price INTEGER DEFAULT 0,promo_price INTEGER DEFAULT 0,avg_selling_price INTEGER DEFAULT 0,margin_rate REAL DEFAULT 0,season_label TEXT,status TEXT DEFAULT '기획',notes TEXT,created_at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE planning_b2b(id INTEGER PRIMARY KEY,partner_name TEXT NOT NULL,product_name TEXT,owner_id INTEGER,contract_date TEXT,supply_price INTEGER DEFAULT 0,sales_amount INTEGER DEFAULT 0,settlement_month TEXT,settlement_amount INTEGER DEFAULT 0,settlement_status TEXT DEFAULT '대기',sync_status TEXT DEFAULT '정상',issue_notes TEXT,notes TEXT,created_at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE planning_site_ops(id INTEGER PRIMARY KEY,work_type TEXT NOT NULL,title TEXT NOT NULL,owner_id INTEGER,request_date TEXT,due_date TEXT,completed_date TEXT,status TEXT DEFAULT '대기',page_url TEXT,issue_count INTEGER DEFAULT 0,complaint_count INTEGER DEFAULT 0,change_summary TEXT,notes TEXT,created_at TEXT DEFAULT CURRENT_TIMESTAMP);

CREATE TABLE mcc_reviews(id INTEGER PRIMARY KEY,review_type TEXT NOT NULL DEFAULT '교재',target_name TEXT NOT NULL,compare_standard TEXT,source_version TEXT,owner_id INTEGER,team_id INTEGER DEFAULT 1,status TEXT DEFAULT '검수중',priority TEXT DEFAULT 'NORMAL',total_items INTEGER DEFAULT 0,changed_items INTEGER DEFAULT 0,matched_items INTEGER DEFAULT 0,progress INTEGER DEFAULT 0,started_at TEXT,due_date TEXT,completed_at TEXT,notes TEXT,created_at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE mcc_review_items(id INTEGER PRIMARY KEY,review_id INTEGER NOT NULL,item_no INTEGER,category TEXT,source_text TEXT,reference_text TEXT,result_type TEXT DEFAULT '일치',change_detail TEXT,decision TEXT,reviewer_id INTEGER,status TEXT DEFAULT '대기',page_ref TEXT,created_at TEXT DEFAULT CURRENT_TIMESTAMP);
INSERT INTO divisions VALUES(1,'콘텐츠사업부')""")
teams=[(1,1,'MCC',1,1),(2,1,'마케팅',1,2),(3,1,'콘텐츠기획',1,3),(4,1,'원격기획',1,4),(5,1,'시니어기획',0,5),(6,1,'서비스운영',1,6)]
c.executemany("INSERT INTO teams VALUES(?,?,?,?,?)",teams)

users=[
(1,None,'이원행','이사','콘텐츠사업부 총괄','7120','real0061@moaeg.com','DIVISION_ADMIN',0,1),
(2,1,'오민정','CT','MCC 팀장 / 센터장','7144','jeong3d02@moaeg.com','TEAM_LEADER',1,1),
(3,1,'김영연','CT','','7145','mrnal@moaeg.com','MEMBER',0,1),(4,1,'천은지','CT','','7146','1000eungi@moaeg.com','MEMBER',0,1),(5,1,'남유진','CT','','7147','royalyujin@moaeg.com','MEMBER',0,1),(6,1,'김용재','CT','','7148','kimwrink@moaeg.com','MEMBER',0,1),(7,1,'김수빈','CT','','7165','ddobin@moaeg.com','MEMBER',0,1),(8,1,'강다현','CT','','7166','dahyun49@moaeg.com','MEMBER',0,1),
(9,2,'안혁진','차장','마케팅팀장','7127','hyuokjin@moaeg.com','TEAM_LEADER',1,1),(10,2,'정선영','과장','','7130','smj@moaeg.com','MEMBER',0,1),(11,2,'김해미','대리','','7129','haemee1217@moaeg.com','MEMBER',0,1),(12,2,'유영림','사원','','7128','rim753@moaeg.com','MEMBER',0,1),(13,2,'이현진','사원','','7149','leehj@moaeg.com','MEMBER',0,1),
(14,3,'윤영환','과장','콘텐츠기획팀장','7132','yunyh0506@moaeg.com','TEAM_LEADER',1,1),(15,3,'정진영','대리','','7133','jjy062512@moaeg.com','MEMBER',0,1),(16,3,'유홍곤','주임','','7135','dbfmtm1312@moaeg.com','MEMBER',0,1),(17,3,'이혜림','사원','','7134','pretty950830@moaeg.com','MEMBER',0,1),(18,3,'원희준','사원','','7150','rain9241@moaeg.com','MEMBER',0,1),(19,3,'최어진','사원','','7151','eojin5831@moaeg.com','MEMBER',0,1),(20,3,'박연진','사원','','7154','duswls8642@moaeg.com','MEMBER',0,1),(21,3,'구지윤','사원','','7167','jiyunk@moaeg.com','MEMBER',0,1),
(22,4,'정해근','과장','원격기획팀장','7136','windyletter@moaeg.com','TEAM_LEADER',1,1),(23,4,'이희정','사원','','7137','hjlee@moaeg.com','MEMBER',0,1),(24,4,'이하은','사원','','7131','haeun730@moaeg.com','MEMBER',0,1),(25,4,'김수정','주임','','7153','ksj0327@moaeg.com','MEMBER',0,1),
(26,6,'안미선','대리','서비스운영팀장','7116','greenams74@moaeg.com','TEAM_LEADER',1,1),(27,6,'황서현','주임','','7114','sb3540@moaeg.com','MEMBER',0,1),(28,6,'김희정','주임','','7113','heejungkim855@moaeg.com','MEMBER',0,1),(29,6,'구교은','사원','','7115','koo38@moaeg.com','MEMBER',0,1)
]
c.executemany("INSERT INTO users VALUES(?,?,?,?,?,?,?,?,?,?)",users)

projects=[(1,'2027 소방시설관리사 필기 신규과정','콘텐츠기획→촬영→상품화→마케팅→서비스운영→국비전환 협업 예시',14,'2026-10-15','ACTIVE'),(2,'2026 하반기 국비과정 전환 프로젝트','기존 판매상품의 국비지원 상품 전환',22,'2026-09-30','ACTIVE')]
c.executemany("INSERT INTO projects VALUES(?,?,?,?,?,?)",projects)

tasks=[
(1,1,'상품 기획 확정','강의 구성과 판매 포지셔닝 확정',3,14,'URGENT','DONE','2026-07-18',1,5),
(2,1,'촬영 일정 수립','강사 및 스튜디오 일정 확정',1,2,'HIGH','IN_PROGRESS','2026-07-25',1,2),
(3,1,'강의 촬영','촬영 진행',1,3,'HIGH','TODO','2026-08-10',2,1),
(4,1,'상품 상세페이지 제작','판매페이지 문안/구성',3,17,'HIGH','IN_PROGRESS','2026-08-20',2,2),
(5,1,'광고 예산안 수립','매체별 예산안 작성',2,9,'NORMAL','TODO','2026-08-05',3,1),
(6,1,'FAQ 초안 작성','상품 상담 FAQ 준비',6,26,'NORMAL','TODO','2026-09-20',4,1),
(7,1,'국비전환 사전검토','과정개발 가능성 및 심사 일정 검토',4,22,'HIGH','REVIEW','2026-08-15',1,4),
(8,2,'국비전환 대상상품 목록 확정','판매상품 중 우선 전환대상 확정',4,22,'URGENT','IN_PROGRESS','2026-07-28',1,2),
(9,2,'과정심사 자료 분담','담당자별 심사자료 배정',4,25,'HIGH','BLOCKED','2026-07-29',2,3)
]
tasks=[tuple(list(t)+[("결정 또는 확인이 필요해 진행이 중단됨" if t[6]=="BLOCKED" else None),None,("2026-07-22 09:00:00" if t[6]=="BLOCKED" else None)]) for t in tasks]
c.executemany("INSERT INTO tasks VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",tasks)

requests=[(1,22,2,1,'QUESTION','소방시설관리사 촬영 일정 확인','촬영 가능일 확정 여부 확인 요청','WAITING','HIGH','2026-07-24',None),(2,22,9,2,'CONFIRM','광고 예산 초안 검토 요청','국비전환 일정과 광고 집행 시점 조율 필요','WAITING','NORMAL','2026-07-25',None),(3,14,2,1,'TASK','촬영 일정 우선 확정 요청','상품 오픈 일정 영향으로 촬영 일정 우선 확정 필요','IN_PROGRESS','URGENT','2026-07-24',2)]
c.executemany("INSERT INTO requests VALUES(?,?,?,?,?,?,?,?,?,?,?)",requests)

calendar_events=[
(1,'콘텐츠사업부 주간회의','2026-07-22','2026-07-22','MEETING',None,1,'사업부 공용 주간회의'),
(2,'소방시설관리사 촬영','2026-07-25','2026-07-25','SHOOT',1,2,'MCC 촬영 일정'),
(3,'신규상품 광고 캠페인 시작','2026-07-28','2026-07-28','MARKETING',2,9,'마케팅 캠페인 시작'),
(4,'원격훈련 과정심사 내부마감','2026-07-29','2026-07-29','REVIEW',4,22,'과정심사 자료 내부 마감'),
(5,'신규상품 FAQ 배포','2026-08-01','2026-08-01','SERVICE',6,26,'서비스운영 FAQ 배포')
]
c.executemany("INSERT INTO calendar_events VALUES(?,?,?,?,?,?,?,?)",calendar_events)

modules=[(1,'업무/프로젝트','TASKS',1),(2,'Calendar','CALENDAR',2),(3,'체크리스트','CHECKLIST',3),(4,'KPI','KPI',4),(5,'회의록','MEETING',5),(6,'자료/링크','RESOURCE',6),(7,'데이터보드','DATABOARD',7)]
c.executemany("INSERT INTO modules VALUES(?,?,?,?)",modules)
for tid in [1,2,3,4,6]:
    for mid in range(1,8):
        c.execute("INSERT INTO team_modules VALUES(?,?,1)",(tid,mid))

boards=[
(1,1,'시험복원·출제경향','회차별 과목 출제경향 관리','TABLE'),
(2,1,'시험응시계획','시험 종목 및 일정 관리','TABLE'),
(3,1,'교재·인강 구매내역','구매내역 관리','TABLE'),
(4,1,'환불과정 분석','환불 사유 및 개선사항 축적','TABLE'),
(5,2,'광고 캠페인 관리','캠페인 예산 및 성과 관리','TABLE'),
(6,3,'신규상품 관리','상품기획부터 오픈까지 관리','TABLE'),
(7,4,'과정심사 관리','국비과정 심사 준비 및 진행 관리','TABLE'),
(8,6,'VOC 관리','문의/불만/환불 사유 축적','TABLE')
]
c.executemany("INSERT INTO data_boards VALUES(?,?,?,?,?)",boards)

cols=[
(1,1,'exam','시험/종목','TEXT',1),(2,1,'round','회차','TEXT',2),(3,1,'subject','과목','TEXT',3),(4,1,'trend','출제경향','TEXT',4),
(5,2,'exam','시험종목','TEXT',1),(6,2,'date','시험일','DATE',2),(7,2,'person','응시자','TEXT',3),(8,2,'status','상태','STATUS',4),
(9,3,'item','상품명','TEXT',1),(10,3,'vendor','구매처','TEXT',2),(11,3,'amount','금액','NUMBER',3),(12,3,'purpose','목적','TEXT',4),
(13,4,'course','과정','TEXT',1),(14,4,'reason','환불사유','TEXT',2),(15,4,'category','유형','TEXT',3),(16,4,'improve','개선아이디어','TEXT',4)
]
c.executemany("INSERT INTO data_board_columns VALUES(?,?,?,?,?,?)",cols)

rows=[(1,1,'2026년 1회 소방시설관리사'),(2,2,'2026 소방시설관리사 시험'),(3,3,'경쟁사 교재 구매'),(4,4,'환불사유 사례')]
c.executemany("INSERT INTO data_board_rows(id,board_id,title) VALUES(?,?,?)",rows)
vals=[
(1,1,'exam','소방시설관리사'),(2,1,'round','2026-1회'),(3,1,'subject','소방수리학'),(4,1,'trend','계산형 비중 증가'),
(5,2,'exam','소방시설관리사'),(6,2,'date','2026-09-12'),(7,2,'person','김영연, 천은지'),(8,2,'status','신청예정'),
(9,3,'item','경쟁사 필기 교재'),(10,3,'vendor','온라인서점'),(11,3,'amount','85000'),(12,3,'purpose','경쟁사 분석'),
(13,4,'course','소방시설관리사'),(14,4,'reason','설명이 너무 빠름'),(15,4,'category','강의품질'),(16,4,'improve','기초설명 보강')
]
c.executemany("INSERT INTO data_board_values VALUES(?,?,?,?)",vals)

meetings=[(1,1,'MCC 주간회의','2026-07-20','오민정, 김영연, 천은지','8월 촬영 일정 우선 확정','촬영계획 확정 / 강사 일정 확인')]
c.executemany("INSERT INTO meetings VALUES(?,?,?,?,?,?,?)",meetings)
checklists=[(1,1,'MCC 월간 정기업무','MONTHLY','시험공고 확인\n응시신청\n응시자 지정\n시험복원 준비\n결과 취합')]
c.executemany("INSERT INTO checklists VALUES(?,?,?,?,?)",checklists)
resources=[(1,1,'MCC 시험복원 구글시트','LINK','https://docs.google.com/','시험복원 공용자료 링크')]
c.executemany("INSERT INTO resources VALUES(?,?,?,?,?,?)",resources)


# Sample purchase data
c.executemany("INSERT INTO purchases(team_id,purchaser_id,item_type,item_name,vendor,amount,purchase_date,payment_method,account_id,password_hint,note) VALUES(?,?,?,?,?,?,?,?,?,?,?)",[
(1,7,'교재','2026 박문각 환경기능사 필기+실기','온라인서점',38000,'2026-06-05','법인카드','','','출판사업부 결제'),
(1,3,'인강','건축설비기사 실기','온라인강의',120000,'2026-06-12','법인카드','mcc-study','****','시험분석용'),
(4,22,'교재','원격훈련 과정심사 가이드','서점',25000,'2026-07-01','법인카드','','','업무 참고용')
])
# Sample MCC refunds
c.executemany("INSERT INTO refunds(team_id,instructor_id,course_name,refund_reason,refund_amount,incentive_adjustment,improvement_action,status,refund_date) VALUES(1,?,?,?,?,?,?,?,?)",[
(2,'2025 가스산업기사 선택형 종합','강의가 생각보다 어려움',150000,-15000,'기초 설명 보강','OPEN','2026-06-15'),
(3,'전기기사','강사가 마음에 들지 않음',180000,-18000,'강의 전달 방식 점검','REVIEWED','2026-06-20')
])
# Sample book discussions
c.executemany("INSERT INTO book_discussions(team_id,month_label,book_title,selected_reason,category,status,completed_date,rating,reflection) VALUES(?,?,?,?,?,?,?,?,?)",[
(1,'2026-06','다크심리학','심리와 관계에 대한 이해','자기계발','DONE','2026-06-29',4,'업무 관계에서 상대의 의도를 이해하는 관점이 도움됨'),
(4,'2026-06','좋은지 나쁜지 누가 아는가','삶과 판단에 대한 관점 확장','자기계발','DONE','2026-06-30',5,'결과를 단정하지 않는 태도를 생각하게 됨')
])

# Service Operations sample data
c.executemany("INSERT INTO customers(name,phone,email,company,member_id,customer_type,onboarding_status,interests,notes,last_contact_at) VALUES(?,?,?,?,?,?,?,?,?,?)",[
('김고객','010-1111-2222','customer1@example.com','','kimstudy','일반','상담중','소방시설관리사, 국비지원','전화상담 선호','2026-07-20 10:30:00'),
('이회원','010-3333-4444','member2@example.com','ABC소방','lee2026','기업','온보딩완료','소방설비기사','사업주훈련 문의','2026-07-19 15:10:00')
])
c.executemany("INSERT INTO consultation_logs(customer_id,counselor_id,product_name,inquiry_category,inquiry_text,response_text,channel,status,followup_date,share_to_content,share_to_planning) VALUES(?,?,?,?,?,?,?,?,?,?,?)",[
(1,26,'소방시설관리사','수강/상품','비전공자인데 처음부터 수강 가능한가요?','기초 개념 과정부터 순차 수강을 안내했습니다.','전화','완료',None,1,1),
(2,27,'소방설비기사','국비지원','사업주훈련으로 신청 가능한가요?','기업 규모 및 지원요건 확인 후 신청 절차를 안내했습니다.','전화','완료',None,0,1)
])
c.executemany("INSERT INTO service_knowledge(product_name,category,question,answer,source_type,usage_count,updated_by) VALUES(?,?,?,?,?,?,?)",[
('소방시설관리사','수강/상품','비전공자도 수강할 수 있나요?','가능합니다. 기초 개념부터 순차적으로 학습할 수 있도록 과정 구성을 안내합니다.','상담',12,26),
('소방설비기사','국비지원','사업주훈련으로 신청 가능한가요?','기업 규모와 지원요건 확인 후 사업주훈련 절차를 안내합니다.','상담',9,27)
])
c.executemany("INSERT INTO product_studies(product_name,title,content,keywords,owner_id) VALUES(?,?,?,?,?)",[
('소방시설관리사','신규 상담 기본 스터디','시험 구조, 응시자격, 과목 구성, 추천 학습순서, 비전공자 안내 포인트를 정리합니다.','응시자격,비전공자,학습순서',26),
('소방설비기사','국비지원 상담 스터디','내일배움카드와 사업주훈련 차이, 자부담, 입과 절차를 정리합니다.','국비,내일배움카드,사업주훈련',27)
])

# Marketing sample data
c.executemany("INSERT INTO marketing_channels(channel_type,content_name,content_type,owner_id,status,planned_date,publish_date,url,notes) VALUES(?,?,?,?,?,?,?,?,?)",[
('유튜브','소방시설관리사 합격전략','정보성',11,'발행완료','2026-07-10','2026-07-18','https://youtube.com/','시험 시즌 핵심 콘텐츠'),
('블로그','건축설비기사 신규과정 소개','홍보성',12,'검토','2026-07-22',None,'','상품 오픈 연계')])
c.executemany("INSERT INTO marketing_campaigns(platform,campaign_name,product_name,owner_id,start_date,end_date,budget,spend,impressions,clicks,conversions,leads,status,landing_url,notes) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",[
('네이버','소방시설관리사 여름 캠페인','소방시설관리사',9,'2026-07-15','2026-08-15',5000000,2100000,180000,6200,310,420,'집행중','https://www.moa-ba.com/','검색광고'),
('구글','전기기사 리타겟팅','전기기사',10,'2026-07-20','2026-08-20',3000000,900000,95000,2800,140,170,'집행중','https://www.moa-ba.com/','리타겟팅')])
c.executemany("INSERT INTO marketing_events(event_name,event_type,owner_id,event_date,registration_count,marketing_inflow_count,promo_material_status,landing_status,message_status,notes) VALUES(?,?,?,?,?,?,?,?,?,?)",[
('2026 하반기 합격전략 설명회','설명회',9,'2026-08-08',120,82,'완료','완료','진행중','전사 행사 지원')])
c.executemany("INSERT INTO marketing_collab_requests(requester_team_id,requester_id,title,request_detail,owner_id,due_date,status,feedback_score,result_notes) VALUES(?,?,?,?,?,?,?,?,?)",[
(3,14,'신규 소방시설관리사 상품 홍보 요청','상세페이지 오픈 일정에 맞춘 광고/콘텐츠 집행 요청',9,'2026-07-28','진행중',0,'')])

c.commit(); c.close(); print(DB)
