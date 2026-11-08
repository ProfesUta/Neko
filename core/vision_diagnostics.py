"""Bounded diagnostics for capture/model failures; never records image or chat data."""

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

logger = logging.getLogger("capri.vision")


class VisionReadError(RuntimeError):
    def __init__(self, message, stage="analysis"):
        super().__init__(message)
        self.stage = stage


def configure_vision_logging(data_dir):
    path = Path(data_dir) / "vision.log"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if not any(isinstance(handler, RotatingFileHandler) and handler.baseFilename == str(path.resolve())
                   for handler in logger.handlers):
            handler = RotatingFileHandler(path, maxBytes=512 * 1024, backupCount=2, encoding="utf-8")
            handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
            logger.addHandler(handler)
            logger.setLevel(logging.INFO)
    except OSError as error:
        logging.warning("Cannot create vision diagnostic log: %s", error)
    return path
