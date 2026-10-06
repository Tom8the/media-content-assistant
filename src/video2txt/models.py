from __future__ import annotations

from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, Field, model_validator


class TaskStatus(StrEnum):
    QUEUED = "queued"
    DOWNLOADING = "downloading"
    RECORDING = "recording"
    PROBING = "probing"
    EXTRACTING = "extracting"
    TRANSCRIBING = "transcribing"
    SUBTITLE_PROCESSING = "subtitle_processing"
    ALIGNING = "aligning"
    EXPORTING = "exporting"
    TRANSLATING = "translating"
    SUMMARIZING = "summarizing"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class StreamKind(StrEnum):
    VIDEO = "video"
    AUDIO = "audio"
    SUBTITLE = "subtitle"
    DATA = "data"
    ATTACHMENT = "attachment"
    UNKNOWN = "unknown"


class SubtitleKind(StrEnum):
    TEXT = "text"
    IMAGE = "image"
    NONE = "none"
    UNKNOWN = "unknown"


class SourceType(StrEnum):
    ASR = "asr"
    EMBEDDED_TEXT = "embedded_text"
    EMBEDDED_IMAGE = "embedded_image"
    HARD_SUBTITLE = "hard_subtitle"
    EXTERNAL_SUBTITLE = "external_subtitle"
    MERGED = "merged"


class FusionMode(StrEnum):
    VERBATIM = "verbatim"
    SUBTITLE = "subtitle"
    CLEAN = "clean"


class ASRMode(StrEnum):
    LOCAL = "local"
    API = "api"


class MediaStream(BaseModel):
    index: int = Field(ge=0)
    kind: StreamKind
    codec_name: str | None = None
    language: str | None = None
    title: str | None = None
    is_default: bool = False
    channels: int | None = Field(default=None, ge=1)
    sample_rate: int | None = Field(default=None, ge=1)
    width: int | None = Field(default=None, ge=1)
    height: int | None = Field(default=None, ge=1)
    subtitle_kind: SubtitleKind = SubtitleKind.NONE


class MediaProbe(BaseModel):
    path: Path
    sha256: str
    format_name: str | None = None
    duration: float | None = Field(default=None, ge=0)
    bit_rate: int | None = Field(default=None, ge=0)
    streams: list[MediaStream] = Field(default_factory=list)

    @property
    def audio_streams(self) -> list[MediaStream]:
        return [stream for stream in self.streams if stream.kind == StreamKind.AUDIO]

    @property
    def video_streams(self) -> list[MediaStream]:
        return [stream for stream in self.streams if stream.kind == StreamKind.VIDEO]

    @property
    def subtitle_streams(self) -> list[MediaStream]:
        return [stream for stream in self.streams if stream.kind == StreamKind.SUBTITLE]


class TranscriptWord(BaseModel):
    start: float = Field(ge=0)
    end: float = Field(ge=0)
    text: str
    probability: float | None = Field(default=None, ge=0, le=1)

    @model_validator(mode="after")
    def validate_time_range(self) -> TranscriptWord:
        if self.end < self.start:
            raise ValueError("word end must be greater than or equal to start")
        return self


class TranscriptSegment(BaseModel):
    id: str
    start: float = Field(ge=0)
    end: float = Field(ge=0)
    text: str
    confidence: float | None = Field(default=None, ge=0, le=1)
    words: list[TranscriptWord] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_time_range(self) -> TranscriptSegment:
        if self.end < self.start:
            raise ValueError("segment end must be greater than or equal to start")
        return self


class Transcript(BaseModel):
    engine: str
    model: str
    language: str | None = None
    audio_sha256: str
    options: dict[str, object] = Field(default_factory=dict)
    segments: list[TranscriptSegment] = Field(default_factory=list)


class SubtitleCue(BaseModel):
    id: str
    start: float = Field(ge=0)
    end: float = Field(ge=0)
    text: str
    raw_text: str | None = None
    source: SourceType
    confidence: float | None = Field(default=None, ge=0, le=1)
    language: str | None = None

    @model_validator(mode="after")
    def validate_time_range(self) -> SubtitleCue:
        if self.end < self.start:
            raise ValueError("subtitle end must be greater than or equal to start")
        return self


class OCRLine(BaseModel):
    text: str
    confidence: float = Field(ge=0, le=1)
    bbox: list[list[float]] | None = None


class OCRObservation(BaseModel):
    frame: str
    timestamp: float = Field(ge=0)
    image_difference: float = Field(ge=0, le=1)
    skipped_as_duplicate: bool = False
    lines: list[OCRLine] = Field(default_factory=list)


class FusionSegment(BaseModel):
    start: float = Field(ge=0)
    end: float = Field(ge=0)
    text: str
    source: SourceType = SourceType.MERGED
    asr_ids: list[str] = Field(default_factory=list)
    subtitle_ids: list[str] = Field(default_factory=list)
    time_score: float | None = Field(default=None, ge=0, le=1)
    text_similarity: float | None = Field(default=None, ge=0, le=1)
    decision: str
    needs_review: bool = False
    include_in_transcript: bool = True
    original_text: str | None = None
    paragraph_break_before: bool = False


class AlignmentGroup(BaseModel):
    asr_ids: list[str] = Field(default_factory=list)
    subtitle_ids: list[str] = Field(default_factory=list)
    start: float = Field(ge=0)
    end: float = Field(ge=0)
    time_score: float = Field(ge=0, le=1)
    text_similarity: float = Field(ge=0, le=1)
    confidence_score: float = Field(ge=0, le=1)
    score: float = Field(ge=0, le=1)
    matched: bool


class AlignmentResult(BaseModel):
    subtitle_offset: float = 0.0
    groups: list[AlignmentGroup] = Field(default_factory=list)


class TaskProgress(BaseModel):
    stage: str
    current: int = Field(default=0, ge=0)
    total: int = Field(default=0, ge=0)
    ocr_calls: int = Field(default=0, ge=0)
    skipped: int = Field(default=0, ge=0)


class TaskManifest(BaseModel):
    task_id: str
    status: TaskStatus
    input_path: Path
    original_filename: str | None = None
    batch_id: str | None = None
    source_url: str | None = None
    source_kind: str | None = None
    source_video_id: str | None = None
    download_quality: str | None = None
    transcribe_after_download: bool = True
    summarize: bool = False
    summary_error: str | None = None
    recording_minutes: int | None = None
    input_sha256: str | None = None
    work_dir: Path
    output_dir: Path
    mode: FusionMode
    asr_mode: ASRMode = ASRMode.LOCAL
    api_language: str | None = None
    api_hotwords: list[str] = Field(default_factory=list)
    hard_subtitles: bool = False
    translate_to_chinese: bool = False
    translation_backend: str | None = None
    selected_audio_stream: int | None = None
    selected_subtitle_stream: int | None = None
    artifacts: dict[str, str] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)
    progress: TaskProgress | None = None
    cookie_login_required: bool = False
    error: str | None = None
    created_at: str
    updated_at: str
