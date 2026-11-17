import re
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy.orm import Session
from db import CloudFile, User, get_db
from utils.auth import create_access_token, get_current_user, hash_password, verify_password
from utils.vault_manager import cleanup_expired_files

router = APIRouter(prefix="/api/auth", tags=["auth"])


class AuthRequest(BaseModel):
    email: str = Field(..., description="User email address")
    password: str = Field(..., min_length=6, description="Password (at least 6 characters)")


class UserResponse(BaseModel):
    id: int
    email: str
    storage_used_bytes: int
    storage_cap_bytes: int
    file_count: int = 0


class AuthResponse(BaseModel):
    token: str
    user: UserResponse


EMAIL_REGEX = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


@router.post("/register", response_model=AuthResponse)
def register(req: AuthRequest, db: Session = Depends(get_db)):
    email = req.email.strip().lower()
    if not EMAIL_REGEX.match(email):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid email address format.",
        )

    if len(req.password) < 6:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Password must be at least 6 characters.",
        )

    # Check if user already exists
    existing = db.query(User).filter(User.email == email).first()
    if existing:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="An account with this email address already exists. Please sign in.",
        )

    pw_hash = hash_password(req.password)
    new_user = User(
        email=email,
        password_hash=pw_hash,
        storage_cap_bytes=100 * 1024 * 1024,  # 100 MB default
        storage_used_bytes=0,
    )
    db.add(new_user)
    db.commit()
    db.refresh(new_user)

    token = create_access_token(user_id=new_user.id, email=new_user.email)
    return {
        "token": token,
        "user": {
            "id": new_user.id,
            "email": new_user.email,
            "storage_used_bytes": new_user.storage_used_bytes or 0,
            "storage_cap_bytes": new_user.storage_cap_bytes or (100 * 1024 * 1024),
            "file_count": 0,
        },
    }


@router.post("/login", response_model=AuthResponse)
def login(req: AuthRequest, db: Session = Depends(get_db)):
    email = req.email.strip().lower()
    user = db.query(User).filter(User.email == email).first()
    if not user or not verify_password(req.password, user.password_hash):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect email or password.",
        )

    # Clean up expired files to make sure quota is fresh
    cleanup_expired_files(db)
    db.refresh(user)

    file_count = db.query(CloudFile).filter(CloudFile.user_id == user.id).count()
    token = create_access_token(user_id=user.id, email=user.email)

    return {
        "token": token,
        "user": {
            "id": user.id,
            "email": user.email,
            "storage_used_bytes": user.storage_used_bytes or 0,
            "storage_cap_bytes": user.storage_cap_bytes or (100 * 1024 * 1024),
            "file_count": file_count,
        },
    }


@router.get("/me", response_model=UserResponse)
def get_me(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    cleanup_expired_files(db)
    db.refresh(user)
    file_count = db.query(CloudFile).filter(CloudFile.user_id == user.id).count()

    return {
        "id": user.id,
        "email": user.email,
        "storage_used_bytes": user.storage_used_bytes or 0,
        "storage_cap_bytes": user.storage_cap_bytes or (100 * 1024 * 1024),
        "file_count": file_count,
    }
