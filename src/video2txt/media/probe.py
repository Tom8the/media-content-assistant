from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path
from typing import Any

from video2txt.models import MediaProbe, MediaStream, StreamKind, SubtitleKind

TEXT_SUBTITLE_CODECS = {
    "ass",
    "eia_608",
    "eia_708",
    "microdvd",
    "mov_text",
    "mpl2",
    "sami",
    "srt",
    "ssa",
    "subrip",
    "text",
    "ttml",
    "webvtt",
}

IMAGE_SUBTITLE_CODECS = {
    "dvb_subtitle",
    "dvd_subtitle",
    "hdmv_pgs_subtitle",
    "xsub",
}


class MediaProbeError(RuntimeError):
    """Raised when FFprobe cannot inspect an input file."""


def sha256_file(path: Path, *, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


def classify_subtitle(codec_name: str | None) -> SubtitleKind:
    if not codec_name:
        return SubtitleKind.UNKNOWN
    normalized = codec_name.lower()
    if normalized in TEXT_SUBTITLE_CODECS:
        return SubtitleKind.TEXT
    if normalized in IMAGE_SUBTITLE_CODECS:
        return SubtitleKind.IMAGE
    return SubtitleKind.UNKNOWN


def _optional_int(value: object) -> int | None:
    if value in (None, "", "N/A"):
        return None
    try:
        return int(str(value))
    except ValueError:
        return None


def _optional_float(value: object) -> float | None:
    if value in (None, "", "N/A"):
        return None
    try:
        return float(str(value))
    except ValueError:
        return None


def parse_ffprobe(payload: dict[str, Any], *, path: Path, sha256: str) -> MediaProbe:
    streams: list[MediaStream] = []
    for raw in payload.get("streams", []):
        codec_type = str(raw.get("codec_type", "unknown"))
        try:
            kind = StreamKind(codec_type)
        except ValueError:
            kind = StreamKind.UNKNOWN

        codec_name = raw.get("codec_name")
        tags = raw.get("tags") or {}
        disposition = raw.get("disposition") or {}
        streams.append(
            MediaStream(
                index=int(raw["index"]),
                kind=kind,
                codec_name=str(codec_name) if codec_name else None,
                language=tags.get("language"),
                title=tags.get("title"),
                is_default=bool(disposition.get("default", 0)),
                channels=_optional_int(raw.get("channels")),
                sample_rate=_optional_int(raw.get("sample_rate")),
                width=_optional_int(raw.get("width")),
                height=_optional_int(raw.get("height")),
                subtitle_kind=(
                    classify_subtitle(str(codec_name) if codec_name else None)
                    if kind == StreamKind.SUBTITLE
                    else SubtitleKind.NONE
                ),
            )
        )

    raw_format = payload.get("format") or {}
    return MediaProbe(
        path=path,
        sha256=sha256,
        format_name=raw_format.get("format_name"),
        duration=_optional_float(raw_format.get("duration")),
        bit_rate=_optional_int(raw_format.get("bit_rate")),
        streams=streams,
    )


def probe_media(path: Path, *, ffprobe_path: str = "ffprobe") -> MediaProbe:
    source = path.resolve()
    if not source.is_file():
        raise FileNotFoundError(source)

    command = [
        ffprobe_path,
        "-v",
        "error",
        "-print_format",
        "json",
        "-show_format",
        "-show_streams",
        str(source),
    ]
    try:
        result = subprocess.run(command, capture_output=True, check=False)
    except OSError as error:
        raise MediaProbeError(f"无法启动 FFprobe：{error}") from error
    stdout = result.stdout.decode("utf-8", errors="replace")
    stderr = result.stderr.decode("utf-8", errors="replace")
    if result.returncode != 0:
        detail = stderr.strip() or f"exit code {result.returncode}"
        raise MediaProbeError(f"FFprobe 无法解析媒体文件：{detail}")
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError as error:
        raise MediaProbeError("FFprobe 返回了无效 JSON") from error
    return parse_ffprobe(payload, path=source, sha256=sha256_file(source))


def select_stream(
    probe: MediaProbe,
    kind: StreamKind,
    *,
    stream_index: int | None = None,
    language: str | None = None,
) -> MediaStream | None:
    """Select a stream deterministically; explicit index always wins."""
    candidates = [stream for stream in probe.streams if stream.kind == kind]
    if stream_index is not None:
        selected = next((stream for stream in candidates if stream.index == stream_index), None)
        if selected is None:
            raise ValueError(f"未找到索引为 {stream_index} 的 {kind.value} 流")
        return selected
    if not candidates:
        return None

    normalized_language = language.casefold() if language else None
    if normalized_language:
        matching = [
            stream
            for stream in candidates
            if stream.language and stream.language.casefold() == normalized_language
        ]
        if matching:
            candidates = matching

    default_stream = next((stream for stream in candidates if stream.is_default), None)
    return default_stream or candidates[0]


def select_audio_stream(
    probe: MediaProbe, *, stream_index: int | None = None, language: str | None = None
) -> MediaStream:
    selected = select_stream(probe, StreamKind.AUDIO, stream_index=stream_index, language=language)
    if selected is None:
        raise ValueError("输入媒体没有音轨")
    return selected


def select_subtitle_stream(
    probe: MediaProbe, *, stream_index: int | None = None, language: str | None = None
) -> MediaStream | None:
    return select_stream(probe, StreamKind.SUBTITLE, stream_index=stream_index, language=language)
