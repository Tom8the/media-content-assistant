from __future__ import annotations

import re

from video2txt.align.normalize import normalize_text
from video2txt.config import AlignmentSettings
from video2txt.models import (
    AlignmentResult,
    FusionMode,
    FusionSegment,
    SourceType,
    SubtitleCue,
    TranscriptSegment,
)


def _join_text(parts: list[str]) -> str:
    text = "".join(part.strip() for part in parts if part.strip())
    return re.sub(r"[ \t]+", " ", text).strip()


def fuse_timeline(
    alignment: AlignmentResult,
    asr_segments: list[TranscriptSegment],
    subtitles: list[SubtitleCue],
    settings: AlignmentSettings,
    *,
    mode: FusionMode = FusionMode.VERBATIM,
) -> list[FusionSegment]:
    asr_by_id = {segment.id: segment for segment in asr_segments}
    subtitle_by_id = {cue.id: cue for cue in subtitles}
    result: list[FusionSegment] = []

    for group in alignment.groups:
        asr_text = _join_text([asr_by_id[item_id].text for item_id in group.asr_ids])
        subtitle_text = _join_text([subtitle_by_id[item_id].text for item_id in group.subtitle_ids])
        has_asr = bool(asr_text)
        has_subtitle = bool(subtitle_text)
        has_hard_subtitle = any(
            subtitle_by_id[item_id].source == SourceType.HARD_SUBTITLE
            for item_id in group.subtitle_ids
        )
        is_unmatched_hard_subtitle = bool(
            has_subtitle
            and not has_asr
            and all(
                subtitle_by_id[item_id].source == SourceType.HARD_SUBTITLE
                for item_id in group.subtitle_ids
            )
        )
        include_in_transcript = not (
            is_unmatched_hard_subtitle and not settings.include_unmatched_hard_subtitles
        )
        normalized_lengths = (len(normalize_text(asr_text)), len(normalize_text(subtitle_text)))
        length_ratio = (
            min(normalized_lengths) / max(normalized_lengths)
            if max(normalized_lengths, default=0) > 0
            else 0.0
        )

        if has_asr and has_subtitle and group.matched:
            if mode == FusionMode.SUBTITLE:
                text = subtitle_text
                decision = "matched_subtitle_preferred"
            elif has_hard_subtitle:
                text = asr_text
                decision = "matched_asr_hard_subtitle_reference"
            elif mode == FusionMode.CLEAN:
                if group.text_similarity >= 0.60:
                    text = subtitle_text
                    decision = "matched_clean_subtitle"
                else:
                    text = asr_text
                    decision = "matched_clean_asr_conflict"
            elif group.text_similarity >= 0.80 and length_ratio >= 0.80:
                text = subtitle_text
                decision = "matched_subtitle_punctuation"
            else:
                text = asr_text
                decision = "matched_asr_verbatim"
            source = SourceType.MERGED
        elif has_asr:
            text = asr_text
            decision = "asr_only" if not has_subtitle else "conflict_asr_retained"
            source = SourceType.ASR
        else:
            text = subtitle_text
            decision = (
                "hard_subtitle_unmatched_review"
                if is_unmatched_hard_subtitle and not include_in_transcript
                else "subtitle_only"
            )
            source = subtitle_by_id[group.subtitle_ids[0]].source

        needs_review = bool(
            is_unmatched_hard_subtitle
            or (
                has_asr
                and has_subtitle
                and (not group.matched or group.score < settings.review_threshold)
            )
        )
        result.append(
            FusionSegment(
                start=group.start,
                end=group.end,
                text=text,
                source=source,
                asr_ids=group.asr_ids,
                subtitle_ids=group.subtitle_ids,
                time_score=group.time_score,
                text_similarity=group.text_similarity,
                decision=decision,
                needs_review=needs_review,
                include_in_transcript=include_in_transcript,
            )
        )
    return result
