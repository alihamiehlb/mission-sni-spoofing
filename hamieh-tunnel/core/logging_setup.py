"""
Structured logging configuration for Hamieh Tunnel.

Supports plain text and JSON output, file rotation, and per-module log levels.
"""

import json
import logging
import logging.handlers
import sys
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .config import LogConfig


class JsonFormatter(logging.Formatter):
    """Emit log records as single-line JSON objects."""

    def format(self, record: logging.LogRecord) -> str:
        obj = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(record.created)),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        if record.exc_info:
            obj["exc"] = self.formatException(record.exc_info)
        # Attach any extra fields passed via the `extra` kwarg
        for key, val in record.__dict__.items():
            if key not in {
                "name", "msg", "args", "levelname", "levelno", "pathname",
                "filename", "module", "exc_info", "exc_text", "stack_info",
                "lineno", "funcName", "created", "msecs", "relativeCreated",
                "thread", "threadName", "processName", "process", "message",
                "taskName",
            }:
                obj[key] = val
        return json.dumps(obj)


def setup_logging(cfg: "LogConfig") -> None:
    """Apply logging configuration globally."""
    level = getattr(logging, cfg.level.upper(), logging.INFO)

    formatter: logging.Formatter
    if cfg.json_format:
        formatter = JsonFormatter()
    else:
        formatter = logging.Formatter(
            fmt="%(asctime)s [%(levelname)-8s] %(name)s: %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )

    handlers: list[logging.Handler] = []

    # Console handler
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(formatter)
    handlers.append(console)

    # File handler (rotating, 10 MB × 5 files)
    if cfg.file:
        file_handler = logging.handlers.RotatingFileHandler(
            cfg.file, maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8"
        )
        file_handler.setFormatter(formatter)
        handlers.append(file_handler)

    logging.basicConfig(level=level, handlers=handlers, force=True)

    # Quiet noisy third-party loggers
    for noisy in ("websockets", "asyncio", "urllib3", "httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
