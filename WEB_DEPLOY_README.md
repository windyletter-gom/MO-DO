# MO DO 웹배포판

## 권장 배포: Render + Persistent Disk
이 배포판은 기존 SQLite DB와 업로드 파일을 유지하면서 Render에서 실행하도록 수정했습니다.

### 주요 변경
- Render의 `PORT` 환경변수 자동 사용
- `/var/data` 영구 디스크에 DB와 업로드 파일 저장
- 로그인 성공 시 HttpOnly 세션 쿠키 발급
- API의 `actor_id`, `viewer_id`, `user_id` 등 행위자 식별값을 로그인 세션 사용자로 강제
- `/uploads/` 접근도 로그인 세션 필요
- 클라우드에서는 브라우저 자동 실행 비활성화
- 로그인 화면에서 공통 초기 비밀번호 노출 제거

## 배포 절차
1. 이 폴더의 파일 전체를 비공개 GitHub 저장소에 업로드합니다.
2. Render에서 `New > Blueprint`를 선택하고 해당 저장소를 연결합니다.
3. 저장소의 `render.yaml`을 인식하면 `mo-do` Web Service와 1GB Persistent Disk 설정이 제안됩니다.
4. 배포를 실행합니다.
5. 배포 완료 후 Render가 제공하는 `https://<서비스명>.onrender.com` 주소로 접속합니다.

## 중요
- Persistent Disk는 Render의 유료 Web Service에서 사용합니다. 무료 서비스의 로컬 파일시스템은 재배포/재시작 시 데이터가 사라질 수 있으므로 실사용에 적합하지 않습니다.
- 첫 실행 시 `/var/data/moa_work.db`가 없으면 배포본에 포함된 기존 DB를 영구 디스크로 복사합니다.
- 이후 DB와 첨부파일은 `/var/data` 아래에 남습니다.
- 기존 계정 비밀번호는 그대로 유지됩니다. 외부 공개 전 모든 사용자가 비밀번호를 변경하는 것을 권장합니다.
- 본 배포판은 단일 인스턴스 + SQLite 구조입니다. 사용자가 크게 늘면 PostgreSQL/Object Storage로 이전하는 것이 좋습니다.

## 로컬 웹배포판 테스트
Windows에서는 기존 BAT 대신 아래처럼 실행할 수 있습니다.

```bash
python server_stdlib.py
```

로컬 HTTP 환경에서는 자동으로 Secure 쿠키를 끄도록 되어 있습니다. Render에서는 자동으로 Secure 쿠키를 사용합니다.
