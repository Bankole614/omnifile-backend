from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional
from fastapi import APIRouter, Depends, Form, HTTPException, UploadFile, status
from fastapi.responses import FileResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session
from db import CloudFile, User, get_db
from utils.auth import get_current_user
from utils.vault_manager import (
    VAULT_DIR,
    cleanup_expired_files,
    delete_file_from_vault,
    format_bytes,
    save_file_to_vault,
)

router = APIRouter(prefix="/api/cloud", tags=["cloud"])


class CloudFileItem(BaseModel):
    id: str
    filename: str
    file_size: int
    file_size_formatted: str
    mime_type: str
    created_at: datetime
    expires_at: datetime
    seconds_remaining: int
    is_expired: bool


class CloudVaultStatus(BaseModel):
    storage_used_bytes: int
    storage_cap_bytes: int
    storage_used_formatted: str
    storage_cap_formatted: str
    used_percentage: float
    files: List[CloudFileItem]


@router.get("/files", response_model=CloudVaultStatus)
def list_user_files(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """List all stored cloud vault files for the authenticated user, automatically purging expired items."""
    cleanup_expired_files(db)
    db.refresh(user)

    now = datetime.now(timezone.utc)
    user_files = (
        db.query(CloudFile)
        .filter(CloudFile.user_id == user.id)
        .order_by(CloudFile.created_at.desc())
        .all()
    )

    items: List[CloudFileItem] = []
    for f in user_files:
        exp = f.expires_at
        if exp.tzinfo is None:
            exp = exp.replace(tzinfo=timezone.utc)
        diff = int((exp - now).total_seconds())

        items.append(
            CloudFileItem(
                id=f.id,
                filename=f.filename,
                file_size=f.file_size,
                file_size_formatted=format_bytes(f.file_size),
                mime_type=f.mime_type,
                created_at=f.created_at,
                expires_at=f.expires_at,
                seconds_remaining=max(0, diff),
                is_expired=diff <= 0,
            )
        )

    used = user.storage_used_bytes or 0
    cap = user.storage_cap_bytes or (100 * 1024 * 1024)
    pct = round((used / cap) * 100, 2) if cap > 0 else 0.0

    return CloudVaultStatus(
        storage_used_bytes=used,
        storage_cap_bytes=cap,
        storage_used_formatted=format_bytes(used),
        storage_cap_formatted=format_bytes(cap),
        used_percentage=min(100.0, pct),
        files=items,
    )


@router.post("/upload", response_model=CloudFileItem)
async def upload_to_vault(
    file: UploadFile,
    expiry_days: int = Form(7),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Upload or save a file to the user's cloud vault, subject to strict per-user storage cap and auto-expiry."""
    content = await file.read()
    if not content:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="File is empty.",
        )

    original_name = file.filename or "uploaded_file"
    mime = file.content_type or "application/octet-stream"

    saved = save_file_to_vault(
        user=user,
        file_bytes=content,
        original_filename=original_name,
        mime_type=mime,
        db=db,
        expiry_days=expiry_days,
    )

    now = datetime.now(timezone.utc)
    exp = saved.expires_at
    if exp.tzinfo is None:
        exp = exp.replace(tzinfo=timezone.utc)
    diff = int((exp - now).total_seconds())

    return CloudFileItem(
        id=saved.id,
        filename=saved.filename,
        file_size=saved.file_size,
        file_size_formatted=format_bytes(saved.file_size),
        mime_type=saved.mime_type,
        created_at=saved.created_at,
        expires_at=saved.expires_at,
        seconds_remaining=max(0, diff),
        is_expired=False,
    )


@router.get("/download/{file_id}")
def download_from_vault(
    file_id: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Download a file from the user's cloud vault."""
    cleanup_expired_files(db)

    cloud_file = (
        db.query(CloudFile)
        .filter(CloudFile.id == file_id, CloudFile.user_id == user.id)
        .first()
    )
    if not cloud_file:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="File not found or has expired.",
        )

    disk_path = VAULT_DIR / cloud_file.storage_key
    if not disk_path.exists():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="File content not found on server.",
        )

    return FileResponse(
        path=disk_path,
        media_type=cloud_file.mime_type,
        filename=cloud_file.filename,
    )


@router.delete("/delete/{file_id}")
def delete_from_vault(
    file_id: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Delete a file from the user's cloud vault and reclaim storage quota."""
    success = delete_file_from_vault(user=user, file_id=file_id, db=db)
    if not success:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="File not found or already deleted.",
        )

    return {
        "success": True,
        "message": "File deleted successfully.",
        "storage_used_bytes": user.storage_used_bytes,
        "storage_used_formatted": format_bytes(user.storage_used_bytes),
    }
