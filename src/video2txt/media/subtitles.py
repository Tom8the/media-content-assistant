from __future__ import annotations

import subprocess
from pathlib import Path


class SubtitleExtractionError(RuntimeError):
    """Raised when FFmpeg cannot extract a text subtitle stream."""


def extract_text_subtitle(
    input_path: Path,
    output_path: Path,
    *,
    ffmpeg_path: str = "ffmpeg",
    stream_index: int | None = None,
) -> Path:
    source = input_path.resolve()
    target = output_path.resolve()
    if not source.is_file():
        raise FileNotFoundError(source)
    target.parent.mkdir(parents=True, exist_ok=True)
    selector = f"0:{stream_index}" if stream_index is not None else "0:s:0"
    command = [
        ffmpeg_path,
        "-y",
        "-v",
        "error",
        "-i",
        str(source),
        "-map",
        selector,
        "-vn",
        "-an",
        "-dn",
        "-c:s",
        "srt",
        str(target),
    ]
    try:
        result = subprocess.run(command, capture_output=True, text=True, check=False)
    except OSError as error:
        raise SubtitleExtractionError(f"无法启动 FFmpeg：{error}") from error
    if result.returncode != 0:
        detail = result.stderr.strip() or f"exit code {result.returncode}"
        raise SubtitleExtractionError(f"文本字幕提取失败：{detail}")
    if not target.is_file() or target.stat().st_size == 0:
        raise SubtitleExtractionError("FFmpeg 未生成有效字幕文件")
    return target
