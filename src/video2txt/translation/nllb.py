"""Direct Arabic/English to simplified-Chinese translation with NLLB."""

from __future__ import annotations

import re
from typing import Any

from video2txt.config import TranslationSettings
from video2txt.models import FusionSegment


class TranslationError(RuntimeError):
    """Raised when the local NLLB model is unavailable or cannot translate."""


_ARABIC_PATTERN = re.compile(r"[\u0600-\u06ff\u0750-\u077f\u08a0-\u08ff]")
_LATIN_PATTERN = re.compile(r"[A-Za-z]")
_CJK_PATTERN = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]")
_LANGUAGE_CODES = {"ar": "arb_Arab", "en": "eng_Latn", "zh": "zho_Hans"}


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


class NLLBChineseTranslator:
    """Run one direct NLLB translation pass for each subtitle cue."""

    def __init__(self, settings: TranslationSettings) -> None:
        self.settings = settings
        self._translator: Any | None = None
        self._tokenizer: Any | None = None

    def _load(self) -> None:
        if self._translator is not None and self._tokenizer is not None:
            return
        model_dir = self.settings.model_path.resolve()
        tokenizer_path = model_dir / "sentencepiece.bpe.model"
        if not (model_dir / "model.bin").is_file() or not tokenizer_path.is_file():
            raise TranslationError(
                "NLLB 离线翻译模型尚未安装；请执行 video2txt install-translation-models"
            )
        try:
            import ctranslate2
            import sentencepiece as spm
        except ImportError as error:
            raise TranslationError(
                '未安装离线翻译依赖，请执行 pip install -e ".[translation]"'
            ) from error
        self._translator = ctranslate2.Translator(
            str(model_dir),
            device=self.settings.device,
            compute_type=self.settings.compute_type,
            inter_threads=self.settings.inter_threads,
            intra_threads=self.settings.intra_threads,
        )
        self._tokenizer = spm.SentencePieceProcessor(model_file=str(tokenizer_path))

    def translate_text(self, text: str) -> str:
        source = detect_translation_source(text)
        if source in {None, "zh"}:
            return text
        self._load()
        assert self._tokenizer is not None
        assert self._translator is not None
        source_tokens = [
            _LANGUAGE_CODES[source],
            *self._tokenizer.encode(text, out_type=str),
            "</s>",
        ]
        result = self._translator.translate_batch(
            [source_tokens],
            target_prefix=[[_LANGUAGE_CODES["zh"]]],
            beam_size=4,
            max_batch_size=1,
            batch_type="tokens",
            length_penalty=0.2,
        )[0]
        tokens = result.hypotheses[0]
        if tokens and tokens[0] == _LANGUAGE_CODES["zh"]:
            tokens = tokens[1:]
        translated = self._tokenizer.decode(tokens).strip()
        return translated or text

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
