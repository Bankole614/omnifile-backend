import os
from datetime import datetime, timezone
from pathlib import Path
from sqlalchemy import (
    BigInteger,
    Column,
    DateTime,
    ForeignKey,
    Integer,
    String,
    create_engine,
)
from sqlalchemy.orm import declarative_base, relationship, sessionmaker

# Database URL configuration
# Supports PostgreSQL (e.g. postgresql://user:pass@localhost:5432/omnifile)
# Defaults to SQLite if DATABASE_URL is not set or empty.
DEFAULT_DB_FILE = Path(__file__).parent / "omnifile.db"
DATABASE_URL = os.getenv("DATABASE_URL", f"sqlite:///{DEFAULT_DB_FILE}")

# Fix postgres:// legacy URI if passed by some hosting providers
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

connect_args = {}
if DATABASE_URL.startswith("sqlite"):
    connect_args = {"check_same_thread": False}

engine = create_engine(
    DATABASE_URL,
    connect_args=connect_args,
    pool_pre_ping=True,
)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


def utcnow():
    return datetime.now(timezone.utc)


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    email = Column(String(255), unique=True, index=True, nullable=False)
    password_hash = Column(String(255), nullable=False)
    # Default 100 MB per user: 100 * 1024 * 1024 = 104,857,600 bytes
    storage_cap_bytes = Column(BigInteger, default=104857600, nullable=False)
    storage_used_bytes = Column(BigInteger, default=0, nullable=False)
    created_at = Column(DateTime(timezone=True), default=utcnow, nullable=False)

    files = relationship("CloudFile", back_populates="user", cascade="all, delete-orphan")


class CloudFile(Base):
    __tablename__ = "cloud_files"

    id = Column(String(36), primary_key=True, index=True)  # UUID4 string
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False)
    filename = Column(String(255), nullable=False)
    storage_key = Column(String(255), nullable=False)
    file_size = Column(BigInteger, nullable=False)
    mime_type = Column(String(100), default="application/octet-stream", nullable=False)
    expires_at = Column(DateTime(timezone=True), index=True, nullable=False)
    created_at = Column(DateTime(timezone=True), default=utcnow, nullable=False)

    user = relationship("User", back_populates="files")


def init_db():
    """Create all tables in the database if they don't exist."""
    Base.metadata.create_all(bind=engine)


def get_db():
    """FastAPI dependency for yielding database session."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
