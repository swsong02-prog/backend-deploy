from fastapi import FastAPI, UploadFile, File, Form, Depends, HTTPException, status, Header, Response
from fastapi.responses import FileResponse
from fastapi.security import OAuth2PasswordBearer
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session
from passlib.context import CryptContext
from datetime import datetime, timedelta, timezone
from jose import jwt, JWTError
import json
import os
import hmac
import math
import uuid
import hashlib
import io
import zipfile
import xml.etree.ElementTree as ET
from collections import OrderedDict

import question_bank
import models
from database import engine, get_db

app = FastAPI()

models.Base.metadata.create_all(bind=engine)


def _migrate_add_company_column(target_engine=engine):
    """SQLite create_all은 기존 테이블에 컬럼을 추가하지 못하므로,
    interview_sessions에 company 컬럼이 없으면 직접 추가한다.
    실패해도 서버 기동은 막지 않는다."""
    try:
        from sqlalchemy import text
        with target_engine.begin() as conn:
            cols = conn.execute(text("PRAGMA table_info(interview_sessions)")).fetchall()
            col_names = [c[1] for c in cols]
            if cols and "company" not in col_names:
                conn.execute(text("ALTER TABLE interview_sessions ADD COLUMN company TEXT"))
                print("[마이그레이션] interview_sessions.company 컬럼 추가 완료")
            if cols and "career" not in col_names:
                conn.execute(text("ALTER TABLE interview_sessions ADD COLUMN career TEXT"))
                print("[마이그레이션] interview_sessions.career 컬럼 추가 완료")
    except Exception as e:
        print(f"[마이그레이션 경고] company 컬럼 추가 실패 (서버는 계속 뜸): {e}")


def _migrate_add_career_column(target_engine=engine):
    """analysis_jobs에 career 컬럼이 없으면 직접 추가한다.
    (경력자 평가 개선 — 워커가 신입/경력 프롬프트를 분기하는 데 사용)
    실패해도 서버 기동은 막지 않는다."""
    try:
        from sqlalchemy import text
        with target_engine.begin() as conn:
            cols = conn.execute(text("PRAGMA table_info(analysis_jobs)")).fetchall()
            col_names = [c[1] for c in cols]
            if cols and "career" not in col_names:
                conn.execute(text(
                    "ALTER TABLE analysis_jobs ADD COLUMN career TEXT DEFAULT '신입'"))
                print("[마이그레이션] analysis_jobs.career 컬럼 추가 완료")
    except Exception as e:
        print(f"[마이그레이션 경고] career 컬럼 추가 실패 (서버는 계속 뜸): {e}")


def _migrate_question_jobs_columns(target_engine=engine):
    """question_jobs 테이블은 create_all이 만들지만, 이전 버전 테이블이 남아 있을 경우를
    대비해 빠진 컬럼이 있으면 직접 추가한다. 실패해도 서버 기동은 막지 않는다."""
    expected = {
        "user_id": "INTEGER",
        "job": "TEXT",
        "sub": "TEXT",
        "level": "TEXT DEFAULT '중'",
        "career": "TEXT DEFAULT '신입'",
        "resume_text": "TEXT",
        "status": "TEXT DEFAULT 'pending'",
        "result_json": "TEXT",
        "error": "TEXT",
        "created_at": "DATETIME",
        "processing_started_at": "DATETIME",
    }
    try:
        from sqlalchemy import text
        with target_engine.begin() as conn:
            cols = conn.execute(text("PRAGMA table_info(question_jobs)")).fetchall()
            col_names = [c[1] for c in cols]
            if not cols:
                return
            for name, ddl in expected.items():
                if name not in col_names:
                    conn.execute(text(f"ALTER TABLE question_jobs ADD COLUMN {name} {ddl}"))
                    print(f"[마이그레이션] question_jobs.{name} 컬럼 추가 완료")
    except Exception as e:
        print(f"[마이그레이션 경고] question_jobs 컬럼 보강 실패 (서버는 계속 뜸): {e}")


def _migrate_add_claim_token_columns(target_engine=engine):
    """analysis_jobs / question_jobs에 claim_token 컬럼이 없으면 추가한다.
    (워커가 가져간 '이번 배정'의 결과만 받아들이기 위한 배정 토큰)
    실패해도 서버 기동은 막지 않는다."""
    try:
        from sqlalchemy import text
        with target_engine.begin() as conn:
            for table in ("analysis_jobs", "question_jobs"):
                cols = conn.execute(text(f"PRAGMA table_info({table})")).fetchall()
                if cols and "claim_token" not in [c[1] for c in cols]:
                    conn.execute(text(f"ALTER TABLE {table} ADD COLUMN claim_token TEXT"))
                    print(f"[마이그레이션] {table}.claim_token 컬럼 추가 완료")
    except Exception as e:
        print(f"[마이그레이션 경고] claim_token 컬럼 추가 실패 (서버는 계속 뜸): {e}")


_migrate_add_company_column()
_migrate_add_career_column()
_migrate_question_jobs_columns()
_migrate_add_claim_token_columns()


def _utc_iso(dt):
    """DB created_at(SQLite func.now() → UTC, tz 정보 없는 naive datetime)을
    UTC 명시 ISO 문자열('...Z')로 변환한다. 프론트의 new Date()가 로컬 시각으로
    올바르게 변환하도록 하기 위함(9시간 오차 방지)."""
    if dt is None:
        return None
    if isinstance(dt, str):
        try:
            dt = datetime.fromisoformat(dt.replace("Z", "+00:00"))
        except ValueError:
            return dt
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)
    return dt.isoformat(timespec="seconds").replace("+00:00", "Z")


pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

# ── 비밀키: 코드에 박지 않고 서버 환경변수에서 읽어온다 ──
# 기본값이 코드(공개 저장소)에 있으면 누구나 토큰을 위조할 수 있으므로,
# 환경변수가 없으면 서버를 띄우지 않는다.
# 로컬 테스트에서만 ALLOW_DEV_KEYS=1 로 개발용 키를 허용한다.
_ALLOW_DEV_KEYS = os.environ.get("ALLOW_DEV_KEYS") == "1"


def _required_secret(name: str, dev_default: str) -> str:
    value = os.environ.get(name, "").strip()
    if value:
        return value
    if _ALLOW_DEV_KEYS:
        print(f"[경고] {name} 환경변수가 없어 개발용 키를 사용합니다 (로컬 테스트 전용).")
        return dev_default
    raise RuntimeError(
        f"{name} 환경변수가 설정되지 않았습니다. 서버에 충분히 긴 무작위 값을 설정하세요. "
        f"(로컬 테스트라면 ALLOW_DEV_KEYS=1)")


SECRET_KEY = _required_secret("SECRET_KEY", "coachcoach-local-dev-secret")
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 60 * 24 * 7  # 7일 — 60분 만료로 기록·피드백 화면이 죽던 문제 해결

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="login")

# ── CORS: 허용할 프론트 주소를 환경변수에서 읽어온다 ──
# ALLOWED_ORIGINS 환경변수에 콤마로 주소들을 넣으면 그것만 허용.
# 없으면 (로컬 테스트용) 전체 허용.
_origins_env = os.environ.get("ALLOWED_ORIGINS", "").strip()
if _origins_env:
    allow_origins = [o.strip() for o in _origins_env.split(",") if o.strip()]
else:
    allow_origins = ["*"]

app.add_middleware(
    CORSMiddleware,
    allow_origins=allow_origins,
    allow_methods=["*"],
    allow_headers=["*"],
)

JOB_DATA = {
    "개발":         {"ic": "💻", "subs": ["프론트엔드", "백엔드", "iOS", "안드로이드", "임베디드", "DevOps"]},
    "데이터·AI":    {"ic": "📊", "subs": ["데이터 분석", "ML·AI", "데이터 엔지니어", "PM·기획"]},
    "디자인":       {"ic": "🎨", "subs": ["UI·UX", "그래픽", "제품 디자인", "영상·모션"]},
    "마케팅":       {"ic": "📢", "subs": ["브랜드", "퍼포먼스", "콘텐츠", "SNS"]},
    "영업":         {"ic": "🤝", "subs": ["B2B 영업", "B2C 영업", "해외영업", "영업관리"]},
    "경영사무":     {"ic": "📋", "subs": ["인사(HR)", "총무", "재무·회계", "전략기획"]},
    "금융":         {"ic": "💰", "subs": ["은행", "증권·투자", "보험", "리스크·심사"]},
    "연구·엔지니어링": {"ic": "🔬", "subs": ["기계", "전자·전기", "화공·소재", "연구개발"]},
    "공공·행정":    {"ic": "🏛️", "subs": ["행정직", "경찰·소방", "군인·국방", "공기업", "정책·기획"]},
    "교육":         {"ic": "🎓", "subs": ["교사", "강사", "교육기획", "교수·연구"]},
    "의료·보건":    {"ic": "⚕️", "subs": ["간호사", "약사", "보건직", "임상·검사"]},
    "서비스·유통":  {"ic": "✈️", "subs": ["승무원", "호텔", "판매·MD", "외식·식음"]},
    "미디어·콘텐츠": {"ic": "🎬", "subs": ["PD", "기자·에디터", "영상·편집", "작가·기획"]},
}

# ────────────────────────────────────────────────────────────
#  [AWS 배포 버전] 분석기 로딩을 끈다.
#  무거운 AI 분석(YOLO·Whisper·Ollama)은 AWS 무료서버(메모리 1GB)가
#  감당 못 하므로, 여기서는 로딩하지 않는다.
#  분석은 3단계에서 '내 PC 워커'가 맡는다. (작전계획서 설계)
#  → 로컬 PC에서 분석까지 돌리려면 원본 main.py를 쓸 것.
# ────────────────────────────────────────────────────────────
analyzer = None


def get_current_user(token: str = Depends(oauth2_scheme),
                     db: Session = Depends(get_db)) -> models.User:
    cred_error = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="입장권이 유효하지 않습니다. 다시 로그인해주세요.",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        user_id = payload.get("sub")
        if user_id is None:
            raise cred_error
    except JWTError:
        raise cred_error
    user = db.query(models.User).filter(models.User.user_id == int(user_id)).first()
    if user is None:
        raise cred_error
    return user


class QuestionRequest(BaseModel):
    job: str = "개발"
    sub: str = ""
    level: str = "중"
    career: str = "신입"
    resume_text: str = ""


@app.get("/")
def root():
    return {"message": "코치코치 백엔드가 작동 중입니다!"}


@app.get("/api/jobs")
def get_jobs():
    return JOB_DATA


DEFAULT_QUESTIONS = [
    "간단하게 자기소개를 해주세요.",
    "지원하신 곳(회사·기관)과 이 직무에 지원하신 동기는 무엇인가요?",
    "본인의 가장 큰 강점은 무엇인가요?",
    "지원한 직무에 본인이 적합하다고 생각하는 이유는 무엇인가요?",
    "최근에 어려운 문제를 해결했던 경험을 말해주세요.",
    "5년 후 본인의 모습을 어떻게 그리고 있나요?",
]


def _bank_questions(job_role: str, level: str, career: str):
    """정적 질문은행에서 6개를 뽑는다(배포 서버엔 Ollama가 없으므로 즉시 반환).
    오류 시 기본 질문으로 대체."""
    try:
        return question_bank.build_questions(
            job_role=job_role, level=level, n=6, career=career)
    except Exception as e:
        print(f"[경고] 질문 생성 오류, 기본 질문 대체: {e}")
        return list(DEFAULT_QUESTIONS)


def _optional_user_id(authorization: str | None, db: Session) -> int | None:
    """Authorization 헤더가 있고 유효하면 user_id, 아니면 None (인증 강제 안 함)."""
    if not authorization or not authorization.lower().startswith("bearer "):
        return None
    token = authorization.split(" ", 1)[1].strip()
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        user_id = payload.get("sub")
        if user_id is None:
            return None
        user = db.query(models.User).filter(models.User.user_id == int(user_id)).first()
        return user.user_id if user else None
    except (JWTError, ValueError):
        return None


@app.post("/api/questions")
def make_questions(req: QuestionRequest,
                   authorization: str | None = Header(None),
                   db: Session = Depends(get_db)):
    """질문 생성.
    - resume_text 없음 → 기존처럼 질문은행 6개 즉시 반환
    - resume_text 있음 + 워커 온라인 → question_jobs에 등록하고 {job_id, status} 반환
      (프론트는 GET /api/question-result/{job_id} 폴링)
    - resume_text 있음 + 워커 오프라인(최근 5분 하트비트 없음) → 무한 대기 방지를 위해
      질문은행 6개 즉시 반환
    """
    job_role = f"{req.job} {req.sub}".strip()
    resume_text = (req.resume_text or "").strip()

    if not resume_text:
        questions = _bank_questions(job_role, req.level, req.career)
        return {"job_role": job_role, "level": req.level, "career": req.career,
                "questions": questions}

    # 자소서 기반 질문은 개인정보가 담기므로 로그인 사용자만 생성·조회할 수 있다
    user_id = _optional_user_id(authorization, db)
    if user_id is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                            detail="자기소개서 맞춤 질문은 로그인 후 이용할 수 있어요.",
                            headers={"WWW-Authenticate": "Bearer"})

    if not _worker_online():
        print("[질문] 워커 오프라인 → 자소서 질문 대신 질문은행으로 즉시 응답")
        questions = _bank_questions(job_role, req.level, req.career)
        return {"job_role": job_role, "level": req.level, "career": req.career,
                "questions": questions, "fallback": "worker_offline"}

    qjob = models.QuestionJob(
        user_id=user_id,
        job=req.job,
        sub=req.sub,
        level=req.level,
        career=req.career,
        resume_text=resume_text[:RESUME_MAX_TEXT_CHARS],
        status="pending",
    )
    db.add(qjob)
    db.commit()
    db.refresh(qjob)
    return {"job_id": qjob.id, "status": "pending"}


@app.get("/api/question-result/{job_id}")
def get_question_result(job_id: int,
                        current_user: models.User = Depends(get_current_user),
                        db: Session = Depends(get_db)):
    """프론트가 폴링하는 엔드포인트(타임아웃 60초 — 워커 처리 목표 15초 내). 본인 job만 조회 가능."""
    qjob = (db.query(models.QuestionJob)
            .filter(models.QuestionJob.id == job_id,
                    models.QuestionJob.user_id == current_user.user_id)
            .first())
    if qjob is None:
        raise HTTPException(status_code=404, detail="해당 질문 생성 작업을 찾을 수 없습니다.")

    questions = None
    if qjob.status == "done" and qjob.result_json:
        try:
            questions = json.loads(qjob.result_json)
        except Exception:
            questions = None
    return {"status": qjob.status, "questions": questions, "error": qjob.error}


# ════════════════════════════════════════════════════════
#  영상 분석 작업 큐 (배포 서버 ↔ PC GPU 워커 폴링 구조)
#  - 프론트: POST /api/analyze-answer 로 영상 업로드 → job_id 받고
#            GET /api/analysis-result/{job_id} 를 폴링
#  - 워커:   GET /worker/next-job → GET /worker/video/{id} →
#            POST /worker/result/{id}
# ════════════════════════════════════════════════════════

# 영상 저장 폴더 (환경변수 VIDEO_DIR로 변경 가능, 예: /home/ubuntu/videos)
VIDEO_DIR = os.environ.get("VIDEO_DIR", "videos")

# 워커 인증 키 (서버 환경변수 WORKER_KEY 필수)
WORKER_KEY = _required_secret("WORKER_KEY", "coachcoach-worker-dev-key")

# 영상 업로드 상한 (답변 1개 = 보통 수십 MB 이하). 환경변수 MAX_VIDEO_MB로 변경 가능
MAX_VIDEO_BYTES = int(os.environ.get("MAX_VIDEO_MB", "150")) * 1024 * 1024
# 사용자 1명이 동시에 쌓아둘 수 있는 대기/처리 중 분석 작업 수 (면접 1회 = 최대 6문항 + 재시도 여유)
MAX_ACTIVE_JOBS_PER_USER = 8
# 완료되지 못한 채 남은 영상 파일을 정리하는 기준 (시간)
STALE_VIDEO_HOURS = 6

# processing 상태로 이 시간(분)을 넘기면 워커가 죽은 것으로 보고 pending 복구
STUCK_JOB_MINUTES = 10
# 질문 생성 job은 짧지만 Ollama 모델 첫 로딩이 1~3분 걸릴 수 있어 5분 넘게 processing이면 pending 복구
STUCK_QUESTION_JOB_MINUTES = 5

# ── 워커 하트비트: 워커가 /worker/next-job·/worker/next-question-job을 폴링할 때마다 갱신.
#    최근 5분 내 폴링이 없으면 오프라인으로 보고 자소서 질문은 질문은행으로 즉시 폴백한다.
WORKER_ONLINE_MINUTES = 5
_worker_last_seen: datetime | None = None


def _touch_worker():
    global _worker_last_seen
    _worker_last_seen = datetime.now(timezone.utc)


def _worker_online(db: Session | None = None) -> bool:
    if _worker_last_seen is not None and \
            datetime.now(timezone.utc) - _worker_last_seen < timedelta(minutes=WORKER_ONLINE_MINUTES):
        return True
    # 워커가 긴 영상을 분석하느라 폴링이 끊긴 경우: 복구 기준 안에 가져간 작업이 있으면 살아 있는 것으로 본다
    if db is not None:
        cutoff = datetime.utcnow() - timedelta(minutes=STUCK_JOB_MINUTES)
        busy = (db.query(models.AnalysisJob.id)
                .filter(models.AnalysisJob.status == "processing",
                        models.AnalysisJob.processing_started_at >= cutoff)
                .first())
        return busy is not None
    return False


def verify_worker_key(x_worker_key: str = Header(None)):
    if not x_worker_key or not hmac.compare_digest(x_worker_key.encode(), WORKER_KEY.encode()):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                            detail="워커 인증 키가 올바르지 않습니다.")


@app.get("/api/worker-status")
def worker_status(db: Session = Depends(get_db)):
    """프론트가 '맞춤 질문 가능' 표시에 사용."""
    return {"online": _worker_online(db), "last_seen": _utc_iso(_worker_last_seen)}


@app.post("/worker/heartbeat")
def worker_heartbeat(_=Depends(verify_worker_key)):
    """워커가 긴 분석 중에도 살아 있음을 알린다 (분석 스레드와 별도로 주기 호출)."""
    _touch_worker()
    return {"ok": True}


def _delete_job_video(job: "models.AnalysisJob"):
    """분석이 끝난 영상 파일을 디스크에서 지운다 (무료 서버 디스크 보호)."""
    if job.video_path:
        try:
            if os.path.exists(job.video_path):
                os.remove(job.video_path)
        except Exception as e:
            print(f"[작업큐] 영상 삭제 실패 (job {job.id}): {e}")


def _cleanup_stale_videos(db: Session):
    """오래도록 끝나지 않은 작업의 영상을 지우고 실패 처리한다 (워커 장기 중단 시 디스크 보호)."""
    cutoff = datetime.utcnow() - timedelta(hours=STALE_VIDEO_HOURS)
    stale = (db.query(models.AnalysisJob)
             .filter(models.AnalysisJob.status.in_(("pending", "processing")),
                     models.AnalysisJob.created_at < cutoff)
             .all())
    for job in stale:
        job.status = "failed"
        job.error = "분석이 오래 지연되어 취소되었어요. 다시 답변해주세요."
        _delete_job_video(job)
    if stale:
        db.commit()


@app.post("/api/analyze-answer")
async def analyze_answer(
    video: UploadFile = File(...),
    question: str = Form(...),
    job_role: str = Form("일반 직무"),
    career: str = Form("신입"),
    current_user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    # 영상을 서버 디스크에 저장하고 작업 큐에 등록한다.
    # 실제 분석은 내 PC의 GPU 워커가 /worker/* API로 가져가서 처리한다.
    if not _worker_online(db):
        # 워커가 꺼져 있으면 영상을 쌓아두지 않고 바로 알려준다 (사용자가 5분 기다리지 않게)
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                            detail="지금은 AI 분석 서버가 꺼져 있어요. 잠시 후 다시 시도해주세요.")

    _cleanup_stale_videos(db)  # 오래된 작업부터 정리해야 만료 작업이 상한을 영영 막지 않는다

    active = (db.query(models.AnalysisJob)
              .filter(models.AnalysisJob.user_id == current_user.user_id,
                      models.AnalysisJob.status.in_(("pending", "processing")))
              .count())
    if active >= MAX_ACTIVE_JOBS_PER_USER:
        raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                            detail="분석 대기 중인 답변이 너무 많아요. 이전 분석이 끝난 뒤 다시 시도해주세요.")

    os.makedirs(VIDEO_DIR, exist_ok=True)
    ext = os.path.splitext(video.filename or "")[1].lower()
    if ext not in (".webm", ".mp4", ".mkv", ".mov"):
        ext = ".webm"
    video_path = os.path.join(VIDEO_DIR, f"{uuid.uuid4().hex}{ext}")

    # 메모리에 한 번에 올리지 않고 1MB씩 디스크에 쓰면서 크기 상한을 검사한다
    written = 0
    try:
        with open(video_path, "wb") as f:
            while True:
                chunk = await video.read(1024 * 1024)
                if not chunk:
                    break
                written += len(chunk)
                if written > MAX_VIDEO_BYTES:
                    raise HTTPException(
                        status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                        detail=f"영상이 너무 커요. {MAX_VIDEO_BYTES // (1024 * 1024)}MB 이하로 답변해주세요.")
                f.write(chunk)
    except BaseException:
        if os.path.exists(video_path):
            os.remove(video_path)
        raise
    if written == 0:
        os.remove(video_path)
        raise HTTPException(status_code=400, detail="녹화된 영상이 비어 있어요. 다시 답변해주세요.")

    job = models.AnalysisJob(
        user_id=current_user.user_id,
        question=question,
        job_role=job_role,
        career=career,
        video_path=video_path,
        status="pending",
    )
    db.add(job)
    db.commit()
    db.refresh(job)
    return {"job_id": job.id, "status": "pending"}


@app.get("/api/analysis-result/{job_id}")
def get_analysis_result(job_id: int,
                        current_user: models.User = Depends(get_current_user),
                        db: Session = Depends(get_db)):
    # 프론트가 폴링하는 엔드포인트. 본인 job만 조회 가능.
    job = (db.query(models.AnalysisJob)
           .filter(models.AnalysisJob.id == job_id,
                   models.AnalysisJob.user_id == current_user.user_id)
           .first())
    if job is None:
        raise HTTPException(status_code=404, detail="해당 분석 작업을 찾을 수 없습니다.")

    result = None
    if job.status == "done" and job.result_json:
        try:
            result = json.loads(job.result_json)
        except Exception:
            result = None
    return {"status": job.status, "result": result, "error": job.error}


@app.get("/worker/next-job")
def worker_next_job(db: Session = Depends(get_db),
                    _=Depends(verify_worker_key)):
    _touch_worker()  # 하트비트 갱신
    # 1) 오래 물고 있는(stuck) processing job 복구
    cutoff = datetime.utcnow() - timedelta(minutes=STUCK_JOB_MINUTES)
    stuck_jobs = (db.query(models.AnalysisJob)
                  .filter(models.AnalysisJob.status == "processing")
                  .all())
    recovered = False
    for sj in stuck_jobs:
        started = sj.processing_started_at
        if started is not None and started.tzinfo is not None:
            started = started.replace(tzinfo=None)
        if started is None or started < cutoff:
            sj.status = "pending"
            sj.processing_started_at = None
            recovered = True
    if recovered:
        db.commit()

    # 2) pending 중 가장 오래된 job 1개를 워커에게 배정
    job = _claim_next(db, models.AnalysisJob)
    if job is None:
        return Response(status_code=status.HTTP_204_NO_CONTENT)
    return {
        "job_id": job.id,
        "claim_token": job.claim_token,
        "question": job.question,
        "job_role": job.job_role,
        "career": job.career or "신입",
        "video_url": f"/worker/video/{job.id}",
    }


def _claim_next(db: Session, model):
    """pending job 1개를 원자적으로 processing으로 바꾸고 배정 토큰을 붙여 반환한다.
    여러 워커가 동시에 요청해도 조건부 UPDATE가 성공한 쪽만 job을 가져간다."""
    for _ in range(5):
        candidate = (db.query(model.id)
                     .filter(model.status == "pending")
                     .order_by(model.id.asc())
                     .first())
        if candidate is None:
            return None
        token = uuid.uuid4().hex
        claimed = (db.query(model)
                   .filter(model.id == candidate.id, model.status == "pending")
                   .update({model.status: "processing",
                            model.processing_started_at: datetime.utcnow(),
                            model.claim_token: token},
                           synchronize_session=False))
        db.commit()
        if claimed == 1:
            return db.query(model).filter(model.id == candidate.id).first()
    return None


def _accept_result(job, claim_token: str | None):
    """지금 배정(processing)된 결과만 받는다. 재배정 전 워커의 늦은 결과는 거절한다.
    (구버전 워커는 claim_token을 보내지 않으므로, 그 경우엔 processing 상태만 확인)"""
    if job.status != "processing":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT,
                            detail="이미 처리되었거나 다시 대기열로 돌아간 작업입니다.")
    if claim_token is not None and claim_token != job.claim_token:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT,
                            detail="다른 워커에게 다시 배정된 작업입니다.")


@app.get("/worker/video/{job_id}")
def worker_get_video(job_id: int,
                     db: Session = Depends(get_db),
                     _=Depends(verify_worker_key)):
    job = db.query(models.AnalysisJob).filter(models.AnalysisJob.id == job_id).first()
    if job is None:
        raise HTTPException(status_code=404, detail="해당 작업이 없습니다.")
    if not job.video_path or not os.path.exists(job.video_path):
        raise HTTPException(status_code=404, detail="영상 파일이 없습니다.")
    return FileResponse(job.video_path, media_type="application/octet-stream",
                        filename=os.path.basename(job.video_path))


class WorkerResult(BaseModel):
    ok: bool
    result: dict | None = None
    error: str | None = None
    claim_token: str | None = None


def _clamp_score(v):
    """점수는 0~100 정수로 맞춘다. 숫자가 아니면 None(측정 실패)."""
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    return max(0, min(100, round(v)))


@app.post("/worker/result/{job_id}")
def worker_post_result(job_id: int,
                       payload: WorkerResult,
                       db: Session = Depends(get_db),
                       _=Depends(verify_worker_key)):
    job = db.query(models.AnalysisJob).filter(models.AnalysisJob.id == job_id).first()
    if job is None:
        raise HTTPException(status_code=404, detail="해당 작업이 없습니다.")
    _accept_result(job, payload.claim_token)

    if payload.ok:
        result = dict(payload.result or {})
        for k in ("posture_score", "content_score"):
            if k in result:
                result[k] = _clamp_score(result[k])
        job.status = "done"
        job.result_json = json.dumps(result, ensure_ascii=False)
        job.error = None
    else:
        job.status = "failed"
        job.error = payload.error or "워커에서 알 수 없는 오류가 발생했습니다."
    db.commit()

    # 분석이 끝났으니 영상은 즉시 삭제 (디스크 보호)
    _delete_job_video(job)
    return {"message": "결과 저장 완료", "job_id": job.id, "status": job.status}


# ════════════════════════════════════════════════════════
#  자소서 맞춤 질문 생성 작업 큐 (배포 서버 ↔ PC 워커, Ollama는 워커에만 있음)
#  - 프론트: POST /api/questions(resume_text 포함) → job_id 받고
#            GET /api/question-result/{job_id} 를 폴링
#  - 워커:   GET /worker/next-question-job → POST /worker/question-result/{id}
# ════════════════════════════════════════════════════════
@app.get("/worker/next-question-job")
def worker_next_question_job(db: Session = Depends(get_db),
                             _=Depends(verify_worker_key)):
    _touch_worker()  # 하트비트 갱신
    # 1) 2분 넘게 processing인 job은 pending 복구
    cutoff = datetime.utcnow() - timedelta(minutes=STUCK_QUESTION_JOB_MINUTES)
    stuck = (db.query(models.QuestionJob)
             .filter(models.QuestionJob.status == "processing")
             .all())
    recovered = False
    for sj in stuck:
        started = sj.processing_started_at
        if started is not None and started.tzinfo is not None:
            started = started.replace(tzinfo=None)
        if started is None or started < cutoff:
            sj.status = "pending"
            sj.processing_started_at = None
            recovered = True
    if recovered:
        db.commit()

    # 2) pending 중 가장 오래된 job 1개 배정
    qjob = _claim_next(db, models.QuestionJob)
    if qjob is None:
        return Response(status_code=status.HTTP_204_NO_CONTENT)
    return {
        "job_id": qjob.id,
        "claim_token": qjob.claim_token,
        "job": qjob.job or "",
        "sub": qjob.sub or "",
        "level": qjob.level or "중",
        "career": qjob.career or "신입",
        "resume_text": qjob.resume_text or "",
    }


class WorkerQuestionResult(BaseModel):
    ok: bool
    questions: list[str] | None = None
    error: str | None = None
    claim_token: str | None = None


@app.post("/worker/question-result/{job_id}")
def worker_post_question_result(job_id: int,
                                payload: WorkerQuestionResult,
                                db: Session = Depends(get_db),
                                _=Depends(verify_worker_key)):
    qjob = db.query(models.QuestionJob).filter(models.QuestionJob.id == job_id).first()
    if qjob is None:
        raise HTTPException(status_code=404, detail="해당 작업이 없습니다.")
    _accept_result(qjob, payload.claim_token)

    if payload.ok and payload.questions:
        qjob.status = "done"
        qjob.result_json = json.dumps(list(payload.questions), ensure_ascii=False)
        qjob.error = None
    else:
        qjob.status = "failed"
        qjob.error = payload.error or "워커에서 질문을 생성하지 못했습니다."
    db.commit()
    return {"message": "결과 저장 완료", "job_id": qjob.id, "status": qjob.status}


# ════════════════════════════════════════════════════════
#  회원가입
# ════════════════════════════════════════════════════════
class SignupRequest(BaseModel):
    email: str
    password: str


@app.post("/signup")
def signup(req: SignupRequest, db: Session = Depends(get_db)):
    exists = db.query(models.User).filter(models.User.email == req.email).first()
    if exists:
        raise HTTPException(status_code=400, detail="이미 가입된 이메일입니다.")
    hashed = pwd_context.hash(req.password)
    new_user = models.User(email=req.email, password_hash=hashed)
    db.add(new_user)
    db.commit()
    db.refresh(new_user)
    return {"message": "회원가입 완료", "user_id": new_user.user_id, "email": new_user.email}


# ════════════════════════════════════════════════════════
#  로그인
# ════════════════════════════════════════════════════════
class LoginRequest(BaseModel):
    email: str
    password: str


@app.post("/login")
def login(req: LoginRequest, db: Session = Depends(get_db)):
    user = db.query(models.User).filter(models.User.email == req.email).first()
    if not user:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                            detail="이메일 또는 비밀번호가 올바르지 않습니다.")
    if not pwd_context.verify(req.password, user.password_hash):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                            detail="이메일 또는 비밀번호가 올바르지 않습니다.")
    expire = datetime.utcnow() + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    payload = {"sub": str(user.user_id), "email": user.email, "exp": expire}
    token = jwt.encode(payload, SECRET_KEY, algorithm=ALGORITHM)
    return {"access_token": token, "token_type": "bearer", "user_id": user.user_id}


# ════════════════════════════════════════════════════════
#  내 정보 확인
# ════════════════════════════════════════════════════════
@app.get("/me")
def read_me(current_user: models.User = Depends(get_current_user)):
    return {"user_id": current_user.user_id, "email": current_user.email,
            "created_at": current_user.created_at}


# ════════════════════════════════════════════════════════
#  면접 결과 저장
# ════════════════════════════════════════════════════════
class ResultIn(BaseModel):
    question: str = Field("", max_length=1000)
    answer_stt: str = Field("", max_length=20000)
    posture_score: int | None = Field(None, ge=0, le=100)   # None = 측정 실패 (0점과 구분)
    content_score: int | None = Field(None, ge=0, le=100)
    feedback: str = Field("", max_length=20000)
    model_answer: str = Field("", max_length=20000)
    duration_sec: int = Field(0, ge=0, le=36000)
    filler_count: int = Field(0, ge=0, le=10000)

class SessionIn(BaseModel):
    job: str = Field("", max_length=100)
    sub_job: str = Field("", max_length=100)
    company: str | None = Field(None, max_length=100)   # 지원 회사 (선택, 없거나 빈 문자열이면 null 저장)
    level: str = Field("중", max_length=10)
    career: str | None = Field(None, max_length=10)     # 신입/경력 (추천 '같은 조건' 복원용)
    results: list[ResultIn] = Field(default_factory=list, max_length=30)


def _avg(values):
    """측정된 값(None 제외)만 평균. 하나도 없으면 None."""
    vals = [v for v in values if v is not None]
    # 파이썬 round()는 .5를 짝수로 보내므로(66.5→66) 프론트(Math.round, 66.5→67)와 맞게 반올림한다
    return math.floor(sum(vals) / len(vals) + 0.5) if vals else None


@app.post("/interview/finish")
def finish_interview(payload: SessionIn,
                     current_user: models.User = Depends(get_current_user),
                     db: Session = Depends(get_db)):
    if not payload.results:
        raise HTTPException(status_code=400, detail="저장할 답변 결과가 없어요.")
    # 측정 실패(None)는 0점으로 치지 않고 평균에서 뺀다 (결과 화면과 같은 기준)
    posture_avg = _avg(r.posture_score for r in payload.results)
    content_avg = _avg(r.content_score for r in payload.results)
    total = _avg((posture_avg, content_avg))

    company = (payload.company or "").strip() or None  # 빈 문자열이면 null 저장
    career = payload.career if payload.career in ("신입", "경력") else None

    session = models.InterviewSession(
        user_id=current_user.user_id,
        job=payload.job,
        sub_job=payload.sub_job,
        company=company,
        career=career,
        level=payload.level,
        posture_score=posture_avg,
        content_score=content_avg,
        total_score=total,
    )
    db.add(session)
    db.commit()
    db.refresh(session)

    for r in payload.results:
        db.add(models.QuestionResult(
            session_id=session.session_id,
            question=r.question,
            answer_stt=r.answer_stt,
            posture_score=r.posture_score,
            content_score=r.content_score,
            feedback=r.feedback,
            model_answer=r.model_answer,
            duration_sec=r.duration_sec,
            filler_count=r.filler_count,
        ))
    db.commit()

    return {"message": "면접 기록 저장 완료",
            "session_id": session.session_id,
            "total_score": total}


# ════════════════════════════════════════════════════════
#  내 면접 기록 목록 보기
# ════════════════════════════════════════════════════════
@app.get("/history")
def get_history(current_user: models.User = Depends(get_current_user),
                db: Session = Depends(get_db)):
    sessions = (db.query(models.InterviewSession)
                .filter(models.InterviewSession.user_id == current_user.user_id)
                .order_by(models.InterviewSession.created_at.desc())
                .all())
    return [{
        "session_id": s.session_id,
        "job": s.job,
        "sub_job": s.sub_job,
        "company": s.company,
        "career": s.career,
        "level": s.level,
        "posture_score": s.posture_score,
        "content_score": s.content_score,
        "total_score": s.total_score,
        "created_at": _utc_iso(s.created_at),
    } for s in sessions]


# ════════════════════════════════════════════════════════
#  성장 곡선 데이터
# ════════════════════════════════════════════════════════
@app.get("/growth")
def get_growth(current_user: models.User = Depends(get_current_user),
               db: Session = Depends(get_db)):
    sessions = (db.query(models.InterviewSession)
                .filter(models.InterviewSession.user_id == current_user.user_id)
                .order_by(models.InterviewSession.created_at.asc())
                .all())

    points = []
    for i, s in enumerate(sessions, start=1):
        points.append({
            "round": i,
            "session_id": s.session_id,
            "posture_score": s.posture_score,
            "content_score": s.content_score,
            "total_score": s.total_score,
            "created_at": _utc_iso(s.created_at),
        })

    improvement = None
    if len(points) >= 2:
        def _diff(key):
            a, b = points[0][key], points[-1][key]
            return None if a is None or b is None else b - a
        improvement = {
            "posture": _diff("posture_score"),
            "content": _diff("content_score"),
            "total":   _diff("total_score"),
        }

    return {"count": len(points), "points": points, "improvement": improvement}


# ════════════════════════════════════════════════════════
#  특정 면접 상세 보기
# ════════════════════════════════════════════════════════
@app.get("/history/{session_id}")
def get_history_detail(session_id: int,
                       current_user: models.User = Depends(get_current_user),
                       db: Session = Depends(get_db)):
    session = (db.query(models.InterviewSession)
               .filter(models.InterviewSession.session_id == session_id,
                       models.InterviewSession.user_id == current_user.user_id)
               .first())
    if session is None:
        raise HTTPException(status_code=404, detail="해당 면접 기록을 찾을 수 없습니다.")

    results = (db.query(models.QuestionResult)
               .filter(models.QuestionResult.session_id == session_id)
               .all())

    return {
        "session": {
            "session_id": session.session_id,
            "job": session.job,
            "sub_job": session.sub_job,
            "company": session.company,
            "career": session.career,
            "level": session.level,
            "posture_score": session.posture_score,
            "content_score": session.content_score,
            "total_score": session.total_score,
            "created_at": _utc_iso(session.created_at),
        },
        "results": [{
            "result_id": r.result_id,
            "question": r.question,
            "answer_stt": r.answer_stt,
            "posture_score": r.posture_score,
            "content_score": r.content_score,
            "feedback": r.feedback,
            "model_answer": r.model_answer,
            "duration_sec": r.duration_sec,
            "filler_count": r.filler_count,
        } for r in results],
    }


# ════════════════════════════════════════════════════════
#  자소서 파일 업로드 파싱
# ════════════════════════════════════════════════════════
RESUME_MAX_FILE_BYTES = 5 * 1024 * 1024   # 5MB
RESUME_MAX_TEXT_CHARS = 10_000
RESUME_MAX_XML_BYTES = 20 * 1024 * 1024   # docx 본문 XML 압축 해제 상한
RESUME_MAX_PDF_PAGES = 30
RESUME_EMPTY_TEXT_MSG = "파일에서 텍스트를 찾지 못했어요. 내용을 직접 붙여넣어 주세요."


def _extract_text_from_txt(data: bytes) -> str:
    for enc in ("utf-8", "cp949"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    raise ValueError(RESUME_EMPTY_TEXT_MSG)


def _extract_text_from_docx(data: bytes) -> str:
    # .docx는 zip 압축 파일 — 표준 라이브러리만으로 word/document.xml에서 텍스트 추출
    W_NS = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            info = zf.getinfo("word/document.xml")
            # 압축 폭탄 방지: 풀었을 때 크기를 먼저 확인하고, 실제로도 상한까지만 읽는다
            if info.file_size > RESUME_MAX_XML_BYTES:
                raise ValueError("파일 내용이 너무 커요. 내용을 직접 붙여넣어 주세요.")
            with zf.open(info) as fp:
                xml_bytes = fp.read(RESUME_MAX_XML_BYTES + 1)
            if len(xml_bytes) > RESUME_MAX_XML_BYTES:
                raise ValueError("파일 내용이 너무 커요. 내용을 직접 붙여넣어 주세요.")
        root = ET.fromstring(xml_bytes)
    except (zipfile.BadZipFile, KeyError, ET.ParseError):
        raise ValueError(RESUME_EMPTY_TEXT_MSG)

    paragraphs = []
    for p in root.iter(f"{W_NS}p"):
        parts = []
        for node in p.iter():
            if node.tag == f"{W_NS}t" and node.text:
                parts.append(node.text)
            elif node.tag in (f"{W_NS}tab",):
                parts.append("\t")
            elif node.tag in (f"{W_NS}br", f"{W_NS}cr"):
                parts.append("\n")
        paragraphs.append("".join(parts))
    return "\n".join(paragraphs)


def _extract_text_from_pdf(data: bytes) -> str:
    try:
        from pypdf import PdfReader
    except ImportError:
        raise ValueError("PDF는 지원 준비 중이에요. txt 또는 docx 파일을 이용해 주세요.")
    try:
        reader = PdfReader(io.BytesIO(data))
        if len(reader.pages) > RESUME_MAX_PDF_PAGES:
            raise ValueError(f"PDF는 {RESUME_MAX_PDF_PAGES}쪽 이하만 올려주세요.")
        pages = [(page.extract_text() or "") for page in reader.pages]
    except ValueError:
        raise
    except Exception:
        raise ValueError(RESUME_EMPTY_TEXT_MSG)
    return "\n".join(pages)


def extract_resume_text(filename: str, data: bytes) -> str:
    """업로드된 자소서 파일(bytes)에서 텍스트를 추출해 정리(공백 제거, 10,000자 절단)해서 반환.

    실패 시 사용자에게 보여줄 한국어 메시지를 담은 ValueError를 던진다.
    """
    name = (filename or "").lower()
    if name.endswith(".txt"):
        text = _extract_text_from_txt(data)
    elif name.endswith(".docx"):
        text = _extract_text_from_docx(data)
    elif name.endswith(".pdf"):
        text = _extract_text_from_pdf(data)
    else:
        raise ValueError("지원하지 않는 파일 형식이에요. txt, docx, pdf 파일만 올려주세요.")

    text = text.strip()
    if not text:
        raise ValueError(RESUME_EMPTY_TEXT_MSG)
    return text[:RESUME_MAX_TEXT_CHARS]


@app.post("/api/parse-resume")
async def parse_resume(file: UploadFile = File(...),
                       current_user: models.User = Depends(get_current_user)):
    data = await file.read(RESUME_MAX_FILE_BYTES + 1)  # 상한 넘게는 읽지 않는다
    if len(data) > RESUME_MAX_FILE_BYTES:
        raise HTTPException(status_code=400,
                            detail="파일이 너무 커요. 5MB 이하 파일만 올려주세요.")
    try:
        text = extract_resume_text(file.filename or "", data)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"text": text, "filename": file.filename, "chars": len(text)}


# ════════════════════════════════════════════════════════
#  면접 질문 음성 합성 (MS 뉴럴 음성 — 브라우저 TTS보다 자연스러움)
# ════════════════════════════════════════════════════════
TTS_VOICE = "ko-KR-SunHiNeural"   # 또렷한 여성 — 발주자 청음 후 확정 (2026-09-05)
TTS_RATE = "-5%"                   # 살짝 느리게 — 면접관 톤
TTS_MAX_TEXT_CHARS = 500
TTS_CACHE_MAX = 100

# 같은 질문을 여러 번 읽는 경우가 많아 text 해시로 mp3를 캐싱 (오래된 것부터 제거)
_tts_cache: "OrderedDict[str, bytes]" = OrderedDict()


class TTSRequest(BaseModel):
    text: str


@app.post("/api/tts")
async def synthesize_speech(req: TTSRequest,
                            current_user: models.User = Depends(get_current_user)):
    text = req.text.strip()
    if not text:
        raise HTTPException(status_code=400, detail="읽을 텍스트가 비어 있어요.")
    if len(text) > TTS_MAX_TEXT_CHARS:
        raise HTTPException(status_code=400,
                            detail=f"텍스트가 너무 길어요. {TTS_MAX_TEXT_CHARS}자 이하로 보내주세요.")

    key = hashlib.sha256(f"{TTS_VOICE}|{TTS_RATE}|{text}".encode("utf-8")).hexdigest()
    cached = _tts_cache.get(key)
    if cached is not None:
        _tts_cache.move_to_end(key)  # 최근 사용으로 갱신
        return Response(content=cached, media_type="audio/mpeg")

    try:
        import edge_tts
        communicate = edge_tts.Communicate(text, TTS_VOICE, rate=TTS_RATE)
        chunks = []
        async for chunk in communicate.stream():
            if chunk["type"] == "audio":
                chunks.append(chunk["data"])
        audio = b"".join(chunks)
        if not audio:
            raise RuntimeError("빈 오디오 응답")
    except Exception as e:
        # 오프라인 등 네트워크 실패 — 프론트가 브라우저 TTS로 폴백할 수 있게 503
        print(f"[TTS 오류] {e}")
        raise HTTPException(status_code=503, detail="음성 생성에 실패했어요")

    _tts_cache[key] = audio
    while len(_tts_cache) > TTS_CACHE_MAX:
        _tts_cache.popitem(last=False)  # 가장 오래된 항목 제거

    return Response(content=audio, media_type="audio/mpeg")
