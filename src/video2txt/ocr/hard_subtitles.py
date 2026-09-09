from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path

from PIL import Image, ImageFilter, ImageOps

from video2txt.config import OCRSettings
from video2txt.media.frames import extract_subtitle_frames
from video2txt.models import OCRLine, OCRObservation, SourceType, SubtitleCue
from video2txt.ocr.base import OCREngine
from video2txt.ocr.paddle import get_paddle_ocr_engine


@dataclass
class HardSubtitleResult:
    cues: list[SubtitleCue]
    observations: list[OCRObservation]
    frame_count: int
    ocr_calls: int


@dataclass(frozen=True)
class HardSubtitleProgress:
    total_frames: int
    processed_frames: int
    ocr_calls: int
    skipped_frames: int
    observation: OCRObservation | None = None


def _image_signature(path: Path, settings: OCRSettings) -> bytes:
    with Image.open(path) as image:
        gray = image.convert("L")
        width, height = gray.size
        box = (
            round(width * settings.signature_left),
            round(height * settings.signature_top),
            round(width * settings.signature_right),
            round(height * settings.signature_bottom),
        )
        focused = ImageOps.autocontrast(gray.crop(box)).resize((192, 32))
        edges = focused.filter(ImageFilter.FIND_EDGES)
        return bytes(
            255 if value >= settings.signature_edge_threshold else 0 for value in edges.tobytes()
        )


def _image_difference(left: bytes, right: bytes) -> float:
    if len(left) != len(right) or not left:
        return 1.0
    return sum(abs(a - b) for a, b in zip(left, right, strict=True)) / (len(left) * 255)


def _matching_text(value: str) -> str:
    return "".join(character.casefold() for character in value if character.isalnum())


def _text_similarity(left: str, right: str) -> float:
    normalized_left = _matching_text(left)
    normalized_right = _matching_text(right)
    if not normalized_left or not normalized_right:
        return 0.0
    return SequenceMatcher(None, normalized_left, normalized_right).ratio()


def _filter_subtitle_lines(
    lines: list[OCRLine],
    image_size: tuple[int, int],
    settings: OCRSettings,
) -> list[OCRLine]:
    """Keep text shaped and positioned like a subtitle inside the crop."""
    image_width, image_height = image_size
    accepted: list[tuple[float, float, OCRLine]] = []
    for line in lines:
        if not line.bbox:
            accepted.append((0, 0, line))
            continue
        xs = [point[0] for point in line.bbox]
        ys = [point[1] for point in line.bbox]
        width = max(xs) - min(xs)
        height = max(ys) - min(ys)
        center_x = (max(xs) + min(xs)) / 2
        center_y = (max(ys) + min(ys)) / 2
        if width < image_width * settings.text_min_width_ratio:
            continue
        if height < image_height * settings.text_min_height_ratio:
            continue
        if not (
            image_width * settings.text_center_left
            <= center_x
            <= image_width * settings.text_center_right
        ):
            continue
        if not (
            image_height * settings.text_center_top
            <= center_y
            <= image_height * settings.text_center_bottom
        ):
            continue
        accepted.append((center_y, center_x, line))
    accepted.sort(key=lambda item: item[0])
    row_tolerance = max(1.0, image_height * 0.08)
    rows: list[list[tuple[float, float, OCRLine]]] = []
    for item in accepted:
        if not rows or abs(item[0] - rows[-1][0][0]) > row_tolerance:
            rows.append([item])
        else:
            rows[-1].append(item)
    for row in rows:
        row.sort(key=lambda item: item[1])
    return [line for row in rows for _, _, line in row]


def extract_hard_subtitles(
    source: Path,
    frame_dir: Path,
    settings: OCRSettings,
    *,
    ffmpeg_path: str = "ffmpeg",
    duration: float | None = None,
    engine: OCREngine | None = None,
    progress_callback: Callable[[HardSubtitleProgress], None] | None = None,
) -> HardSubtitleResult:
    frames = extract_subtitle_frames(
        source,
        frame_dir,
        settings,
        ffmpeg_path=ffmpeg_path,
    )
    resolved_engine = engine or get_paddle_ocr_engine(settings)
    cues: list[SubtitleCue] = []
    observations: list[OCRObservation] = []
    previous_signature: bytes | None = None
    active_cue: SubtitleCue | None = None
    ocr_calls = 0
    skipped_frames = 0

    if progress_callback is not None:
        progress_callback(
            HardSubtitleProgress(
                total_frames=len(frames),
                processed_frames=0,
                ocr_calls=0,
                skipped_frames=0,
            )
        )

    for index, frame in enumerate(frames):
        timestamp = round(index * settings.sample_interval, 3)
        cue_end = timestamp + settings.sample_interval
        if duration is not None:
            cue_end = min(cue_end, duration)
        signature = _image_signature(frame, settings)
        difference = (
            1.0 if previous_signature is None else _image_difference(previous_signature, signature)
        )
        previous_signature = signature

        if difference < settings.image_change_threshold:
            if active_cue is not None:
                active_cue.end = round(max(active_cue.end, cue_end), 3)
            observation = OCRObservation(
                frame=frame.name,
                timestamp=timestamp,
                image_difference=difference,
                skipped_as_duplicate=True,
            )
            observations.append(observation)
            skipped_frames += 1
            if progress_callback is not None:
                progress_callback(
                    HardSubtitleProgress(
                        total_frames=len(frames),
                        processed_frames=index + 1,
                        ocr_calls=ocr_calls,
                        skipped_frames=skipped_frames,
                        observation=observation,
                    )
                )
            continue

        recognized_lines = resolved_engine.recognize(frame)
        ocr_calls += 1
        with Image.open(frame) as image:
            lines = _filter_subtitle_lines(recognized_lines, image.size, settings)
        observation = OCRObservation(
            frame=frame.name,
            timestamp=timestamp,
            image_difference=difference,
            lines=lines,
        )
        observations.append(observation)
        if progress_callback is not None:
            progress_callback(
                HardSubtitleProgress(
                    total_frames=len(frames),
                    processed_frames=index + 1,
                    ocr_calls=ocr_calls,
                    skipped_frames=skipped_frames,
                    observation=observation,
                )
            )
        text = " ".join(line.text for line in lines).strip()
        if not text:
            active_cue = None
            continue
        confidence = sum(line.confidence for line in lines) / len(lines)
        if (
            active_cue is not None
            and _text_similarity(active_cue.text, text) >= settings.text_similarity_threshold
        ):
            active_cue.end = round(max(active_cue.end, cue_end), 3)
            if confidence > (active_cue.confidence or 0):
                active_cue.text = text
                active_cue.raw_text = text
                active_cue.confidence = confidence
            continue

        active_cue = SubtitleCue(
            id=f"ocr-{len(cues) + 1:04d}",
            start=timestamp,
            end=round(cue_end, 3),
            text=text,
            raw_text=text,
            source=SourceType.HARD_SUBTITLE,
            confidence=confidence,
            language=settings.language,
        )
        cues.append(active_cue)

    cues = [cue for cue in cues if cue.end - cue.start >= settings.min_duration]
    return HardSubtitleResult(
        cues=cues,
        observations=observations,
        frame_count=len(frames),
        ocr_calls=ocr_calls,
    )
