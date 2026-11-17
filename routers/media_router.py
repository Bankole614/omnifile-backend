import subprocess
from pathlib import Path
import imageio_ffmpeg
from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from fastapi.responses import Response
from utils.temp_manager import cleanup_dir, create_job_dir, save_upload

router = APIRouter(prefix="/api/media", tags=["Audio & Video Tools"])

AUDIO_FORMATS = {
    "mp3": ("libmp3lame", "audio/mpeg", ".mp3"),
    "wav": ("pcm_s16le", "audio/wav", ".wav"),
    "m4a": ("aac", "audio/mp4", ".m4a"),
    "ogg": ("libvorbis", "audio/ogg", ".ogg"),
}


def get_ffmpeg_bin() -> str:
    try:
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=f"FFmpeg binary unavailable via imageio-ffmpeg: {exc}",
        )


def _trim_args(start_time: str, end_time: str) -> list[str]:
    args: list[str] = []
    s = (start_time or "").strip()
    e = (end_time or "").strip()
    if s:
        args += ["-ss", s]
    if e:
        args += ["-to", e]
    return args


@router.post("/video-to-audio")
async def video_to_audio(
    file: UploadFile = File(...),
    output_format: str = Form("mp3"),
    bitrate: str = Form("192k"),
    start_time: str = Form(""),
    end_time: str = Form(""),
):
    """Extract audio stream from an uploaded video file with optional start/end trim."""
    fmt_key = output_format.lower()
    if fmt_key not in AUDIO_FORMATS:
        fmt_key = "mp3"
    codec, mime_type, ext = AUDIO_FORMATS[fmt_key]
    bitrate = bitrate if bitrate in ("64k", "96k", "128k", "192k", "256k", "320k") else "192k"

    job_dir = create_job_dir()
    try:
        in_path = await save_upload(file, job_dir)
        out_name = f"{Path(file.filename or 'video').stem}_audio{ext}"
        out_path = job_dir / out_name

        ffmpeg_exe = get_ffmpeg_bin()
        cmd = [ffmpeg_exe, "-y", "-i", str(in_path)]
        cmd += _trim_args(start_time, end_time)
        cmd += ["-vn", "-acodec", codec]
        if fmt_key != "wav":
            cmd += ["-b:a", bitrate]
        cmd.append(str(out_path))

        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
        if proc.returncode != 0 or not out_path.exists():
            err_tail = (proc.stderr or "")[-400:]
            raise HTTPException(
                status_code=400,
                detail=f"Failed to extract audio from video. Ensure the video has an audio track. Details: {err_tail}",
            )

        out_bytes = out_path.read_bytes()
        orig_size = in_path.stat().st_size
        return Response(
            content=out_bytes,
            media_type=mime_type,
            headers={
                "Content-Disposition": f'attachment; filename="{out_name}"',
                "X-Original-Size": str(orig_size),
                "X-Output-Size": str(len(out_bytes)),
            },
        )
    finally:
        cleanup_dir(job_dir)


@router.post("/compress-audio")
async def compress_audio(
    file: UploadFile = File(...),
    bitrate: str = Form("96k"),
    channels: str = Form("original"),
    output_format: str = Form("mp3"),
    start_time: str = Form(""),
    end_time: str = Form(""),
):
    """Compress an audio file to a lower target bitrate, optional mono channel, and optional trim."""
    fmt_key = output_format.lower()
    if fmt_key not in ("mp3", "m4a", "ogg"):
        fmt_key = "mp3"
    codec, mime_type, ext = AUDIO_FORMATS[fmt_key]
    bitrate = bitrate if bitrate in ("48k", "64k", "96k", "128k", "192k") else "96k"

    job_dir = create_job_dir()
    try:
        in_path = await save_upload(file, job_dir)
        out_name = f"{Path(file.filename or 'audio').stem}_compressed{ext}"
        out_path = job_dir / out_name

        ffmpeg_exe = get_ffmpeg_bin()
        cmd = [ffmpeg_exe, "-y", "-i", str(in_path)]
        cmd += _trim_args(start_time, end_time)
        cmd += ["-vn", "-acodec", codec, "-b:a", bitrate]
        if channels == "mono":
            cmd += ["-ac", "1"]
        cmd.append(str(out_path))

        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
        if proc.returncode != 0 or not out_path.exists():
            err_tail = (proc.stderr or "")[-400:]
            raise HTTPException(
                status_code=400,
                detail=f"Audio compression failed: {err_tail}",
            )

        out_bytes = out_path.read_bytes()
        orig_size = in_path.stat().st_size
        return Response(
            content=out_bytes,
            media_type=mime_type,
            headers={
                "Content-Disposition": f'attachment; filename="{out_name}"',
                "X-Original-Size": str(orig_size),
                "X-Output-Size": str(len(out_bytes)),
            },
        )
    finally:
        cleanup_dir(job_dir)
