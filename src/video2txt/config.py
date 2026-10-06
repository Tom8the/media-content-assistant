from __future__ import annotations

import os
import tomllib
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator


class FFmpegSettings(BaseModel):
    ffmpeg_path: str = "ffmpeg"
    ffprobe_path: str = "ffprobe"


class ASRSettings(BaseModel):
    engine: Literal["faster-whisper"] = "faster-whisper"
    model_path: Path | None = None
    # None lets Whisper detect each recording instead of forcing Chinese ASR.
    language: str | None = None
    device: Literal["auto", "cpu", "cuda"] = "cpu"
    compute_type: str = "int8"
    cpu_threads: int = Field(default=4, ge=1)
    beam_size: int = Field(default=5, ge=1)
    vad_filter: bool = True
    word_timestamps: bool = True
    condition_on_previous_text: bool = False


class OCRSettings(BaseModel):
    enabled: bool = False
    engine: Literal["paddleocr"] = "paddleocr"
    language: str = "ch"
    device: Literal["cpu", "gpu"] = "cpu"
    detection_model_name: str = "PP-OCRv5_mobile_det"
    recognition_model_name: str = "PP-OCRv5_mobile_rec"
    sample_interval: float = Field(default=1.2, ge=0.2, le=5)
    crop_top: float = Field(default=0.78, ge=0, le=1)
    crop_bottom: float = Field(default=0.94, ge=0, le=1)
    scale: int = Field(default=1, ge=1, le=4)
    min_confidence: float = Field(default=0.55, ge=0, le=1)
    image_change_threshold: float = Field(default=0.12, ge=0, le=1)
    signature_left: float = Field(default=0.20, ge=0, le=1)
    signature_right: float = Field(default=0.80, ge=0, le=1)
    signature_top: float = Field(default=0.15, ge=0, le=1)
    signature_bottom: float = Field(default=0.92, ge=0, le=1)
    signature_edge_threshold: int = Field(default=45, ge=0, le=255)
    text_center_left: float = Field(default=0.10, ge=0, le=1)
    text_center_right: float = Field(default=0.90, ge=0, le=1)
    text_center_top: float = Field(default=0.10, ge=0, le=1)
    text_center_bottom: float = Field(default=0.98, ge=0, le=1)
    text_min_height_ratio: float = Field(default=0.20, ge=0, le=1)
    text_min_width_ratio: float = Field(default=0.025, ge=0, le=1)
    text_similarity_threshold: float = Field(default=0.88, ge=0, le=1)
    min_duration: float = Field(default=0.3, ge=0)
    max_frames: int = Field(default=12_000, ge=1)
    progress_interval_frames: int = Field(default=5, ge=1, le=500)

    @model_validator(mode="after")
    def validate_crop(self) -> OCRSettings:
        if self.crop_bottom <= self.crop_top:
            raise ValueError("ocr crop_bottom must be greater than crop_top")
        if self.signature_right <= self.signature_left:
            raise ValueError("ocr signature_right must be greater than signature_left")
        if self.signature_bottom <= self.signature_top:
            raise ValueError("ocr signature_bottom must be greater than signature_top")
        if self.text_center_right <= self.text_center_left:
            raise ValueError("ocr text_center_right must be greater than text_center_left")
        if self.text_center_bottom <= self.text_center_top:
            raise ValueError("ocr text_center_bottom must be greater than text_center_top")
        return self


class AlignmentSettings(BaseModel):
    time_weight: float = Field(default=0.55, ge=0)
    text_weight: float = Field(default=0.35, ge=0)
    confidence_weight: float = Field(default=0.10, ge=0)
    match_threshold: float = Field(default=0.55, ge=0, le=1)
    review_threshold: float = Field(default=0.72, ge=0, le=1)
    include_unmatched_hard_subtitles: bool = False

    @model_validator(mode="after")
    def validate_weights(self) -> AlignmentSettings:
        total = self.time_weight + self.text_weight + self.confidence_weight
        if abs(total - 1.0) > 1e-6:
            raise ValueError("alignment weights must sum to 1.0")
        if self.review_threshold < self.match_threshold:
            raise ValueError("review_threshold must be greater than or equal to match_threshold")
        return self


class PathSettings(BaseModel):
    work_dir: Path = Path("work")
    output_dir: Path = Path("output")


class DouyinSettings(BaseModel):
    """Download defaults. Per-task selections from the web page take precedence."""

    video_quality: Literal[
        "highest", "original", "lowest", "1440p", "1080p", "720p", "540p", "480p", "360p"
    ] = "highest"
    retry_times: int = Field(default=3, ge=0, le=10)


class TranslationSettings(BaseModel):
    model_path: Path = Path("models/nllb-ct2-int8")
    device: Literal["cpu", "cuda"] = "cpu"
    compute_type: Literal["int8", "int8_float32"] = "int8"
    inter_threads: int = Field(default=1, ge=1)
    intra_threads: int = Field(default=8, ge=0)


class QwenASRSettings(BaseModel):
    """Non-secret settings for Qwen file transcription."""

    model: str = "qwen-audio-3.0-asr-flash-filetrans"
    region: Literal["beijing", "singapore"] = "beijing"
    workspace_id: str | None = None
    api_key_environment: str = "DASHSCOPE_API_KEY"
    poll_interval_seconds: float = Field(default=5, ge=1, le=60)
    wait_timeout_seconds: float = Field(default=3600, ge=30, le=86_400)
    hotword_weight: int = Field(default=3, ge=1, le=5)


class CodexTranslationSettings(BaseModel):
    """Local Codex CLI invocation settings; authentication stays in Codex itself."""

    command: str = "codex"
    model: str | None = None
    timeout_seconds: int = Field(default=900, ge=30, le=7_200)
    batch_size: int = Field(default=24, ge=1, le=100)


class Settings(BaseModel):
    ffmpeg: FFmpegSettings = Field(default_factory=FFmpegSettings)
    asr: ASRSettings = Field(default_factory=ASRSettings)
    ocr: OCRSettings = Field(default_factory=OCRSettings)
    alignment: AlignmentSettings = Field(default_factory=AlignmentSettings)
    paths: PathSettings = Field(default_factory=PathSettings)
    douyin: DouyinSettings = Field(default_factory=DouyinSettings)
    translation: TranslationSettings = Field(default_factory=TranslationSettings)
    qwen_asr: QwenASRSettings = Field(default_factory=QwenASRSettings)
    codex_translation: CodexTranslationSettings = Field(default_factory=CodexTranslationSettings)


def _load_toml(path: Path | None) -> dict[str, Any]:
    if path is None:
        return {}
    if not path.is_file():
        raise FileNotFoundError(path)
    with path.open("rb") as handle:
        return tomllib.load(handle)


def load_settings(config_path: str | Path | None = None) -> Settings:
    """Load TOML settings, then apply supported environment overrides."""
    resolved_config = config_path or os.getenv("VIDEO2TXT_CONFIG")
    path = Path(resolved_config).resolve() if resolved_config else None
    payload = _load_toml(path)

    model_path = os.getenv("VIDEO2TXT_MODEL_PATH")
    if model_path:
        payload.setdefault("asr", {})["model_path"] = model_path

    work_dir = os.getenv("VIDEO2TXT_WORK_DIR")
    if work_dir:
        payload.setdefault("paths", {})["work_dir"] = work_dir

    output_dir = os.getenv("VIDEO2TXT_OUTPUT_DIR")
    if output_dir:
        payload.setdefault("paths", {})["output_dir"] = output_dir

    return Settings.model_validate(payload)
