from collections import deque
import io
import math
import zipfile
from pathlib import Path
from typing import Any, List
from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from fastapi.responses import Response
from PIL import Image, ImageFilter, ImageOps

router = APIRouter(prefix="/api/image", tags=["Image Tools"])

try:
    import pillow_heif  # type: ignore

    pillow_heif.register_heif_opener()
except Exception:
    pass

FORMAT_MAP = {
    "jpg": ("JPEG", "image/jpeg", ".jpg"),
    "jpeg": ("JPEG", "image/jpeg", ".jpg"),
    "png": ("PNG", "image/png", ".png"),
    "webp": ("WEBP", "image/webp", ".webp"),
    "ico": ("ICO", "image/x-icon", ".ico"),
    "tiff": ("TIFF", "image/tiff", ".tiff"),
    "tif": ("TIFF", "image/tiff", ".tiff"),
    "bmp": ("BMP", "image/bmp", ".bmp"),
    "heic": ("HEIF", "image/heic", ".heic"),
    "heif": ("HEIF", "image/heif", ".heic"),
}


def process_single_image(
    raw_bytes: bytes,
    filename: str,
    quality: int,
    target_format: str,
    max_width: int | None,
) -> tuple[bytes, str, str]:
    """Compress and optionally convert a single image."""
    try:
        img = Image.open(io.BytesIO(raw_bytes))
        img = ImageOps.exif_transpose(img)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Invalid image file '{filename}': {exc}")

    orig_ext = Path(filename).suffix.lower().lstrip(".")
    fmt_key = target_format.lower() if target_format and target_format != "original" else orig_ext
    if fmt_key not in FORMAT_MAP:
        fmt_key = "jpg"

    pil_format, mime_type, out_ext = FORMAT_MAP[fmt_key]

    if pil_format == "ICO":
        img = img.convert("RGBA")
        img.thumbnail((256, 256), Image.Resampling.LANCZOS)
        out_buf = io.BytesIO()
        img.save(out_buf, format="ICO", sizes=[(16, 16), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
        out_name = f"{Path(filename).stem}{out_ext}"
        return out_buf.getvalue(), out_name, mime_type

    if max_width and max_width > 0 and img.width > max_width:
        ratio = max_width / float(img.width)
        new_height = max(1, int(img.height * ratio))
        img = img.resize((max_width, new_height), Image.Resampling.LANCZOS)

    if pil_format == "JPEG" and img.mode in ("RGBA", "LA", "P"):
        background = Image.new("RGB", img.size, (255, 255, 255))
        if img.mode == "P":
            img = img.convert("RGBA")
        background.paste(img, mask=img.split()[-1] if img.mode in ("RGBA", "LA") else None)
        img = background
    elif pil_format == "JPEG" and img.mode != "RGB":
        img = img.convert("RGB")

    out_buf = io.BytesIO()
    save_kwargs = {"optimize": True}
    if pil_format in ("JPEG", "WEBP", "HEIF"):
        save_kwargs["quality"] = max(5, min(100, quality))
    elif pil_format == "PNG":
        if quality < 85:
            colors = max(16, int((quality / 100.0) * 256))
            img = img.quantize(colors=colors, method=Image.Quantize.FASTOCTREE)
        save_kwargs["compress_level"] = 9
    elif pil_format == "BMP":
        if img.mode in ("RGBA", "LA", "P"):
            img = img.convert("RGB")

    img.save(out_buf, format=pil_format, **save_kwargs)
    out_name = f"{Path(filename).stem}_optimized{out_ext}"
    return out_buf.getvalue(), out_name, mime_type


@router.post("/process")
async def process_images(
    files: List[UploadFile] = File(...),
    quality: int = Form(75),
    target_format: str = Form("original"),
    max_width: int = Form(0),
):
    if not files:
        raise HTTPException(status_code=400, detail="No images uploaded.")

    if len(files) == 1:
        f = files[0]
        raw = await f.read()
        out_bytes, out_name, mime_type = process_single_image(
            raw, f.filename or "image.jpg", quality, target_format, max_width or None
        )
        return Response(
            content=out_bytes,
            media_type=mime_type,
            headers={
                "Content-Disposition": f'attachment; filename="{out_name}"',
                "X-Original-Size": str(len(raw)),
                "X-Output-Size": str(len(out_bytes)),
            },
        )

    zip_buf = io.BytesIO()
    total_orig = 0
    total_out = 0
    with zipfile.ZipFile(zip_buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for idx, f in enumerate(files, start=1):
            raw = await f.read()
            total_orig += len(raw)
            out_bytes, out_name, _ = process_single_image(
                raw, f.filename or f"image_{idx}.jpg", quality, target_format, max_width or None
            )
            total_out += len(out_bytes)
            zf.writestr(out_name, out_bytes)

    zip_bytes = zip_buf.getvalue()
    return Response(
        content=zip_bytes,
        media_type="application/zip",
        headers={
            "Content-Disposition": 'attachment; filename="optimized_images.zip"',
            "X-Original-Size": str(total_orig),
            "X-Output-Size": str(total_out),
        },
    )


_rembg_sessions: dict[str, Any] = {}


def _get_rembg_session(model_name: str = "u2net"):
    global _rembg_sessions
    valid_models = {"u2net", "u2net_human_seg", "isnet-general-use", "birefnet-general"}
    if model_name not in valid_models:
        model_name = "u2net"

    if model_name not in _rembg_sessions:
        try:
            import rembg  # type: ignore

            _rembg_sessions[model_name] = rembg.new_session(model_name)
        except Exception:
            try:
                import rembg  # type: ignore

                _rembg_sessions[model_name] = rembg.new_session("u2net")
            except Exception:
                _rembg_sessions[model_name] = None
    return _rembg_sessions.get(model_name)


def _smart_border_matting(img: Image.Image, tolerance: int = 32, feather: int = 1) -> Image.Image:
    """Remove background using border-seeded flood-fill & soft alpha matting."""
    rgba = img.convert("RGBA")
    orig_w, orig_h = rgba.size

    max_work_dim = 900
    if max(orig_w, orig_h) > max_work_dim:
        scale = max_work_dim / float(max(orig_w, orig_h))
        work_w, work_h = max(1, int(orig_w * scale)), max(1, int(orig_h * scale))
        work_img = rgba.resize((work_w, work_h), Image.Resampling.BILINEAR)
    else:
        work_w, work_h = orig_w, orig_h
        work_img = rgba.copy()

    pixels = work_img.load()
    border_samples = []
    step_x = max(1, work_w // 20)
    step_y = max(1, work_h // 20)
    for x in range(0, work_w, step_x):
        border_samples.append(pixels[x, 0][:3])
        border_samples.append(pixels[x, work_h - 1][:3])
    for y in range(0, work_h, step_y):
        border_samples.append(pixels[0, y][:3])
        border_samples.append(pixels[work_w - 1, y][:3])

    avg_r = sum(c[0] for c in border_samples) / len(border_samples)
    avg_g = sum(c[1] for c in border_samples) / len(border_samples)
    avg_b = sum(c[2] for c in border_samples) / len(border_samples)

    tol_sq = float(tolerance * tolerance * 3)
    soft_sq = float((tolerance + 18) ** 2 * 3)

    visited = [[False] * work_h for _ in range(work_w)]
    alpha_grid = [[255] * work_h for _ in range(work_w)]
    queue: deque[tuple[int, int]] = deque()

    def color_dist_sq(rgb: tuple[int, int, int], ref: tuple[float, float, float]) -> float:
        dr = rgb[0] - ref[0]
        dg = rgb[1] - ref[1]
        db = rgb[2] - ref[2]
        return dr * dr + dg * dg + db * db

    for x in range(work_w):
        for y in (0, work_h - 1):
            if not visited[x][y] and color_dist_sq(pixels[x, y][:3], (avg_r, avg_g, avg_b)) <= soft_sq:
                visited[x][y] = True
                queue.append((x, y))
    for y in range(work_h):
        for x in (0, work_w - 1):
            if not visited[x][y] and color_dist_sq(pixels[x, y][:3], (avg_r, avg_g, avg_b)) <= soft_sq:
                visited[x][y] = True
                queue.append((x, y))

    while queue:
        cx, cy = queue.popleft()
        rgb = pixels[cx, cy][:3]
        d_global = color_dist_sq(rgb, (avg_r, avg_g, avg_b))

        if d_global <= tol_sq:
            alpha_grid[cx][cy] = 0
        elif d_global <= soft_sq:
            ratio = (math.sqrt(d_global) - math.sqrt(tol_sq)) / max(
                1.0, (math.sqrt(soft_sq) - math.sqrt(tol_sq))
            )
            alpha_grid[cx][cy] = max(0, min(255, int(ratio * 255)))
        else:
            continue

        for nx, ny in ((cx - 1, cy), (cx + 1, cy), (cx, cy - 1), (cx, cy + 1)):
            if 0 <= nx < work_w and 0 <= ny < work_h and not visited[nx][ny]:
                n_rgb = pixels[nx, ny][:3]
                d_local = color_dist_sq(n_rgb, rgb)
                if color_dist_sq(n_rgb, (avg_r, avg_g, avg_b)) <= soft_sq or d_local <= (tol_sq * 0.35):
                    visited[nx][ny] = True
                    queue.append((nx, ny))

    mask = Image.new("L", (work_w, work_h), 255)
    mask_px = mask.load()
    for x in range(work_w):
        for y in range(work_h):
            mask_px[x, y] = alpha_grid[x][y]

    if (work_w, work_h) != (orig_w, orig_h):
        mask = mask.resize((orig_w, orig_h), Image.Resampling.BILINEAR)

    if feather > 0:
        mask = mask.filter(ImageFilter.GaussianBlur(radius=min(5, feather)))

    rgba.putalpha(mask)
    return rgba


@router.post("/remove-bg")
async def remove_background(
    file: UploadFile = File(...),
    model: str = Form("u2net"),
    alpha_matting: bool = Form(True),
    alpha_erode: int = Form(10),
    tolerance: int = Form(32),
    feather: int = Form(1),
    bg_color: str = Form("transparent"),
):
    """Remove background with high-accuracy neural AI and optional alpha matting for hair & fine contours."""
    raw = await file.read()
    try:
        img = Image.open(io.BytesIO(raw))
        img = ImageOps.exif_transpose(img)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Invalid image: {exc}")

    tolerance = max(5, min(85, tolerance))
    feather = max(0, min(5, feather))

    result_rgba: Image.Image | None = None
    engine_used = "smart-matting"
    try:
        import rembg  # type: ignore

        session = _get_rembg_session(model)
        kwargs: dict[str, Any] = {
            "post_process_mask": True,
            "alpha_matting": alpha_matting,
        }
        if alpha_matting:
            kwargs["alpha_matting_foreground_threshold"] = 240
            kwargs["alpha_matting_background_threshold"] = 10
            kwargs["alpha_matting_erode_size"] = max(2, min(40, alpha_erode))

        if session:
            out_bytes = rembg.remove(raw, session=session, **kwargs)
        else:
            out_bytes = rembg.remove(raw, **kwargs)
        result_rgba = Image.open(io.BytesIO(out_bytes)).convert("RGBA")
        engine_used = f"ai-{model}" + ("-matting" if alpha_matting else "")
    except Exception:
        result_rgba = _smart_border_matting(img, tolerance=tolerance, feather=feather)

    if bg_color and bg_color.lower() != "transparent":
        hex_val = bg_color.lstrip("#")
        if len(hex_val) == 6:
            try:
                rgb_tuple = (
                    int(hex_val[0:2], 16),
                    int(hex_val[2:4], 16),
                    int(hex_val[4:6], 16),
                )
                solid = Image.new("RGBA", result_rgba.size, (*rgb_tuple, 255))
                solid.alpha_composite(result_rgba)
                result_rgba = solid
            except ValueError:
                pass

    buf = io.BytesIO()
    result_rgba.save(buf, format="PNG", optimize=True)
    final_bytes = buf.getvalue()
    stem = Path(file.filename or "image").stem

    return Response(
        content=final_bytes,
        media_type="image/png",
        headers={
            "Content-Disposition": f'attachment; filename="{stem}_nobg.png"',
            "X-Original-Size": str(len(raw)),
            "X-Output-Size": str(len(final_bytes)),
            "X-Engine-Used": engine_used,
        },
    )
