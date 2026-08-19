"""The local model must be able to read the prompt this product builds (#90).

The bug: Ollama runs llama3.1 with a 4096-token context, while `TOP_K x CHUNK_TARGET_TOKENS` is
4,000 tokens of sources before the system prompt. It overflowed by roughly 7%, so questions that
retrieved five full-size chunks returned HTTP 500 and ones that retrieved a document's short trailing
chunk did not — which read as flakiness rather than as a misconfiguration.

Hermetic: no Ollama, no network. The guard is a pure comparison between a computed prompt size and a
configured window, which is exactly what makes it checkable before a deployment serves anything.
"""

from __future__ import annotations

import pytest

from app.generation import (
    AnthropicLLM,
    ContextWindowExceeded,
    ErrorEvent,
    OllamaLLM,
    TokenEvent,
    stream_grounded_answer,
)
from app.rag import AssembledContext, Source, worst_case_prompt_tokens


def _context(source_text: str = "x " * 50) -> AssembledContext:
    source = Source(
        number=1,
        chunk_id=1,
        document_id=1,
        document_title="doc.txt",
        chunk_index=0,
        start_offset=0,
        end_offset=len(source_text),
        similarity=0.9,
        text=source_text,
    )
    return AssembledContext(
        question="q?",
        sources=(source,),
        system_prompt="system",
        user_prompt=source_text,
    )


class TestTheShippedConfiguration:
    def test_the_worst_case_prompt_fits_the_configured_window(self, settings):
        """The regression test that matters.

        Raising TENANTIQ_RETRIEVAL_TOP_K or TENANTIQ_CHUNK_TARGET_TOKENS, or lowering
        TENANTIQ_LLM_NUM_CTX, now fails here rather than in production on whichever question happens
        to retrieve the most text. This is the whole reason `worst_case_prompt_tokens` is computed
        from configuration instead of measured from traffic.
        """
        needed = worst_case_prompt_tokens() + settings.TENANTIQ_LLM_RESPONSE_HEADROOM_TOKENS

        assert needed <= settings.TENANTIQ_LLM_NUM_CTX, (
            f"the largest prompt these settings can build is ~{needed} tokens, which does not fit "
            f"the {settings.TENANTIQ_LLM_NUM_CTX}-token window the local model is run with"
        )

    def test_the_old_default_really_was_too_small(self, settings):
        # Documents the bug rather than trusting the fix: at Ollama's own 4096 default, the shipped
        # retrieval settings do not fit. If this ever stops being true the issue is stale.
        needed = worst_case_prompt_tokens() + settings.TENANTIQ_LLM_RESPONSE_HEADROOM_TOKENS

        assert needed > 4096

    def test_it_counts_the_sources_the_prompt_can_actually_carry(self, settings):
        settings.TENANTIQ_RETRIEVAL_TOP_K = 5
        settings.TENANTIQ_CHUNK_TARGET_TOKENS = 800

        assert worst_case_prompt_tokens() >= 5 * 800

    def test_it_tracks_the_retrieval_settings(self, settings):
        settings.TENANTIQ_RETRIEVAL_TOP_K = 5
        settings.TENANTIQ_CHUNK_TARGET_TOKENS = 800
        smaller = worst_case_prompt_tokens()
        settings.TENANTIQ_RETRIEVAL_TOP_K = 10

        assert worst_case_prompt_tokens() > smaller


class TestTheGuard:
    def test_a_prompt_that_fits_is_left_alone(self, settings):
        settings.TENANTIQ_LLM_NUM_CTX = 8192

        OllamaLLM()._guard_context("system", "short prompt")  # must not raise

    def test_a_prompt_that_does_not_fit_is_refused_before_the_request(self, settings):
        # Refused *here*, not by the backend: Ollama answers an overflow with an opaque HTTP 500,
        # which is indistinguishable from a crash and gets the retry copy.
        settings.TENANTIQ_LLM_NUM_CTX = 256

        with pytest.raises(ContextWindowExceeded):
            OllamaLLM()._guard_context("system", "word " * 5000)

    def test_the_reserved_answer_room_counts_against_the_window(self, settings):
        # The window covers the prompt *and* the completion. A prompt that exactly fills it leaves no
        # room to answer, which is a failure the naive comparison would miss.
        settings.TENANTIQ_LLM_NUM_CTX = 200
        settings.TENANTIQ_LLM_RESPONSE_HEADROOM_TOKENS = 150
        prompt = "word " * 64  # ~80 tokens: fits alone, does not fit alongside the reservation

        with pytest.raises(ContextWindowExceeded):
            OllamaLLM()._guard_context("", prompt)

    def test_the_failure_names_what_to_change(self, settings):
        # In the log, not to the tenant. A message that says only "too large" leaves the operator
        # guessing which of three settings is at fault.
        settings.TENANTIQ_LLM_NUM_CTX = 128

        with pytest.raises(ContextWindowExceeded, match="TENANTIQ_LLM_NUM_CTX"):
            OllamaLLM()._guard_context("system", "word " * 5000)


class TestWhatTheUserIsTold:
    def test_an_overflow_does_not_tell_the_user_to_retry(self):
        """The honesty fix. The overflow is deterministic for a given question, so "please try
        again" sends the user into a loop that cannot terminate."""

        class _Overflowing:
            model = "too-small"

            def generate(self, system_prompt, user_prompt):  # noqa: ARG002
                raise ContextWindowExceeded("prompt too large")

            def stream(self, system_prompt, user_prompt):  # noqa: ARG002
                raise ContextWindowExceeded("prompt too large")
                yield ""  # pragma: no cover — makes this a generator

        events = list(stream_grounded_answer(_context(), llm=_Overflowing()))

        errors = [e for e in events if isinstance(e, ErrorEvent)]
        assert len(errors) == 1
        assert "try again" not in errors[0].message.lower()
        assert "will not help" in errors[0].message

    def test_the_message_leaks_no_configuration(self):
        class _Overflowing:
            model = "too-small"

            def generate(self, system_prompt, user_prompt):  # noqa: ARG002
                raise ContextWindowExceeded("prompt needs 9000 tokens but num_ctx is 4096")

            def stream(self, system_prompt, user_prompt):  # noqa: ARG002
                raise ContextWindowExceeded("prompt needs 9000 tokens but num_ctx is 4096")
                yield ""  # pragma: no cover

        events = list(stream_grounded_answer(_context(), llm=_Overflowing()))

        message = next(e.message for e in events if isinstance(e, ErrorEvent))
        # #47's rule: the reason a tenant sees is sanitized; the numbers go to the log.
        assert "num_ctx" not in message
        assert "9000" not in message
        assert "4096" not in message

    def test_an_ordinary_failure_still_says_try_again(self):
        # The distinction only means something if the transient case keeps its own copy.
        class _Broken:
            model = "broken"

            def generate(self, system_prompt, user_prompt):  # noqa: ARG002
                raise RuntimeError("connection reset")

            def stream(self, system_prompt, user_prompt):  # noqa: ARG002
                raise RuntimeError("connection reset")
                yield ""  # pragma: no cover

        events = list(stream_grounded_answer(_context(), llm=_Broken()))

        message = next(e.message for e in events if isinstance(e, ErrorEvent))
        assert "try again" in message.lower()

    def test_tokens_already_streamed_are_kept(self):
        # A partial answer is real model output (#19). An overflow mid-stream must not discard it.
        class _DiesAfterOneToken:
            model = "flaky"

            def generate(self, system_prompt, user_prompt):  # noqa: ARG002
                raise ContextWindowExceeded("x")

            def stream(self, system_prompt, user_prompt):  # noqa: ARG002
                yield "Invoices are payable "
                raise ContextWindowExceeded("x")

        events = list(stream_grounded_answer(_context(), llm=_DiesAfterOneToken()))

        assert [e.text for e in events if isinstance(e, TokenEvent)] == ["Invoices are payable "]
        assert any(isinstance(e, ErrorEvent) for e in events)


class TestTheLocalBackendsTimeout:
    """Local inference is categorically slower than a hosted API, and shares no budget with it.

    Sizing the window correctly (#90) only converted the HTTP 500 into a timeout until the local
    backend stopped borrowing `TENANTIQ_LLM_TIMEOUT_SECONDS`, which was chosen for Anthropic. Same
    user-visible outcome, different exception — worth its own setting rather than raising a shared
    one and quietly making a genuinely stuck hosted call wait five minutes.
    """

    def _capture_timeout(self, monkeypatch) -> list[float]:
        seen: list[float] = []

        class _Response:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self):
                return b'{"message": {"content": "{}"}}'

            def __iter__(self):
                return iter([b'{"message": {"content": "hi"}, "done": true}'])

        def _fake_urlopen(request, timeout=None):  # noqa: ARG001
            seen.append(timeout)
            return _Response()

        monkeypatch.setattr("app.generation._urlrequest.urlopen", _fake_urlopen)
        return seen

    def test_generate_uses_the_local_timeout(self, monkeypatch, settings):
        settings.TENANTIQ_LLM_OLLAMA_TIMEOUT_SECONDS = 321
        settings.TENANTIQ_LLM_TIMEOUT_SECONDS = 7
        seen = self._capture_timeout(monkeypatch)

        OllamaLLM().generate("system", "short")

        assert seen == [321]

    def test_stream_uses_the_local_timeout(self, monkeypatch, settings):
        settings.TENANTIQ_LLM_OLLAMA_TIMEOUT_SECONDS = 321
        settings.TENANTIQ_LLM_TIMEOUT_SECONDS = 7
        seen = self._capture_timeout(monkeypatch)

        list(OllamaLLM().stream("system", "short"))

        assert seen == [321]

    def test_the_window_it_asks_for_is_the_window_it_checked_against(self, monkeypatch, settings):
        # Guarding against 8192 and then asking Ollama for its 4096 default would reintroduce the
        # bug with the guard still passing — the two numbers have to be the same one.
        settings.TENANTIQ_LLM_NUM_CTX = 8192
        sent: list[dict] = []

        class _Response:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self):
                return b'{"message": {"content": "{}"}}'

        def _fake_urlopen(request, timeout=None):  # noqa: ARG001
            import json

            sent.append(json.loads(request.data))
            return _Response()

        monkeypatch.setattr("app.generation._urlrequest.urlopen", _fake_urlopen)

        OllamaLLM().generate("system", "short")

        assert sent[0]["options"]["num_ctx"] == 8192

    def test_the_hosted_backend_still_honours_the_setting_named_after_it(self, settings):
        """`TENANTIQ_LLM_TIMEOUT_SECONDS` must configure something.

        Before #90 the only backend that read it was Ollama, despite the generic name. Giving the
        local path its own value would have left this one inert — a setting an operator can set that
        does nothing, which is the same class of trap as a documented-but-unforwarded compose
        variable. It now configures the hosted client, which is what its name always implied.
        """
        settings.TENANTIQ_LLM_TIMEOUT_SECONDS = 42

        assert AnthropicLLM().timeout == 42

    def test_the_two_timeouts_are_independent(self, settings):
        settings.TENANTIQ_LLM_TIMEOUT_SECONDS = 42
        settings.TENANTIQ_LLM_OLLAMA_TIMEOUT_SECONDS = 300

        assert AnthropicLLM().timeout == 42
        assert OllamaLLM().num_ctx  # local backend unaffected by the hosted timeout
