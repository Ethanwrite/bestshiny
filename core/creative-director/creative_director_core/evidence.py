"""Server-side proof that a claimed user statement really is one.

The DIRECTOR model reports a ``confidence`` on every brief operation, and
``USER_STATED`` is the one value that lets it replace or remove a fact the user
established. That word is a *suggestion*: the model writes it, so the model
could write it about anything. This module is the check that turns the
suggestion into a finding - the operation's ``evidence`` must be found in the
user's own words, in the message being answered or in an explicit earlier user
turn, and the verdict (which turn, which span) is recorded either way.

Matching folds case, collapses whitespace and treats punctuation as a space, so
a quote survives the ordinary differences between what a person typed and what
a model echoed back. It does *not* fold anything semantic: a paraphrase is not
a quote, and an unprovable claim is recorded as MODEL_INFERRED rather than
refused, because a director that misquotes is still allowed to have an opinion.
"""

from __future__ import annotations

import unicodedata
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

#: Why an operation's USER_STATED claim was or was not honoured.
QUOTED_BY_USER = "QUOTED_BY_USER"
NO_EVIDENCE = "NO_EVIDENCE"
EVIDENCE_NOT_IN_USER_TEXT = "EVIDENCE_NOT_IN_USER_TEXT"
EVIDENCE_TURN_NOT_FOUND = "EVIDENCE_TURN_NOT_FOUND"
NOT_VERIFIED = "NOT_VERIFIED"

#: Unicode general categories that read as punctuation or symbols in both the
#: Latin and CJK halves of this product's audience.
_PUNCTUATION_CATEGORIES = frozenset({"Pc", "Pd", "Pe", "Pf", "Pi", "Po", "Ps", "Sm", "Sk", "So"})


def _fold(text: str) -> tuple[str, list[int]]:
    """Normalized text plus, per normalized character, its original offset.

    Case is folded, every run of whitespace or punctuation becomes one space,
    and leading/trailing separators are dropped. The offset list is what lets a
    verified quote report the span in the text the user actually typed.
    """

    folded: list[str] = []
    offsets: list[int] = []
    pending_separator = False
    for index, character in enumerate(text):
        if character.isspace() or unicodedata.category(character) in _PUNCTUATION_CATEGORIES:
            if folded:
                pending_separator = True
            continue
        if pending_separator:
            folded.append(" ")
            offsets.append(index)
            pending_separator = False
        for piece in character.casefold():
            folded.append(piece)
            offsets.append(index)
    return "".join(folded), offsets


def normalize(text: str) -> str:
    """The comparison form of a quote: folded case, one space per separator run."""

    return _fold(text)[0]


@dataclass(frozen=True)
class UserUtterance:
    """One thing the user actually said, and where it is on record."""

    turn_id: str | None
    turn_sequence: int | None
    text: str


@dataclass(frozen=True)
class EvidenceVerdict:
    """Whether a quote was found in the user's words, and exactly where."""

    verified: bool
    reason: str
    turn_id: str | None = None
    turn_sequence: int | None = None
    #: [start, end) in the ORIGINAL text of that turn, so the span can be shown.
    span: tuple[int, int] | None = None
    quote: str = ""

    def as_json(self) -> dict[str, object]:
        return {
            "verified": self.verified,
            "reason": self.reason,
            "turn_id": self.turn_id,
            "turn_sequence": self.turn_sequence,
            "span": list(self.span) if self.span is not None else None,
            "quote": self.quote,
        }


class UserTextIndex:
    """The user's own words for one session, ready to be quoted against."""

    def __init__(self, utterances: Iterable[UserUtterance]):
        self._utterances: tuple[UserUtterance, ...] = tuple(utterances)
        self._folded: tuple[tuple[str, list[int]], ...] = tuple(
            _fold(item.text) for item in self._utterances
        )

    def __bool__(self) -> bool:
        return bool(self._utterances)

    @property
    def utterances(self) -> Sequence[UserUtterance]:
        return self._utterances

    def verify(self, evidence: str, *, turn_id: str | None = None) -> EvidenceVerdict:
        """Find ``evidence`` in the user's words; say which turn and which span.

        ``turn_id`` narrows the search to the turn the model named. Naming a
        turn that does not exist is itself a failed proof: the model is
        asserting a source that is not on record.
        """

        needle = normalize(evidence)
        if not needle:
            return EvidenceVerdict(False, NO_EVIDENCE)
        candidates = list(zip(self._utterances, self._folded, strict=True))
        if turn_id is not None:
            candidates = [pair for pair in candidates if pair[0].turn_id == turn_id]
            if not candidates:
                return EvidenceVerdict(False, EVIDENCE_TURN_NOT_FOUND, turn_id=turn_id)
        # Newest first: a quote the user has just repeated is attributed to the
        # message being answered rather than to its oldest occurrence.
        for utterance, (haystack, offsets) in reversed(candidates):
            position = haystack.find(needle)
            if position < 0:
                continue
            start = offsets[position]
            end = offsets[position + len(needle) - 1] + 1
            return EvidenceVerdict(
                True,
                QUOTED_BY_USER,
                turn_id=utterance.turn_id,
                turn_sequence=utterance.turn_sequence,
                span=(start, end),
                quote=utterance.text[start:end],
            )
        return EvidenceVerdict(False, EVIDENCE_NOT_IN_USER_TEXT, turn_id=turn_id)


__all__ = [
    "EVIDENCE_NOT_IN_USER_TEXT",
    "EVIDENCE_TURN_NOT_FOUND",
    "NOT_VERIFIED",
    "NO_EVIDENCE",
    "QUOTED_BY_USER",
    "EvidenceVerdict",
    "UserTextIndex",
    "UserUtterance",
    "normalize",
]
