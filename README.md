# 7×7 심리전 딜레마 빙고

FastAPI, WebSocket, SQLAlchemy, MySQL로 구현한 실시간 멀티플레이 빙고 게임입니다. 참가자는 같은 방 코드로 접속해 공개 콜 또는 비밀 거래를 선택하고, 협동과 배신을 이용해 3줄 빙고 완성을 목표로 플레이합니다.

게임 진행 상태는 MySQL에 저장됩니다. 서버가 재시작된 뒤 참가자가 다시 접속하면 방, 참가자, 빙고판, 점수, 턴과 진행 중인 거래 상태를 데이터베이스에서 복구합니다. 현재 연결된 WebSocket과 타이머는 각 서버 프로세스의 메모리에서 관리합니다.

## 주요 기능

- 방 코드 기반 게임방 생성 및 재접속
- 현재 방 목록 조회
- 7×7 무작위 빙고판 생성
- WebSocket을 통한 참가자·턴·게임 상태 동기화
- 공개 콜, 비밀 거래, 협동·배신과 보너스 선택
- 일반 턴, 거래 선택, 보너스 선택 제한 시간 처리
- 거래 당사자와 입력값 검증
- 게임 상태 변경을 MySQL에 먼저 저장한 뒤 참가자에게 전파
- 서버 재시작 후 저장된 방과 진행 상태 복구
- 승자 판정과 누적 점수 저장
- DB 연결 상태를 검사하는 `/health` 엔드포인트
- 별도 프런트엔드 빌드 없이 단일 HTML 화면 제공

## 기술 구성

- Python 3.10+
- FastAPI / Uvicorn
- WebSocket
- SQLAlchemy 2.x async
- `asyncmy` MySQL 드라이버
- Alembic migration
- Pydantic Settings
- pytest / pytest-asyncio

`index.html`은 Tailwind CSS와 Pretendard 글꼴을 CDN에서 불러옵니다. Node.js 빌드는 필요하지 않지만 화면 스타일과 웹 폰트를 불러오려면 브라우저에서 인터넷에 연결할 수 있어야 합니다.

## 파일 구조

```text
.
├── alembic/             # Alembic 환경과 DB migration
├── tests/               # 단위·회귀 테스트
├── main.py              # FastAPI, REST API, WebSocket, 타이머와 게임 흐름
├── game_logic.py        # 플레이어와 게임방 도메인 로직
├── config.py            # 환경 변수와 MySQL URL 정규화
├── database.py          # SQLAlchemy async 엔진과 세션
├── models.py            # rooms, games, room_players, game_results 모델
├── repositories.py      # 게임 상태 저장·조회 repository
├── index.html           # 게임 화면과 브라우저 JavaScript
├── alembic.ini          # Alembic 설정
├── requirements.txt     # Python 의존성
├── .env.example         # 비밀값이 없는 환경 변수 예시
└── README.md
```

## 로컬 실행

### 1. Python과 MySQL 준비

Python 3.10 이상과 MySQL 8.x가 필요합니다.

```powershell
python --version
```

Windows에서 `python` 명령을 사용할 수 없다면 `py`를 사용할 수 있습니다.

현재 개발 PC에는 MySQL 8.4 no-install 배포본이 다음 사용자 전용 경로에 설치되어 있습니다.

```text
%USERPROFILE%\Documents\Codex\LocalServices\DilemmaBingo\mysql-8.4.11-winx64
```

Windows 서비스로 설치된 MySQL은 해당 서비스를 시작하면 됩니다. 현재 개발 PC의 no-install MySQL을 재부팅 후 시작할 때는 PowerShell에서 다음 명령을 사용할 수 있습니다.

```powershell
$mysqlHome = Join-Path $env:USERPROFILE 'Documents\Codex\LocalServices\DilemmaBingo\mysql-8.4.11-winx64'
$mysqlArgs = @(
    "--basedir=$mysqlHome"
    "--datadir=$mysqlHome\data"
    '--port=3306'
    '--bind-address=127.0.0.1'
    "--log-error=$mysqlHome\mysql-error.log"
    "--pid-file=$mysqlHome\mysql.pid"
)
Start-Process -FilePath "$mysqlHome\bin\mysqld.exe" -ArgumentList $mysqlArgs -WindowStyle Hidden
```

포트가 열렸는지 확인합니다.

```powershell
Test-NetConnection 127.0.0.1 -Port 3306
```

현재 로컬 DB 이름은 `dilemma_bingo`이며 앱은 전용 DB 사용자로 연결합니다. root 및 앱 비밀번호는 프로젝트나 README에 기록하지 않습니다.

### 2. 가상 환경 생성 및 활성화

Windows PowerShell:

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
```

Windows 명령 프롬프트:

```bat
py -m venv .venv
.venv\Scripts\activate.bat
```

macOS/Linux:

```bash
python3 -m venv .venv
source .venv/bin/activate
```

### 3. 의존성 설치

```bash
python -m pip install -r requirements.txt
```

MySQL 8의 기본 인증 방식을 처리하기 위해 `cryptography`가 필요하며 `requirements.txt`에 포함되어 있습니다.

### 4. 환경 변수 설정

예시 파일을 복사합니다.

Windows PowerShell:

```powershell
Copy-Item .env.example .env
```

macOS/Linux:

```bash
cp .env.example .env
```

`.env`의 `DATABASE_URL`을 실제 로컬 계정에 맞게 변경합니다.

```dotenv
DATABASE_URL=mysql+asyncmy://app_user:password@127.0.0.1:3306/dilemma_bingo?charset=utf8mb4
```

- `.env`는 Git에서 제외됩니다.
- 비밀번호에 URL 예약 문자가 있으면 URL 인코딩해야 합니다.
- 애플리케이션은 `DATABASE_URL` 또는 `MYSQL_URL`을 사용할 수 있습니다.
- `mysql://` URL은 실행 시 `mysql+asyncmy://`로 자동 변환됩니다.

### 5. DB migration

처음 실행하거나 migration이 추가된 뒤 다음 명령을 실행합니다.

```bash
alembic upgrade head
```

현재 revision과 모델 변경 여부는 다음과 같이 확인할 수 있습니다.

```bash
alembic current
alembic check
```

현재 schema에는 다음 테이블이 생성됩니다.

- `rooms`
- `room_players`
- `games`
- `game_results`
- `alembic_version`

### 6. 서버 실행

```bash
uvicorn main:app --reload
```

또는 다음 명령을 사용할 수 있습니다.

```bash
python main.py
```

브라우저와 API는 다음 주소에서 확인합니다.

- 게임 화면: `http://127.0.0.1:8000`
- API 문서: `http://127.0.0.1:8000/docs`
- DB healthcheck: `http://127.0.0.1:8000/health`

정상 healthcheck 응답:

```json
{"status":"ok","database":"ok"}
```

DB에 연결할 수 없으면 `/health`는 비밀값이나 내부 오류 내용을 노출하지 않고 HTTP 503을 반환합니다.

실제 멀티플레이를 확인하려면 브라우저 창 또는 시크릿 창을 두 개 이상 열고 같은 방 코드로 접속하세요.

## 테스트와 점검

```bash
python -m pytest -q
python -m pip check
alembic check
```

배포 전에 최소한 다음 항목을 확인합니다.

- 전체 테스트 통과
- `pip check`에서 손상된 의존성 없음
- `alembic check`에서 누락된 migration 없음
- `/health`가 200과 `database: ok` 반환
- 두 명 이상의 WebSocket 참가자가 게임 시작과 턴 진행 가능
- 거래 협동, 배신, 보너스와 시간 초과 이후 상태가 정상 진행

## 통신과 저장 구조

브라우저가 `GET /`을 요청하면 FastAPI가 프로젝트 위치를 기준으로 `index.html`을 반환합니다. 대기실은 `GET /api/rooms`로 MySQL에 저장된 활성 방 목록을 조회합니다.

게임 참가 시 브라우저는 현재 페이지의 호스트를 기준으로 다음 WebSocket에 연결합니다.

```text
/ws/{room_id}/{client_id}
```

로컬 HTTP에서는 `ws://`, Railway와 같은 HTTPS 환경에서는 `wss://`가 자동 선택됩니다. 브라우저는 `join`, `start_game`, `public_call`, `propose_deal`, `deal_choice`, `bonus_pick`, `leave` 메시지를 JSON으로 전송합니다.

실시간 연결, 방별 lock과 제한 시간 task는 `ConnectionManager`의 메모리에서 관리합니다. 방, 참가자, 빙고판, 마킹, 점수, 턴, 게임과 거래 상태는 MySQL에 저장합니다. 서버가 재시작되면 기존 WebSocket은 끊기지만 참가자가 같은 ID로 다시 접속할 때 저장된 상태를 불러옵니다.

참가자가 화면의 `나가기`를 선택하면 해당 게임은 중단되고 방은 대기 상태로 전환됩니다. 반면 네트워크 오류나 브라우저 종료처럼 WebSocket만 예기치 않게 끊기면 게임 상태는 유지됩니다. 연결이 끊긴 참가자는 활성 턴과 거래 대상에서 제외되고, 남은 참가자가 있으면 현재 턴을 재계산해 게임을 계속합니다. 같은 클라이언트 ID로 다시 접속하면 저장된 보드, 마킹, 점수와 진행 중인 거래 또는 보너스 상태를 복원합니다.

현재 구조는 메모리 기반 WebSocket 관리자와 타이머를 공유하지 않으므로 Railway 복제본은 1개로 운영해야 합니다. 다중 인스턴스 운영이 필요하면 Redis pub/sub, 분산 lock과 공유 timer/queue 도입이 필요합니다.

## Railway + MySQL 배포

현재 Railway 구성은 GitHub 저장소의 `master` 브랜치와 연결되어 있습니다.

### 필요한 서비스와 변수

1. Railway 프로젝트에 애플리케이션 서비스를 추가하고 GitHub 저장소를 연결합니다.
2. 같은 프로젝트에 MySQL 서비스를 추가합니다.
3. 애플리케이션 서비스에 다음 참조 변수를 설정합니다.

```dotenv
MYSQL_URL=${{MySQL.MYSQL_URL}}
```

참조 변수는 MySQL 비밀번호를 애플리케이션 설정에 복사하지 않고 Railway가 관리하는 연결 URL을 사용하게 합니다. `config.py`가 Railway의 `mysql://` scheme을 SQLAlchemy async dialect로 정규화합니다.

### 애플리케이션 배포 설정

Railway 애플리케이션 서비스의 Deploy 설정을 다음과 같이 구성합니다.

Pre-deploy Command:

```bash
alembic upgrade head
```

Custom Start Command:

```bash
uvicorn main:app --host 0.0.0.0 --port $PORT
```

Healthcheck Path:

```text
/health
```

Pre-deploy migration이 실패하면 새 애플리케이션 배포를 시작하지 않습니다. `/health`는 앱뿐 아니라 MySQL 연결까지 확인하므로 DB를 사용할 수 없는 인스턴스가 정상 배포로 전환되는 것을 막습니다.

### 배포 순서

1. MySQL이 `Online`인지 확인합니다.
2. 앱 코드에 `/health`와 필요한 의존성이 포함되어 있는지 확인합니다.
3. `MYSQL_URL`, Pre-deploy, Start Command, Healthcheck 설정을 검토합니다.
4. migration과 앱 배포를 실행합니다.
5. Railway 로그에서 `alembic upgrade head` 성공과 Uvicorn 기동을 확인합니다.
6. 공개 도메인의 `/health`, `/`, `/api/rooms`를 확인합니다.
7. 두 브라우저로 WebSocket 게임 흐름을 점검합니다.

현재 공개 도메인은 다음과 같습니다.

```text
https://dilemma-bingo-production.up.railway.app
```

## 운영 주의사항

- MySQL과 애플리케이션은 Railway 사용량을 소비합니다.
- MySQL 볼륨을 삭제하면 저장 데이터가 복구되지 않을 수 있습니다.
- `.env`, DB 비밀번호, Railway token은 Git에 커밋하지 않습니다.
- 배포·재시작 시 기존 WebSocket 연결은 끊기므로 클라이언트가 재접속해야 합니다.
- 복제본은 1개로 유지합니다.
- Railway MySQL에는 정기 백업 정책을 설정하는 것이 좋습니다.
- schema 변경은 모델만 수정하지 말고 Alembic revision을 함께 추가합니다.

## 문제 해결

### `DATABASE_URL 또는 MYSQL_URL 환경변수가 필요합니다`

로컬에서는 `.env`에 `DATABASE_URL`이 있는지 확인합니다. Railway에서는 앱 서비스의 `MYSQL_URL`이 `${{MySQL.MYSQL_URL}}`을 참조하는지 확인합니다.

### MySQL 인증 중 `cryptography` 관련 오류

가상 환경의 의존성을 다시 설치합니다.

```bash
python -m pip install -r requirements.txt
```

### 로컬 MySQL 연결 실패

```powershell
Test-NetConnection 127.0.0.1 -Port 3306
Get-Process mysqld -ErrorAction SilentlyContinue
```

MySQL 프로세스, 3306 포트, `.env`의 DB 이름·사용자·비밀번호를 확인합니다.

### Railway healthcheck 실패

다음 순서로 확인합니다.

1. MySQL 서비스가 `Online`인지 확인
2. 앱의 `MYSQL_URL` 참조 확인
3. Pre-deploy 로그에서 Alembic 오류 확인
4. Deploy 로그에서 Uvicorn이 `$PORT`로 기동했는지 확인
5. `/health`가 503이라면 MySQL private network와 인증 설정 확인

### 모델 변경 후 `alembic check` 실패

새 revision을 생성하고 내용을 검토한 뒤 적용합니다.

```bash
alembic revision --autogenerate -m "describe change"
alembic upgrade head
```

## 게임 현황 (공개 관전)

메인 화면의 **게임 현황 · 누구나 관전** 버튼 또는 `/game-status`에서 닉네임 입력이나 방 입장 없이 관전할 수 있습니다.

- 동시에 1개, 2개, 4개 게임을 선택해 표시합니다. 넓은 화면에서는 전체 / 좌우 절반 / 2×2 그리드를 사용하며, 모바일에서는 읽기 편한 세로 배열을 사용합니다.
- 진행 중인 게임을 게임 시작 시각 내림차순으로 정렬합니다. 시작 시각이 같으면 게임 ID 내림차순으로 정렬하며, 선택한 화면 수 단위로 페이지를 이동합니다.
- 현재 차례, 참가자별 완성 줄 수, 승리 임박 참가자 이름과 기존 게임 규칙의 견제 번호를 표시합니다.
- 거래 중인 카드는 보라색으로 강조합니다. 짧게 끝난 거래도 시작 이벤트를 통해 강조 알림을 표시합니다.
- 각 게임의 공개 로그는 최신순으로 한 번에 4개씩 조회합니다. 기존 참가자에게 공개되던 로그만 사용하므로 미확정 거래 선택, 비밀 거래 제안 번호, 비공개 보너스 번호는 공개하지 않습니다.
- 관전 응답에는 빙고판, 전체 마킹 배열, 거래 선택 원본, 토큰, 재접속용 참가자 ID가 포함되지 않습니다. 화면용 참가자 ID는 별도로 부여합니다.

`GET /api/game-status?page=1&page_size=4`는 읽기 전용 조회 API입니다. `page_size`는 1, 2, 4만 허용합니다. `/api/game-status/ws`는 관전 전용 WebSocket이며, `page`, `page_size`, 게임 ID별 `log_pages`, `request_id` 조회 조건만 처리합니다. 참가자 WebSocket과 분리되어 방 입장, 인원 추가, 턴 진행, 게임 저장, 타이머 생성 등을 실행하지 않습니다. 일반적으로 약 1.25초 간격으로 갱신하며, 연결이 끊기면 마지막 수신 상태임을 표시하고 자동 재연결합니다.

**보관 범위:** 기존 DB 구조와 게임 저장 흐름을 바꾸지 않기 위해 공개 로그는 프로세스 메모리에 게임별 최근 200개, 최대 256게임까지 보관합니다. 서버 재시작 전의 로그는 복구되지 않으며, 현재 진행 상태는 DB에서 조회합니다. 참가자가 아직 재접속하지 않은 저장 게임에는 차례를 추측하지 않고 연결 대기로 표시합니다. 기존과 동일하게 단일 서버 프로세스 / 복제본 1개 운영을 전제로 합니다.

관전 기능은 `spectator.py`, `game-status.html`, `game-status.js`로 분리했습니다. `main.py`에는 라우터 등록과 기존 공개 로그의 메모리 수집만 추가했으며, `index.html`에는 진입 링크만 추가했습니다. DB migration이나 새 의존성 설치는 필요하지 않습니다.
