import os
import shutil
import tempfile
from pathlib import Path
from fastapi import UploadFile

TEMP_ROOT = Path(tempfile.gettempdir()) / "omnifile_studio_temp"
TEMP_ROOT.mkdir(parents=True, exist_ok=True)


def create_job_dir() -> Path:
    """Create a unique temporary directory for a request job."""
    job_dir = Path(tempfile.mkdtemp(dir=TEMP_ROOT, prefix="job_"))
    return job_dir


async def save_upload(upload: UploadFile, dest_dir: Path, custom_name: str | None = None) -> Path:
    """Save a FastAPI UploadFile to dest_dir and return the file path."""
    safe_name = custom_name or Path(upload.filename or "uploaded_file").name
    dest_path = dest_dir / safe_name
    with open(dest_path, "wb") as f:
        while chunk := await upload.read(1024 * 1024):
            f.write(chunk)
    await upload.seek(0)
    return dest_path


def cleanup_dir(job_dir: Path) -> None:
    """Remove temporary job directory and all its contents."""
    try:
        if job_dir and job_dir.exists():
            shutil.rmtree(job_dir, ignore_errors=True)
    except Exception:
        pass
