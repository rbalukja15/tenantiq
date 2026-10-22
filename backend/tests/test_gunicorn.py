"""The production server's configuration, run for real (#25, ADR-0022).

``gunicorn.conf.py`` makes claims that only show under concurrency, so none of them can be read off
the file: frames leave as the model produces them, one streamed answer does not hold up other
requests, and an answer is not cut off for taking longer than the worker timeout. They hold for the
gthread worker it selects. The last two fail under gunicorn's default sync worker, which a one-word
edit to that file would bring back.

Each test starts gunicorn on the shipped config with a stand-in app (``tests/gunicorn_probe.py``),
so no database or model is involved. Command-line flags, which gunicorn applies over the file, make
each claim strict: one worker, so a second process cannot answer in the first one's place, and a
two-second timeout, which a four-second stream outlives.
"""

from __future__ import annotations

import http.client
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[1]
CONFIG = BACKEND / "gunicorn.conf.py"

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="gunicorn does not run on Windows")


@contextmanager
def _gunicorn(log: Path, *flags: str) -> Iterator[int]:
    """Serve the probe app through the shipped config; yield the port it listens on."""
    port = _free_port()
    with log.open("w") as output:
        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "gunicorn",
                "--config",
                str(CONFIG),
                "--bind",
                f"127.0.0.1:{port}",
                # Teardown must not wait out the shipped 25-second grace on a stream that a failing
                # test left open.
                "--graceful-timeout",
                "1",
                *flags,
                "tests.gunicorn_probe:application",
            ],
            cwd=BACKEND,
            stdout=output,
            stderr=subprocess.STDOUT,
        )
        try:
            _wait_until_listening(process, port, log)
            yield port
        finally:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def _wait_until_listening(process: subprocess.Popen, port: int, log: Path) -> None:
    give_up = time.monotonic() + 15
    while time.monotonic() < give_up:
        if process.poll() is not None:
            pytest.fail(f"gunicorn exited with {process.returncode}:\n{log.read_text()}")
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                return
        except OSError:
            time.sleep(0.05)
    pytest.fail(f"gunicorn was not listening after 15 seconds:\n{log.read_text()}")


def test_a_streamed_answer_does_not_hold_up_other_requests(tmp_path: Path) -> None:
    """Under the sync worker the ping waits for the whole three-second stream; here it does not."""
    with _gunicorn(tmp_path / "gunicorn.log", "--workers", "1") as port:
        stream = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        sent = time.monotonic()
        stream.request("GET", "/stream?frames=6&interval=0.5")
        answer = stream.getresponse()
        first_frame = answer.readline()
        first_frame_after = time.monotonic() - sent

        ping = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        asked = time.monotonic()
        ping.request("GET", "/ping")
        pong = ping.getresponse().read()
        ping_took = time.monotonic() - asked

        rest = answer.read()

    assert first_frame == b"event: token\n"
    # A server that collected the body before sending it would deliver this frame three seconds in,
    # with the rest, and the user would watch a blank answer until then.
    assert first_frame_after < 1.0, f"the first frame took {first_frame_after:.2f}s to arrive"
    assert pong == b"pong"
    assert ping_took < 1.0, (
        f"a request made while one answer was streaming took {ping_took:.2f}s: the stream is "
        f"holding the worker, as gunicorn's sync worker does (see worker_class in gunicorn.conf.py)"
    )
    assert rest.endswith(b"event: citations\ndata: []\n\n")


def test_an_answer_that_outlives_the_worker_timeout_is_not_cut_off(tmp_path: Path) -> None:
    """The sync worker reports to the master only between requests, so the master kills it two
    seconds into this four-second stream and the client is left with half an answer."""
    with _gunicorn(tmp_path / "gunicorn.log", "--workers", "1", "--timeout", "2") as port:
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=15)
        connection.request("GET", "/stream?frames=8&interval=0.5")
        try:
            body = connection.getresponse().read()
        except (http.client.IncompleteRead, ConnectionError) as error:
            pytest.fail(f"the stream was cut off mid-answer: {error!r}")

    assert body.count(b"event: token") == 8
    assert body.endswith(b"event: citations\ndata: []\n\n")
