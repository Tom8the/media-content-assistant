from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any

from video2txt.config import OCRSettings
from video2txt.models import OCRLine


class PaddleOCREngine:
    def __init__(self, settings: OCRSettings) -> None:
        self.settings = settings
        self._engine: Any | None = None

    def _load(self) -> Any:
        if self._engine is not None:
            return self._engine
        try:
            from paddleocr import PaddleOCR
        except ImportError as error:
            raise RuntimeError('未安装本地 OCR 依赖，请执行 pip install -e ".[ocr]"') from error
        self._engine = PaddleOCR(
            device=self.settings.device,
            text_detection_model_name=self.settings.detection_model_name,
            text_recognition_model_name=self.settings.recognition_model_name,
            use_doc_orientation_classify=False,
            use_doc_unwarping=False,
            use_textline_orientation=False,
            text_rec_score_thresh=self.settings.min_confidence,
            enable_mkldnn=False,
        )
        return self._engine

    def recognize(self, image_path: Path) -> list[OCRLine]:
        results = self._load().predict(str(image_path))
        lines: list[OCRLine] = []
        for result in results:
            texts = result.get("rec_texts", [])
            scores = result.get("rec_scores", [])
            polygons = result.get("rec_polys", [])
            for index, text in enumerate(texts):
                cleaned = str(text).strip()
                score = float(scores[index]) if index < len(scores) else 0.0
                if not cleaned or score < self.settings.min_confidence:
                    continue
                polygon = polygons[index] if index < len(polygons) else None
                bbox = (
                    [[float(point[0]), float(point[1])] for point in polygon]
                    if polygon is not None
                    else None
                )
                lines.append(OCRLine(text=cleaned, confidence=score, bbox=bbox))
        return lines


@lru_cache(maxsize=4)
def _cached_engine(
    language: str,
    device: str,
    min_confidence: float,
    detection_model_name: str,
    recognition_model_name: str,
) -> PaddleOCREngine:
    settings = OCRSettings(
        language=language,
        device=device,  # type: ignore[arg-type]
        min_confidence=min_confidence,
        detection_model_name=detection_model_name,
        recognition_model_name=recognition_model_name,
    )
    return PaddleOCREngine(settings)


def get_paddle_ocr_engine(settings: OCRSettings) -> PaddleOCREngine:
    return _cached_engine(
        settings.language,
        settings.device,
        settings.min_confidence,
        settings.detection_model_name,
        settings.recognition_model_name,
    )
