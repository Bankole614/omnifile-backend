import io
from pathlib import Path
import fitz  # PyMuPDF
from docx import Document
from docx.shared import Inches, Pt
from fastapi import APIRouter, File, HTTPException, UploadFile
from fastapi.responses import Response
from utils.temp_manager import cleanup_dir, create_job_dir, save_upload

router = APIRouter(prefix="/api/doc", tags=["Document Tools"])


def _fallback_pdf_to_docx(pdf_path: Path, docx_path: Path) -> None:
    """Convert PDF to DOCX using PyMuPDF block extraction + python-docx when pdf2docx is not installed."""
    pdf_doc = fitz.open(str(pdf_path))
    word_doc = Document()

    for page_idx, page in enumerate(pdf_doc):
        if page_idx > 0:
            word_doc.add_page_break()

        blocks = page.get_text("blocks")
        blocks.sort(key=lambda b: (b[1], b[0]))
        for b in blocks:
            text = (b[4] or "").strip()
            if not text:
                continue
            cleaned = " ".join(line.strip() for line in text.splitlines() if line.strip())
            if cleaned:
                p = word_doc.add_paragraph(cleaned)
                p.paragraph_format.space_after = Pt(6)

        for img_info in page.get_images(full=True):
            xref = img_info[0]
            try:
                extracted = pdf_doc.extract_image(xref)
                if extracted and "image" in extracted:
                    img_stream = io.BytesIO(extracted["image"])
                    word_doc.add_picture(img_stream, width=Inches(4.5))
            except Exception:
                continue

    pdf_doc.close()
    word_doc.save(str(docx_path))


@router.post("/pdf-to-docx")
async def pdf_to_docx(file: UploadFile = File(...)):
    """Convert a PDF file into an editable Word (.docx) document."""
    job_dir = create_job_dir()
    try:
        in_path = await save_upload(file, job_dir)
        stem = Path(file.filename or "document").stem
        out_name = f"{stem}.docx"
        out_path = job_dir / out_name

        try:
            try:
                from pdf2docx import Converter  # type: ignore

                cv = Converter(str(in_path))
                cv.convert(str(out_path), start=0, end=None)
                cv.close()
            except ImportError:
                _fallback_pdf_to_docx(in_path, out_path)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"PDF to DOCX conversion failed: {exc}")

        if not out_path.exists():
            raise HTTPException(status_code=500, detail="Conversion completed without generating output file.")

        out_bytes = out_path.read_bytes()
        return Response(
            content=out_bytes,
            media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            headers={"Content-Disposition": f'attachment; filename="{out_name}"'},
        )
    finally:
        cleanup_dir(job_dir)


@router.post("/docx-to-pdf")
async def docx_to_pdf(file: UploadFile = File(...)):
    """Convert a Word (.docx) file into a clean formatted PDF using python-docx + PyMuPDF."""
    raw = await file.read()
    try:
        doc = Document(io.BytesIO(raw))
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Invalid .docx file: {exc}")

    html_parts = [
        "<html><body style='font-family: sans-serif; font-size: 11pt; line-height: 1.45; color: #111;'>"
    ]
    for para in doc.paragraphs:
        text = (
            para.text.replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
            .strip()
        )
        if not text:
            html_parts.append("<p>&nbsp;</p>")
            continue
        style_name = (para.style.name if para.style else "").lower()
        if "heading 1" in style_name or "title" in style_name:
            html_parts.append(f"<h1 style='font-size: 18pt; margin-top: 12pt; margin-bottom: 6pt;'>{text}</h1>")
        elif "heading 2" in style_name:
            html_parts.append(f"<h2 style='font-size: 14pt; margin-top: 10pt; margin-bottom: 4pt;'>{text}</h2>")
        elif "heading 3" in style_name:
            html_parts.append(f"<h3 style='font-size: 12pt; margin-top: 8pt; margin-bottom: 4pt;'>{text}</h3>")
        else:
            html_parts.append(f"<p style='margin-bottom: 6pt;'>{text}</p>")

    for table in doc.tables:
        html_parts.append(
            "<table border='1' cellspacing='0' cellpadding='5' style='border-collapse: collapse; width: 100%; margin: 8pt 0;'>"
        )
        for row in table.rows:
            html_parts.append("<tr>")
            for cell in row.cells:
                cell_text = (
                    cell.text.replace("&", "&amp;")
                    .replace("<", "&lt;")
                    .replace(">", "&gt;")
                    .strip()
                )
                html_parts.append(f"<td>{cell_text}</td>")
            html_parts.append("</tr>")
        html_parts.append("</table>")

    html_parts.append("</body></html>")
    full_html = "\n".join(html_parts)

    try:
        story = fitz.Story(html=full_html)
        writer_buf = io.BytesIO()
        writer = fitz.DocumentWriter(writer_buf)
        mediabox = fitz.paper_rect("a4")
        where = mediabox + (42, 42, -42, -42)
        more = 1
        while more:
            device = writer.begin_page(mediabox)
            more, _ = story.place(where)
            story.draw(device)
            writer.end_page()
        writer.close()
        pdf_bytes = writer_buf.getvalue()
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Failed to render PDF from DOCX: {exc}")

    stem = Path(file.filename or "document").stem
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{stem}.pdf"'},
    )
