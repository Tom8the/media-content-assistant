"""Small, local-only runner for Argos CTranslate2 language packages.

The app translates subtitle cues, which are already short.  Using the model
directly keeps the translation runtime lightweight: it does not need PyTorch,
Stanza, or a network request while a task is running.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from video2txt.models import FusionSegment


class TranslationError(RuntimeError):
    """Raised when an offline translation model is unavailable or fails."""


_ARABIC_PATTERN = re.compile(r"[\u0600-\u06ff\u0750-\u077f\u08a0-\u08ff]")
_LATIN_PATTERN = re.compile(r"[A-Za-z]")
_CJK_PATTERN = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]")


def detect_translation_source(text: str) -> str | None:
    """Return the dominant supported source language for one subtitle cue."""
    arabic = len(_ARABIC_PATTERN.findall(text))
    latin = len(_LATIN_PATTERN.findall(text))
    if arabic > latin and arabic:
        return "ar"
    if latin:
        return "en"
    if _CJK_PATTERN.search(text):
        return "zh"
    return None


class _ArgosModel:
    def __init__(self, package_dir: Path) -> None:
        if (
            not (package_dir / "model").is_dir()
            or not (package_dir / "sentencepiece.model").is_file()
        ):
            raise TranslationError(f"离线翻译模型不完整：{package_dir}")
        try:
            import ctranslate2
            import sentencepiece as spm
        except ImportError as error:
            raise TranslationError(
                '未安装离线翻译依赖，请执行 pip install -e ".[translation]"'
            ) from error
        self._tokenizer: Any = spm.SentencePieceProcessor(
            model_file=str(package_dir / "sentencepiece.model")
        )
        self._translator: Any = ctranslate2.Translator(
            str(package_dir / "model"), device="cpu", inter_threads=1
        )

    def translate(self, text: str) -> str:
        if not text.strip():
            return text
        tokens = self._tokenizer.encode(text, out_type=str)
        if not tokens:
            return text
        result = self._translator.translate_batch(
            [tokens],
            replace_unknowns=True,
            beam_size=4,
            max_batch_size=1,
            batch_type="tokens",
            length_penalty=0.2,
        )[0]
        translated = self._tokenizer.decode(result.hypotheses[0]).strip()
        return translated or text


class OfflineChineseTranslator:
    """Translate Arabic and English subtitle cues to simplified Chinese.

    Arabic is translated through the installed Arabic→English and
    English→Chinese models.  Chinese and unsupported cues are left unchanged.
    """

    def __init__(self, model_dir: Path) -> None:
        self.model_dir = model_dir.resolve()
        self._models: dict[str, _ArgosModel] = {}

    def _model(self, pair: str) -> _ArgosModel:
        if pair not in self._models:
            self._models[pair] = _ArgosModel(self.model_dir / pair)
        return self._models[pair]

    def translate_text(self, text: str) -> str:
        source = detect_translation_source(text)
        if source in {None, "zh"}:
            return text
        if source == "en":
            return self._model("en_zh").translate(text)
        english = self._model("ar_en").translate(text)
        return self._model("en_zh").translate(english)

    def translate_segments(self, segments: list[FusionSegment]) -> list[FusionSegment]:
        cache: dict[str, str] = {}
        translated: list[FusionSegment] = []
        for segment in segments:
            text = segment.text
            if segment.include_in_transcript and text.strip():
                if text not in cache:
                    cache[text] = self.translate_text(text)
                text = cache[text]
            translated.append(
                segment.model_copy(
                    update={
                        "text": text,
                        "original_text": segment.text,
                        "decision": f"{segment.decision}_translated_zh",
                    }
                )
            )
        return translated
