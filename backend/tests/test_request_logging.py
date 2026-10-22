"""Following one request through the logs (#26, ADR-0024).

The acceptance criterion is that the logs let you follow a single request end to end. So the tests
read the lines exactly as a deployment prints them, through the real filter and the JSON formatter,
and follow them by ``request_id``:

- **Every request** gets an id, returns it as ``X-Request-ID``, and logs one line with its tenant.
  Django's own 4xx and 5xx lines carry it too.
- **A query** is followed from the API through retrieval and the floor to the model and back, and
  its last lines are logged after the view has returned, while the answer streams.
- **An upload** shares its id with the ingestion it queues, across a real publish into a worker.
- **Nothing leaks**: no line names another request's tenant, even with two answers streaming in
  turn on one thread, and no line carries a question, an answer or a passage, at any level.
- **Every process prints the same way**: JSON in a deployment, text under DEBUG, and the worker
  through the same configuration as the API.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import uuid
from decimal import Decimal
from pathlib import Path

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import connection
from rest_framework.test import APIClient

from app.embeddings import EmbeddingCountError
from app.log_context import (
    TASK_HEADER,
    JsonFormatter,
    LogContextFilter,
    TextFormatter,
    current_log_context,
    log_scope,
)
from app.models import Chunk, Document, Tenant
from app.tenant_context import get_current_tenant_id, tenant_context
from tests.conftest import TEST_CLIENT_ID, TEST_ISSUER

pytestmark = pytest.mark.django_db

BACKEND = Path(__file__).resolve().parent.parent

requires_postgres = pytest.mark.skipif(
    connection.vendor != "postgresql", reason="vector search is a Postgres/pgvector feature"
)


# --- helpers --------------------------------------------------------------------------------------


class _Lines(logging.Handler):
    """Every line the root logger emits, through the production filter and JSON formatter."""

    def __init__(self) -> None:
        super().__init__(logging.INFO)
        self.addFilter(LogContextFilter())
        self.setFormatter(JsonFormatter())
        self.raw: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.raw.append(self.format(record))

    @property
    def parsed(self) -> list[dict]:
        return [json.loads(line) for line in self.raw]

    def events(self, request_id: str) -> list[dict]:
        """The lines that carry an ``event``, for one request, in the order they were logged."""
        mine = [line for line in self.parsed if line.get("request_id") == request_id]
        return [line for line in mine if "event" in line]

    def event(self, request_id: str, name: str) -> dict:
        matching = [line for line in self.events(request_id) if line["event"] == name]
        assert len(matching) == 1, (name, self.events(request_id))
        return matching[0]


@pytest.fixture
def lines():
    handler = _Lines()
    root = logging.getLogger()
    previous = root.level
    root.addHandler(handler)
    root.setLevel(logging.INFO)
    yield handler
    root.removeHandler(handler)
    root.setLevel(previous)


@pytest.fixture
def configured_auth(settings, rsa_keys):
    from app.auth.tenancy import tenant_for_issuer
    from app.auth.verifier import TenantTokenVerifier

    _, public_pem = rsa_keys
    settings.TENANTIQ_TOKEN_VERIFIER_FACTORY = lambda: TenantTokenVerifier(
        key_resolver=lambda token, tenant: public_pem,
        tenant_lookup=tenant_for_issuer,
    )


@pytest.fixture(autouse=True)
def _isolated_media(settings, tmp_path):
    settings.MEDIA_ROOT = str(tmp_path)


@pytest.fixture(autouse=True)
def _admit_every_source(settings):
    # The suite embeds with the hashing embedder, whose similarities are on a different scale from a
    # real model's, so these tests pin the floor rather than inherit one measured elsewhere. The one
    # test about refusals sets its own.
    settings.TENANTIQ_RETRIEVAL_MIN_SIMILARITY = 0.0


def _tenant(slug: str) -> Tenant:
    issuer = TEST_ISSUER if slug == "acme" else f"https://keycloak.test/realms/{slug}"
    client_id = TEST_CLIENT_ID if slug == "acme" else f"tenantiq-{slug}"
    return Tenant.objects.create(
        slug=slug, name=slug.title(), oidc_issuer=issuer, oidc_client_id=client_id
    )


def _bearer(mint_token, tenant: Tenant, sub: str = "alice") -> dict[str, str]:
    token = mint_token(sub=sub, issuer=tenant.oidc_issuer, audience=tenant.oidc_client_id)
    return {"HTTP_AUTHORIZATION": f"Bearer {token}"}


def _seed(tenant: Tenant, texts: list[str]) -> None:
    from app.embeddings import get_embedder

    embedder = get_embedder()
    with tenant_context(tenant):
        doc = Document.objects.create(title="doc")
        for i, text in enumerate(texts):
            Chunk.objects.create(
                document=doc,
                index=i,
                text=text,
                char_count=len(text),
                start_offset=0,
                end_offset=len(text),
                embedding=embedder.embed_query(text),
                embedding_model=embedder.model,
            )


def _ask(mint_token, tenant: Tenant, question: str, sub: str = "alice"):
    return APIClient().post(
        "/api/query", {"question": question}, format="json", **_bearer(mint_token, tenant, sub)
    )


# --- every request --------------------------------------------------------------------------------


def test_every_request_gets_an_id_that_it_returns_and_logs_with_its_tenant(
    configured_auth, mint_token, lines
):
    acme = _tenant("acme")

    response = APIClient().get("/api/me", **_bearer(mint_token, acme))

    assert response.status_code == 200
    request_id = response["X-Request-ID"]
    assert len(request_id) == 32 and int(request_id, 16) >= 0  # a fresh uuid4, as hex
    line = lines.event(request_id, "request")
    assert line["tenant_id"] == str(acme.id)
    assert line["method"] == "GET"
    assert line["path"] == "/api/me"
    assert line["route"] == "api/me"
    assert line["status"] == 200
    assert isinstance(line["duration_ms"], int) and line["duration_ms"] >= 0
    assert line["level"] == "INFO" and line["logger"] == "app.middleware"
    assert "streamed" not in line


def test_an_upstream_request_id_is_kept(configured_auth, mint_token, lines):
    acme = _tenant("acme")

    response = APIClient().get(
        "/api/me", HTTP_X_REQUEST_ID="edge-7f3a.42", **_bearer(mint_token, acme)
    )

    assert response["X-Request-ID"] == "edge-7f3a.42"
    assert lines.event("edge-7f3a.42", "request")["tenant_id"] == str(acme.id)


@pytest.mark.parametrize(
    "supplied",
    ["", "has a space", "line\nbreak", 'quote"d', "x" * 129, "<script>"],
    ids=["empty", "space", "newline", "quote", "too-long", "markup"],
)
def test_a_request_id_that_is_not_an_identifier_is_replaced(configured_auth, mint_token, supplied):
    # It is printed on every line the request logs, so anything that could break a line, or pass
    # for something else in it, is not taken.
    response = APIClient().get(
        "/api/me", HTTP_X_REQUEST_ID=supplied, **_bearer(mint_token, _tenant("acme"))
    )

    assert response["X-Request-ID"] != supplied
    assert len(response["X-Request-ID"]) == 32


def test_a_request_with_no_tenant_is_logged_without_one(lines):
    response = APIClient().get("/api/me")

    assert response.status_code == 401
    line = lines.event(response["X-Request-ID"], "request")
    assert line["status"] == 401
    assert "tenant_id" not in line


def test_django_s_own_4xx_line_carries_the_request_id(configured_auth, mint_token, lines):
    # Django logs a 4xx the view returned once the whole middleware chain has returned, so after the
    # request's fields are gone. It reads them off the request instead.
    acme = _tenant("acme")

    response = APIClient().get("/api/documents/999999", **_bearer(mint_token, acme))

    assert response.status_code == 404
    (django_line,) = [line for line in lines.parsed if line["logger"] == "django.request"]
    assert django_line["message"] == "Not Found: /api/documents/999999"
    assert django_line["request_id"] == response["X-Request-ID"]
    assert django_line["tenant_id"] == str(acme.id)


def test_an_unhandled_error_is_logged_with_its_request_and_its_traceback(
    configured_auth, mint_token, lines, monkeypatch
):
    from app.views import MeView

    def _broken(self, request):
        raise RuntimeError("the view broke")

    monkeypatch.setattr(MeView, "get", _broken)
    acme = _tenant("acme")

    response = APIClient(raise_request_exception=False).get("/api/me", **_bearer(mint_token, acme))

    assert response.status_code == 500
    (error,) = [line for line in lines.parsed if line["logger"] == "django.request"]
    assert error["level"] == "ERROR" and error["message"] == "Internal Server Error: /api/me"
    assert error["request_id"] == response["X-Request-ID"]
    assert error["tenant_id"] == str(acme.id)
    assert "RuntimeError: the view broke" in error["exception"]
    assert lines.event(response["X-Request-ID"], "request")["status"] == 500


def test_nothing_outlives_the_request(configured_auth, mint_token):
    APIClient().get("/api/me", **_bearer(mint_token, _tenant("acme")))

    # The next request on this thread starts from nothing, whatever this one bound.
    assert current_log_context() == {}
    assert get_current_tenant_id() is None


def test_two_tenants_requests_never_share_a_field(configured_auth, mint_token, lines):
    acme, globex = _tenant("acme"), _tenant("globex")

    first = APIClient().get("/api/me", **_bearer(mint_token, acme))
    second = APIClient().get("/api/me", **_bearer(mint_token, globex, sub="bob"))

    assert first["X-Request-ID"] != second["X-Request-ID"]
    assert lines.event(first["X-Request-ID"], "request")["tenant_id"] == str(acme.id)
    assert lines.event(second["X-Request-ID"], "request")["tenant_id"] == str(globex.id)
    by_tenant = {
        line["tenant_id"]: line["request_id"] for line in lines.parsed if "tenant_id" in line
    }
    assert by_tenant == {
        str(acme.id): first["X-Request-ID"],
        str(globex.id): second["X-Request-ID"],
    }


# --- a query, from the API to the model and back (Postgres/pgvector) -------------------------------


@requires_postgres
def test_one_query_can_be_followed_from_the_api_to_the_model(configured_auth, mint_token, lines):
    acme = _tenant("acme")
    _seed(acme, ["Invoice payment terms are net thirty days after receipt."])

    response = _ask(mint_token, acme, "when are invoice payment terms due?")
    request_id = response["X-Request-ID"]
    # The view has returned. Everything from here on is logged while the answer streams.
    before_the_stream = [line["event"] for line in lines.events(request_id)]
    b"".join(response.streaming_content)

    events = lines.events(request_id)
    assert before_the_stream == ["retrieval", "sources"]
    assert [line["event"] for line in events] == ["retrieval", "sources", "generation", "request"]
    assert {line["tenant_id"] for line in events} == {str(acme.id)}

    retrieval = lines.event(request_id, "retrieval")
    assert retrieval["embedding_model"] == "hashing-fake-v1"
    assert retrieval["candidates"] == 1 and retrieval["k"] == 5
    assert retrieval["embed_ms"] + retrieval["search_ms"] == retrieval["duration_ms"]

    sources = lines.event(request_id, "sources")
    with tenant_context(acme):
        chunk_ids = list(Chunk.objects.values_list("id", flat=True))
    assert sources["sources"] == 1 and sources["source_chunk_ids"] == chunk_ids
    assert 0.0 < sources["best_similarity"] <= 1.0

    generation = lines.event(request_id, "generation")
    assert generation["outcome"] == "answered"
    assert generation["model"] == "fake-llm-v1"
    assert generation["cited_chunk_ids"] == chunk_ids and generation["citations"] == 1
    assert generation["input_tokens"] > 0 and generation["output_tokens"] > 0
    assert 0 <= generation["first_token_ms"] <= generation["duration_ms"]

    request = lines.event(request_id, "request")
    assert request["status"] == 200 and request["route"] == "api/query"
    assert request["streamed"] is True and request["completed"] is True
    assert request["duration_ms"] >= generation["duration_ms"]


@requires_postgres
def test_a_client_that_hangs_up_still_leaves_a_traced_answer(configured_auth, mint_token, lines):
    # The view's own generator is closed before the middleware's, so its cleanup (accounting and the
    # generation line) runs after the client has gone. That is the case most worth tracing, since
    # the tenant is charged for the tokens produced.
    acme = _tenant("acme")
    _seed(acme, ["Refund policy: returns accepted within fourteen days."])

    response = _ask(mint_token, acme, "what is the refund policy?")
    frames = iter(response.streaming_content)
    next(frames)  # the first token frame reaches the client...
    response.close()  # ...and then it hangs up

    request_id = response["X-Request-ID"]
    generation = lines.event(request_id, "generation")
    assert generation["outcome"] == "disconnected"
    assert generation["tenant_id"] == str(acme.id)
    assert generation["output_tokens"] > 0  # charged for what it did produce (#17)
    request = lines.event(request_id, "request")
    assert request["completed"] is False and request["tenant_id"] == str(acme.id)
    assert current_log_context() == {}


@requires_postgres
def test_a_refusal_is_traced_with_the_best_score_that_missed_the_floor(
    configured_auth, mint_token, lines, settings
):
    settings.TENANTIQ_RETRIEVAL_MIN_SIMILARITY = 0.5
    acme = _tenant("acme")
    _seed(acme, ["Invoice payment terms are net thirty days after receipt."])

    response = _ask(mint_token, acme, "who won the cup final?")  # shares no word with the corpus
    b"".join(response.streaming_content)

    request_id = response["X-Request-ID"]
    sources = lines.event(request_id, "sources")
    assert sources["sources"] == 0 and sources["candidates"] == 1
    assert sources["best_similarity"] < sources["min_similarity"] == 0.5
    generation = lines.event(request_id, "generation")
    assert generation["outcome"] == "refused"
    # The model was never called, so nothing claims it was.
    assert "model" not in generation and "first_token_ms" not in generation
    assert generation["input_tokens"] == generation["output_tokens"] == 0


class _CanaryLLM:
    model = "canary-llm"

    def stream(self, system_prompt: str, user_prompt: str):  # noqa: ARG002
        yield "ANSWERCANARY is in the answer "
        yield "[1]"

    def generate(self, system_prompt: str, user_prompt: str):  # noqa: ARG002
        raise NotImplementedError


@requires_postgres
def test_no_question_answer_or_passage_text_reaches_the_logs(
    configured_auth, mint_token, lines, settings
):
    # Logs leave the tenant boundary: an operator reads them, a collector stores them, and none of
    # the isolation the database has applies (docs/threat-model.md, T15). Each canary is in exactly
    # one of the three places, and the answer path really ran (the passage was cited), so a line
    # that logged any of them would have had it to log.
    settings.TENANTIQ_LLM_FACTORY = lambda: _CanaryLLM()
    acme = _tenant("acme")
    _seed(acme, ["PASSAGECANARY the warranty covers parts and labour for two years."])

    response = _ask(mint_token, acme, "QUESTIONCANARY what does the warranty cover?")
    body = b"".join(response.streaming_content)

    assert b"ANSWERCANARY" in body
    assert lines.event(response["X-Request-ID"], "generation")["citations"] == 1
    logged = "\n".join(lines.raw)
    assert "QUESTIONCANARY" not in logged
    assert "PASSAGECANARY" not in logged
    assert "ANSWERCANARY" not in logged


@requires_postgres
def test_a_stream_keeps_its_own_tenant_while_another_tenants_request_runs(
    configured_auth, mint_token, lines
):
    acme, globex = _tenant("acme"), _tenant("globex")
    _seed(acme, ["Invoice payment terms are net thirty days after receipt."])

    answer = _ask(mint_token, acme, "when are invoice payment terms due?")
    frames = iter(answer.streaming_content)
    next(frames)  # acme's answer is mid-stream when globex's request runs
    other = APIClient().get("/api/me", **_bearer(mint_token, globex, sub="bob"))
    list(frames)

    assert lines.event(other["X-Request-ID"], "request")["tenant_id"] == str(globex.id)
    for name in ("generation", "request"):
        assert lines.event(answer["X-Request-ID"], name)["tenant_id"] == str(acme.id)
    # Every line names one request and that request's tenant, never the other's.
    tenant_of = {answer["X-Request-ID"]: str(acme.id), other["X-Request-ID"]: str(globex.id)}
    for line in lines.parsed:
        if "request_id" in line and "tenant_id" in line:
            assert line["tenant_id"] == tenant_of[line["request_id"]], line


@requires_postgres
def test_two_answers_streamed_in_turn_on_one_thread_keep_their_own_fields(
    configured_auth, mint_token, lines
):
    # A WSGI server finishes one response before it starts the next on a thread. The test client
    # does not, and nothing guarantees that whatever iterates a body never interleaves two. Each
    # frame is produced inside its own request's fields, so neither answer's lines can carry the
    # other's request or tenant, and nothing is left behind once both are done.
    acme, globex = _tenant("acme"), _tenant("globex")
    _seed(acme, ["Acme payment terms are net thirty days."])
    _seed(globex, ["Globex payment terms are net sixty days."])

    first = _ask(mint_token, acme, "payment terms?")
    second = _ask(mint_token, globex, "payment terms?", sub="bob")
    streams = [iter(first.streaming_content), iter(second.streaming_content)]
    while streams:  # one frame from each in turn, until both are done
        for stream in list(streams):
            if next(stream, None) is None:
                streams.remove(stream)

    tenant_of = {first["X-Request-ID"]: str(acme.id), second["X-Request-ID"]: str(globex.id)}
    for request_id, tenant_id in tenant_of.items():
        for name in ("retrieval", "sources", "generation", "request"):
            assert lines.event(request_id, name)["tenant_id"] == tenant_id
    for line in lines.parsed:
        if "request_id" in line:
            assert line.get("tenant_id") == tenant_of[line["request_id"]], line
    assert current_log_context() == {}


# --- an upload, and the ingestion it queues ---------------------------------------------------------


@pytest.fixture
def broker(monkeypatch):
    """A real publish into an in-memory broker, with the worker's own request handling on the far
    side, instead of the eager mode the rest of the suite runs tasks in.

    Eager mode runs a task inside its caller, so the caller's fields are simply still there: it can
    say nothing about whether they travel with the message. This publishes for real, and the
    worker's ``Request`` executes the task from the message it received.
    """
    from config.celery import app

    monkeypatch.setitem(app.conf, "CELERY_TASK_ALWAYS_EAGER", False)
    monkeypatch.setitem(app.conf, "CELERY_BROKER_URL", "memory://")
    # The first eager delay() in the suite builds the app's connection and producer pools against
    # the configured broker, and they are kept for the life of the app. Set them aside, so this
    # publish builds its own against the one above.
    monkeypatch.setattr(app, "_pool", None)
    monkeypatch.setattr(app.amqp, "_producer_pool", None)
    connection = app.connection_for_read()
    queue = connection.SimpleQueue("celery")
    queue.clear()  # the memory broker lives as long as the process: start from an empty queue

    def run_next_task() -> dict:
        """Receive one message as the worker would, run it, and return its headers."""
        from celery.worker.request import Request

        message = queue.get(timeout=2)
        message.ack()
        Request(message, app=app, task=app.tasks[message.headers["task"]]).execute()
        return message.headers

    yield run_next_task
    queue.clear()  # a retry the test left unrun
    queue.close()
    connection.release()


@pytest.mark.django_db(transaction=True)
def test_an_upload_and_the_ingestion_it_queues_share_a_request_id(
    configured_auth, mint_token, lines, broker
):
    # transaction=True so the upload really commits, and its on_commit publish runs where it does in
    # production: inside the request, before the response leaves.
    acme = _tenant("acme")

    response = APIClient().post(
        "/api/documents",
        {"file": SimpleUploadedFile("notes.txt", b"Plenty of words for one chunk.", "text/plain")},
        format="multipart",
        HTTP_X_REQUEST_ID="upload-1",
        **_bearer(mint_token, acme),
    )
    assert response.status_code == 201, response.content
    assert current_log_context() == {}  # the worker below starts from nothing, as a real one does

    headers = broker()

    assert headers[TASK_HEADER] == {"request_id": "upload-1", "tenant_id": str(acme.id)}
    ingestion = lines.event("upload-1", "ingestion")
    assert ingestion["tenant_id"] == str(acme.id)
    assert ingestion["task_name"] == "app.tasks.ingest_document"
    assert ingestion["task_id"] == headers["id"]
    assert ingestion["document_id"] == response.json()["id"] and ingestion["chunks"] == 1
    celery_line = next(line for line in lines.parsed if line["logger"] == "celery.app.trace")
    assert "succeeded" in celery_line["message"]
    assert celery_line["request_id"] == "upload-1" and celery_line["task_id"] == headers["id"]
    assert current_log_context() == {}  # and the task's fields went with it


@pytest.mark.django_db(transaction=True)
def test_a_retried_ingestion_keeps_the_request_it_came_from(
    configured_auth, mint_token, broker, monkeypatch
):
    # A transient failure is retried by republishing the task, which is a new message. It has to
    # carry the request with it, or every attempt after the first is untraceable.
    acme = _tenant("acme")

    def _short_batch(*args, **kwargs):
        raise EmbeddingCountError("the embedding backend returned fewer vectors than texts")

    monkeypatch.setattr("app.ingestion.embed_in_batches", _short_batch)
    APIClient().post(
        "/api/documents",
        {"file": SimpleUploadedFile("notes.txt", b"Plenty of words for one chunk.", "text/plain")},
        format="multipart",
        HTTP_X_REQUEST_ID="upload-2",
        **_bearer(mint_token, acme),
    )

    first = broker()  # fails, and is retried
    retry = broker()

    assert retry["id"] == first["id"] and retry["retries"] == 1
    assert retry[TASK_HEADER] == {"request_id": "upload-2", "tenant_id": str(acme.id)}


@pytest.mark.django_db(transaction=True)
def test_a_task_no_request_queued_is_named_by_its_own_id(lines, broker):
    from app.tasks import ingest_document

    acme = _tenant("acme")
    with tenant_context(acme):
        document = Document.objects.create(
            title="notes.txt",
            content_type="text/plain",
            file=SimpleUploadedFile("notes.txt", b"Plenty of words for one chunk.", "text/plain"),
        )
    ingest_document.delay(document.id, acme.id)  # as beat or a shell would, outside any request

    headers = broker()

    assert TASK_HEADER not in headers
    ingestion = next(line for line in lines.parsed if line.get("event") == "ingestion")
    assert ingestion["task_id"] == headers["id"]
    assert "request_id" not in ingestion
    # Outside the tenant context there is no tenant to name; the message text still names it.
    assert str(acme.id) in ingestion["message"]


def test_a_carried_field_that_is_not_an_identifier_is_dropped(lines):
    from celery.app.task import Context

    from app.log_context import enter_task, exit_task

    class _Task:
        name = "app.tasks.ingest_document"
        request = Context(
            {
                "id": "task-1",
                "task": "app.tasks.ingest_document",
                TASK_HEADER: {"request_id": "ok-1", "tenant_id": "bad\nvalue", "extra": "x"},
            }
        )

    enter_task(_Task)
    try:
        assert current_log_context() == {
            "request_id": "ok-1",
            "task_id": "task-1",
            "task_name": "app.tasks.ingest_document",
        }
    finally:
        exit_task(_Task)
    assert current_log_context() == {}


# --- how every process prints ---------------------------------------------------------------------


def _record(message: str = "hello %s", *args: object, **extra: object) -> logging.LogRecord:
    record = logging.getLogger("app.example").makeRecord(
        "app.example", logging.WARNING, __file__, 1, message, args or ("world",), None, extra=extra
    )
    LogContextFilter().filter(record)
    return record


def test_a_json_line_carries_the_fields_the_extras_and_the_exception():
    with log_scope(request_id="r-1", tenant_id="t-1"):
        try:
            raise ValueError("boom")
        except ValueError:
            record = _record(event="example", count=3)
            record.exc_info = sys.exc_info()

    line = json.loads(JsonFormatter().format(record))

    assert list(line)[:6] == ["time", "level", "logger", "message", "request_id", "tenant_id"]
    assert line["time"].endswith("Z") and line["level"] == "WARNING"
    assert line["message"] == "hello world"
    assert line["event"] == "example" and line["count"] == 3
    assert line["exception"].startswith("Traceback") and "ValueError: boom" in line["exception"]
    assert "task_id" not in line  # unset fields are left out, not printed as null


def test_an_extra_never_overwrites_a_line_s_own_keys():
    line = json.loads(JsonFormatter().format(_record(level="spoofed", logger="spoofed")))

    assert line["level"] == "WARNING" and line["logger"] == "app.example"


def test_a_value_json_cannot_encode_is_printed_as_text():
    ident = uuid.uuid4()
    line = json.loads(JsonFormatter().format(_record(tenant=ident, cost=Decimal("0.000123"))))

    assert line["tenant"] == str(ident) and line["cost"] == "0.000123"


def test_a_text_line_prints_the_same_fields():
    with log_scope(request_id="r-1"):
        record = _record(event="example", note="two words")

    text = TextFormatter().format(record)

    assert " WARNING app.example hello world  " in text
    assert 'request_id=r-1 event=example note="two words"' in text


def _in_a_fresh_process(script: str, *argv: str, **env: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-c", script, *argv],
        cwd=BACKEND,
        env={**os.environ, "DJANGO_SETTINGS_MODULE": "config.settings", **env},
        capture_output=True,
        text=True,
        timeout=60,
    )


_ONE_WARNING = (
    "import django; django.setup(); import logging;"
    "logging.getLogger('app.example').warning('hello')"
)


def test_a_deployment_prints_json_and_debug_prints_text():
    deployed = _in_a_fresh_process(_ONE_WARNING, DJANGO_DEBUG="false", TENANTIQ_LOG_FORMAT="")
    debugging = _in_a_fresh_process(_ONE_WARNING, DJANGO_DEBUG="true", TENANTIQ_LOG_FORMAT="")

    assert json.loads(deployed.stderr.strip())["message"] == "hello"
    assert debugging.stderr.strip().endswith("WARNING app.example hello")


def test_a_deployment_prints_django_s_server_errors():
    # Before #26, with DEBUG off, Django sent these to its console handler, which prints only under
    # DEBUG, and to mail_admins, with no ADMINS to mail: a 500's traceback was printed nowhere.
    script = (
        "import django; django.setup(); import logging\n"
        "try:\n"
        "    raise RuntimeError('the view broke')\n"
        "except RuntimeError:\n"
        "    logging.getLogger('django.request').error('Internal Server Error: /api/me', exc_info=True)\n"
    )

    result = _in_a_fresh_process(script, DJANGO_DEBUG="false", TENANTIQ_LOG_FORMAT="")

    line = json.loads(result.stderr.strip())
    assert line["level"] == "ERROR" and line["logger"] == "django.request"
    assert "RuntimeError: the view broke" in line["exception"]


@pytest.mark.parametrize(
    "setting, value, complaint",
    [
        ("TENANTIQ_LOG_FORMAT", "xml", "TENANTIQ_LOG_FORMAT must be 'json' or 'text'"),
        ("TENANTIQ_LOG_LEVEL", "verbose", "TENANTIQ_LOG_LEVEL must be DEBUG, INFO, WARNING"),
    ],
)
def test_an_unknown_log_setting_stops_the_process_at_startup(setting, value, complaint):
    result = _in_a_fresh_process(_ONE_WARNING, **{setting: value})

    assert result.returncode != 0
    assert complaint in result.stderr


def test_debug_logs_ours_but_never_the_model_request_or_the_sql():
    # The Anthropic SDK prints each request whole at DEBUG, prompt and passages included, and Django
    # prints each SQL statement with its parameters: tenant content, either way (T15).
    script = (
        "import django; django.setup(); import logging;"
        "print(*(logging.getLogger(name).isEnabledFor(logging.DEBUG) for name in"
        " ('app.rag', 'anthropic._base_client', 'django.db.backends')))"
    )

    result = _in_a_fresh_process(script, DJANGO_DEBUG="true", TENANTIQ_LOG_LEVEL="debug")

    assert result.stdout.split() == ["True", "False", "False"], result.stderr


def test_the_celery_worker_logs_through_djangos_configuration():
    # Without a receiver on setup_logging, Celery replaces the root handlers with its own text
    # format when a worker or beat starts. Disconnecting the receiver is the control.
    script = (
        "import sys, django, logging; django.setup();"
        "from celery.signals import setup_logging;"
        "from config.celery import app, _log_through_django;"
        "sys.argv[1:] == ['--without-receiver'] and setup_logging.disconnect(_log_through_django);"
        "app.log.setup(loglevel='INFO');"
        "logging.getLogger('celery.worker').info('worker ready')"
    )

    def start(*argv: str) -> str:
        result = _in_a_fresh_process(script, *argv, TENANTIQ_LOG_FORMAT="json")
        assert result.returncode == 0, result.stderr
        return result.stderr.strip().splitlines()[-1]

    assert json.loads(start())["logger"] == "celery.worker"
    with pytest.raises(json.JSONDecodeError):
        json.loads(start("--without-receiver"))
