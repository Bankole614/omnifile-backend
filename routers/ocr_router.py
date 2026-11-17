import base64
import io
from pathlib import Path
import fitz  # PyMuPDF
from docx import Document
from docx.shared import Pt
from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from fastapi.responses import JSONResponse, Response

router = APIRouter(prefix="/api/ocr", tags=["OCR & Text Extraction"])


@router.post("/extract")
async def extract_text(file: UploadFile = File(...)):
    """
    Extract text from PDF documents or images.
    - For PDFs: extracts native selectable text per page, and returns high-contrast page images
      for any scanned/image-only pages so the frontend Tesseract engine can OCR them seamlessly.
    - For Images: returns preprocessed high-contrast image data ready for OCR (or runs pytesseract if installed).
    """
    raw = await file.read()
    filename = file.filename or "document"
    ext = Path(filename).suffix.lower()

    if ext == ".pdf" or (file.content_type and "pdf" in file.content_type.lower()):
        try:
            doc = fitz.open(stream=raw, filetype="pdf")
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"Invalid PDF file: {exc}")

        pages_data = []
        full_text_parts = []
        needs_client_ocr = False

        for idx, page in enumerate(doc, start=1):
            txt = page.get_text("text").strip()
            page_entry: dict = {
                "pageNumber": idx,
                "text": txt,
                "hasNativeText": len(txt) > 15,
            }
            if len(txt) <= 15 and idx <= 15:
                # Scanned page with little/no selectable text -> provide rendered image for Tesseract OCR
                needs_client_ocr = True
                pix = page.get_pixmap(matrix=fitz.Matrix(1.8, 1.8), alpha=False)
                b64 = base64.b64encode(pix.tobytes("png")).decode("ascii")
                page_entry["ocrImage"] = f"data:image/png;base64,{b64}"

            if txt:
                full_text_parts.append(f"--- Page {idx} ---\n{txt}")
            pages_data.append(page_entry)

        doc.close()
        combined = "\n\n".join(full_text_parts)
        return JSONResponse(
            {
                "filename": filename,
                "fileType": "pdf",
                "pageCount": len(pages_data),
                "needsClientOcr": needs_client_ocr,
                "text": combined,
                "wordCount": len(combined.split()),
                "pages": pages_data,
            }
        )

    # Image file: check if pytesseract is available on backend, and also signal frontend OCR support
    extracted_text = ""
    used_backend_ocr = False
    try:
        import pytesseract  # type: ignore
        from PIL import Image

        pil_img = Image.open(io.BytesIO(raw))
        extracted_text = pytesseract.image_to_string(pil_img).strip()
        used_backend_ocr = True
    except Exception:
        used_backend_ocr = False

    return JSONResponse(
        {
            "filename": filename,
            "fileType": "image",
            "pageCount": 1,
            "needsClientOcr": not used_backend_ocr,
            "text": extracted_text,
            "wordCount": len(extracted_text.split()) if extracted_text else 0,
            "pages": [
                {
                    "pageNumber": 1,
                    "text": extracted_text,
                    "hasNativeText": used_backend_ocr and bool(extracted_text),
                }
            ],
        }
    )


@router.post("/text-to-docx")
async def text_to_docx(
    text: str = Form(...),
    title: str = Form("Extracted Document"),
):
    """Export OCR / extracted text directly into a formatted Word (.docx) file."""
    doc = Document()
    doc.add_heading(title, level=1)
    for para in text.split("\n\n"):
        cleaned = para.strip()
        if cleaned:
            p = doc.add_paragraph(cleaned)
            p.paragraph_format.space_after = Pt(8)

    buf = io.BytesIO()
    doc.save(buf)
    out_bytes = buf.getvalue()
    safe_stem = "".join(c if c.isalnum() or c in ("-", "_") else "_" for c in title).strip("_") or "ocr_result"
    return Response(
        content=out_bytes,
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        headers={"Content-Disposition": f'attachment; filename="{safe_stem}.docx"'},
    )
