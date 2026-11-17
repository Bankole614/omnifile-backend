from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from routers.doc_router import router as doc_router
from routers.image_router import router as image_router
from routers.media_router import router as media_router
from routers.ocr_router import router as ocr_router
from routers.pdf_router import router as pdf_router
from routers.zip_router import router as zip_router

app = FastAPI(
    title="OmniFile Studio API",
    description="All-in-One File Utility & Productivity Suite Backend",
    version="2.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["Content-Disposition", "X-Original-Size", "X-Output-Size"],
)

app.include_router(image_router)
app.include_router(pdf_router)
app.include_router(media_router)
app.include_router(doc_router)
app.include_router(ocr_router)
app.include_router(zip_router)


@app.get("/api/health")
async def health_check():
    return {
        "status": "ok",
        "service": "OmniFile Studio API",
        "version": "2.0.0",
    }
