"""Request middleware: the tenant-context lifecycle, and the request's log fields.

``TenantContextMiddleware`` bounds the tenant context. The tenant is *activated* by the DRF
authenticator — the first point the verified tenant exists in the request (see
``app.auth.authentication``). This middleware's sole job is to guarantee the contextvar is cleared
once the response is produced, so a reused worker thread can never carry one request's tenant into
the next. The Postgres GUC needs no cleanup here: ``SET LOCAL`` resets at the end of each request's
transaction (``ATOMIC_REQUESTS``).

``RequestLogMiddleware`` gives every request an id, puts it on every line logged while the request
is served, returns it as ``X-Request-ID`` and logs one line when the request ends (#26, ADR-0024).
"""

from __future__ import annotations

import logging
import time
import uuid
from collections.abc import Callable, Iterable, Iterator
from typing import Any

from app.log_context import SAFE_ID, current_log_context, log_scope, ms_since
from app.tenant_context import clear_current_tenant

logger = logging.getLogger(__name__)

REQUEST_ID_HEADER = "X-Request-ID"


class TenantContextMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        try:
            return self.get_response(request)
        finally:
            clear_current_tenant()


class RequestLogMiddleware:
    """Name the request on every log line, and log one line when it ends.

    Outermost in ``MIDDLEWARE``, so everything that logs while the request is served carries its id,
    Django's own error lines included. The one line Django logs after the chain has returned, for a
    4xx a view returned, reads the fields this leaves on the request (see ``LogContextFilter``). The
    id is the caller's ``X-Request-ID`` when that looks like an identifier (a proxy in front of the
    API can then correlate its own lines with ours) and a fresh one otherwise. The BFF forwards no
    such header from a browser, so in this deployment the id is always ours.

    **A streamed response ends after this method returns.** Its body is produced as the server sends
    it, after the scope below has closed. So each frame is produced inside the request's fields as
    they stood when the view returned, tenant included, and so is the response's close, where a view
    cleans up after a client that hung up. The line is logged when the last frame has gone out or the
    client has hung up. Without that, the model call (the slowest part of the request, and the reason
    to trace it) would log with no request id at all.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        request_id = _request_id_from(request)
        started = time.perf_counter()
        with log_scope(request_id=request_id):
            response = self.get_response(request)
            response[REQUEST_ID_HEADER] = request_id
            request.log_fields = fields = current_log_context()
            if response.streaming and not getattr(response, "is_async", False):
                response.streaming_content = _streamed(
                    response.streaming_content,
                    fields,
                    request=request,
                    status=response.status_code,
                    started=started,
                )
                # Django closes the view's own iterator before the one above, so what the view does
                # when its client hangs up (the query view records usage and logs its generation
                # line) would run with no fields unless the whole close runs inside them.
                response.close = _in_fields(fields, response.close)
            else:
                _log_request(request, response.status_code, started)
        return response


def _request_id_from(request) -> str:
    supplied = request.META.get("HTTP_X_REQUEST_ID", "")
    return supplied if SAFE_ID.fullmatch(supplied) else uuid.uuid4().hex


def _streamed(
    content: Iterable[bytes],
    fields: dict[str, str],
    *,
    request: Any,
    status: int,
    started: float,
) -> Iterator[bytes]:
    """Produce ``content`` one frame at a time inside the request's fields, then log the request.

    The fields are set while each frame is produced and reset before it is sent, rather than held
    from the first frame to the last, so two bodies iterated in turn on one thread each keep their
    own. A WSGI server sends one response at a time on a thread; the test client, for one, does not.
    """
    frames = iter(content)
    completed = False
    try:
        while True:
            with log_scope(**fields):
                frame = next(frames, None)
            if frame is None:
                completed = True
                return
            yield frame
    finally:
        with log_scope(**fields):
            _log_request(request, status, started, streamed=True, completed=completed)


def _in_fields(fields: dict[str, str], close: Callable[[], None]) -> Callable[[], None]:
    def close_in_fields() -> None:
        with log_scope(**fields):
            close()

    return close_in_fields


def _log_request(
    request: Any,
    status: int,
    started: float,
    *,
    streamed: bool = False,
    completed: bool = True,
) -> None:
    duration_ms = ms_since(started)
    match = getattr(request, "resolver_match", None)
    fields: dict[str, Any] = {
        "event": "request",
        "method": request.method,
        # The path, never the query string: a parameter can carry anything a client chose to send.
        "path": request.path,
        "status": status,
        "duration_ms": duration_ms,
    }
    if match is not None:
        fields["route"] = match.route  # the pattern, e.g. api/documents/<int:pk>, for aggregating
    if streamed:
        fields["streamed"] = True
        fields["completed"] = completed  # false: the client hung up, or the body raised
    logger.info(
        "%s %s %s in %d ms",
        request.method,
        request.path,
        status,
        duration_ms,
        extra=fields,
    )
