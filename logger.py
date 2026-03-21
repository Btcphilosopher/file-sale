"""
logger.py — Structured, coloured logging for btc-file-market.
Uses colorlog for terminal output + rotating file handler.
"""

import logging
import logging.handlers
from pathlib import Path

try:
    import colorlog
    _HAS_COLOR = True
except ImportError:
    _HAS_COLOR = False


def get_logger(name: str = "btc_market") -> logging.Logger:
    """
    Return (or create) the named logger.
    Safe to call multiple times — handlers are added only once.
    """
    logger = logging.getLogger(name)

    if logger.handlers:
        return logger  # already configured

    # Lazy import to avoid circular dependency at module load time
    from config import LOG_LEVEL, LOG_FILE, STORAGE_DIR  # noqa: PLC0415

    log_level = getattr(logging, LOG_LEVEL, logging.INFO)
    logger.setLevel(log_level)

    fmt_plain = (
        "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"
    )
    fmt_color = (
        "%(log_color)s%(asctime)s | %(levelname)-8s%(reset)s | "
        "%(cyan)s%(name)s%(reset)s | %(message)s"
    )

    # ── Console handler ───────────────────────────────────────
    ch = logging.StreamHandler()
    ch.setLevel(log_level)
    if _HAS_COLOR:
        ch.setFormatter(
            colorlog.ColoredFormatter(
                fmt_color,
                log_colors={
                    "DEBUG":    "white",
                    "INFO":     "green",
                    "WARNING":  "yellow,bold",
                    "ERROR":    "red,bold",
                    "CRITICAL": "red,bg_white,bold",
                },
                datefmt="%H:%M:%S",
            )
        )
    else:
        ch.setFormatter(logging.Formatter(fmt_plain, datefmt="%H:%M:%S"))
    logger.addHandler(ch)

    # ── File handler (rotating, 5 MB × 3 backups) ─────────────
    try:
        Path(LOG_FILE).parent.mkdir(parents=True, exist_ok=True)
        fh = logging.handlers.RotatingFileHandler(
            LOG_FILE, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
        )
        fh.setLevel(log_level)
        fh.setFormatter(logging.Formatter(fmt_plain))
        logger.addHandler(fh)
    except OSError as exc:
        logger.warning("Could not open log file %s: %s", LOG_FILE, exc)

    logger.propagate = False
    return logger
