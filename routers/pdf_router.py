import base64
import io
import json
import zipfile
from pathlib import Path
from typing import List
import fitz  # PyMuPDF
from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from fastapi.responses import JSONResponse, Response
from PIL import Image, ImageOps

router = APIRouter(prefix="/api/pdf", tags=["PDF Tools"])

PAGE_SIZES = {
    "a4": (595.28, 841.89),
    "letter": (612.0, 792.0),
}


@router.post("/inspect")
async def inspect_pdf(file: UploadFile = File(...)):
    """Return page count, encryption status, dimensions, and base64 thumbnail previews."""
    raw = await file.read()
    try:
        doc = fitz.open(stream=raw, filetype="pdf")
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Invalid PDF file: {exc}")

    if doc.needs_pass:
        doc.close()
        return JSONResponse(
            {
                "filename": file.filename,
                "fileSize": len(raw),
                "isEncrypted": True,
                "pageCount": 0,
                "pages": [],
            }
        )

    pages_info = []
    max_preview_pages = min(len(doc), 60)
    for i in range(max_preview_pages):
        page = doc[i]
        pix = page.get_pixmap(matrix=fitz.Matrix(0.3, 0.3), alpha=False)
        thumb_b64 = base64.b64encode(pix.tobytes("jpeg")).decode("ascii")
        pages_info.append(
            {
                "pageNumber": i + 1,
                "width": round(page.rect.width, 1),
                "height": round(page.rect.height, 1),
                "rotation": page.rotation,
                "thumbnail": f"data:image/jpeg;base64,{thumb_b64}",
            }
        )

    total_pages = len(doc)
    doc.close()
    return JSONResponse(
        {
            "filename": file.filename,
            "fileSize": len(raw),
            "isEncrypted": False,
            "pageCount": total_pages,
            "pages": pages_info,
        }
    )


@router.post("/compress")
async def compress_pdf(
    file: UploadFile = File(...),
    level: str = Form("medium"),  # low | medium | extreme
):
    """Compress PDF by re-encoding embedded images and deflating streams."""
    raw = await file.read()
    try:
        doc = fitz.open(stream=raw, filetype="pdf")
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Could not open PDF: {exc}")

    presets = {
        "low": {"max_dim": 1800, "jpg_quality": 80},
        "medium": {"max_dim": 1300, "jpg_quality": 62},
        "extreme": {"max_dim": 900, "jpg_quality": 42},
    }
    cfg = presets.get(level.lower(), presets["medium"])

    seen_xrefs = set()
    for page in doc:
        for img_info in page.get_images(full=True):
            xref = img_info[0]
            if xref in seen_xrefs:
                continue
            seen_xrefs.add(xref)
            try:
                base_img = doc.extract_image(xref)
                if not base_img or "image" not in base_img:
                    continue
                img_bytes = base_img["image"]
                pil_img = Image.open(io.BytesIO(img_bytes))
                if pil_img.mode in ("RGBA", "LA", "P"):
                    continue
                if pil_img.mode != "RGB":
                    pil_img = pil_img.convert("RGB")

                w, h = pil_img.size
                max_dim = cfg["max_dim"]
                if max(w, h) > max_dim:
                    scale = max_dim / float(max(w, h))
                    pil_img = pil_img.resize(
                        (max(1, int(w * scale)), max(1, int(h * scale))),
                        Image.Resampling.LANCZOS,
                    )

                buf = io.BytesIO()
                pil_img.save(buf, format="JPEG", quality=cfg["jpg_quality"], optimize=True)
                new_bytes = buf.getvalue()
                if len(new_bytes) < len(img_bytes):
                    page.replace_image(xref, stream=new_bytes)
            except Exception:
                continue

    doc.set_metadata({})
    out_bytes = doc.tobytes(garbage=4, deflate=True, clean=True, deflate_images=True, deflate_fonts=True)
    doc.close()

    final_bytes = out_bytes if len(out_bytes) < len(raw) else raw
    stem = Path(file.filename or "document.pdf").stem
    return Response(
        content=final_bytes,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'attachment; filename="{stem}_compressed.pdf"',
            "X-Original-Size": str(len(raw)),
            "X-Output-Size": str(len(final_bytes)),
        },
    )


@router.post("/from-images")
async def images_to_pdf(
    files: List[UploadFile] = File(...),
    page_size: str = Form("a4"),
    orientation: str = Form("auto"),
    margin: int = Form(20),
):
    """Combine uploaded images into a single formatted PDF."""
    if not files:
        raise HTTPException(status_code=400, detail="No images uploaded.")

    doc = fitz.open()
    margin = max(0, min(100, margin))

    for f in files:
        raw = await f.read()
        try:
            pil_img = Image.open(io.BytesIO(raw))
            pil_img = ImageOps.exif_transpose(pil_img)
            if pil_img.mode in ("RGBA", "LA", "P"):
                bg = Image.new("RGB", pil_img.size, (255, 255, 255))
                if pil_img.mode == "P":
                    pil_img = pil_img.convert("RGBA")
                bg.paste(pil_img, mask=pil_img.split()[-1] if pil_img.mode in ("RGBA", "LA") else None)
                pil_img = bg
            elif pil_img.mode != "RGB":
                pil_img = pil_img.convert("RGB")
        except Exception as exc:
            doc.close()
            raise HTTPException(status_code=400, detail=f"Invalid image '{f.filename}': {exc}")

        img_w, img_h = pil_img.size
        img_buf = io.BytesIO()
        pil_img.save(img_buf, format="JPEG", quality=90, optimize=True)
        img_bytes = img_buf.getvalue()

        if page_size.lower() == "fit":
            pw, ph = float(img_w), float(img_h)
            page = doc.new_page(width=pw, height=ph)
            page.insert_image(fitz.Rect(0, 0, pw, ph), stream=img_bytes)
        else:
            base_w, base_h = PAGE_SIZES.get(page_size.lower(), PAGE_SIZES["a4"])
            if orientation == "landscape" or (orientation == "auto" and img_w > img_h):
                pw, ph = base_h, base_w
            else:
                pw, ph = base_w, base_h

            page = doc.new_page(width=pw, height=ph)
            avail_w = max(10.0, pw - 2 * margin)
            avail_h = max(10.0, ph - 2 * margin)
            scale = min(avail_w / img_w, avail_h / img_h)
            draw_w = img_w * scale
            draw_h = img_h * scale
            x0 = (pw - draw_w) / 2.0
            y0 = (ph - draw_h) / 2.0
            page.insert_image(fitz.Rect(x0, y0, x0 + draw_w, y0 + draw_h), stream=img_bytes)

    pdf_bytes = doc.tobytes(garbage=3, deflate=True)
    doc.close()
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": 'attachment; filename="images_combined.pdf"'},
    )


@router.post("/to-images")
async def pdf_to_images(
    file: UploadFile = File(...),
    fmt: str = Form("png"),
    dpi: int = Form(150),
):
    """Render each page of a PDF into high-res images (returns single image or ZIP)."""
    raw = await file.read()
    try:
        doc = fitz.open(stream=raw, filetype="pdf")
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Invalid PDF: {exc}")

    dpi = max(72, min(300, dpi))
    zoom = dpi / 72.0
    mat = fitz.Matrix(zoom, zoom)
    ext = "jpg" if fmt.lower() in ("jpg", "jpeg") else "png"
    pymupdf_fmt = "jpeg" if ext == "jpg" else "png"
    stem = Path(file.filename or "document").stem

    if len(doc) == 1:
        pix = doc[0].get_pixmap(matrix=mat, alpha=False)
        img_bytes = pix.tobytes(pymupdf_fmt)
        doc.close()
        media = "image/jpeg" if ext == "jpg" else "image/png"
        return Response(
            content=img_bytes,
            media_type=media,
            headers={"Content-Disposition": f'attachment; filename="{stem}_page_1.{ext}"'},
        )

    zip_buf = io.BytesIO()
    with zipfile.ZipFile(zip_buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for i, page in enumerate(doc, start=1):
            pix = page.get_pixmap(matrix=mat, alpha=False)
            zf.writestr(f"{stem}_page_{i:03d}.{ext}", pix.tobytes(pymupdf_fmt))

    doc.close()
    return Response(
        content=zip_buf.getvalue(),
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{stem}_pages.zip"'},
    )


@router.post("/merge")
async def merge_pdfs(files: List[UploadFile] = File(...)):
    """Merge multiple PDF files into one document."""
    if len(files) < 2:
        raise HTTPException(status_code=400, detail="Upload at least 2 PDF files to merge.")

    merged = fitz.open()
    for f in files:
        raw = await f.read()
        try:
            src = fitz.open(stream=raw, filetype="pdf")
            merged.insert_pdf(src)
            src.close()
        except Exception as exc:
            merged.close()
            raise HTTPException(status_code=400, detail=f"Failed to read '{f.filename}': {exc}")

    out_bytes = merged.tobytes(garbage=3, deflate=True)
    merged.close()
    return Response(
        content=out_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": 'attachment; filename="merged_document.pdf"'},
    )


@router.post("/edit")
async def edit_pdf(
    file: UploadFile = File(...),
    operations: str = Form(...),
):
    """Apply page reorder/rotate/delete/split, watermarks, stamps, and page numbering to a PDF."""
    raw = await file.read()
    try:
        src = fitz.open(stream=raw, filetype="pdf")
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Invalid PDF: {exc}")

    try:
        ops = json.loads(operations)
    except Exception as exc:
        src.close()
        raise HTTPException(status_code=400, detail=f"Invalid operations JSON: {exc}")

    page_specs = ops.get("pages") or [
        {"pageNumber": i + 1, "rotation": 0} for i in range(len(src))
    ]
    watermark_text = (ops.get("watermarkText") or "").strip()
    add_page_numbers = bool(ops.get("addPageNumbers", False))
    stamp_text = (ops.get("stampText") or "").strip()

    out_doc = fitz.open()
    for spec in page_specs:
        p_idx = int(spec.get("pageNumber", 1)) - 1
        if 0 <= p_idx < len(src):
            out_doc.insert_pdf(src, from_page=p_idx, to_page=p_idx)
            added_page = out_doc[-1]
            rot = int(spec.get("rotation", 0)) % 360
            if rot:
                added_page.set_rotation((added_page.rotation + rot) % 360)

    if len(out_doc) == 0:
        out_doc.close()
        src.close()
        raise HTTPException(status_code=400, detail="No valid pages selected for output.")

    total_out = len(out_doc)
    for idx, page in enumerate(out_doc, start=1):
        rect = page.rect
        if watermark_text:
            wm_rect = fitz.Rect(rect.width * 0.1, rect.height * 0.44, rect.width * 0.9, rect.height * 0.56)
            page.insert_textbox(
                wm_rect,
                watermark_text,
                fontsize=36,
                color=(0.78, 0.78, 0.82),
                align=fitz.TEXT_ALIGN_CENTER,
                overlay=True,
            )

        if stamp_text:
            stamp_rect = fitz.Rect(rect.width - 210, 18, rect.width - 18, 54)
            page.draw_rect(stamp_rect, color=(0.15, 0.38, 0.85), width=1.2)
            page.insert_textbox(
                stamp_rect,
                stamp_text,
                fontsize=10,
                color=(0.15, 0.38, 0.85),
                align=fitz.TEXT_ALIGN_CENTER,
                overlay=True,
            )

        if add_page_numbers:
            footer_rect = fitz.Rect(0, rect.height - 28, rect.width, rect.height - 10)
            page.insert_textbox(
                footer_rect,
                f"Page {idx} of {total_out}",
                fontsize=9,
                color=(0.4, 0.4, 0.45),
                align=fitz.TEXT_ALIGN_CENTER,
                overlay=True,
            )

    out_bytes = out_doc.tobytes(garbage=3, deflate=True)
    out_doc.close()
    src.close()

    stem = Path(file.filename or "document").stem
    return Response(
        content=out_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{stem}_edited.pdf"'},
    )


@router.post("/security")
async def pdf_security(
    file: UploadFile = File(...),
    mode: str = Form("protect"),  # protect | unlock
    password: str = Form(...),
):
    """Encrypt a PDF with AES-256 password protection, or unlock an encrypted PDF."""
    if not password:
        raise HTTPException(status_code=400, detail="Please enter a password.")

    raw = await file.read()
    try:
        doc = fitz.open(stream=raw, filetype="pdf")
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Invalid PDF: {exc}")

    stem = Path(file.filename or "document").stem

    if mode.lower() == "unlock":
        if doc.needs_pass:
            auth_ok = doc.authenticate(password)
            if not auth_ok:
                doc.close()
                raise HTTPException(status_code=400, detail="Incorrect password for this PDF.")
        out_bytes = doc.tobytes(garbage=3, deflate=True, encryption=fitz.PDF_ENCRYPT_NONE)
        doc.close()
        return Response(
            content=out_bytes,
            media_type="application/pdf",
            headers={"Content-Disposition": f'attachment; filename="{stem}_unlocked.pdf"'},
        )

    # Protect mode
    if doc.needs_pass:
        doc.close()
        raise HTTPException(status_code=400, detail="This PDF is already password-protected. Unlock it first.")

    out_bytes = doc.tobytes(
        garbage=3,
        deflate=True,
        encryption=fitz.PDF_ENCRYPT_AES_256,
        user_pw=password,
        owner_pw=password,
    )
    doc.close()
    return Response(
        content=out_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{stem}_protected.pdf"'},
    )
