import io
import mimetypes
import zipfile
from pathlib import Path
from typing import List
from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from fastapi.responses import JSONResponse, Response

router = APIRouter(prefix="/api/zip", tags=["Batch ZIP Tools"])


@router.post("/create")
async def create_zip_archive(
    files: List[UploadFile] = File(...),
    archive_name: str = Form("archive.zip"),
    compression_level: int = Form(6),
):
    """Bundle and compress multiple uploaded files into a single ZIP archive."""
    if not files:
        raise HTTPException(status_code=400, detail="No files uploaded.")

    compression_level = max(0, min(9, compression_level))
    comp_method = zipfile.ZIP_STORED if compression_level == 0 else zipfile.ZIP_DEFLATED

    zip_buf = io.BytesIO()
    total_orig = 0
    seen_names: dict[str, int] = {}

    with zipfile.ZipFile(zip_buf, "w", compression=comp_method, compresslevel=compression_level) as zf:
        for f in files:
            raw = await f.read()
            total_orig += len(raw)
            base_name = Path(f.filename or "file").name
            if base_name in seen_names:
                seen_names[base_name] += 1
                stem = Path(base_name).stem
                ext = Path(base_name).suffix
                base_name = f"{stem}_{seen_names[base_name]}{ext}"
            else:
                seen_names[base_name] = 1
            zf.writestr(base_name, raw)

    out_bytes = zip_buf.getvalue()
    safe_name = archive_name.strip() or "bundle.zip"
    if not safe_name.lower().endswith(".zip"):
        safe_name += ".zip"

    return Response(
        content=out_bytes,
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="{safe_name}"',
            "X-Original-Size": str(total_orig),
            "X-Output-Size": str(len(out_bytes)),
        },
    )


@router.post("/inspect")
async def inspect_zip_archive(file: UploadFile = File(...)):
    """Inspect contents of an uploaded .zip archive."""
    raw = await file.read()
    try:
        with zipfile.ZipFile(io.BytesIO(raw), "r") as zf:
            entries = []
            for info in zf.infolist():
                if info.is_dir():
                    continue
                entries.append(
                    {
                        "name": info.filename,
                        "size": info.file_size,
                        "compressedSize": info.compress_size,
                    }
                )
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Invalid ZIP archive: {exc}")

    return JSONResponse(
        {
            "filename": file.filename,
            "totalFiles": len(entries),
            "entries": entries,
        }
    )


@router.post("/extract-item")
async def extract_zip_item(
    file: UploadFile = File(...),
    entry_name: str = Form(...),
):
    """Extract and download a single file from inside an uploaded .zip archive."""
    raw = await file.read()
    try:
        with zipfile.ZipFile(io.BytesIO(raw), "r") as zf:
            if entry_name not in zf.namelist():
                raise HTTPException(status_code=404, detail=f"Entry '{entry_name}' not found in archive.")
            item_bytes = zf.read(entry_name)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Could not read ZIP archive: {exc}")

    base_name = Path(entry_name).name
    guessed_mime, _ = mimetypes.guess_type(base_name)
    return Response(
        content=item_bytes,
        media_type=guessed_mime or "application/octet-stream",
        headers={"Content-Disposition": f'attachment; filename="{base_name}"'},
    )
