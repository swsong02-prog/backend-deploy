# 코치코치 백엔드 (배포용)

AI 모의면접 웹 서비스 코치코치(CoachCoach)의 서버 코드입니다. 지금 AWS Lightsail에서 돌고 있습니다.
서비스 소개와 전체 구조는 [frontend 저장소](https://github.com/swsong02-prog/frontend)에 정리해 두었습니다.

## 맡은 일

로그인, 면접 기록 저장과 조회, 작업 큐, 채용공고 조회처럼 항상 켜져 있어야 하는 가벼운 일만 합니다.
영상 분석과 질문 생성은 GPU가 필요해서 이 서버에서 하지 않고, 제 PC에서 도는 분석 워커가 가져가서 처리합니다.

## 작업 큐

서버가 PC를 직접 호출하려면 PC의 인바운드 포트를 열어야 합니다. 그래서 반대로 워커가 서버에 일감이 있는지 주기적으로 묻고 가져가게 했습니다.

1. 사용자가 답변 영상을 올리면 서버가 작업을 `pending`으로 저장합니다.
2. 워커가 `/worker/next-job`으로 작업을 하나 가져가면 `processing`이 됩니다.
3. 워커가 분석을 마치고 `/worker/result/{job_id}`로 결과를 보내면 `done`이 됩니다.

워커가 작업을 가져간 채 멈추면 분석은 10분, 질문 생성은 5분 뒤 다시 `pending`으로 돌아갑니다. 같은 작업이 세 번 돌아오면 `failed`로 처리해서 끝없이 재시도하지 않게 했습니다.
워커는 `/worker/heartbeat`로 신호를 보내고, 5분 넘게 신호가 없으면 서버는 맞춤 질문 대신 기본 질문은행을 씁니다.

## API

| 구분 | 경로 |
|---|---|
| 회원 | `POST /signup`, `POST /login`, `GET /me`, `PATCH /me` |
| 면접 | `POST /api/questions`, `POST /api/analyze-answer`, `POST /interview/finish`, `POST /api/parse-resume`, `POST /api/tts` |
| 결과와 기록 | `GET /api/question-result/{job_id}`, `GET /api/analysis-result/{job_id}`, `GET /history`, `GET /history/{session_id}`, `GET /growth` |
| 공고 | `GET /api/jobs`, `GET /api/postings` (공공데이터포털 잡알리오) |
| 워커 전용 | `/worker/next-job`, `/worker/video/{job_id}`, `/worker/result/{job_id}`, `/worker/next-question-job`, `/worker/question-result/{job_id}`, `/worker/heartbeat` |
| 상태 | `GET /api/worker-status` |

워커 전용 경로는 `X-Worker-Key` 헤더가 서버의 `WORKER_KEY`와 같을 때만 응답합니다.

## 파일

```
main.py            FastAPI 앱, 작업 큐, 워커 API
models.py          DB 모델 (회원, 면접 기록, 분석 작업, 질문 작업)
database.py        SQLite 연결 (WAL 모드, busy_timeout 10초)
question_bank.py   직군별 기본 질문은행 (워커가 꺼져 있을 때 사용)
job_postings.py    잡알리오 공공기관 채용공고 조회와 추천
requirements.txt   의존성
```

## 환경변수

| 변수 | 설명 |
|---|---|
| `SECRET_KEY` | JWT 서명 키. 없으면 서버가 시작되지 않습니다. |
| `WORKER_KEY` | 워커 인증 키. 없으면 서버가 시작되지 않습니다. |
| `ALLOWED_ORIGINS` | CORS 허용 도메인 (쉼표로 구분) |
| `ALIO_SERVICE_KEY` | 공공데이터포털 인증키 (채용공고 조회용) |
| `MAX_VIDEO_MB` | 답변 영상 업로드 상한 (기본 150) |
| `ALLOW_DEV_KEYS` | `1`이면 위 두 키 없이 개발용 키로 실행합니다. 로컬 테스트에서만 씁니다. |

서버에서는 이 값들을 저장소가 아니라 systemd 서비스의 환경변수 파일에 넣어 둡니다.

## 실행

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
ALLOW_DEV_KEYS=1 uvicorn main:app --host 127.0.0.1 --port 8000
```

서버에서는 systemd 서비스로 등록해 두어서 재부팅해도 다시 켜집니다. 외부에서는 Cloudflare Tunnel로 들어옵니다.

## 참고

- `bcrypt`는 4.0.1로 고정했습니다. 5.x에서는 로그인할 때 오류가 났습니다.
- SSH 키와 DB 파일은 저장소에 올리지 않습니다.
- 서버가 한 대라 이 서버가 멈추면 서비스 전체가 멈춥니다. 터널도 임시 주소라서 서버를 재시작하면 주소가 바뀝니다.
