"""The LLM judge: does the cited source actually support the claim? (#22)

Pluggable exactly like the embedder (ADR-0004) and the answer LLM (#15): a deterministic fake under
pytest so the suite never touches a network, an Anthropic adapter, and an Ollama fallback so
``make eval`` works with no API key. Selected by ``TENANTIQ_EVAL_JUDGE_FACTORY``.

**The judge is given one narrow job.** It does not decide what the claims are (``app.eval.claims``
does that, mechanically), it does not see the question, and it is not asked whether the answer is
*good*. It is asked, per claim: is this sentence supported by the text of the sources it cites? Every
broader question invites the judge to reward fluency, and a fluency score dressed as a faithfulness
score is worse than no score.

**A judge is not an oracle**, and the harness treats it as a measurement instrument with error rather
than as ground truth:

- it never sees a source the claim did not cite, so it cannot quietly credit a claim to evidence the
  answer never pointed at;
- ``verdict`` is forced to one of three values and anything unparseable becomes ``unclear``, which is
  reported separately rather than folded into either the supported or the unsupported count;
- when the judge and the generator are the same model — the default in local development, where both
  are Ollama — the report says so, because self-assessment is a known-optimistic measurement.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol, runtime_checkable
from urllib import request as _urlrequest

from django.conf import settings
from django.utils.module_loading import import_string


@dataclass(frozen=True)
class JudgedSource:
    """One source as the judge sees it: its citation number and its verbatim text."""

    number: int
    text: str


@dataclass(frozen=True)
class Verdict:
    """The judge's ruling on one claim.

    ``supported`` is a tri-state rather than a boolean: a judge that cannot tell is a real outcome,
    and collapsing it either way invents precision the measurement does not have.
    """

    index: int
    supported: bool | None
    reason: str

    @property
    def is_supported(self) -> bool:
        return self.supported is True

    @property
    def is_unsupported(self) -> bool:
        return self.supported is False

    @property
    def is_unclear(self) -> bool:
        return self.supported is None


@runtime_checkable
class Judge(Protocol):
    """Rules on whether each claim is supported by the sources it cites."""

    model: str

    def judge(
        self, claims: Sequence[tuple[int, str, tuple[int, ...]]], sources: Sequence[JudgedSource]
    ) -> tuple[Verdict, ...]: ...


_SYSTEM_PROMPT = (
    "You are a strict grounding auditor. You are given numbered SOURCES extracted from a customer's "
    "own documents, and a list of CLAIMS taken from an answer that was generated from them. Each "
    "claim names the source numbers it cites.\n\n"
    "For each claim, decide whether the text of the sources it cites supports it.\n\n"
    "Rules:\n"
    "- Judge support only against the sources that claim cites. Ignore your own knowledge entirely.\n"
    "- 'supported' means the cited source states the claim, or the claim follows from it directly "
    "with no added facts. Paraphrase is fine; added specifics are not.\n"
    "- 'unsupported' means the cited source does not state it, contradicts it, or the claim adds a "
    "figure, date, name, or condition that is not in the cited source.\n"
    "- A number in the claim must appear in a cited source. A figure that is close but not equal is "
    "'unsupported', not 'supported'.\n"
    "- 'unclear' is for when the claim is too vague to check, not for when you are undecided about "
    "evidence you can see.\n"
    "- Judge every claim you are given, and return exactly one entry per claim index.\n"
    "- Do not reward good writing. Fluency is not evidence."
)

_VERDICT_SCHEMA = {
    "type": "object",
    "properties": {
        "verdicts": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "index": {"type": "integer"},
                    "verdict": {"type": "string", "enum": ["supported", "unsupported", "unclear"]},
                    "reason": {"type": "string"},
                },
                "required": ["index", "verdict", "reason"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["verdicts"],
    "additionalProperties": False,
}

_VERDICT_VALUES: dict[str, bool | None] = {
    "supported": True,
    "unsupported": False,
    "unclear": None,
}


def build_prompt(
    claims: Sequence[tuple[int, str, tuple[int, ...]]], sources: Sequence[JudgedSource]
) -> str:
    """The user prompt: the cited sources verbatim, then the claims with what each cites.

    Pure, and tested directly. Two properties matter and neither is visible from the output of a
    judged run: **only cited sources appear** (so the judge cannot credit a claim to evidence the
    answer never pointed at), and the source text is verbatim (#45) rather than summarised.
    """
    cited_numbers = {number for _, _, numbers in claims for number in numbers}
    relevant = [source for source in sources if source.number in cited_numbers]
    blocks = (
        "\n\n".join(f"SOURCE [{s.number}]:\n{s.text}" for s in relevant) or "(no sources cited)"
    )
    lines = "\n".join(
        f"CLAIM {index} (cites {', '.join(f'[{n}]' for n in numbers) or 'nothing'}): {text}"
        for index, text, numbers in claims
    )
    return f"{blocks}\n\n---\n\n{lines}"


def coerce_verdicts(raw: object, expected: Sequence[int]) -> tuple[Verdict, ...]:
    """Turn a backend's payload into one :class:`Verdict` per expected claim index.

    Never raises. A judge that returns malformed JSON, skips a claim, invents an index, or answers
    with a word outside the enum yields ``unclear`` for whatever it failed to rule on — which is
    reported as its own count. The alternative, defaulting a missing verdict to supported or
    unsupported, would let a flaky judge move the headline number in whichever direction its
    failures happened to fall.
    """
    by_index: dict[int, Verdict] = {}
    entries = raw.get("verdicts") if isinstance(raw, dict) else None
    for entry in entries if isinstance(entries, list) else []:
        if not isinstance(entry, dict):
            continue
        index = entry.get("index")
        if not isinstance(index, int) or isinstance(index, bool) or index not in expected:
            continue
        verdict = entry.get("verdict")
        reason = entry.get("reason")
        by_index[index] = Verdict(
            index=index,
            supported=_VERDICT_VALUES.get(verdict) if isinstance(verdict, str) else None,
            reason=reason if isinstance(reason, str) else "",
        )
    return tuple(
        by_index.get(
            index, Verdict(index=index, supported=None, reason="judge returned no verdict")
        )
        for index in expected
    )


class FakeJudge:
    """Deterministic judge for the hermetic suite: no network, no key, no variance.

    Supports a claim when every word of four or more characters in it also appears in one of the
    sources it cites. That is lexical containment, not comprehension — it is not a stand-in for a real
    judge and its scores are never a result. What it is for is exercising the whole pipeline
    deterministically, so the scoring, aggregation and reporting code can be tested without a model.
    """

    model = "fake-judge-v1"

    def judge(
        self, claims: Sequence[tuple[int, str, tuple[int, ...]]], sources: Sequence[JudgedSource]
    ) -> tuple[Verdict, ...]:
        by_number = {source.number: source.text.lower() for source in sources}
        verdicts: list[Verdict] = []
        for index, text, numbers in claims:
            haystack = " ".join(by_number.get(number, "") for number in numbers)
            words = [word for word in _words(text) if len(word) >= 4]
            supported = bool(words) and all(word in haystack for word in words)
            verdicts.append(
                Verdict(
                    index=index,
                    supported=supported,
                    reason=(
                        "lexical containment" if supported else "words absent from cited sources"
                    ),
                )
            )
        return tuple(verdicts)


def _words(text: str) -> list[str]:
    import re

    return re.findall(r"[a-z0-9]+", text.lower())


class AnthropicJudge:
    """Anthropic Messages API with schema-enforced structured output (mirrors ``AnthropicLLM``)."""

    def __init__(self, *, model: str | None = None, max_tokens: int = 2048) -> None:
        self.model = model or settings.TENANTIQ_EVAL_JUDGE_MODEL
        self.max_tokens = max_tokens

    def judge(
        self, claims: Sequence[tuple[int, str, tuple[int, ...]]], sources: Sequence[JudgedSource]
    ) -> tuple[Verdict, ...]:
        import anthropic

        client = anthropic.Anthropic()
        response = client.messages.create(
            model=self.model,
            max_tokens=self.max_tokens,
            system=_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": build_prompt(claims, sources)}],
            output_config={"format": {"type": "json_schema", "schema": _VERDICT_SCHEMA}},
        )
        text = next((b.text for b in response.content if b.type == "text"), "{}")
        return coerce_verdicts(json.loads(text), [index for index, _, _ in claims])


class OllamaJudge:
    """Local fallback so ``make eval`` produces a faithfulness number with no API key."""

    def __init__(self, *, model: str | None = None, base_url: str | None = None) -> None:
        self.model = model or settings.TENANTIQ_EVAL_JUDGE_OLLAMA_MODEL
        self.base_url = (base_url or settings.OLLAMA_BASE_URL).rstrip("/")

    def judge(
        self, claims: Sequence[tuple[int, str, tuple[int, ...]]], sources: Sequence[JudgedSource]
    ) -> tuple[Verdict, ...]:
        payload = json.dumps(
            {
                "model": self.model,
                "stream": False,
                "format": _VERDICT_SCHEMA,
                "options": {
                    # Temperature 0: the same answer and the same sources should produce the same
                    # verdict twice. It does not make an LLM judge deterministic, but it removes the
                    # variance that is ours to remove.
                    "temperature": 0,
                    # Ollama defaults llama3.1 to a 4096-token context, and this prompt carries up
                    # to `TENANTIQ_RETRIEVAL_TOP_K` verbatim chunks of `TENANTIQ_CHUNK_TARGET_TOKENS`
                    # each — 4000 tokens of sources before the system prompt and the claims. Left at
                    # the default, the judge returns HTTP 500 on exactly the questions that
                    # retrieved the most evidence, which would bias the measurement towards the
                    # answers that had least to be judged.
                    "num_ctx": settings.TENANTIQ_EVAL_JUDGE_NUM_CTX,
                },
                "messages": [
                    {"role": "system", "content": _SYSTEM_PROMPT},
                    {"role": "user", "content": build_prompt(claims, sources)},
                ],
            }
        ).encode()
        request = _urlrequest.Request(
            f"{self.base_url}/api/chat",
            data=payload,
            headers={"Content-Type": "application/json"},
        )
        with _urlrequest.urlopen(request, timeout=settings.TENANTIQ_EVAL_JUDGE_TIMEOUT) as response:
            body = json.loads(response.read())
        return coerce_verdicts(
            json.loads(body["message"]["content"]), [index for index, _, _ in claims]
        )


def build_fake_judge() -> Judge:
    return FakeJudge()


def build_default_judge() -> Judge:
    """Anthropic when a key is configured, else the local Ollama fallback."""
    if settings.ANTHROPIC_API_KEY:
        return AnthropicJudge()
    return OllamaJudge()


def get_judge() -> Judge:
    """Build the configured judge (``TENANTIQ_EVAL_JUDGE_FACTORY``), read fresh so tests override."""
    factory = settings.TENANTIQ_EVAL_JUDGE_FACTORY
    if isinstance(factory, str):
        factory = import_string(factory)
    return factory()
