"""TDD for grounded answer generation with an enforced citation schema (#15).

Generation turns an ``AssembledContext`` (#14) into a ``GroundedAnswer``: it calls the LLM with the
grounded prompt, enforces a structured ``{answer, citations}`` shape, and resolves each cited source
number back to the real chunk it stands for. The load-bearing guarantees the tests pin down:

- **No context → refuse without calling the LLM.** A question with no retrieved sources returns a
  refusal and spends no tokens.
- **Every returned citation resolves to a real source chunk** — and a number the model invents that
  doesn't match a source is dropped, never surfaced as a citation.

These run fully hermetically: the ``AssembledContext`` is built in-process and a fake LLM is injected,
so no database, network, or API key is touched. The Anthropic/Ollama adapters are the un-hermetic
edge and are exercised only through the pure result-coercion helper.
"""

from __future__ import annotations

from app.generation import (
    Citation,
    GroundedAnswer,
    LLMResult,
    _coerce_result,
    generate_answer,
)
from app.rag import AssembledContext, Source, build_grounded_prompt


def _source(number: int, *, chunk_id: int, title: str = "Doc", text: str = "some text") -> Source:
    return Source(
        number=number,
        chunk_id=chunk_id,
        document_id=chunk_id // 10,
        document_title=title,
        chunk_index=number - 1,
        start_offset=0,
        end_offset=len(text),
        similarity=0.9,
        text=text,
    )


def _context(
    sources: tuple[Source, ...], question: str = "what are the terms?"
) -> AssembledContext:
    system, user = build_grounded_prompt(question, sources)
    return AssembledContext(
        question=question, sources=sources, system_prompt=system, user_prompt=user
    )


class _FakeLLM:
    """A deterministic LLM stand-in. Returns a canned result and records what it was asked."""

    def __init__(self, result: LLMResult) -> None:
        self.result = result
        self.calls: list[tuple[str, str]] = []

    def generate(self, system_prompt: str, user_prompt: str) -> LLMResult:
        self.calls.append((system_prompt, user_prompt))
        return self.result


class _ExplodingLLM:
    """Fails if generate() is ever called — proves the no-context path never reaches the model."""

    def generate(self, system_prompt: str, user_prompt: str) -> LLMResult:  # noqa: ARG002
        raise AssertionError("the LLM must not be called when there is no context")


# --- refusal path ---------------------------------------------------------------------------------


def test_refuses_without_calling_the_llm_when_there_is_no_context():
    ctx = _context(sources=())  # nothing retrieved
    answer = generate_answer(ctx, llm=_ExplodingLLM())

    assert isinstance(answer, GroundedAnswer)
    assert answer.refused is True
    assert answer.citations == ()
    assert answer.text  # a non-empty "I don't have that" message, not a fabricated answer


# --- grounded answer + citation resolution --------------------------------------------------------


def test_answer_text_and_prompts_flow_through_to_the_model():
    sources = (_source(1, chunk_id=10), _source(2, chunk_id=20))
    ctx = _context(sources)
    llm = _FakeLLM(LLMResult(answer="Net thirty days [1].", citations=(1,)))

    answer = generate_answer(ctx, llm=llm)

    assert answer.text == "Net thirty days [1]."
    assert answer.refused is False
    # The model was called once, with exactly the assembled grounded prompt.
    assert llm.calls == [(ctx.system_prompt, ctx.user_prompt)]


def test_citations_resolve_to_the_real_source_chunks():
    sources = (_source(1, chunk_id=11), _source(2, chunk_id=22))
    ctx = _context(sources)
    llm = _FakeLLM(LLMResult(answer="A [1] and B [2].", citations=(1, 2)))

    answer = generate_answer(ctx, llm=llm)

    assert [c.chunk_id for c in answer.citations] == [11, 22]
    assert all(isinstance(c, Citation) for c in answer.citations)
    # Each citation carries the stable anchor (document + offsets), not just the chunk PK.
    first = answer.citations[0]
    assert (first.number, first.document_id, first.start_offset, first.end_offset) == (
        1,
        sources[0].document_id,
        sources[0].start_offset,
        sources[0].end_offset,
    )


def test_hallucinated_citation_numbers_are_dropped():
    sources = (_source(1, chunk_id=11), _source(2, chunk_id=22))
    ctx = _context(sources)
    # The model cites [1] (real), [5] and [0] (out of range) — only [1] must survive.
    llm = _FakeLLM(LLMResult(answer="See [1], [5], [0].", citations=(1, 5, 0)))

    answer = generate_answer(ctx, llm=llm)

    assert [c.number for c in answer.citations] == [1]
    assert [c.chunk_id for c in answer.citations] == [11]


def test_every_citation_resolves_to_a_source_in_context():
    # The acceptance criterion: no citation ever points outside the retrieved sources.
    sources = tuple(_source(n, chunk_id=n * 10) for n in range(1, 6))
    ctx = _context(sources)
    llm = _FakeLLM(LLMResult(answer="...", citations=(2, 4, 99)))

    answer = generate_answer(ctx, llm=llm)

    valid_chunk_ids = {s.chunk_id for s in sources}
    assert answer.citations  # something resolved
    assert all(c.chunk_id in valid_chunk_ids for c in answer.citations)


def test_duplicate_citations_are_deduplicated_keeping_order():
    sources = (_source(1, chunk_id=11), _source(2, chunk_id=22))
    ctx = _context(sources)
    llm = _FakeLLM(LLMResult(answer="[2] then [1] then [2] again.", citations=(2, 1, 2)))

    answer = generate_answer(ctx, llm=llm)

    assert [c.number for c in answer.citations] == [2, 1]  # first-seen order, no repeats


# --- untrusted-output coercion (the un-hermetic backends' shared parse step) -----------------------


def test_coerce_result_accepts_a_well_formed_payload():
    result = _coerce_result({"answer": "hello", "citations": [1, 3]})
    assert result == LLMResult(answer="hello", citations=(1, 3))


def test_coerce_result_tolerates_missing_or_malformed_fields():
    assert _coerce_result({}) == LLMResult(answer="", citations=())
    assert _coerce_result({"answer": 42}) == LLMResult(answer="", citations=())  # non-str answer
    assert _coerce_result({"answer": "x", "citations": None}) == LLMResult(answer="x", citations=())


def test_coerce_result_drops_non_integer_and_boolean_citation_entries():
    # A model might emit strings, floats, or bools; only genuine ints are citation numbers.
    result = _coerce_result({"answer": "x", "citations": [1, "2", 3.0, True, 4]})
    assert result.citations == (1, 4)


def test_coerce_result_never_raises_on_a_non_list_citations_field():
    # A model may emit a single citation unwrapped (`"citations": 3`) or malform it entirely; the
    # shared parse step must degrade to an uncited answer, never raise (its documented contract).
    assert _coerce_result({"answer": "x", "citations": 3}) == LLMResult(answer="x", citations=())
    assert _coerce_result({"answer": "x", "citations": 2.5}) == LLMResult(answer="x", citations=())
    assert _coerce_result({"answer": "x", "citations": "nope"}) == LLMResult(
        answer="x", citations=()
    )


def test_coerce_result_never_raises_on_a_non_object_payload():
    # json.loads can return a list / scalar / None if the model ignores the object schema.
    assert _coerce_result(["not", "an", "object"]) == LLMResult(answer="", citations=())
    assert _coerce_result(None) == LLMResult(answer="", citations=())


def test_answer_with_context_but_no_citations_is_not_a_refusal():
    # The model answered from context but cited nothing — that's a valid (if weak) answer,
    # distinct from the no-context refusal path.
    sources = (_source(1, chunk_id=11),)
    ctx = _context(sources)
    llm = _FakeLLM(LLMResult(answer="The document does not specify a due date.", citations=()))

    answer = generate_answer(ctx, llm=llm)

    assert answer.refused is False
    assert answer.citations == ()
    assert "does not specify" in answer.text
