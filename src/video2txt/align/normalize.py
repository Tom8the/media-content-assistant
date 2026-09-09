from __future__ import annotations

import unicodedata
from functools import lru_cache


@lru_cache(maxsize=1)
def _opencc_converter():  # type: ignore[no-untyped-def]
    try:
        from opencc import OpenCC
    except ImportError as error:
        raise RuntimeError('未安装简繁转换依赖，请执行 pip install -e ".[subtitles]"') from error
    return OpenCC("t2s")


def to_simplified(text: str) -> str:
    return str(_opencc_converter().convert(text))


def normalize_text(text: str, *, simplify_chinese: bool = True) -> str:
    """Return comparison-only text without mutating the displayed source text."""
    normalized = unicodedata.normalize("NFKC", text).casefold()
    if simplify_chinese:
        normalized = to_simplified(normalized)
    return "".join(
        character
        for character in normalized
        if not character.isspace() and not unicodedata.category(character).startswith(("P", "S"))
    )
