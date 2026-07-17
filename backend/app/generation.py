"""Grounded answer generation with an enforced citation schema (#15).

Turns an ``AssembledContext`` (#14) into a ``GroundedAnswer``: call the LLM with the grounded prompt,
require a structured ``{answer, citations}`` result, and resolve each cited source *number* back to the
real chunk it stands for. Two guarantees hold by construction:

- **The LLM never invents a citation.** The model cites source numbers (``[1]``, ``[2]`` …) from the
  prompt; we map each number to the ``Source`` it was assigned in #14 and *drop* any number that
  doesn't match one. A citation therefore always resolves to a real, retrieved chunk (CLAUDE.md).
- **No context → refuse without spending a token.** If retrieval found nothing (``has_context`` is
  false), we return a refusal and never call the model.

**Citation anchoring (ADR-0008).** Chunk primary keys are *not* stable across re-ingestion — the #13
retry path deletes and recreates chunks with new PKs. So a ``Citation`` carries the durable anchor a
resolver (#51) can re-locate the span by — ``document_id`` + ``chunk_index`` + ``start_offset`` /
``end_offset`` (#45) — alongside ``chunk_id`` as the current snapshot.

The LLM client is pluggable like the embedder (ADR-0004): a deterministic fake under pytest (no
network, no key), an Anthropic adapter otherwise, with an Ollama fallback. Generation itself makes no
DB query — it operates on the already-retrieved ``AssembledContext`` — so the tenant scoping done in
#14 is inherited.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Protocol
from urllib import request as _urlrequest

from django.conf import settings
from django.utils.module_loading import import_string

from app.rag import AssembledContext, Source


@dataclass(frozen=True)
class LLMResult:
    """The raw structured output of one generation: the answer text and the source numbers cited."""

    answer: str
    citations: tuple[int, ...]


@dataclass(frozen=True)
class Citation:
    """A resolved citation: the ``[number]`` the answer used, plus the identifiers it resolves to.
    ``chunk_id`` is the current snapshot; ``(document_id, chunk_index, start_offset, end_offset)`` is
    the anchor that survives a re-ingestion (ADR-0008)."""

    number: int
    chunk_id: int
    document_id: int
    document_title: str
    chunk_index: int
    start_offset: int
    end_offset: int


@dataclass(frozen=True)
class GroundedAnswer:
    """A generated answer plus the citations that resolved to real retrieved chunks. ``refused`` is
    true only for the no-context path — an answer that cites nothing is still a real answer."""

    text: str
    citations: tuple[Citation, ...]
    refused: bool


class LLMClient(Protocol):
    """Generate a structured answer from a grounded prompt. Implementations must not add context —
    the prompt already carries the sources — and must return source *numbers*, not chunk IDs."""

    def generate(self, system_prompt: str, user_prompt: str) -> LLMResult: ...


_REFUSAL_TEXT = "I don't have relevant information in your documents to answer that question."


def generate_answer(context: AssembledContext, *, llm: LLMClient | None = None) -> GroundedAnswer:
    """Generate a grounded, cited answer for ``context``.

    If nothing was retrieved, refuse without calling the LLM. Otherwise call the model, then keep only
    the citations whose number maps to a source in ``context`` — deduplicated, in first-seen order.
    """
    if not context.has_context:
        return GroundedAnswer(text=_REFUSAL_TEXT, citations=(), refused=True)

    llm = llm if llm is not None else get_llm()
    result = llm.generate(context.system_prompt, context.user_prompt)
    citations = _resolve_citations(context.sources, result.citations)
    return GroundedAnswer(text=result.answer, citations=citations, refused=False)


def _resolve_citations(
    sources: tuple[Source, ...], cited_numbers: tuple[int, ...]
) -> tuple[Citation, ...]:
    """Map cited source numbers to :class:`Citation`s, dropping unknown numbers and duplicates."""
    by_number = {source.number: source for source in sources}
    resolved: list[Citation] = []
    seen: set[int] = set()
    for number in cited_numbers:
        source = by_number.get(number)
        if source is None or number in seen:
            continue  # invented number, or already cited
        seen.add(number)
        resolved.append(
            Citation(
                number=source.number,
                chunk_id=source.chunk_id,
                document_id=source.document_id,
                document_title=source.document_title,
                chunk_index=source.chunk_index,
                start_offset=source.start_offset,
                end_offset=source.end_offset,
            )
        )
    return tuple(resolved)


def _coerce_result(raw: object) -> LLMResult:
    """Validate a raw ``{answer, citations}`` payload from a backend into an :class:`LLMResult`.

    Tolerant of a model that omits or malforms fields — it must never raise, so a malformed payload
    degrades to an uncited (or empty) answer that the caller can still use. A non-object payload, a
    non-string answer, and a ``citations`` field that isn't a list of integers are all absorbed."""
    if not isinstance(raw, dict):
        return LLMResult(answer="", citations=())
    answer = raw.get("answer")
    text = answer if isinstance(answer, str) else ""
    raw_citations = raw.get("citations")
    entries = raw_citations if isinstance(raw_citations, list) else []
    numbers: list[int] = []
    for value in entries:
        if isinstance(value, bool):  # bool is an int subclass — exclude it explicitly
            continue
        if isinstance(value, int):
            numbers.append(value)
    return LLMResult(answer=text, citations=tuple(numbers))


# --- pluggable backends ---------------------------------------------------------------------------

# Structured-output contract shared by the real backends: answer text + the source numbers cited.
_ANSWER_SCHEMA = {
    "type": "object",
    "properties": {
        "answer": {"type": "string"},
        "citations": {
            "type": "array",
            "items": {"type": "integer"},
            "description": "The source numbers ([1], [2], ...) the answer relies on.",
        },
    },
    "required": ["answer", "citations"],
    "additionalProperties": False,
}


class FakeLLM:
    """Deterministic, dependency-free LLM for tests and offline runs. Grounds a trivial answer in the
    first source of the prompt so the citation-resolution path is exercised without a network call.
    """

    def __init__(self, *, model: str = "fake-llm-v1") -> None:
        self.model = model

    def generate(self, system_prompt: str, user_prompt: str) -> LLMResult:  # noqa: ARG002
        cite_first = "[1]" if "[1]" in user_prompt else ""
        return LLMResult(
            answer=f"Based on your documents, here is the answer. {cite_first}".strip(),
            citations=(1,) if cite_first else (),
        )


class AnthropicLLM:
    """Anthropic Messages API with schema-enforced structured output (see the claude-api reference).

    Uses ``output_config.format`` to constrain the reply to ``_ANSWER_SCHEMA``; the first text block is
    then valid JSON. Not exercised by the hermetic suite (needs a key + network) — the parsing it feeds
    is covered via :func:`_coerce_result`."""

    def __init__(self, *, model: str | None = None, max_tokens: int | None = None) -> None:
        self.model = model or settings.TENANTIQ_LLM_MODEL
        self.max_tokens = max_tokens or settings.TENANTIQ_LLM_MAX_TOKENS

    def generate(self, system_prompt: str, user_prompt: str) -> LLMResult:
        import anthropic

        client = anthropic.Anthropic()
        response = client.messages.create(
            model=self.model,
            max_tokens=self.max_tokens,
            system=system_prompt,
            messages=[{"role": "user", "content": user_prompt}],
            output_config={"format": {"type": "json_schema", "schema": _ANSWER_SCHEMA}},
        )
        text = next((b.text for b in response.content if b.type == "text"), "{}")
        return _coerce_result(json.loads(text))


class OllamaLLM:
    """Local fallback: an Ollama chat model constrained to JSON via ``format`` (mirrors OllamaEmbedder).
    Keeps ``make dev`` answering without an Anthropic key. Not hermetically tested."""

    def __init__(self, *, model: str | None = None, base_url: str | None = None) -> None:
        self.model = model or settings.TENANTIQ_LLM_OLLAMA_MODEL
        self.base_url = (base_url or settings.OLLAMA_BASE_URL).rstrip("/")

    def generate(self, system_prompt: str, user_prompt: str) -> LLMResult:
        payload = json.dumps(
            {
                "model": self.model,
                "stream": False,
                "format": _ANSWER_SCHEMA,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
            }
        ).encode()
        req = _urlrequest.Request(
            f"{self.base_url}/api/chat",
            data=payload,
            headers={"Content-Type": "application/json"},
        )
        with _urlrequest.urlopen(req, timeout=settings.TENANTIQ_LLM_TIMEOUT_SECONDS) as resp:
            body = json.loads(resp.read())
        return _coerce_result(json.loads(body["message"]["content"]))


def build_fake_llm() -> LLMClient:
    return FakeLLM()


def build_default_llm() -> LLMClient:
    """Anthropic when an API key is configured, else the local Ollama fallback (ADR-0001/ADR-0008)."""
    if settings.ANTHROPIC_API_KEY:
        return AnthropicLLM()
    return OllamaLLM()


def get_llm() -> LLMClient:
    """Build the configured LLM client (``TENANTIQ_LLM_FACTORY``), read fresh so tests can override."""
    factory = settings.TENANTIQ_LLM_FACTORY
    if isinstance(factory, str):
        factory = import_string(factory)
    return factory()
