from __future__ import annotations

from pathlib import Path

from video2txt.models import SourceType, SubtitleCue

DEFAULT_ENCODINGS = ("utf-8-sig", "utf-8", "gb18030", "big5")


class SubtitleParseError(RuntimeError):
    """Raised when a subtitle file cannot be decoded or parsed."""


def _load_subtitle(path: Path):  # type: ignore[no-untyped-def]
    try:
        import pysubs2
    except ImportError as error:
        raise SubtitleParseError('未安装字幕依赖，请执行 pip install -e ".[subtitles]"') from error

    failures: list[str] = []
    for encoding in DEFAULT_ENCODINGS:
        try:
            return pysubs2.load(str(path), encoding=encoding)
        except UnicodeError as error:
            failures.append(f"{encoding}: {error}")
        except Exception as error:
            raise SubtitleParseError(f"字幕格式解析失败：{error}") from error
    raise SubtitleParseError("无法识别字幕编码：" + "; ".join(failures))


def parse_subtitle_file(
    path: Path,
    *,
    source: SourceType = SourceType.EXTERNAL_SUBTITLE,
    language: str | None = None,
) -> list[SubtitleCue]:
    subtitle_path = path.resolve()
    if not subtitle_path.is_file():
        raise FileNotFoundError(subtitle_path)
    subtitles = _load_subtitle(subtitle_path)
    cues: list[SubtitleCue] = []
    for index, event in enumerate(subtitles, start=1):
        raw_text = str(event.text)
        clean_text = str(event.plaintext).replace("\r\n", "\n").strip()
        if not clean_text:
            continue
        cues.append(
            SubtitleCue(
                id=f"sub-{index:04d}",
                start=round(float(event.start) / 1000, 3),
                end=round(float(event.end) / 1000, 3),
                text=clean_text,
                raw_text=raw_text,
                source=source,
                confidence=1.0,
                language=language,
            )
        )
    if not cues:
        raise SubtitleParseError("字幕文件中没有可用文本")
    return cues
