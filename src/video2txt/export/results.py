from __future__ import annotations

import json
from pathlib import Path

import pysubs2

from video2txt.models import AlignmentResult, FusionSegment


def export_text(segments: list[FusionSegment], path: Path) -> Path:
    target = path.resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    included = [
        segment for segment in segments if segment.include_in_transcript and segment.text.strip()
    ]
    if any(segment.original_text is not None for segment in included):
        paragraphs: list[str] = []
        current: list[str] = []
        for segment in included:
            if segment.paragraph_break_before and current:
                paragraphs.append("".join(current))
                current = []
            current.append(segment.text.strip())
        if current:
            paragraphs.append("".join(current))
        content = "\n\n".join(paragraphs)
    else:
        content = "\n".join(segment.text for segment in included)
    target.write_text(content + "\n", encoding="utf-8")
    return target


def export_srt(segments: list[FusionSegment], path: Path) -> Path:
    target = path.resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    subtitles = pysubs2.SSAFile()
    for segment in segments:
        if not segment.include_in_transcript or not segment.text.strip():
            continue
        subtitles.events.append(
            pysubs2.SSAEvent(
                start=round(segment.start * 1000),
                end=max(round(segment.end * 1000), round(segment.start * 1000) + 1),
                text=segment.text.replace("\n", "\\N"),
            )
        )
    subtitles.save(str(target), format_="srt", encoding="utf-8")
    return target


def export_json(
    alignment: AlignmentResult,
    segments: list[FusionSegment],
    path: Path,
) -> Path:
    target = path.resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "alignment": alignment.model_dump(mode="json"),
        "segments": [segment.model_dump(mode="json") for segment in segments],
    }
    target.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return target
