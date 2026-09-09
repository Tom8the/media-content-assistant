"""Download the CTranslate2 NLLB model used by the local translation stage."""

from __future__ import annotations

from pathlib import Path

NLLB_REPOSITORY = "mijuanlo/nllb-200-distilled-600M-ct2-int8"


def install_nllb_model(model_dir: Path) -> Path:
    """Download the direct Arabic/English→Chinese NLLB INT8 model once."""
    try:
        from huggingface_hub import snapshot_download
    except ImportError as error:
        raise RuntimeError('未安装模型下载依赖，请执行 pip install -e ".[translation]"') from error
    destination = model_dir.resolve()
    snapshot_download(
        repo_id=NLLB_REPOSITORY,
        local_dir=destination,
        local_dir_use_symlinks=False,
    )
    return destination
