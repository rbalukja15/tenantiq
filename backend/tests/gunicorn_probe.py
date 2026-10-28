"""A stand-in WSGI app that test_gunicorn.py serves through gunicorn.conf.py.

Not a test module (pytest collects only ``test_*.py``) and not Django: what is under test is the
server configuration, so the app is the smallest thing shaped like the two requests the API serves.
``/stream`` sends SSE frames slowly, the way an answer arrives while the model generates, and
``/ping`` answers at once, the way a document list does.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterable, Iterator
from urllib.parse import parse_qs


def application(environ: dict, start_response: Callable) -> Iterable[bytes]:
    if environ["PATH_INFO"] == "/stream":
        params = parse_qs(environ.get("QUERY_STRING", ""))
        frames = int(params.get("frames", ["6"])[0])
        interval = float(params.get("interval", ["0.5"])[0])
        start_response(
            "200 OK", [("Content-Type", "text/event-stream"), ("Cache-Control", "no-cache")]
        )
        return _frames(frames, interval)

    start_response("200 OK", [("Content-Type", "text/plain"), ("Content-Length", "4")])
    return [b"pong"]


def _frames(count: int, interval: float) -> Iterator[bytes]:
    for n in range(count):
        yield f"event: token\ndata: {n}\n\n".encode()
        time.sleep(interval)
    yield b"event: citations\ndata: []\n\n"
