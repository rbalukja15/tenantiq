"""Request-scoped log fields, and the formatters that print them (#26, ADR-0024).

Every log line names the request it belongs to and the tenant it ran for, so one request can be
followed from the API through retrieval and the model call, and on into the ingestion task it queued.

- **The fields** live in one :class:`~contextvars.ContextVar`. ``RequestLogMiddleware`` opens a
  scope per request with a fresh ``request_id``, the authenticator adds ``tenant_id`` once the token
  is verified, and a Celery task runs inside the fields of the request that published it
  (:func:`carry_into_headers` on the way out, :func:`enter_task` on the way in).
- **The filter** copies them onto every record a handler emits, so the lines Django, Celery and the
  model clients log carry them too. Inside a ``tenant_context`` the active tenant is the
  ``tenant_id``, so a sweep or a management command is labelled without knowing about logging.
- **The formatters**: one JSON object per line for a deployment, where a collector parses them, and a
  readable line for development that still prints every field.

A tenant id on a log line is a label for following a request. Nothing reads it back: scoping runs on
``app.tenant_context`` alone. Lines carry identifiers, counts and timings, never a question, an
answer or a passage, because logs leave the tenant boundary (docs/threat-model.md, T15).
"""

from __future__ import annotations

import contextlib
import json
import logging
import re
import sys
import time
from collections.abc import Iterator, Mapping, MutableMapping
from contextvars import ContextVar
from datetime import UTC, datetime
from types import MappingProxyType
from typing import Any

from app.tenant_context import get_current_tenant_id

_EMPTY: Mapping[str, str] = MappingProxyType({})
_fields: ContextVar[Mapping[str, str]] = ContextVar("log_fields", default=_EMPTY)

#: The correlation fields, in the order a line prints them.
CONTEXT_FIELDS = ("request_id", "tenant_id", "task_id", "task_name")

#: What an identifier from outside may look like: an upstream ``X-Request-ID``, or a field read back
#: from a task message. Long enough for a UUID or a trace id, and nothing that could break a line.
SAFE_ID = re.compile(r"[A-Za-z0-9._:-]{1,128}")

#: The message header a task's fields travel in, from the request that queued it to the worker.
TASK_HEADER = "tenantiq_log_context"

#: The fields worth carrying into a task. The task names itself once it runs.
_CARRIED = ("request_id", "tenant_id")


def current_log_context() -> dict[str, str]:
    """The fields a line logged here and now would carry. A copy: changing it changes nothing."""
    fields = dict(_fields.get())
    tenant_id = get_current_tenant_id()
    if tenant_id is not None:
        fields["tenant_id"] = str(tenant_id)
    return fields


@contextlib.contextmanager
def log_scope(**fields: str) -> Iterator[None]:
    """Run the block with exactly ``fields`` as the log fields, restoring the previous ones after.

    A scope replaces rather than adds, so a request can never start with something an earlier one
    left behind.
    """
    token = _fields.set(MappingProxyType(dict(fields)))
    try:
        yield
    finally:
        _fields.reset(token)


def bind_log_context(**fields: str) -> None:
    """Add ``fields`` to the current scope until that scope ends.

    For a value that becomes known partway through a scope, as the tenant does at authentication.
    There is no token to undo it with, deliberately: the scope that opened the fields restores what
    came before it as a whole, which discards these too.
    """
    _fields.set(MappingProxyType({**_fields.get(), **fields}))


def ms_since(started: float) -> int:
    """Whole milliseconds since ``started``, a :func:`time.perf_counter` reading."""
    return round((time.perf_counter() - started) * 1000)


# --- Celery: the request's fields travel with the task it queues ----------------------------------


def carry_into_headers(headers: MutableMapping[str, Any]) -> None:
    """``before_task_publish``: put the publishing request's fields on the task message.

    A retry republishes the message with its headers, so a retried attempt keeps them. ``setdefault``
    rather than assignment for the same reason: inside a worker, the fields being published are the
    ones the task was already carrying.
    """
    fields = {key: value for key, value in current_log_context().items() if key in _CARRIED}
    if fields:
        headers.setdefault(TASK_HEADER, fields)


def enter_task(task: Any) -> None:
    """``task_prerun``: run the task inside the fields it was published with, plus its own id.

    A task the worker received carries them in its message. One that runs in-process (eager, under
    pytest) was never published and inherits the caller's fields instead. Beat's tasks carry none and
    are labelled by their own id alone.
    """
    request = task.request
    carried = _carried_fields(request)
    fields = carried if carried is not None else current_log_context()
    token = _fields.set(
        MappingProxyType({**fields, "task_id": str(request.id), "task_name": task.name})
    )
    request.tenantiq_log_token = token


def exit_task(task: Any) -> None:
    """``task_postrun``: restore the fields the worker had before the task started."""
    token = getattr(task.request, "tenantiq_log_token", None)
    if token is not None:
        _fields.reset(token)
        task.request.tenantiq_log_token = None


def _carried_fields(request: Any) -> dict[str, str] | None:
    """The fields a received task carried, or ``None`` when it carried none.

    Only the known keys, and only values that look like identifiers: whatever is in a message gets
    printed on every line the task logs.
    """
    headers = getattr(request, "headers", None)
    carried = headers.get(TASK_HEADER) if isinstance(headers, Mapping) else None
    if not isinstance(carried, Mapping):
        return None
    return {
        key: value
        for key in _CARRIED
        if isinstance(value := carried.get(key), str) and SAFE_ID.fullmatch(value)
    }


# --- the filter and the formatters (wired up by LOGGING in config/settings.py) --------------------


class LogContextFilter(logging.Filter):
    """Copy the current fields onto every record, so any formatter can print them.

    Attached to the handler rather than to a logger, because a logger's filters do not run for
    records that propagate up from its children, and most lines come from somebody else's logger.
    A field the call passed itself through ``extra`` is left alone.

    Django logs a 4xx that a view returned, rather than raised, after the whole middleware chain
    has returned, so after the request's fields are gone. That line carries the request, and
    ``RequestLogMiddleware`` leaves the fields on it as ``request.log_fields``.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        fields = current_log_context()
        kept = getattr(getattr(record, "request", None), "log_fields", None)
        if isinstance(kept, Mapping):
            fields = {**kept, **fields}
        for key in CONTEXT_FIELDS:
            if getattr(record, key, None) is None:
                setattr(record, key, fields.get(key))
        return True


#: Attributes every record has. Anything else on a record was passed through ``extra``.
_RECORD_ATTRIBUTES = frozenset(vars(logging.makeLogRecord({}))) | {"message", "asctime"}
#: Extras that are not ours to print. Django passes the request object with its error lines, and its
#: repr carries the query string.
_DROPPED_EXTRAS = frozenset({"request"})


def _record_fields(record: logging.LogRecord) -> dict[str, Any]:
    """The correlation fields, then the call's own extras, leaving out any that are unset."""
    fields = {key: getattr(record, key, None) for key in CONTEXT_FIELDS}
    for key, value in vars(record).items():
        if key not in _RECORD_ATTRIBUTES and key not in _DROPPED_EXTRAS and key not in fields:
            fields[key] = value
    return {key: value for key, value in fields.items() if value is not None}


class JsonFormatter(logging.Formatter):
    """One JSON object per line: time, level, logger, message, then every field."""

    def format(self, record: logging.LogRecord) -> str:
        line: dict[str, Any] = {
            "time": datetime.fromtimestamp(record.created, UTC)
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key, value in _record_fields(record).items():
            line.setdefault(key, value)  # an extra never overwrites the four above
        if record.exc_info:
            line["exception"] = self.formatException(record.exc_info)
        if record.stack_info:
            line["stack"] = self.formatStack(record.stack_info)
        # `default=str`: a UUID, a Decimal or a library's own object prints as text rather than
        # losing the line.
        return json.dumps(line, default=str)


class TextFormatter(logging.Formatter):
    """A line to read in a terminal that prints the same fields as the JSON: ``key=value`` pairs."""

    def format(self, record: logging.LogRecord) -> str:
        fields = " ".join(f"{key}={_text(value)}" for key, value in _record_fields(record).items())
        line = f"{self.formatTime(record)} {record.levelname} {record.name} {record.getMessage()}"
        if fields:
            line = f"{line}  {fields}"
        if record.exc_info:
            line = f"{line}\n{self.formatException(record.exc_info)}"
        if record.stack_info:
            line = f"{line}\n{self.formatStack(record.stack_info)}"
        return line


def _text(value: Any) -> str:
    if isinstance(value, str) and value and not any(char.isspace() for char in value):
        return value
    return json.dumps(value, default=str)


class StderrHandler(logging.StreamHandler):
    """A stream handler that writes to whatever ``sys.stderr`` is when the line is emitted.

    A plain ``StreamHandler`` keeps the stream it was given at configuration time. Django configures
    logging once per process, so anything that swaps ``sys.stderr`` later (pytest's output capture
    does, per test) would leave it writing to a stream that has since been closed.
    """

    def __init__(self) -> None:
        logging.Handler.__init__(self)

    @property
    def stream(self) -> Any:
        return sys.stderr
