from sqlalchemy import Column, Integer, String, Text, DateTime, ForeignKey
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from database import Base


# 설계 문서의 users 테이블
class User(Base):
    __tablename__ = "users"

    user_id = Column(Integer, primary_key=True, index=True)   # 회원 고유번호
    email = Column(String, unique=True, index=True)           # 로그인 아이디(중복 금지)
    password_hash = Column(String)                            # 해싱된 비밀번호
    name = Column(String, nullable=True)                      # 표시 이름(회원가입 때 입력, 예전 회원은 비어 있을 수 있음)
    created_at = Column(DateTime(timezone=True), server_default=func.now())  # 가입 일시

    # 이 회원의 면접 세션들과 연결(1:N)
    sessions = relationship("InterviewSession", back_populates="user")


# 설계 문서의 interview_sessions 테이블 (면접 1회차 = 1줄)
class InterviewSession(Base):
    __tablename__ = "interview_sessions"

    session_id = Column(Integer, primary_key=True, index=True)        # 세션 고유번호
    user_id = Column(Integer, ForeignKey("users.user_id"))            # 어느 회원의 면접인지
    job = Column(String)                                             # 직무 분야 (예: 개발)
    sub_job = Column(String)                                         # 세부 직무 (예: 백엔드)
    company = Column(String, nullable=True)                          # 지원 회사 (예: 삼성전자, 없으면 null)
    career = Column(String, nullable=True)                           # 신입/경력 (없으면 null)
    level = Column(String)                                           # 난이도 (하/중/상)
    posture_score = Column(Integer)                                  # 자세·표정 종합 점수
    content_score = Column(Integer)                                  # 답변 내용 종합 점수
    total_score = Column(Integer)                                    # 종합 점수
    created_at = Column(DateTime(timezone=True), server_default=func.now())  # 면접 본 일시

    # 위로는 회원, 아래로는 문항 결과들과 연결
    user = relationship("User", back_populates="sessions")
    results = relationship("QuestionResult", back_populates="session")


# 영상 분석 작업 큐 (배포 서버 ↔ PC GPU 워커 폴링 구조)
class AnalysisJob(Base):
    __tablename__ = "analysis_jobs"

    id = Column(Integer, primary_key=True, index=True)        # 작업 고유번호
    user_id = Column(Integer, ForeignKey("users.user_id"))    # 누가 올린 영상인지
    question = Column(Text)                                   # 면접 질문
    job_role = Column(String)                                 # 직무 (예: 개발 백엔드)
    career = Column(String, default="신입")                    # 신입/경력 구분 (워커 평가 프롬프트 분기용)
    video_path = Column(String)                               # 서버 디스크에 저장된 영상 경로
    status = Column(String, default="pending", index=True)    # pending / processing / done / failed
    result_json = Column(Text, nullable=True)                 # 워커가 보낸 분석 결과(JSON 문자열)
    error = Column(Text, nullable=True)                       # 실패 시 오류 메시지
    created_at = Column(DateTime(timezone=True), server_default=func.now())  # 작업 생성 일시
    processing_started_at = Column(DateTime(timezone=True), nullable=True)   # 워커가 가져간 시각(stuck 복구용)
    claim_token = Column(String, nullable=True)                              # 이번 배정 토큰(늦은 결과 거절용)
    attempts = Column(Integer, nullable=False, default=0, server_default="0")  # 복구 횟수


# 자소서 맞춤 질문 생성 작업 큐 (배포 서버엔 Ollama가 없어 PC 워커가 대신 생성)
class QuestionJob(Base):
    __tablename__ = "question_jobs"

    id = Column(Integer, primary_key=True, index=True)        # 작업 고유번호
    user_id = Column(Integer, ForeignKey("users.user_id"), nullable=True)  # 토큰이 있을 때만 기록
    job = Column(String)                                      # 직무 분야 (예: 개발)
    sub = Column(String)                                      # 세부 직무 (예: 백엔드)
    level = Column(String, default="중")                       # 난이도 (하/중/상)
    career = Column(String, default="신입")                    # 신입/경력 구분
    resume_text = Column(Text)                                # 자소서 본문
    status = Column(String, default="pending", index=True)    # pending / processing / done / failed
    result_json = Column(Text, nullable=True)                 # 워커가 보낸 질문 리스트(JSON 문자열)
    error = Column(Text, nullable=True)                       # 실패 시 오류 메시지
    created_at = Column(DateTime(timezone=True), server_default=func.now())  # 작업 생성 일시
    processing_started_at = Column(DateTime(timezone=True), nullable=True)   # 워커가 가져간 시각(stuck 복구용)
    claim_token = Column(String, nullable=True)                              # 이번 배정 토큰(늦은 결과 거절용)
    attempts = Column(Integer, nullable=False, default=0, server_default="0")  # 복구 횟수


# 설계 문서의 question_results 테이블 (문항 1개 = 1줄)
class QuestionResult(Base):
    __tablename__ = "question_results"

    result_id = Column(Integer, primary_key=True, index=True)             # 결과 고유번호
    session_id = Column(Integer, ForeignKey("interview_sessions.session_id"))  # 어느 세션의 문항인지
    question = Column(String)         # 질문 내용
    answer_stt = Column(Text)         # Whisper로 받아쓴 답변
    posture_score = Column(Integer)   # 이 문항 자세·표정 점수
    content_score = Column(Integer)   # 이 문항 내용 점수
    feedback = Column(Text)           # "왜 이 점수인지" 설명
    model_answer = Column(Text)       # AI 생성 모범답안
    duration_sec = Column(Integer)    # 답변 길이(초)
    filler_count = Column(Integer)    # 군더더기 말 횟수

    # 위로 세션과 연결
    session = relationship("InterviewSession", back_populates="results")
