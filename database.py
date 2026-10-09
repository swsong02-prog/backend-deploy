from pathlib import Path
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker, declarative_base

# 우리 창고(DB) 주소. 일단 backend 폴더 안에 파일 하나로 시작.
# sqlite = 설치 필요 없이 파일 하나가 곧 DB. 나중에 AWS 가면 이 줄만 바꾸면 됨.
DATABASE_URL = f"sqlite:///{Path(__file__).resolve().parent / 'coachcoach.db'}"

# 엔진 = DB로 들어가는 '문'. sqlite는 한 가지 옵션이 더 필요해서 connect_args를 붙임.
engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False, "timeout": 10})


@event.listens_for(engine, "connect")
def _configure_sqlite(dbapi_connection, connection_record):
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA busy_timeout=10000")
        cursor.execute("PRAGMA journal_mode=WAL")
    finally:
        cursor.close()

# SessionLocal = DB와 대화하는 '창구 직원'을 찍어내는 틀. 요청마다 한 명씩 부름.
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

# Base = 앞으로 만들 모든 테이블 설계도가 물려받는 '부모 양식'.
Base = declarative_base()


# 요청이 올 때마다 창구 직원을 부르고, 일이 끝나면 돌려보내는 함수.
def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
