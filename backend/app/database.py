import os
from datetime import datetime, timezone

from sqlalchemy import create_engine, Column, String, Boolean, Float, JSON, DateTime
from sqlalchemy.orm import sessionmaker, declarative_base

DATABASE_URL = os.environ.get(
    "DATABASE_URL", "postgresql://sandbox:sandbox@db:5432/sandbox"
)

engine = create_engine(DATABASE_URL, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
Base = declarative_base()


class Submission(Base):
    __tablename__ = "submissions"

    submission_id = Column(String, primary_key=True)
    repo_url = Column(String, nullable=False)
    project_type = Column(String, nullable=False)
    status = Column(String, nullable=False)
    build_success = Column(Boolean, nullable=False)
    execution_success = Column(Boolean, nullable=False)
    duration_seconds = Column(Float, nullable=False)
    logs = Column(String)
    scores = Column(JSON)
    error = Column(String)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))


def init_db():
    Base.metadata.create_all(bind=engine)


def get_session():
    return SessionLocal()
