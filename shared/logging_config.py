"""
Structured JSON logging with @trace decorator.

Every tool/resource/prompt function gets decorated with @trace(logger) which
produces three structured log lines per call:
  ENTER  (DEBUG) — function name, call_id, bound arguments
  EXIT   (DEBUG) — call_id, duration_ms, truncated result preview
  FAILED (ERROR) — call_id, duration_ms, exception type, full traceback

Why structured JSON?
  Log lines are machine-parseable. In production banking you feed these into
  a SIEM (Security Information and Event Management) system like Splunk or
  Elastic to query across millions of calls. Free-text logs cannot be queried
  reliably.

Why call_id?
  A single tool call produces an ENTER and an EXIT. The call_id lets you
  correlate them even when log lines from concurrent calls are interleaved.
"""

import os
import json
import uuid
import time
import inspect
import logging
import asyncio
import traceback
import functools
from typing import Any, Callable, Optional

LOG_LEVEL = os.getenv("LOG_LEVEL", "DEBUG").upper()


class _JSONFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        obj: dict = {
            "timestamp": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
            "level": record.levelname,
            "logger": record.name,
        }
        # trace() puts its payload in record.data
        if hasattr(record, "data"):
            obj.update(record.data)
        else:
            msg = record.getMessage()
            if msg:
                obj["message"] = msg
        if record.exc_info:
            obj["exception"] = self.formatException(record.exc_info)
        return json.dumps(obj, default=str)


def get_logger(name: str) -> logging.Logger:
    logger = logging.getLogger(name)
    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(_JSONFormatter())
        logger.addHandler(handler)
    logger.setLevel(getattr(logging, LOG_LEVEL, logging.DEBUG))
    logger.propagate = False
    return logger


def trace(logger: logging.Logger, redact: Optional[Callable[[dict], dict]] = None):
    """
    Decorator factory. Usage:

        @trace(logger)
        def my_tool(account_id: str) -> dict: ...

        @trace(logger, redact=redact_for_logging)
        def my_tool(account_id: str) -> dict: ...

    Works on both regular and async functions.
    """
    def decorator(fn: Callable) -> Callable:
        if asyncio.iscoroutinefunction(fn):
            @functools.wraps(fn)
            async def async_wrapper(*args, **kwargs):
                call_id, logged_args = _enter(fn, args, kwargs, redact, logger)
                t0 = time.perf_counter()
                try:
                    result = await fn(*args, **kwargs)
                    _exit(fn, call_id, t0, result, logger)
                    return result
                except Exception as exc:
                    _failed(fn, call_id, t0, exc, logger)
                    raise
            return async_wrapper
        else:
            @functools.wraps(fn)
            def sync_wrapper(*args, **kwargs):
                call_id, logged_args = _enter(fn, args, kwargs, redact, logger)
                t0 = time.perf_counter()
                try:
                    result = fn(*args, **kwargs)
                    _exit(fn, call_id, t0, result, logger)
                    return result
                except Exception as exc:
                    _failed(fn, call_id, t0, exc, logger)
                    raise
            return sync_wrapper
    return decorator


# ── internal helpers ──────────────────────────────────────────────────────────

def _fqname(fn: Callable) -> str:
    return f"{fn.__module__}.{fn.__qualname__}"


def _bind_args(fn: Callable, args: tuple, kwargs: dict) -> dict:
    try:
        sig = inspect.signature(fn)
        bound = sig.bind(*args, **kwargs)
        bound.apply_defaults()
        return {k: v for k, v in bound.arguments.items()}
    except Exception:
        return {"_args": repr(args), "_kwargs": repr(kwargs)}


def _truncate(s: str, max_len: int = 200) -> str:
    return s if len(s) <= max_len else s[:max_len] + "…"


def _enter(fn, args, kwargs, redact, logger):
    call_id = uuid.uuid4().hex[:8]
    bound = _bind_args(fn, args, kwargs)
    logged = redact(bound) if redact else bound
    logger.debug("", extra={"data": {
        "event": "ENTER",
        "call_id": call_id,
        "function": _fqname(fn),
        "args": {k: str(v) for k, v in logged.items()},
    }})
    return call_id, logged


def _exit(fn, call_id, t0, result, logger):
    duration_ms = round((time.perf_counter() - t0) * 1000, 2)
    logger.debug("", extra={"data": {
        "event": "EXIT",
        "call_id": call_id,
        "function": _fqname(fn),
        "duration_ms": duration_ms,
        "result_preview": _truncate(repr(result)),
    }})


def _failed(fn, call_id, t0, exc, logger):
    duration_ms = round((time.perf_counter() - t0) * 1000, 2)
    logger.error("", extra={"data": {
        "event": "FAILED",
        "call_id": call_id,
        "function": _fqname(fn),
        "duration_ms": duration_ms,
        "exception_type": type(exc).__name__,
        "traceback": traceback.format_exc(),
    }})
