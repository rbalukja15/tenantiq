"""Content guardrails: PII redaction on ingest + prompt-injection hardening of retrieved text (#16).

Two attack surfaces, two controls (see docs/threat-model.md and ADR-0010):

- **PII (privacy / data minimization).** :func:`redact_pii` runs on the *extracted text at ingest,
  before chunking*, so recognizable personal data (email, phone, US SSN, Luhn-valid payment card)
  never lands in a stored chunk, the vector index, or a generated answer. Running before chunking is
  what keeps offsets faithful (#45): chunks slice the *redacted* extracted text, so ``chunk.text`` is
  still an exact ``source[start:end]`` slice. Payment cards are Luhn-checked so random long digit
  runs (order IDs, references) are not mistaken for cards.

- **Prompt injection (integrity).** Retrieved chunk text is untrusted: a tenant's own document can
  carry text engineered to override the system prompt ("ignore previous instructions…"). Defense is
  *structural*, not a phrase blocklist — blocklists are brittle and bypassable. :func:`fence_source`
  wraps each source in a doubled-bracket fence the content **cannot forge** (:func:`neutralize_
  untrusted_text` breaks any bracket run and defuses chat-role/control tokens), so the system prompt
  can treat everything between the markers as inert data. Neutralization is applied only to the copy
  rendered into the prompt; the stored chunk and the citation text stay verbatim (#45).
"""

from __future__ import annotations

import re

# --- PII redaction --------------------------------------------------------------------------------

# Separators tolerate whitespace runs, not just single spaces/hyphens: PDF extraction joins pages
# with "\n" and lays out tables with runs of spaces, so PII regularly straddles a newline or a
# multi-space gap (review #16). Runs are bounded (never unlimited/newline-greedy) so a match can't
# span unrelated numbers across paragraphs.
_EMAIL = re.compile(r"\b[A-Za-z0-9._%+-]+\s*@\s*[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
# US SSN: NNN-NN-NNNN. Pattern-only (context-free); a real separator (hyphen or whitespace) is
# required between groups so a bare 9-digit run isn't swept up.
_SSN = re.compile(r"(?<!\d)\d{3}[\s-]{1,3}\d{2}[\s-]{1,3}\d{4}(?!\d)")
# A candidate payment card: 13–19 digits, split by up to two whitespace/hyphen chars, not embedded in
# a longer digit run. Confirmed by a Luhn check before redaction to keep precision high.
_CARD_CANDIDATE = re.compile(r"(?<!\d)\d(?:[\s-]{0,2}\d){12,18}(?!\d)")
# A North-American-style phone number: optional +1, a 3-digit area code (bare or parenthesized), then
# 3 + 4 digits, separated by short runs of whitespace/dots/hyphens. Conservative on group sizes so
# years, prices and ISO dates are not swept up.
_PHONE = re.compile(
    r"(?<!\d)(?:\+?1[\s.-]{0,2})?(?:\(\d{3}\)|\d{3})[\s.-]{0,3}\d{3}[\s.-]{0,3}\d{4}(?!\d)"
)


def _luhn_ok(digits: str) -> bool:
    """True if ``digits`` (a bare digit string) satisfies the Luhn checksum used by payment cards."""
    if not (13 <= len(digits) <= 19) or not digits.isdigit():
        return False
    total = 0
    for i, ch in enumerate(reversed(digits)):
        d = int(ch)
        if i % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


def _redact_cards(text: str) -> str:
    def repl(match: re.Match[str]) -> str:
        digits = re.sub(r"[\s-]", "", match.group())
        return "[REDACTED_CARD]" if _luhn_ok(digits) else match.group()

    return _CARD_CANDIDATE.sub(repl, text)


def redact_pii(text: str) -> str:
    """Replace recognizable PII in ``text`` with typed placeholders (``[REDACTED_EMAIL]`` …).

    Order matters: email and SSN are unambiguous and go first; cards (Luhn-verified) before phones so
    a 16-digit card is not mis-split into a phone. Idempotent — placeholders contain no PII patterns,
    so re-running is a no-op — which makes ingest-time redaction and a re-ingestion backfill safe to
    compose.
    """
    if not text:
        return text
    text = _EMAIL.sub("[REDACTED_EMAIL]", text)
    text = _SSN.sub("[REDACTED_SSN]", text)
    text = _redact_cards(text)
    text = _PHONE.sub("[REDACTED_PHONE]", text)
    return text


# --- prompt-injection hardening -------------------------------------------------------------------

# Chat-role / control tokens that try to forge a system or assistant turn inside document text.
# Defused by inserting spaces so the exact token no longer appears; the words stay readable as data.
_CONTROL_TOKENS: tuple[tuple[str, str], ...] = (
    ("<<SYS>>", "<< SYS >>"),
    ("<</SYS>>", "<< /SYS >>"),
    ("[INST]", "[ INST ]"),
    ("[/INST]", "[ /INST ]"),
    ("<|", "<| "),
    ("|>", " |>"),
)


def neutralize_untrusted_text(text: str) -> str:
    """Make ``text`` safe to drop inside a source fence: it cannot forge the fence or a role turn.

    Breaks any run of two or more ``[`` or ``]`` (the fence uses doubled brackets, so this guarantees
    the only fence markers in the assembled prompt are the ones we place) and spaces out known
    chat-role/control tokens. Ordinary prose has neither, so this is a no-op on real content.
    """
    text = re.sub(r"(?<=\[)(?=\[)", " ", text)  # split any [[... so content can't open a fence
    text = re.sub(r"(?<=\])(?=\])", " ", text)  # split any ...]] so content can't close a fence
    for token, defanged in _CONTROL_TOKENS:
        text = text.replace(token, defanged)
    return text


def fence_source(number: int, title: str, text: str) -> str:
    """Render one retrieved source as a fenced, untrusted block the model must treat as data.

    The doubled-bracket markers carry the citation ``[number]`` the answer refers to; the title and
    body are neutralized so nothing in the (untrusted) document can forge a marker or role token. The
    title is tenant-controlled too, so its brackets are replaced outright (not just broken) — a title
    ending in ``]`` (``[DRAFT]``) must not abut the marker's own ``]]`` and blur the fence boundary.
    """
    safe_title = neutralize_untrusted_text(title).replace("[", "(").replace("]", ")")
    body = neutralize_untrusted_text(text)
    open_marker = f"[[UNTRUSTED SOURCE [{number}]: {safe_title}]]"
    close_marker = f"[[END SOURCE [{number}]]]"
    return f"{open_marker}\n{body}\n{close_marker}"
