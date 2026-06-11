from fastapi import FastAPI, UploadFile, File, Form, Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from sqlalchemy.orm import Session
from passlib.context import CryptContext
from datetime import datetime, timedelta
from jose import jwt, JWTError
import os

import question_bank
import models
from database import engine, get_db

app = FastAPI()

models.Base.metadata.create_all(bind=engine)

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

# ── 비밀키: 코드에 박지 않고 서버 환경변수에서 읽어온다 ──
# 서버에 SECRET_KEY 환경변수가 있으면 그걸 쓰고, 없으면 (로컬 테스트용) 기본값을 쓴다.
SECRET_KEY = os.environ.get("SECRET_KEY", "coachcoach-secret-key-change-this-later")
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 60

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
    "공공·행정":    {"ic": "🏛️", "subs": ["행정직", "군인·국방", "공기업", "정책·기획"]},
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


@app.post("/api/questions")
def make_questions(req: QuestionRequest):
    job_role = f"{req.job} {req.sub}".strip()
    try:
        if req.resume_text and req.resume_text.strip():
            questions = question_bank.build_questions_from_resume(
                req.resume_text, job_role=job_role, level=req.level,
                n=6, career=req.career)
        else:
            questions = question_bank.build_questions(
                job_role=job_role, level=req.level, n=6, career=req.career)
    except Exception as e:
        print(f"[경고] 질문 생성 오류, 기본 질문 대체: {e}")
        questions = [
            "간단하게 자기소개를 해주세요.",
            "우리 회사(또는 이 직무)에 지원하신 동기는 무엇인가요?",
            "본인의 가장 큰 강점은 무엇인가요?",
            "지원한 직무에 본인이 적합하다고 생각하는 이유는 무엇인가요?",
            "최근에 어려운 문제를 해결했던 경험을 말해주세요.",
            "5년 후 본인의 모습을 어떻게 그리고 있나요?",
        ]
    return {"job_role": job_role, "level": req.level, "career": req.career, "questions": questions}


@app.post("/api/analyze-answer")
async def analyze_answer(
    video: UploadFile = File(...),
    question: str = Form(...),
    job_role: str = Form("일반 직무"),
):
    # AWS 배포 버전에서는 분석기가 꺼져 있다.
    # (분석은 내 PC 워커가 담당 — 3단계에서 연결 예정)
    if analyzer is None:
        return {"error": "이 서버에서는 영상 분석이 비활성화되어 있습니다. (분석은 PC 워커 담당)"}
    video_bytes = await video.read()
    try:
        result = analyzer.analyze(video_bytes, question=question, job_role=job_role)
        return result
    except Exception as e:
        print(f"[분석 오류] {e}")
        return {"error": f"분석 중 오류가 발생했습니다: {e}"}


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
    question: str = ""
    answer_stt: str = ""
    posture_score: int = 0
    content_score: int = 0
    feedback: str = ""
    model_answer: str = ""
    duration_sec: int = 0
    filler_count: int = 0

class SessionIn(BaseModel):
    job: str = ""
    sub_job: str = ""
    level: str = "중"
    results: list[ResultIn] = []


@app.post("/interview/finish")
def finish_interview(payload: SessionIn,
                     current_user: models.User = Depends(get_current_user),
                     db: Session = Depends(get_db)):
    n = len(payload.results)
    if n > 0:
        posture_avg = round(sum(r.posture_score for r in payload.results) / n)
        content_avg = round(sum(r.content_score for r in payload.results) / n)
    else:
        posture_avg = content_avg = 0
    total = round((posture_avg + content_avg) / 2)

    session = models.InterviewSession(
        user_id=current_user.user_id,
        job=payload.job,
        sub_job=payload.sub_job,
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
        "level": s.level,
        "posture_score": s.posture_score,
        "content_score": s.content_score,
        "total_score": s.total_score,
        "created_at": s.created_at,
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
            "created_at": s.created_at,
        })

    improvement = None
    if len(points) >= 2:
        improvement = {
            "posture": points[-1]["posture_score"] - points[0]["posture_score"],
            "content": points[-1]["content_score"] - points[0]["content_score"],
            "total":   points[-1]["total_score"]   - points[0]["total_score"],
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
            "level": session.level,
            "posture_score": session.posture_score,
            "content_score": session.content_score,
            "total_score": session.total_score,
            "created_at": session.created_at,
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
