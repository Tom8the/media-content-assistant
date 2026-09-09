from __future__ import annotations

from statistics import median

from rapidfuzz.fuzz import ratio

from video2txt.align.normalize import normalize_text
from video2txt.config import AlignmentSettings
from video2txt.models import (
    AlignmentGroup,
    AlignmentResult,
    SubtitleCue,
    TranscriptSegment,
)


def text_similarity(left: str, right: str) -> float:
    normalized_left = normalize_text(left)
    normalized_right = normalize_text(right)
    if not normalized_left or not normalized_right:
        return 0.0
    return float(ratio(normalized_left, normalized_right)) / 100


def interval_overlap_score(
    left_start: float, left_end: float, right_start: float, right_end: float
) -> float:
    intersection = max(0.0, min(left_end, right_end) - max(left_start, right_start))
    shortest = min(left_end - left_start, right_end - right_start)
    if shortest <= 0:
        return 0.0
    return min(1.0, intersection / shortest)


def estimate_subtitle_offset(
    asr_segments: list[TranscriptSegment],
    subtitles: list[SubtitleCue],
    *,
    similarity_threshold: float = 0.72,
    max_offset: float = 30.0,
) -> float:
    """Estimate subtitle delay as subtitle midpoint minus ASR midpoint."""
    offsets: list[float] = []
    for asr in asr_segments:
        best: tuple[float, SubtitleCue] | None = None
        for subtitle in subtitles:
            similarity = text_similarity(asr.text, subtitle.text)
            if similarity < similarity_threshold:
                continue
            candidate = (similarity, subtitle)
            if best is None or candidate[0] > best[0]:
                best = candidate
        if best is None:
            continue
        subtitle = best[1]
        asr_midpoint = (asr.start + asr.end) / 2
        subtitle_midpoint = (subtitle.start + subtitle.end) / 2
        offset = subtitle_midpoint - asr_midpoint
        if abs(offset) <= max_offset:
            offsets.append(offset)
    return round(float(median(offsets)), 3) if offsets else 0.0


def _interval_gap(
    left_start: float, left_end: float, right_start: float, right_end: float
) -> float:
    if left_end < right_start:
        return right_start - left_end
    if right_end < left_start:
        return left_start - right_end
    return 0.0


def _confidence(values: list[float | None]) -> float:
    present = [value for value in values if value is not None]
    return sum(present) / len(present) if present else 0.5


def align_timeline(
    asr_segments: list[TranscriptSegment],
    subtitles: list[SubtitleCue],
    settings: AlignmentSettings,
    *,
    tolerance: float = 0.25,
) -> AlignmentResult:
    if not asr_segments and not subtitles:
        return AlignmentResult()

    offset = estimate_subtitle_offset(asr_segments, subtitles)
    groups: list[AlignmentGroup] = []
    asr_index = 0
    subtitle_index = 0

    def build_group(
        asr_items: list[TranscriptSegment], subtitle_items: list[SubtitleCue]
    ) -> AlignmentGroup:
        starts = [item.start for item in asr_items]
        starts.extend(max(0.0, item.start - offset) for item in subtitle_items)
        ends = [item.end for item in asr_items]
        ends.extend(max(0.0, item.end - offset) for item in subtitle_items)

        if asr_items and subtitle_items:
            asr_start = min(item.start for item in asr_items)
            asr_end = max(item.end for item in asr_items)
            subtitle_start = min(max(0.0, item.start - offset) for item in subtitle_items)
            subtitle_end = max(max(0.0, item.end - offset) for item in subtitle_items)
            time_score = interval_overlap_score(asr_start, asr_end, subtitle_start, subtitle_end)
            similarity = text_similarity(
                "".join(item.text for item in asr_items),
                "".join(item.text for item in subtitle_items),
            )
            confidence_score = _confidence(
                [item.confidence for item in asr_items]
                + [item.confidence for item in subtitle_items]
            )
            score = (
                settings.time_weight * time_score
                + settings.text_weight * similarity
                + settings.confidence_weight * confidence_score
            )
            matched = score >= settings.match_threshold
        else:
            time_score = 0.0
            similarity = 0.0
            confidence_score = _confidence(
                [item.confidence for item in asr_items]
                + [item.confidence for item in subtitle_items]
            )
            score = 0.0
            matched = False

        return AlignmentGroup(
            asr_ids=[item.id for item in asr_items],
            subtitle_ids=[item.id for item in subtitle_items],
            start=round(min(starts), 3),
            end=round(max(ends), 3),
            time_score=round(time_score, 6),
            text_similarity=round(similarity, 6),
            confidence_score=round(confidence_score, 6),
            score=round(score, 6),
            matched=matched,
        )

    while asr_index < len(asr_segments) or subtitle_index < len(subtitles):
        if asr_index >= len(asr_segments):
            groups.append(build_group([], [subtitles[subtitle_index]]))
            subtitle_index += 1
            continue
        if subtitle_index >= len(subtitles):
            groups.append(build_group([asr_segments[asr_index]], []))
            asr_index += 1
            continue

        current_asr = asr_segments[asr_index]
        current_subtitle = subtitles[subtitle_index]
        adjusted_subtitle_start = max(0.0, current_subtitle.start - offset)
        adjusted_subtitle_end = max(adjusted_subtitle_start, current_subtitle.end - offset)
        current_gap = _interval_gap(
            current_asr.start,
            current_asr.end,
            adjusted_subtitle_start,
            adjusted_subtitle_end,
        )

        candidates: list[tuple[float, int, int, AlignmentGroup]] = []
        if current_gap <= tolerance:
            for asr_count in range(1, min(3, len(asr_segments) - asr_index) + 1):
                for subtitle_count in range(1, min(3, len(subtitles) - subtitle_index) + 1):
                    candidate = build_group(
                        asr_segments[asr_index : asr_index + asr_count],
                        subtitles[subtitle_index : subtitle_index + subtitle_count],
                    )
                    size_penalty = 0.025 * (asr_count + subtitle_count - 2)
                    candidates.append(
                        (candidate.score - size_penalty, asr_count, subtitle_count, candidate)
                    )

        best = max(candidates, default=None, key=lambda item: (item[0], -item[1] - item[2]))
        if best is not None and best[3].matched:
            _, asr_count, subtitle_count, group = best
            groups.append(group)
            asr_index += asr_count
            subtitle_index += subtitle_count
            continue

        if current_asr.end <= adjusted_subtitle_end:
            groups.append(build_group([current_asr], []))
            asr_index += 1
        else:
            groups.append(build_group([], [current_subtitle]))
            subtitle_index += 1

    return AlignmentResult(subtitle_offset=offset, groups=groups)
