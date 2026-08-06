"""Logging factory configured by settings.LOG_LEVEL."""
from __future__ import annotations

import logging

from settings import LOG_LEVEL

_CONFIGURED = False


def get_logger(name: str = "anara") -> logging.Logger:
    global _CONFIGURED
    if not _CONFIGURED:
        logging.basicConfig(
            level=getattr(logging, LOG_LEVEL, logging.INFO),
            format="%(asctime)s  %(levelname)-7s %(name)s  %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
        _CONFIGURED = True
    return logging.getLogger(name)
