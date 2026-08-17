"""Breaking an answer into checkable claims (#22).

Pure, deterministic, and separate from the judge on purpose. **The judge is asked to rule on claims
it did not choose.** If the model that scores an answer also decides what the units of that answer
are, the denominator moves between runs and a score can drift without anything about the answer
changing — the most common way an LLM-as-judge harness produces numbers nobody can compare.

So the split is mechanical: sentences, with their citation markers attached. The judge's only job is
the part that genuinely needs reading comprehension — whether a cited source supports the sentence
that cites it.

Three properties of a claim are decided here, without a model:

- **Is it cited?** The grounding contract (ADR-0007) requires every claim to carry the source
  number(s) it rests on, so an uncited factual sentence is a violation on its own — separately from
  whether anything supports it.
- **Does it state a number?** ``CLAUDE.md``'s hardest rule is that the LLM never computes numbers.
  An uncited sentence containing a figure is the highest-value thing this whole harness can flag.
- **Does it cite a source that does not exist?** ``generation._resolve_citations`` drops an invented
  number from the resolved citation list, but the *prose keeps the marker* — the answer still reads
  "as set out in [7]" when there was no source 7. That is exactly the failure the project claims is
  impossible, and it is invisible unless something looks at the text.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

#: Sentence boundary: a terminator followed by whitespace. Deliberately naive — an abbreviation like
#: "e.g. " splits early. The consequence is a slightly finer-grained claim list, not a wrong verdict,
#: and every alternative worth having is a sentence-segmentation dependency this project does not
#: need (ADR-0003's lean-deps grain).
_SENTENCE_BOUNDARY = re.compile(r"(?<=[.!?])\s+")

_CITATION_MARKER = re.compile(r"\[(\d+)\]")

#: A digit that is not part of a citation marker (markers are stripped before this runs).
_DIGIT = re.compile(r"\d")

#: Below this length a fragment is punctuation or a stray heading, not a claim worth judging.
_MIN_CLAIM_CHARS = 12


@dataclass(frozen=True)
class Claim:
    """One sentence of an answer, with what it cites and what it asserts."""

    index: int
    text: str
    #: Source numbers the sentence cites, in order of appearance, deduplicated.
    cited: tuple[int, ...]
    #: Cited numbers that do not correspond to any retrieved source — an invented citation.
    invented: tuple[int, ...]
    #: The sentence states a figure. Read together with ``is_cited``: an uncited number is the
    #: single most serious thing an answer can contain in this product.
    has_number: bool

    @property
    def is_cited(self) -> bool:
        return bool(self.cited)

    @property
    def cites_only_real_sources(self) -> bool:
        return not self.invented


def split_claims(answer: str, available_sources: int) -> tuple[Claim, ...]:
    """Split ``answer`` into claims, resolving each one's citation markers.

    ``available_sources`` is how many sources the prompt actually offered (they are numbered 1..n),
    which is what makes an invented marker detectable.
    """
    claims: list[Claim] = []
    for fragment in _SENTENCE_BOUNDARY.split(answer.strip()):
        text = fragment.strip()
        if len(text) < _MIN_CLAIM_CHARS:
            continue
        numbers = _cited_numbers(text)
        # Markers are stripped before looking for digits: `[1]` is a citation, not a claim about the
        # number one, and counting it would make every cited sentence look like a numeric claim.
        without_markers = _CITATION_MARKER.sub(" ", text)
        claims.append(
            Claim(
                index=len(claims),
                text=text,
                cited=numbers,
                invented=tuple(n for n in numbers if n < 1 or n > available_sources),
                has_number=bool(_DIGIT.search(without_markers)),
            )
        )
    return tuple(claims)


def _cited_numbers(text: str) -> tuple[int, ...]:
    seen: list[int] = []
    for match in _CITATION_MARKER.findall(text):
        number = int(match)
        if number not in seen:
            seen.append(number)
    return tuple(seen)
