from fastapi import FastAPI, UploadFile, File, Form, Depends, HTTPException, status, Header, Response, Query
from fastapi.responses import FileResponse
from fastapi.security import OAuth2PasswordBearer
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session
from sqlalchemy import or_
from sqlalchemy.exc import IntegrityError
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
import zlib
import xml.etree.ElementTree as ET
from collections import OrderedDict

import question_bank
import job_postings
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
                if cols and "attempts" not in [c[1] for c in cols]:
                    conn.execute(text(
                        f"ALTER TABLE {table} ADD COLUMN attempts INTEGER NOT NULL DEFAULT 0"))
    except Exception as e:
        print(f"[마이그레이션 경고] claim_token 컬럼 추가 실패 (서버는 계속 뜸): {e}")


def _migrate_add_user_name_column(target_engine=engine):
    """users.name(표시 이름) 컬럼이 없으면 추가. 실패해도 서버 기동은 막지 않는다."""
    try:
        from sqlalchemy import text
        with target_engine.begin() as conn:
            cols = [c[1] for c in conn.execute(text("PRAGMA table_info(users)")).fetchall()]
            if cols and "name" not in cols:
                conn.execute(text("ALTER TABLE users ADD COLUMN name TEXT"))
                print("[마이그레이션] users.name 컬럼 추가 완료")
    except Exception as e:
        print(f"[마이그레이션 경고] users.name 컬럼 추가 실패 (서버는 계속 뜸): {e}")


def _migrate_loop_columns(target_engine=engine):
    """루프 기능(말문 트기)용 컬럼을 기존 DB에 추가한다. 실패하면 서버 기동을 중단한다(11/06 GPT 리뷰 반영).
    - interview_sessions.mode / analysis_jobs.mode : 'normal'(기본) / 'practice'
    - question_jobs.with_answers / answers_json   : 모범답안 생성 여부·결과"""
    plan = {
        "interview_sessions": {"mode": "TEXT DEFAULT 'normal'"},
        "analysis_jobs": {"mode": "TEXT DEFAULT 'normal'"},
        "question_jobs": {"with_answers": "INTEGER NOT NULL DEFAULT 0", "answers_json": "TEXT"},
        "question_results": {"first_voice_ms": "INTEGER"},  # 11/07 효과 측정: 첫 마디까지 걸린 시간
        "calm_logs": {"kind": "TEXT"},                       # 11/07 루틴 종류: full / short
    }
    try:
        from sqlalchemy import text
        with target_engine.begin() as conn:
            for table, cols_needed in plan.items():
                cols = [c[1] for c in conn.execute(text(f"PRAGMA table_info({table})")).fetchall()]
                if not cols:
                    continue
                for name, ddl in cols_needed.items():
                    if name not in cols:
                        conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}"))
                        print(f"[마이그레이션] {table}.{name} 컬럼 추가 완료")
    except Exception as e:
        # 이 컬럼들은 새 API가 바로 쓰므로, 추가에 실패하면 서버를 띄우지 않는다(조용한 반쯤 동작 방지)
        raise RuntimeError(f"[마이그레이션 실패] 루프 기능 컬럼 추가 실패, 서버 기동 중단: {e}") from e
    # 기동 전 스키마 확인: 필요한 컬럼이 실제로 다 있는지 다시 본다
    from sqlalchemy import text as _t
    with target_engine.connect() as conn:
        for table, cols_needed in plan.items():
            cols = [c[1] for c in conn.execute(_t(f"PRAGMA table_info({table})")).fetchall()]
            if cols and not set(cols_needed) <= set(cols):
                raise RuntimeError(f"[마이그레이션 실패] {table}에 {set(cols_needed) - set(cols)} 컬럼이 없음, 서버 기동 중단")


_migrate_add_company_column()
_migrate_add_user_name_column()
_migrate_add_career_column()
_migrate_question_jobs_columns()
_migrate_add_claim_token_columns()
_migrate_loop_columns()


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
    "개발":         {"ic": "💻", "subs": ["프론트엔드", "백엔드", "풀스택", "iOS", "안드로이드", "임베디드", "게임 개발", "QA·테스트"]},
    "클라우드·인프라": {"ic": "☁️", "subs": ["클라우드 엔지니어", "클라우드 아키텍트", "DevOps", "SRE", "시스템 엔지니어", "네트워크 엔지니어", "DBA"]},
    "정보보안":     {"ic": "🛡️", "subs": ["보안 관제", "모의해킹", "보안 컨설팅", "정보보호 관리", "클라우드 보안", "개인정보보호"]},
    "데이터·AI":    {"ic": "📊", "subs": ["데이터 분석", "데이터 사이언티스트", "ML·AI", "AI 엔지니어(LLM)", "데이터 엔지니어", "PM·기획"]},
    "디자인":       {"ic": "🎨", "subs": ["UI·UX", "그래픽", "제품 디자인", "영상·모션", "브랜드 디자인", "웹 디자인"]},
    "마케팅":       {"ic": "📢", "subs": ["브랜드", "퍼포먼스", "콘텐츠", "SNS", "CRM", "홍보·PR"]},
    "영업":         {"ic": "🤝", "subs": ["B2B 영업", "B2C 영업", "해외영업", "영업관리", "기술영업", "영업지원"]},
    "경영사무":     {"ic": "📋", "subs": ["인사(HR)", "총무", "재무·회계", "전략기획", "법무", "구매·자재", "서비스 기획", "사무·비서"]},
    "금융":         {"ic": "💰", "subs": ["은행", "증권·투자", "보험", "리스크·심사", "핀테크", "세무·회계사"]},
    "연구·엔지니어링": {"ic": "🔬", "subs": ["기계", "전자·전기", "화공·소재", "연구개발", "반도체", "건축·토목", "생산·품질관리", "환경·안전"]},
    "공공·행정":    {"ic": "🏛️", "subs": ["행정직", "경찰·소방", "군인·국방", "공기업", "정책·기획", "기술직 공무원", "사회복지"]},
    "교육":         {"ic": "🎓", "subs": ["교사", "강사", "교육기획", "교수·연구", "유아·보육", "상담"]},
    "의료·보건":    {"ic": "⚕️", "subs": ["간호사", "약사", "보건직", "임상·검사", "방사선사", "물리·작업치료", "영양사", "의료행정"]},
    "서비스·유통":  {"ic": "✈️", "subs": ["승무원", "호텔", "판매·MD", "외식·식음", "물류·SCM", "고객상담(CS)", "여행·관광"]},
    "미디어·콘텐츠": {"ic": "🎬", "subs": ["PD", "기자·에디터", "영상·편집", "작가·기획", "아나운서", "게임 기획"]},
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
    with_answers: bool = False   # 말문 트기: 모범답안(answers)도 함께 받기


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


def _complete_questions(raw, qjob):
    questions = []
    def add(items):
        for q in items:
            if isinstance(q, str):
                q = q.strip()
                if q and q not in questions:
                    questions.append(q)
            if len(questions) >= 6:
                break
    add(raw if isinstance(raw, list) else [])
    if len(questions) < 6:
        role = f"{qjob.job or ''} {qjob.sub or ''}".strip() or "일반 직무"
        add(_bank_questions(role, qjob.level or "중", qjob.career or "신입"))
    if len(questions) < 6:
        add(DEFAULT_QUESTIONS)
    return questions[:6]


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
        out = {"job_role": job_role, "level": req.level, "career": req.career,
               "questions": questions}
        if req.with_answers:
            out["answers"] = [None] * len(questions)   # 자소서가 없으면 근거도 없음
        return out

    # 자소서 기반 질문은 개인정보가 담기므로 로그인 사용자만 생성·조회할 수 있다
    user_id = _optional_user_id(authorization, db)
    if user_id is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                            detail="자기소개서 맞춤 질문은 로그인 후 이용할 수 있어요.",
                            headers={"WWW-Authenticate": "Bearer"})

    if not _worker_online():
        print("[질문] 워커 오프라인 → 자소서 질문 대신 질문은행으로 즉시 응답")
        questions = _bank_questions(job_role, req.level, req.career)
        out = {"job_role": job_role, "level": req.level, "career": req.career,
               "questions": questions, "fallback": "worker_offline"}
        if req.with_answers:
            out["answers"] = [None] * len(questions)
        return out

    qjob = models.QuestionJob(
        user_id=user_id,
        job=req.job,
        sub=req.sub,
        level=req.level,
        career=req.career,
        resume_text=resume_text[:RESUME_MAX_TEXT_CHARS],
        with_answers=1 if req.with_answers else 0,
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
    if qjob.status == "done":
        questions = _complete_questions(questions, qjob)
    out = {"status": qjob.status, "questions": questions, "error": qjob.error}
    if qjob.with_answers:
        answers = None
        if questions is not None:
            try:
                saved = json.loads(qjob.answers_json) if qjob.answers_json else None
            except Exception:
                saved = None
            if not isinstance(saved, list) or len(saved) != len(questions):
                saved = [None] * len(questions)
            answers = saved
        out["answers"] = answers
    return out


def _align_answers(final_questions, raw_questions, raw_answers, resume_text):
    """워커가 보낸 (질문, 답안) 짝을 최종 질문 순서에 맞춘다.
    서버에서 한 번 더 검증한다(src 원문 포함·숫자) → 어기면 null. 짝이 없는 질문(은행 보충)도 null."""
    by_q = {}
    if isinstance(raw_questions, list) and isinstance(raw_answers, list):
        for q, a in zip(raw_questions, raw_answers):
            if isinstance(q, str) and isinstance(a, dict):
                by_q[q.strip()] = question_bank.validate_model_answer(
                    a.get("text"), a.get("src"), resume_text or "")
    return [by_q.get(q) for q in final_questions]


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


@app.get("/api/postings")
def postings(job: str = "", sub: str = "", career: str = "", companies: str = "",
             scope: str = "job", limit: int = 8, region: str = ""):
    """공공기관 채용공고(잡알리오) — 내 직무·관심 회사·대전충청 기준으로 골라 준다. 공개 데이터라 로그인 불필요."""
    if scope not in ("job", "region", "local", "company"):
        scope = "job"
    comps = [c.strip() for c in companies.split(",") if c.strip()][:5]
    return job_postings.search(job=job[:30], sub=sub[:30], career=career[:5],
                               companies=comps, scope=scope, limit=limit, region=region[:10])


def _csv(v, n=20, m=20):
    return [x.strip()[:m] for x in (v or "").split(",") if x.strip()][:n]


@app.get("/api/postings/browse")
def postings_browse(jobs: str = "", subs: str = "", regions: str = "", hires: str = "", ses: str = "",
                    q: str = "", closing: int = 0, sort: str = "match", page: int = 1, size: int = 20,
                    career: str = "", local: int = 0):
    """채용공고 탐색 화면 — 직무·세부직무·지역·고용형태·신입/경력·마감·지역인재 필터 + 항목별 개수. 공개 데이터라 로그인 불필요."""
    return job_postings.browse(jobs=_csv(jobs), subs=_csv(subs, 40), regions=_csv(regions),
                               hires=_csv(hires), ses=_csv(ses), q=q[:40], closing=max(0, min(closing, 60)),
                               sort=sort[:10], page=page, size=size, career=career[:5], local=bool(local))


@app.get("/api/postings/{pid}")
def posting_detail(pid: str):
    d = job_postings.detail(pid[:20])
    if not d:
        raise HTTPException(status_code=404, detail="마감됐거나 없는 공고예요")
    return d


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
    mode: str = Form("normal"),   # "practice"(말문 트기)면 워커가 내용 평가를 건너뜀
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
        mode="practice" if mode == "practice" else "normal",
        video_path=video_path,
        status="pending",
    )
    db.add(job)
    try:
        db.commit()
    except Exception:
        db.rollback()
        _delete_job_video(job)
        raise
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
    _recover_stuck(db, models.AnalysisJob, cutoff)

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
        **({"mode": "practice"} if job.mode == "practice" else {}),   # 일반 일감은 예전과 같은 모양
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


def _recover_stuck(db: Session, model, cutoff):
    """조회 이후 완료/재배정된 작업은 조건부 UPDATE로 보호한다."""
    expired = or_(model.processing_started_at.is_(None),
                  model.processing_started_at < cutoff)
    stuck = db.query(model).filter(model.status == "processing", expired).all()
    for job in stuck:
        attempts = (job.attempts or 0) + 1
        failed = attempts >= 3
        updated = (db.query(model)
                   .filter(model.id == job.id, model.status == "processing",
                           (model.claim_token.is_(None) if job.claim_token is None
                            else model.claim_token == job.claim_token), expired)
                   .update({model.status: "failed" if failed else "pending",
                            model.attempts: attempts,
                            model.processing_started_at: None,
                            model.claim_token: None,
                            model.error: "작업이 3회 지연되어 실패했어요. 다시 시도해주세요." if failed else None},
                           synchronize_session=False))
        if updated and failed and model is models.AnalysisJob:
            _delete_job_video(job)
    if stuck:
        db.commit()


def _accept_result(db: Session, model, job_id: int, claim_token: str | None, values):
    """현재 배정 토큰과 processing 상태가 모두 일치할 때만 저장한다."""
    if not claim_token or not claim_token.strip():
        raise HTTPException(status_code=status.HTTP_409_CONFLICT,
                            detail="다른 워커에게 다시 배정된 작업입니다.")
    updated = (db.query(model)
               .filter(model.id == job_id, model.status == "processing",
                       model.claim_token == claim_token)
               .update(values, synchronize_session=False))
    if updated != 1:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT,
                            detail="이미 처리되었거나 다시 대기열로 돌아간 작업입니다.")
    db.commit()


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
    if isinstance(v, float) and not math.isfinite(v):
        return None
    return max(0, min(100, round(v)))


def _clean_numbers(value):
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, dict):
        return {k: _clean_numbers(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_clean_numbers(v) for v in value]
    return value


@app.post("/worker/result/{job_id}")
def worker_post_result(job_id: int,
                       payload: WorkerResult,
                       db: Session = Depends(get_db),
                       _=Depends(verify_worker_key)):
    job = db.query(models.AnalysisJob).filter(models.AnalysisJob.id == job_id).first()
    if job is None:
        raise HTTPException(status_code=404, detail="해당 작업이 없습니다.")

    if payload.ok:
        result = _clean_numbers(dict(payload.result or {}))
        for k in ("posture_score", "content_score"):
            if k in result:
                result[k] = _clamp_score(result[k])
        if job.mode == "practice":
            # 말문 트기: 옛 워커가 내용 평가를 해 와도 서버에서 지운다(계약: content null, practice true)
            result["content"] = None
            result["content_score"] = None
            result["practice"] = True
        values = {models.AnalysisJob.status: "done",
                  models.AnalysisJob.result_json: json.dumps(result, ensure_ascii=False, allow_nan=False),
                  models.AnalysisJob.error: None}
    else:
        values = {models.AnalysisJob.status: "failed",
                  models.AnalysisJob.error: payload.error or "워커에서 알 수 없는 오류가 발생했습니다."}
    _accept_result(db, models.AnalysisJob, job_id, payload.claim_token, values)
    db.refresh(job)

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
    # 1) 오래 물고 있는 processing job 복구
    cutoff = datetime.utcnow() - timedelta(minutes=STUCK_QUESTION_JOB_MINUTES)
    _recover_stuck(db, models.QuestionJob, cutoff)

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
        **({"with_answers": True} if qjob.with_answers else {}),   # 일반 일감은 예전과 같은 모양
    }


class WorkerQuestionResult(BaseModel):
    ok: bool
    questions: list[str] | None = None
    answers: list[dict | None] | None = None   # with_answers 일감일 때만 (questions와 같은 순서)
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

    if payload.ok:
        questions = _complete_questions(payload.questions, qjob)
        values = {models.QuestionJob.status: "done",
                  models.QuestionJob.result_json: json.dumps(questions, ensure_ascii=False, allow_nan=False),
                  models.QuestionJob.error: None}
        if qjob.with_answers:
            answers = _align_answers(questions, payload.questions, payload.answers, qjob.resume_text)
            values[models.QuestionJob.answers_json] = json.dumps(answers, ensure_ascii=False)
    else:
        values = {models.QuestionJob.status: "failed",
                  models.QuestionJob.error: payload.error or "워커에서 질문을 생성하지 못했습니다."}
    _accept_result(db, models.QuestionJob, job_id, payload.claim_token, values)
    db.refresh(qjob)
    return {"message": "결과 저장 완료", "job_id": qjob.id, "status": qjob.status}


# ════════════════════════════════════════════════════════
#  회원가입
# ════════════════════════════════════════════════════════
class SignupRequest(BaseModel):
    email: str
    password: str
    name: str = ""


def _clean_name(raw):
    """표시 이름: 앞뒤 공백 제거, 연속 공백 하나로, 1~20자"""
    name = " ".join(str(raw or "").split())
    if not name:
        raise HTTPException(status_code=400, detail="이름을 입력해주세요.")
    if len(name) > 20:
        raise HTTPException(status_code=400, detail="이름은 20자 이내로 입력해주세요.")
    return name


@app.post("/signup")
def signup(req: SignupRequest, db: Session = Depends(get_db)):
    exists = db.query(models.User).filter(models.User.email == req.email).first()
    if exists:
        raise HTTPException(status_code=400, detail="이미 가입된 이메일입니다.")
    name = _clean_name(req.name)
    hashed = pwd_context.hash(req.password)
    new_user = models.User(email=req.email, password_hash=hashed, name=name)
    db.add(new_user)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=400, detail="이미 가입된 이메일입니다.")
    db.refresh(new_user)
    return {"message": "회원가입 완료", "user_id": new_user.user_id, "email": new_user.email, "name": new_user.name}


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
    return {"access_token": token, "token_type": "bearer", "user_id": user.user_id, "name": user.name}


# ════════════════════════════════════════════════════════
#  내 정보 확인
# ════════════════════════════════════════════════════════
@app.get("/me")
def read_me(current_user: models.User = Depends(get_current_user)):
    return {"user_id": current_user.user_id, "email": current_user.email,
            "name": current_user.name, "created_at": current_user.created_at}


class NameUpdate(BaseModel):
    name: str


@app.patch("/me")
def update_me(req: NameUpdate, current_user: models.User = Depends(get_current_user),
              db: Session = Depends(get_db)):
    """표시 이름 변경 (예전 회원이 설정 화면에서 이름을 처음 넣을 때도 사용)"""
    current_user.name = _clean_name(req.name)
    db.commit()
    return {"user_id": current_user.user_id, "email": current_user.email, "name": current_user.name}


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
    first_voice_ms: int | None = Field(None, ge=0, le=600000)  # 첫 마디까지(ms), 옛 화면은 보내지 않음

class SessionIn(BaseModel):
    job: str = Field("", max_length=100)
    sub_job: str = Field("", max_length=100)
    company: str | None = Field(None, max_length=100)   # 지원 회사 (선택, 없거나 빈 문자열이면 null 저장)
    level: str = Field("중", max_length=10)
    career: str | None = Field(None, max_length=10)     # 신입/경력 (추천 '같은 조건' 복원용)
    mode: str | None = Field(None, max_length=20)       # normal(기본) / practice(말문 트기)
    results: list[ResultIn] = Field(default_factory=list, max_length=30)
    calm_log_id: int | None = None                       # 이 면접 전에 마친 긴장 풀기 루틴 기록


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
        mode="practice" if payload.mode == "practice" else "normal",
        posture_score=posture_avg,
        content_score=content_avg,
        total_score=total,
    )
    db.add(session)
    db.flush()

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
            first_voice_ms=r.first_voice_ms,
        ))
    # 면접 전에 마친 루틴 기록과 연결 (내 기록이고 아직 연결 안 된 것만)
    if payload.calm_log_id:
        cl = db.query(models.CalmLog).filter(models.CalmLog.id == payload.calm_log_id,
                                             models.CalmLog.user_id == current_user.user_id,
                                             models.CalmLog.session_id.is_(None)).first()
        if cl:
            cl.session_id = session.session_id
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
        "mode": s.mode or "normal",
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
            "mode": session.mode or "normal",
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
    except (zipfile.BadZipFile, zipfile.LargeZipFile, KeyError, ET.ParseError,
            NotImplementedError, RuntimeError, OSError, EOFError, zlib.error):
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
TTS_VOICE = "ko-KR-HyunsuMultilingualNeural"   # 차분한 남성 — 발주자 청음 후 확정 (2026-11-04), 이전: SunHiNeural
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


# ════════════════════════════════════════════════════════
#  루프 기능: 목표 → 데일리 플래너 → 달성 소식 → 연속 기록·주간 퀘스트
#  날짜는 'YYYY-MM-DD', 기준은 한국 시간(KST)
# ════════════════════════════════════════════════════════
KST = timezone(timedelta(hours=9))
LOOP_TEXT_MAX = 200
FEED_MESSAGE_MAX = 300


def _kst_today():
    return datetime.now(KST).date()


def _to_kst_date(dt):
    """DB created_at(naive UTC) → KST 날짜."""
    if dt is None:
        return None
    if isinstance(dt, str):
        try:
            dt = datetime.fromisoformat(dt.replace("Z", "+00:00"))
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(KST).date()


def _check_date(value, field="date"):
    """'YYYY-MM-DD' 형식 확인. 틀리면 400."""
    if value is None:
        return None
    v = str(value).strip()
    try:
        datetime.strptime(v, "%Y-%m-%d")
    except ValueError:
        raise HTTPException(status_code=400, detail=f"{field}는 YYYY-MM-DD 형식이어야 해요.")
    return v


def _check_text(value, field, max_len=LOOP_TEXT_MAX):
    v = " ".join(str(value or "").split())
    if not v:
        raise HTTPException(status_code=400, detail=f"{field}을(를) 입력해주세요.")
    if len(v) > max_len:
        raise HTTPException(status_code=400, detail=f"{field}은(는) {max_len}자 이내로 입력해주세요.")
    return v


def _mask_name(name):
    """'나영성' → '나*성', '김철' → '김*', 이름이 없으면 '익명'."""
    n = (name or "").strip()
    if not n:
        return "익명"
    if len(n) == 1:
        return "*"
    if len(n) == 2:
        return n[0] + "*"
    return n[0] + "*" * (len(n) - 2) + n[-1]


def _goal_out(g):
    return {"id": g.id, "job": g.job, "title": g.title, "due_date": g.due_date,
            "is_public": bool(g.is_public), "achieved_at": _utc_iso(g.achieved_at),
            "created_at": _utc_iso(g.created_at)}


def _plan_out(p):
    return {"id": p.id, "date": p.date, "text": p.text, "done": bool(p.done), "goal_id": p.goal_id}


def _my_goal(db, user, goal_id):
    g = (db.query(models.Goal)
         .filter(models.Goal.id == goal_id, models.Goal.user_id == user.user_id).first())
    if g is None:
        raise HTTPException(status_code=404, detail="해당 목표를 찾을 수 없습니다.")
    return g


def _my_plan(db, user, plan_id):
    p = (db.query(models.PlanItem)
         .filter(models.PlanItem.id == plan_id, models.PlanItem.user_id == user.user_id).first())
    if p is None:
        raise HTTPException(status_code=404, detail="해당 할 일을 찾을 수 없습니다.")
    return p


# ── 목표 ──
class GoalIn(BaseModel):
    job: str = Field(..., max_length=100)
    title: str = Field(..., max_length=LOOP_TEXT_MAX)
    due_date: str | None = None
    is_public: bool = False


class GoalPatch(BaseModel):
    title: str | None = Field(None, max_length=LOOP_TEXT_MAX)
    due_date: str | None = None
    is_public: bool | None = None
    achieved: bool | None = None


@app.get("/goals")
def list_goals(current_user: models.User = Depends(get_current_user),
               db: Session = Depends(get_db)):
    goals = (db.query(models.Goal)
             .filter(models.Goal.user_id == current_user.user_id)
             .order_by(models.Goal.created_at.desc(), models.Goal.id.desc()).all())
    return [_goal_out(g) for g in goals]


@app.post("/goals")
def create_goal(req: GoalIn, current_user: models.User = Depends(get_current_user),
                db: Session = Depends(get_db)):
    g = models.Goal(user_id=current_user.user_id,
                    job=_check_text(req.job, "직무", 100),
                    title=_check_text(req.title, "목표"),
                    due_date=_check_date(req.due_date, "due_date") if req.due_date else None,
                    is_public=bool(req.is_public))
    db.add(g)
    db.commit()
    db.refresh(g)
    return _goal_out(g)


@app.patch("/goals/{goal_id}")
def update_goal(goal_id: int, req: GoalPatch,
                current_user: models.User = Depends(get_current_user),
                db: Session = Depends(get_db)):
    g = _my_goal(db, current_user, goal_id)
    sent = req.model_fields_set   # 보낸 필드만 바꾼다 (due_date: null 은 날짜 지우기)
    if "title" in sent and req.title is not None:
        g.title = _check_text(req.title, "목표")
    if "due_date" in sent:
        g.due_date = _check_date(req.due_date, "due_date") if req.due_date else None
    if "is_public" in sent and req.is_public is not None:
        g.is_public = bool(req.is_public)
    if "achieved" in sent and req.achieved is not None:
        g.achieved_at = datetime.utcnow() if req.achieved else None
    db.commit()
    db.refresh(g)
    return _goal_out(g)


@app.delete("/goals/{goal_id}")
def delete_goal(goal_id: int, current_user: models.User = Depends(get_current_user),
                db: Session = Depends(get_db)):
    g = _my_goal(db, current_user, goal_id)
    # 연결된 할 일·소식은 남기고 연결만 끊는다
    (db.query(models.PlanItem).filter(models.PlanItem.goal_id == g.id)
     .update({models.PlanItem.goal_id: None}, synchronize_session=False))
    (db.query(models.FeedPost).filter(models.FeedPost.goal_id == g.id)
     .update({models.FeedPost.goal_id: None}, synchronize_session=False))
    db.delete(g)
    db.commit()
    return {"ok": True, "id": goal_id}


# ── 데일리 플래너 ──
class PlanIn(BaseModel):
    date: str
    text: str = Field(..., max_length=LOOP_TEXT_MAX)
    goal_id: int | None = None


class PlanPatch(BaseModel):
    text: str | None = Field(None, max_length=LOOP_TEXT_MAX)
    done: bool | None = None
    date: str | None = None


@app.get("/plans")
def list_plans(current_user: models.User = Depends(get_current_user),
                     db: Session = Depends(get_db),
                     from_: str | None = Query(None, alias="from"),
                     to: str | None = None):
    q = db.query(models.PlanItem).filter(models.PlanItem.user_id == current_user.user_id)
    if from_:
        q = q.filter(models.PlanItem.date >= _check_date(from_, "from"))
    if to:
        q = q.filter(models.PlanItem.date <= _check_date(to, "to"))
    items = q.order_by(models.PlanItem.date.asc(), models.PlanItem.id.asc()).limit(1000).all()
    return [_plan_out(p) for p in items]


@app.post("/plans")
def create_plan(req: PlanIn, current_user: models.User = Depends(get_current_user),
                db: Session = Depends(get_db)):
    if req.goal_id is not None:
        _my_goal(db, current_user, req.goal_id)   # 남의 목표에 붙이지 못하게
    p = models.PlanItem(user_id=current_user.user_id,
                        date=_check_date(req.date),
                        text=_check_text(req.text, "할 일"),
                        goal_id=req.goal_id, done=False)
    db.add(p)
    db.commit()
    db.refresh(p)
    return _plan_out(p)


@app.patch("/plans/{plan_id}")
def update_plan(plan_id: int, req: PlanPatch,
                current_user: models.User = Depends(get_current_user),
                db: Session = Depends(get_db)):
    p = _my_plan(db, current_user, plan_id)
    if req.text is not None:
        p.text = _check_text(req.text, "할 일")
    if req.done is not None:
        p.done = bool(req.done)
    if req.date is not None:
        p.date = _check_date(req.date)
    db.commit()
    db.refresh(p)
    return _plan_out(p)


@app.delete("/plans/{plan_id}")
def delete_plan(plan_id: int, current_user: models.User = Depends(get_current_user),
                db: Session = Depends(get_db)):
    p = _my_plan(db, current_user, plan_id)
    db.delete(p)
    db.commit()
    return {"ok": True, "id": plan_id}


# ── 달성 소식 ──
class FeedIn(BaseModel):
    goal_id: int
    message: str = Field(..., max_length=FEED_MESSAGE_MAX)


def _feed_post_out(post, author_name):
    return {"id": post.id, "author": _mask_name(author_name), "job": post.job,
            "message": post.message, "created_at": _utc_iso(post.created_at)}


@app.post("/feed")
def create_feed_post(req: FeedIn, current_user: models.User = Depends(get_current_user),
                     db: Session = Depends(get_db)):
    g = _my_goal(db, current_user, req.goal_id)
    if g.achieved_at is None:
        raise HTTPException(status_code=400, detail="달성한 목표에만 소식을 남길 수 있어요.")
    post = models.FeedPost(user_id=current_user.user_id, goal_id=g.id, job=g.job,
                           message=_check_text(req.message, "소식", FEED_MESSAGE_MAX))
    db.add(post)
    db.commit()
    db.refresh(post)
    return _feed_post_out(post, current_user.name)


@app.get("/feed")
def get_feed(job: str = "", limit: int = 20,
             current_user: models.User = Depends(get_current_user),
             db: Session = Depends(get_db)):
    limit = max(1, min(int(limit or 20), 50))
    job = (job or "").strip()[:100]

    pq = (db.query(models.FeedPost, models.User.name)
          .join(models.User, models.User.user_id == models.FeedPost.user_id))
    if job:
        pq = pq.filter(models.FeedPost.job == job)
    posts = pq.order_by(models.FeedPost.created_at.desc(), models.FeedPost.id.desc()).limit(limit).all()

    gq = (db.query(models.Goal, models.User.name)
          .join(models.User, models.User.user_id == models.Goal.user_id)
          .filter(models.Goal.is_public == True))  # noqa: E712
    if job:
        gq = gq.filter(models.Goal.job == job)
    goals = gq.order_by(models.Goal.created_at.desc(), models.Goal.id.desc()).limit(limit).all()

    return {
        "posts": [_feed_post_out(p, name) for p, name in posts],
        "goals": [{"author": _mask_name(name), "job": g.job, "title": g.title,
                   "due_date": g.due_date, "achieved": g.achieved_at is not None}
                  for g, name in goals],
    }


# ── 연속 기록·주간 퀘스트 ──
@app.get("/me/progress")
def my_progress(current_user: models.User = Depends(get_current_user),
                db: Session = Depends(get_db)):
    today = _kst_today()
    monday = today - timedelta(days=today.weekday())   # KST 월요일 시작
    sunday = monday + timedelta(days=6)

    sessions = (db.query(models.InterviewSession.created_at, models.InterviewSession.mode)
                .filter(models.InterviewSession.user_id == current_user.user_id).all())
    done_plans = (db.query(models.PlanItem.date)
                  .filter(models.PlanItem.user_id == current_user.user_id,
                          models.PlanItem.done == True).all())  # noqa: E712

    active_days = set()
    week_interviews = week_practice = 0
    for created_at, mode in sessions:
        d = _to_kst_date(created_at)
        if d is None:
            continue
        active_days.add(d)
        if monday <= d <= sunday:
            week_interviews += 1
            if mode == "practice":
                week_practice += 1

    week_plans_done = 0
    for (date_str,) in done_plans:
        try:
            d = datetime.strptime(date_str, "%Y-%m-%d").date()
        except (TypeError, ValueError):
            continue
        active_days.add(d)
        if monday <= d <= sunday:
            week_plans_done += 1

    # 오늘 또는 어제부터 거꾸로 이어지는 날 수
    streak = 0
    cursor = today if today in active_days else today - timedelta(days=1)
    while cursor in active_days:
        streak += 1
        cursor -= timedelta(days=1)

    def quest(key, label, current, target):
        return {"key": key, "label": label, "current": current, "target": target,
                "done": current >= target}

    return {
        "streak_days": streak,
        "week": {"interviews": week_interviews, "plans_done": week_plans_done},
        "quests": [
            quest("interviews_3", "이번 주 면접 3회", week_interviews, 3),
            quest("plans_5", "이번 주 할 일 5개 완료", week_plans_done, 5),
            quest("practice_1", "말문 트기 연습 1회", week_practice, 1),
        ],
    }



# ════════════════════════════════════════════════════════
#  효과 측정: 긴장 풀기 루틴 전후 점수 저장 (11/07)
#  이름·자기대화 문장은 받지 않는다. 숫자와 고른 큐만 저장한다.
# ════════════════════════════════════════════════════════
class CalmLogIn(BaseModel):
    kind: str | None = Field(None, max_length=10)
    pre: int | None = Field(None, ge=0, le=10)
    post: int | None = Field(None, ge=0, le=10)
    challenge: int | None = Field(None, ge=1, le=7)
    breath_sec: int | None = Field(None, ge=0, le=600)
    cues: list[str] = Field(default_factory=list, max_length=4)


@app.post("/me/calm-logs")
def create_calm_log(req: CalmLogIn, current_user: models.User = Depends(get_current_user),
                    db: Session = Depends(get_db)):
    cues = [str(c)[:40] for c in req.cues][:4]
    kind = req.kind if req.kind in ("full", "short") else "full"
    log = models.CalmLog(user_id=current_user.user_id, kind=kind, pre=req.pre, post=req.post,
                         challenge=req.challenge, breath_sec=req.breath_sec,
                         cues=json.dumps(cues, ensure_ascii=False))
    db.add(log)
    db.commit()
    db.refresh(log)
    return {"id": log.id}
