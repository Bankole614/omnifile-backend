import os
import shutil
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import List, Tuple
from fastapi import HTTPException, status
from sqlalchemy.orm import Session
from db import CloudFile, User

VAULT_DIR = Path(__file__).parent.parent / "uploads" / "vault"
VAULT_DIR.mkdir(parents=True, exist_ok=True)

DEFAULT_EXPIRY_DAYS = 7
MAX_EXPIRY_DAYS = 30


def format_bytes(bytes_count: int) -> str:
    if bytes_count <= 0:
        return "0 B"
    units = ["B", "KB", "MB", "GB"]
    size = float(bytes_count)
    unit_index = 0
    while size >= 1024.0 and unit_index < len(units) - 1:
        size /= 1024.0
        unit_index += 1
    return f"{size:.2f} {units[unit_index]}"


def cleanup_expired_files(db: Session) -> int:
    """Find and purge all cloud files that have passed their expiration date."""
    now = datetime.now(timezone.utc)
    expired_files: List[CloudFile] = (
        db.query(CloudFile).filter(CloudFile.expires_at <= now).all()
    )

    deleted_count = 0
    for cf in expired_files:
        # 1. Remove physical file
        disk_path = VAULT_DIR / cf.storage_key
        try:
            if disk_path.exists():
                disk_path.unlink()
        except Exception:
            pass

        # 2. Reclaim user storage quota
        user = db.query(User).filter(User.id == cf.user_id).first()
        if user:
            user.storage_used_bytes = max(0, (user.storage_used_bytes or 0) - cf.file_size)

        # 3. Delete database record
        db.delete(cf)
        deleted_count += 1

    if deleted_count > 0:
        db.commit()

    return deleted_count


def save_file_to_vault(
    user: User,
    file_bytes: bytes,
    original_filename: str,
    mime_type: str,
    db: Session,
    expiry_days: int = DEFAULT_EXPIRY_DAYS,
) -> CloudFile:
    """Validate storage quota, save file to vault disk, and record in DB with auto-expiry."""
    # First, run a quick cleanup on this user's or any expired files to free quota
    cleanup_expired_files(db)
    db.refresh(user)

    file_size = len(file_bytes)
    current_used = user.storage_used_bytes or 0
    cap = user.storage_cap_bytes or (100 * 1024 * 1024)

    # Enforce strict per-user storage cap
    if current_used + file_size > cap:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=(
                f"Storage quota exceeded. Your vault limit is {format_bytes(cap)}, "
                f"currently using {format_bytes(current_used)}. Adding {format_bytes(file_size)} "
                f"exceeds your available quota. Please delete older files to free space."
            ),
        )

    # Sanitize and prepare path
    user_vault_dir = VAULT_DIR / str(user.id)
    user_vault_dir.mkdir(parents=True, exist_ok=True)

    file_id = str(uuid.uuid4())
    safe_name = Path(original_filename or "file").name
    storage_filename = f"{file_id}_{safe_name}"
    storage_key = f"{user.id}/{storage_filename}"
    dest_path = user_vault_dir / storage_filename

    # Write file to disk
    with open(dest_path, "wb") as f:
        f.write(file_bytes)

    # Expiry calculation (capped between 1 and MAX_EXPIRY_DAYS)
    days = max(1, min(expiry_days, MAX_EXPIRY_DAYS))
    expires_at = datetime.now(timezone.utc) + timedelta(days=days)

    # Create DB entry
    cloud_file = CloudFile(
        id=file_id,
        user_id=user.id,
        filename=safe_name,
        storage_key=storage_key,
        file_size=file_size,
        mime_type=mime_type or "application/octet-stream",
        expires_at=expires_at,
        created_at=datetime.now(timezone.utc),
    )
    db.add(cloud_file)

    # Update user storage used
    user.storage_used_bytes = current_used + file_size
    db.commit()
    db.refresh(cloud_file)

    return cloud_file


def delete_file_from_vault(user: User, file_id: str, db: Session) -> bool:
    """Delete a specific cloud file owned by the user, reclaim storage quota."""
    cloud_file = (
        db.query(CloudFile)
        .filter(CloudFile.id == file_id, CloudFile.user_id == user.id)
        .first()
    )
    if not cloud_file:
        return False

    # Delete physical file
    disk_path = VAULT_DIR / cloud_file.storage_key
    try:
        if disk_path.exists():
            disk_path.unlink()
    except Exception:
        pass

    # Decrement user storage
    user.storage_used_bytes = max(
        0, (user.storage_used_bytes or 0) - cloud_file.file_size
    )

    db.delete(cloud_file)
    db.commit()
    return True
