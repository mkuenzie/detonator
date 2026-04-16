"""Structured JSON logging for the detonator host orchestrator.

Usage
-----
At application startup::

    from detonator.logging import setup_logging
    setup_logging(level="INFO", json_logs=True)

Inside the runner, wrap the module logger to inject ``run_id`` into every
record automatically::

    from detonator.logging import RunAdapter
    self._log = RunAdapter(logger, run_id=str(self.record.id))
    self._log.info("detonating %s", url)   # → JSON with "run_id" field

The ``setup_logging`` function is idempotent: it checks whether the root
logger already has handlers before attaching a new one, so it is safe to
call from tests.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import Any, MutableMapping


class JsonFormatter(logging.Formatter):
    """Emit each log record as a compact single-line JSON object.

    Fields always present: ``ts``, ``level``, ``logger``, ``msg``.
    Optional fields added when present: ``run_id``, ``exc``.
    """

    def format(self, record: logging.LogRecord) -> str:
        data: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        run_id = getattr(record, "run_id", None)
        if run_id is not None:
            data["run_id"] = run_id
        if record.exc_info:
            data["exc"] = self.formatException(record.exc_info)
        return json.dumps(data, default=str)


class RunAdapter(logging.LoggerAdapter):
    """Logger adapter that injects ``run_id`` into every emitted record.

    Example::

        log = RunAdapter(logger, run_id="abc-123")
        log.info("preflight passed")
        # → {"ts": "...", "level": "INFO", ..., "run_id": "abc-123", "msg": "preflight passed"}
    """

    def __init__(self, logger: logging.Logger, run_id: str) -> None:
        super().__init__(logger, {"run_id": run_id})

    def process(
        self, msg: object, kwargs: MutableMapping[str, Any]
    ) -> tuple[object, MutableMapping[str, Any]]:
        kwargs.setdefault("extra", {})
        kwargs["extra"].update(self.extra)
        return msg, kwargs


def setup_logging(level: str = "INFO", *, json_logs: bool = False) -> None:
    """Configure the root logger.

    This function is idempotent: if the root logger already has handlers it
    returns immediately so it is safe to call from library code and tests.

    Args:
        level: Standard Python log level string (e.g. ``"INFO"``, ``"DEBUG"``).
        json_logs: When ``True`` attach a :class:`JsonFormatter`; otherwise
            use a plain human-readable format suitable for development.
    """
    root = logging.getLogger()
    if root.handlers:
        return

    handler = logging.StreamHandler()
    if json_logs:
        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)-8s %(name)s  %(message)s")
        )
    root.addHandler(handler)
    root.setLevel(level.upper())
