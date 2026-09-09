from __future__ import annotations

import subprocess
from pathlib import Path


class AudioExtractionError(RuntimeError):
    """Raised when FFmpeg cannot create the normalized audio file."""


def normalize_audio(
    input_path: Path,
    output_path: Path,
    *,
    ffmpeg_path: str = "ffmpeg",
    stream_index: int | None = None,
    sample_rate: int = 16_000,
    channels: int = 1,
) -> Path:
    source = input_path.resolve()
    target = output_path.resolve()
    if not source.is_file():
        raise FileNotFoundError(source)
    if sample_rate <= 0:
        raise ValueError("sample_rate must be positive")
    if channels <= 0:
        raise ValueError("channels must be positive")
    target.parent.mkdir(parents=True, exist_ok=True)

    stream_selector = f"0:{stream_index}" if stream_index is not None else "0:a:0"
    command = [
        ffmpeg_path,
        "-y",
        "-v",
        "error",
        "-i",
        str(source),
        "-map",
        stream_selector,
        "-vn",
        "-sn",
        "-dn",
        "-ac",
        str(channels),
        "-ar",
        str(sample_rate),
        "-c:a",
        "pcm_s16le",
        str(target),
    ]
    try:
        result = subprocess.run(command, capture_output=True, text=True, check=False)
    except OSError as error:
        raise AudioExtractionError(f"无法启动 FFmpeg：{error}") from error
    if result.returncode != 0:
        detail = result.stderr.strip() or f"exit code {result.returncode}"
        raise AudioExtractionError(f"音频标准化失败：{detail}")
    if not target.is_file() or target.stat().st_size <= 44:
        raise AudioExtractionError("FFmpeg 未生成有效 WAV 文件")
    return target
