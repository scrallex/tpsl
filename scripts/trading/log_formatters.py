"""Shared structured and plaintext log formatters."""

import json
import logging
import os
import sys
from logging import handlers
from pathlib import Path


class JsonFormatter(logging.Formatter):
    """Formatter for structured output in JSON format."""

    def format(
        self, record: logging.LogRecord
    ) -> str:  # pragma: no cover - formatting only
        payload = {
            "timestamp": self.formatTime(record, "%Y-%m-%dT%H:%M:%S.%fZ"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        if record.__dict__:
            extras = {
                k: v
                for k, v in record.__dict__.items()
                if k not in logging.LogRecord.__dict__
            }
            if extras:
                payload.update(extras)
        return json.dumps(payload, ensure_ascii=False)


class PlainFormatter(logging.Formatter):
    """Standard plaintext formatting for local console usage."""

    def __init__(self) -> None:
        super().__init__("%(asctime)s %(levelname)s %(name)s :: %(message)s")


def configure_logging(name: str) -> logging.Logger:
    """Read environment variables and configure root logger."""
    log_level = os.getenv("LOG_LEVEL", "INFO").upper()
    log_format = os.getenv("LOG_FORMAT", "json").lower()
    log_to_file = os.getenv("LOG_TO_FILE", "1").lower() not in {"0", "false", "off"}
    log_file_path = os.getenv("LOG_FILE_PATH", "logs/backend.log")
    max_bytes = int(os.getenv("LOG_MAX_BYTES", str(5 * 1024 * 1024)))
    backup_count = int(os.getenv("LOG_BACKUP_COUNT", "5"))

    root = logging.getLogger()
    root.setLevel(log_level)
    root.handlers.clear()

    formatter: logging.Formatter
    if log_format == "json":
        formatter = JsonFormatter()
    else:
        formatter = PlainFormatter()

    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setFormatter(formatter)
    root.addHandler(stream_handler)

    if log_to_file:
        try:
            Path(log_file_path).parent.mkdir(parents=True, exist_ok=True)
            file_handler = handlers.RotatingFileHandler(
                log_file_path,
                maxBytes=max_bytes,
                backupCount=backup_count,
            )
            file_handler.setFormatter(formatter)
            root.addHandler(file_handler)
        except (OSError, IOError) as exc:  # pragma: no cover - file system error path
            root.warning("Failed to configure file logging: %s", exc)

    return logging.getLogger(name)
