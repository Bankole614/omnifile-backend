import os
from pathlib import Path
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from db import init_db, SessionLocal
from utils.vault_manager import cleanup_expired_files
from routers.auth_router import router as auth_router
from routers.cloud_router import router as cloud_router
from routers.doc_router import router as doc_router
from routers.image_router import router as image_router
from routers.media_router import router as media_router
from routers.ocr_router import router as ocr_router
from routers.pdf_router import router as pdf_router
from routers.zip_router import router as zip_router

# Load .env file if present (no external package required)
env_path = Path(__file__).parent / ".env"
if env_path.exists():
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

DEFAULT_ORIGINS = [
    "http://localhost:5173",
    "http://127.0.0.1:5173",
    "http://localhost:3000",
    "http://127.0.0.1:3000",
    "https://omnifile.bankole.xyz",
    "https://bankole.xyz",
    "https://www.bankole.xyz",
]

env_origins = [
    o.strip().rstrip("/")
    for o in os.getenv("CORS_ORIGINS", "").split(",")
    if o.strip()
]
ALLOWED_ORIGINS = list(dict.fromkeys(DEFAULT_ORIGINS + env_origins))

app = FastAPI(
    title="OmniFile Studio API",
    description="All-in-One File Utility & Productivity Suite Backend",
    version="2.1.0",
)

# Initialize database schema on startup
@app.on_event("startup")
def on_startup():
    try:
        init_db()
        db = SessionLocal()
        try:
            cleanup_expired_files(db)
        finally:
            db.close()
    except Exception as e:
        print(f"[OmniFile] DB Init Warning: {e}")

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_origin_regex=r"https?://(.*\.)?bankole\.xyz(:\d+)?|https?://localhost(:\d+)?|https?://127\.0\.0\.1(:\d+)?",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["Content-Disposition", "X-Original-Size", "X-Output-Size", "X-Engine-Used"],
)

# Core file processing routers
app.include_router(image_router)
app.include_router(pdf_router)
app.include_router(media_router)
app.include_router(doc_router)
app.include_router(ocr_router)
app.include_router(zip_router)

# Optional user accounts & cloud vault routers
app.include_router(auth_router)
app.include_router(cloud_router)


@app.get("/api/health")
async def health_check():
    return {
        "status": "ok",
        "service": "OmniFile Studio API",
        "version": "2.1.0",
        "features": {
            "auth": "optional",
            "cloud_vault": "enabled",
            "storage_cap_mb": 100,
            "auto_expiry_days": 7,
        },
    }
