from __future__ import annotations

import re
from dataclasses import dataclass
from difflib import SequenceMatcher

from video2txt.models import FusionSegment

_WHITESPACE = re.compile(r"\s+")
_CJK_SPACE = re.compile(r"(?<=[\u3400-\u9fff])\s+(?=[\u3400-\u9fff])")
_SPACE_BEFORE_PUNCTUATION = re.compile(r"\s+([，。！？；：、])")
_SPACE_AFTER_PUNCTUATION = re.compile(r"([，。！？；：、])\s+")
_LEADING_FILLER = re.compile(r"^(?:嗯+|呃+|额+|啊+)(?:[，、]\s*|\s+)")
_STANDALONE_FILLER = re.compile(r"(^|[，。！？；：])(?:嗯+|呃+|额+|啊+)(?=$|[，。！？；：])")
_REPEATED_DISCOURSE = re.compile(r"(然后|就是|这个时候|对不对)(?:[，、\s]+\1)+")
_REPEATED_PUNCTUATION = re.compile(r"([，。！？；：])\1+")
_TERMINAL_PUNCTUATION = ("。", "！", "？")
_QUESTION_ENDING = re.compile(r"(?:吗|么|呢|是不是|好不好|对不对)$")
_PUNCTUATION_TRANSLATION = str.maketrans({",": "，", ";": "；", ":": "：", "?": "？", "!": "！"})


@dataclass(frozen=True)
class CleaningResult:
    segments: list[FusionSegment]
    changed_segments: int
    removed_duplicates: int
    removed_fillers: int
    paragraph_count: int


def _comparison_key(value: str) -> str:
    return "".join(character.casefold() for character in value if character.isalnum())


def _clean_sentence(value: str) -> str:
    text = _WHITESPACE.sub(" ", value.strip()).translate(_PUNCTUATION_TRANSLATION)
    text = _LEADING_FILLER.sub("", text)
    text = _STANDALONE_FILLER.sub(r"\1", text)
    text = _REPEATED_DISCOURSE.sub(r"\1", text)
    text = _SPACE_BEFORE_PUNCTUATION.sub(r"\1", text)
    text = _SPACE_AFTER_PUNCTUATION.sub(r"\1", text)
    text = _CJK_SPACE.sub("", text)
    text = _REPEATED_PUNCTUATION.sub(r"\1", text).strip("，；：、 ")
    if not text:
        return ""
    if not text.endswith(_TERMINAL_PUNCTUATION):
        text += "？" if _QUESTION_ENDING.search(text) else "。"
    return text


def clean_fusion_segments(
    segments: list[FusionSegment],
    *,
    paragraph_max_chars: int = 180,
    paragraph_gap_seconds: float = 2.0,
) -> CleaningResult:
    result: list[FusionSegment] = []
    previous_included: FusionSegment | None = None
    previous_key = ""
    last_timeline_end: float | None = None
    paragraph_chars = 0
    paragraph_count = 0
    changed_segments = 0
    removed_duplicates = 0
    removed_fillers = 0

    for segment in segments:
        if not segment.include_in_transcript:
            result.append(segment)
            continue

        cleaned = _clean_sentence(segment.text)
        original_text = segment.text
        if not cleaned:
            result.append(
                segment.model_copy(
                    update={
                        "text": "",
                        "original_text": original_text,
                        "decision": "clean_removed_filler",
                        "include_in_transcript": False,
                    }
                )
            )
            removed_fillers += 1
            last_timeline_end = segment.end
            continue

        key = _comparison_key(cleaned)
        is_duplicate = bool(
            previous_included is not None
            and segment.start - previous_included.end <= paragraph_gap_seconds
            and previous_key
            and (
                key == previous_key
                or (
                    min(len(key), len(previous_key)) >= 8
                    and SequenceMatcher(None, previous_key, key).ratio() >= 0.97
                )
            )
        )
        if is_duplicate:
            result.append(
                segment.model_copy(
                    update={
                        "text": cleaned,
                        "original_text": original_text,
                        "decision": "clean_removed_duplicate",
                        "include_in_transcript": False,
                    }
                )
            )
            removed_duplicates += 1
            last_timeline_end = segment.end
            continue

        gap = (
            segment.start - last_timeline_end
            if last_timeline_end is not None
            else paragraph_gap_seconds
        )
        starts_paragraph = bool(
            previous_included is None
            or gap >= paragraph_gap_seconds
            or paragraph_chars + len(cleaned) > paragraph_max_chars
        )
        if starts_paragraph:
            paragraph_count += 1
            paragraph_chars = 0
        paragraph_chars += len(cleaned)
        if cleaned != original_text.strip():
            changed_segments += 1
        cleaned_segment = segment.model_copy(
            update={
                "text": cleaned,
                "original_text": original_text,
                "decision": f"cleaned_{segment.decision}",
                "paragraph_break_before": starts_paragraph,
            }
        )
        result.append(cleaned_segment)
        previous_included = cleaned_segment
        previous_key = key
        last_timeline_end = segment.end

    return CleaningResult(
        segments=result,
        changed_segments=changed_segments,
        removed_duplicates=removed_duplicates,
        removed_fillers=removed_fillers,
        paragraph_count=paragraph_count,
    )
