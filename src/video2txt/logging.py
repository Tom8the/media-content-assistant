from __future__ import annotations

import logging
from pathlib import Path


def configure_logging(*, verbose: bool = False, log_file: Path | None = None) -> None:
    """Configure deterministic console logging and an optional UTF-8 file log."""
    level = logging.DEBUG if verbose else logging.INFO
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    if log_file is not None:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(log_file, encoding="utf-8"))
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=handlers,
        force=True,
    )
