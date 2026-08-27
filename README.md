# 7×7 심리전 딜레마 빙고

FastAPI와 WebSocket으로 구현한 실시간 멀티플레이 빙고 게임입니다. 참가자는 방 코드를 사용해 같은 방에 접속하고, 공개 콜 또는 비밀 거래를 선택하며 3줄 빙고 완성을 목표로 플레이합니다.

현재 프로젝트는 데이터베이스를 사용하지 않습니다. 방, 참가자, 점수와 게임 진행 상태는 모두 FastAPI 서버의 메모리에 저장됩니다.

## 주요 기능

- 방 코드 기반 게임방 생성 및 참가
- 현재 방 목록 조회
- 7×7 무작위 빙고판 생성
- WebSocket을 통한 실시간 참가자·턴·게임 상태 동기화
- 공개 콜, 비밀 거래, 협동·배신 선택
- 턴 및 거래 제한 시간 처리
- 승자 판정과 서버 실행 중 누적 점수 표시
- 별도 프런트엔드 빌드 없이 단일 HTML 화면 제공

## 파일 구조

```text
.
├── main.py          # FastAPI 앱, REST API, WebSocket 및 게임 로직
├── index.html       # 게임 화면과 브라우저 측 JavaScript
├── requirements.txt # Python 실행 의존성
├── .gitignore       # Git에서 제외할 로컬·생성 파일
└── README.md        # 프로젝트 안내
```

`index.html`은 Tailwind CSS와 Pretendard 글꼴을 CDN에서 불러옵니다. 따라서 Node.js나 별도의 프런트엔드 패키지 설치 과정은 필요하지 않지만, 화면 스타일과 웹 폰트 사용에는 인터넷 연결이 필요합니다.

## 로컬 실행

### 1. Python 확인

Python 3.10 이상 사용을 권장합니다.

```bash
python --version
```

Windows에서 `python` 명령이 연결되어 있지 않다면 `py`를 사용할 수 있습니다.

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

macOS/Linux에서 `python` 대신 `python3`를 사용해야 할 수도 있습니다.

### 4. 서버 실행

개발 중 자동 새로고침을 사용하려면 다음 명령을 실행합니다.

```bash
uvicorn main:app --reload
```

또는 `main.py`의 실행 코드를 사용할 수 있습니다.

```bash
python main.py
```

브라우저에서 다음 주소로 접속합니다.

```text
http://127.0.0.1:8000
```

REST API 문서는 다음 주소에서 확인할 수 있습니다.

```text
http://127.0.0.1:8000/docs
```

실제 멀티플레이 동작을 확인하려면 브라우저 창이나 시크릿 창을 두 개 이상 열고 같은 방 코드로 접속하세요.

## 통신 구조

브라우저가 `GET /`을 요청하면 FastAPI가 `index.html`을 반환합니다. 대기실은 `GET /api/rooms`로 현재 서버 메모리에 있는 방 목록을 조회합니다.

게임 참가 시 브라우저는 현재 페이지의 호스트를 기준으로 다음 WebSocket에 연결합니다.

```text
/ws/{room_id}/{client_id}
```

로컬 HTTP 환경에서는 `ws://`, Railway와 같은 HTTPS 환경에서는 `wss://`가 자동으로 선택됩니다. 브라우저는 `join`, `start_game`, `public_call`, `propose_deal`, `deal_choice`, `bonus_pick`, `leave` 등의 메시지를 JSON으로 전송합니다. 서버는 같은 방의 참가자에게 방 상태, 현재 턴, 이벤트 기록, 거래 화면, 게임 종료 결과 등을 실시간으로 전달합니다.

현재 방과 게임 상태는 `ConnectionManager`가 서버 프로세스의 메모리에서 관리합니다. 이 때문에 서버를 재시작하거나 다시 배포하면 모든 방, 게임 상태와 누적 점수가 초기화됩니다.

## Railway 배포

1. `main.py`, `index.html`, `requirements.txt`, `.gitignore`, `README.md`를 같은 GitHub 저장소의 최상위 폴더에 둡니다.
2. Railway에서 새 프로젝트를 만들고 해당 GitHub 저장소를 연결합니다.
3. Railway가 Python 프로젝트를 감지해 `requirements.txt`의 패키지를 설치하도록 합니다.
4. 시작 명령이 자동으로 잡히지 않으면 FastAPI 서비스의 Start Command를 다음과 같이 설정합니다.

```bash
uvicorn main:app --host 0.0.0.0 --port $PORT
```

5. 배포가 완료되면 서비스의 Networking 설정에서 공개 도메인을 생성합니다.
6. 생성된 HTTPS 주소로 접속하고, 여러 브라우저 창에서 같은 방에 참가해 WebSocket 연결까지 확인합니다.

현재 `main.py`는 상대 경로인 `index.html`을 반환하므로 Railway 서비스의 루트 디렉터리는 두 파일이 함께 있는 저장소 최상위 폴더여야 합니다.

### 현재 구조에서의 배포 주의사항

- 데이터베이스와 영구 저장소가 없으므로 재배포·재시작 시 모든 상태가 사라집니다.
- 여러 서버 인스턴스로 확장하면 각 인스턴스가 서로 다른 메모리 상태를 가지므로 같은 방의 참가자가 분리될 수 있습니다. 현재 버전은 Railway에서 인스턴스(복제본) 1개로 실행하는 것이 안전합니다.
- 서버가 절전 또는 재시작되는 플랜에서는 진행 중인 WebSocket 연결이 끊길 수 있습니다.
- Railway의 공개 주소는 HTTPS이므로 브라우저 코드가 자동으로 보안 WebSocket인 `wss://`를 사용합니다.
- 현재 단계에서는 MySQL, SQLAlchemy, 환경 변수 로더 등 데이터베이스 관련 패키지가 필요하지 않습니다.

영구 저장이나 다중 인스턴스 운영이 필요해질 때 데이터베이스 또는 Redis 같은 공유 저장소 도입을 별도 단계로 검토할 수 있습니다.
