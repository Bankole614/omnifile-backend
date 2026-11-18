import json
import logging
import os
import random
import re
import smtplib
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy.orm import Session
from db import CloudFile, PasswordReset, User, get_db, utcnow
from utils.auth import create_access_token, get_current_user, hash_password, verify_password
from utils.vault_manager import cleanup_expired_files

logger = logging.getLogger(__name__)

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


class ForgotPasswordRequest(BaseModel):
    email: str = Field(..., description="User email address")


class ResetPasswordRequest(BaseModel):
    email: str = Field(..., description="User email address")
    code: str = Field(..., min_length=4, max_length=10, description="Verification code")
    new_password: str = Field(..., min_length=6, description="New password (at least 6 characters)")


EMAIL_REGEX = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def send_via_brevo_api(
    api_key: str,
    sender_email: str,
    sender_name: str,
    to_email: str,
    subject: str,
    html_content: str,
    text_content: str,
) -> bool:
    """Send transactional email via Brevo REST API over HTTPS port 443."""
    url = "https://api.brevo.com/v3/smtp/email"
    headers = {
        "api-key": api_key,
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    payload = {
        "sender": {
            "name": sender_name,
            "email": sender_email,
        },
        "to": [
            {
                "email": to_email,
            }
        ],
        "subject": subject,
        "htmlContent": html_content,
        "textContent": text_content,
    }
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return 200 <= resp.status < 300
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        logger.error(f"Brevo API error ({e.code}): {body}")
        return False
    except Exception as e:
        logger.error(f"Brevo API request failed: {e}")
        return False


def send_reset_email(to_email: str, code: str) -> bool:
    """Send password recovery email via Brevo API or SMTP relay."""
    brevo_key = os.getenv("BREVO_API_KEY")
    sender_email = os.getenv("BREVO_SENDER_EMAIL", "noreply@mail.bankole.xyz")
    sender_name = os.getenv("BREVO_SENDER_NAME", "OmniFile Studio")

    subject = f"OmniFile Password Reset Code: {code}"
    text_content = (
        f"Your OmniFile password reset code is: {code}\n\n"
        f"This code will expire in 15 minutes. If you did not request this, you can safely ignore this email."
    )
    html_content = f"""
    <div style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif; max-width: 480px; margin: 0 auto; padding: 32px 24px; border: 1px solid #e2e8f0; border-radius: 16px; background-color: #ffffff;">
        <div style="margin-bottom: 24px;">
            <h2 style="color: #4f46e5; margin: 0; font-size: 22px; font-weight: 700;">OmniFile Studio</h2>
        </div>
        <p style="color: #334155; font-size: 15px; line-height: 1.5; margin: 0 0 16px 0;">You requested a password reset for your OmniFile account.</p>
        <p style="color: #64748b; font-size: 13px; margin: 0 0 10px 0;">Here is your 6-digit recovery code:</p>
        <div style="font-size: 32px; font-weight: 700; letter-spacing: 6px; padding: 16px 24px; background: #f8fafc; border: 1px solid #e2e8f0; border-radius: 12px; display: block; text-align: center; color: #4338ca; margin: 16px 0 20px 0; font-family: monospace;">
            {code}
        </div>
        <p style="color: #64748b; font-size: 13px; line-height: 1.5; margin: 0;">This code expires in <strong>15 minutes</strong>. If you didn't request a password reset, you can safely ignore this email.</p>
        <hr style="border: none; border-top: 1px solid #f1f5f9; margin: 28px 0 16px 0;" />
        <p style="color: #94a3b8; font-size: 11px; margin: 0; text-align: center;">OmniFile Studio • Private, browser-based file utilities</p>
    </div>
    """

    # 1. Try Brevo REST API first (recommended for reliability over HTTPS 443)
    if brevo_key:
        if send_via_brevo_api(
            api_key=brevo_key,
            sender_email=sender_email,
            sender_name=sender_name,
            to_email=to_email,
            subject=subject,
            html_content=html_content,
            text_content=text_content,
        ):
            return True

    # 2. Fall back to SMTP Relay if SMTP_HOST is configured
    smtp_host = os.getenv("SMTP_HOST")
    if not smtp_host:
        return False

    smtp_port = int(os.getenv("SMTP_PORT", "587"))
    smtp_user = os.getenv("SMTP_USER")
    smtp_pass = os.getenv("SMTP_PASS")
    smtp_from = os.getenv("SMTP_FROM", f"{sender_name} <{sender_email}>")

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = smtp_from
    msg["To"] = to_email

    msg.attach(MIMEText(text_content, "plain"))
    msg.attach(MIMEText(html_content, "html"))

    try:
        if smtp_port == 465:
            with smtplib.SMTP_SSL(smtp_host, smtp_port, timeout=10) as server:
                if smtp_user and smtp_pass:
                    server.login(smtp_user, smtp_pass)
                server.sendmail(smtp_from, [to_email], msg.as_string())
        else:
            with smtplib.SMTP(smtp_host, smtp_port, timeout=10) as server:
                server.starttls()
                if smtp_user and smtp_pass:
                    server.login(smtp_user, smtp_pass)
                server.sendmail(smtp_from, [to_email], msg.as_string())
        return True
    except Exception as e:
        logger.error(f"Failed to send reset email via SMTP: {e}")
        return False


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


@router.post("/forgot-password")
def forgot_password(req: ForgotPasswordRequest, db: Session = Depends(get_db)):
    email = req.email.strip().lower()
    if not EMAIL_REGEX.match(email):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid email address format.",
        )

    user = db.query(User).filter(User.email == email).first()
    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No OmniFile account found with this email address.",
        )

    # Generate 6-digit verification code
    code = f"{random.randint(100000, 999999)}"
    expires_at = utcnow() + timedelta(minutes=15)

    # Remove any existing pending reset requests for this email
    db.query(PasswordReset).filter(PasswordReset.email == email).delete()

    reset_entry = PasswordReset(
        email=email,
        code=code,
        expires_at=expires_at,
    )
    db.add(reset_entry)
    db.commit()

    # Attempt to send email if SMTP server is configured
    sent = send_reset_email(email, code)

    response_data = {
        "message": "A 6-digit recovery code has been sent to your email address.",
        "email": email,
        "expires_in_minutes": 15,
    }

    # If no SMTP provider configured, include dev_code for testing convenience
    if not sent and not os.getenv("SMTP_HOST"):
        response_data["dev_code"] = code
        response_data["message"] = (
            f"Reset code {code} generated! (Email service is in local test mode)."
        )

    return response_data


@router.post("/reset-password", response_model=AuthResponse)
def reset_password(req: ResetPasswordRequest, db: Session = Depends(get_db)):
    email = req.email.strip().lower()
    code = req.code.strip()

    if len(req.new_password) < 6:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="New password must be at least 6 characters.",
        )

    reset_entry = (
        db.query(PasswordReset)
        .filter(PasswordReset.email == email, PasswordReset.code == code)
        .first()
    )

    if not reset_entry:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid verification code. Please check your code or request a new one.",
        )

    # Check expiration (15 minutes)
    entry_expires = reset_entry.expires_at
    if entry_expires.tzinfo is None:
        entry_expires = entry_expires.replace(tzinfo=timezone.utc)

    if entry_expires < utcnow():
        db.delete(reset_entry)
        db.commit()
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="This verification code has expired (valid for 15 minutes). Please request a new code.",
        )

    user = db.query(User).filter(User.email == email).first()
    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Account not found.",
        )

    # Update to new password
    user.password_hash = hash_password(req.new_password)
    db.delete(reset_entry)
    db.commit()
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
