# 코치코치 백엔드 (배포용)

코치코치(CoachCoach) 모의 면접 서비스의 **AWS 배포 전용 백엔드**입니다.
FastAPI 기반이며, 무거운 AI 분석 엔진은 제외한 경량 버전입니다.

> 📌 프로젝트 전체 소개는 [frontend 저장소](https://github.com/swsong02-prog/frontend)를 참고하세요.

---

## 역할

로그인·회원가입, 면접 기록 저장/조회, 자소서 맞춤 질문 생성 등 **가벼운 작업**을 담당합니다.
영상 분석(YOLO·Whisper·Ollama)은 비용 문제로 **로컬 GPU 워커**가 별도로 처리하는 구조이므로, 이 배포용 백엔드에는 분석엔진이 포함되어 있지 않습니다.

---

## 기술 스택

- FastAPI + Uvicorn
- SQLAlchemy + SQLite
- JWT 인증 (python-jose), bcrypt
- 배포: AWS EC2 (Ubuntu, t3.micro) + Cloudflare Tunnel + systemd 상시화

---

## 폴더 구성

```
backend-deploy/
├── main.py            # FastAPI 앱 (분석엔진 로딩 제거, SECRET_KEY·CORS 환경변수화)
├── models.py          # DB 모델 (users / interview_sessions / question_results)
├── database.py        # SQLite 연결
├── question_bank.py   # 신입/경력 질문 뱅크
├── requirements.txt   # 의존성 (경량)
└── .gitignore
```

---

## 환경변수

| 변수 | 설명 |
|---|---|
| `SECRET_KEY` | JWT 서명 키 (미설정 시 기본값) |
| `ALLOWED_ORIGINS` | CORS 허용 도메인 (미설정 시 전체 허용) |

---

## 실행

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
uvicorn main:app --host 0.0.0.0 --port 8000
```

서버에서는 systemd 서비스로 등록되어 24시간 자동 가동됩니다.

---

## 주의

- `bcrypt==4.0.1` 고정 (5.x는 로그인 오류)
- SSH 키(.pem)·개인 DB 파일은 저장소에 포함하지 않습니다
