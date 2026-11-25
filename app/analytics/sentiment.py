from __future__ import annotations

import re

_FRUSTRATED = (
    "frustrated", "annoyed", "angry", "ridiculous", "waste of time", "fed up",
    "unacceptable", "useless", "furious", "again and again", "nobody is helping",
)
_POSITIVE = (
    "great", "excellent", "love", "happy", "satisfied", "thank you", "thanks",
    "appreciate", "helpful", "wonderful", "perfect", "pleased", "good",
)
_NEGATIVE = (
    "bad", "poor", "unhappy", "disappointed", "upset", "problem", "issue",
    "complaint", "wrong", "terrible", "worst", "delay", "confusing", "difficult",
)
_NEGATIONS = ("not", "never", "no", "isn't", "wasn't", "don't", "didn't", "can't")


def _contains(text: str, phrase: str) -> bool:
    if " " in phrase or "'" in phrase:
        return phrase in text
    return re.search(rf"\b{re.escape(phrase)}\b", text) is not None


def classify_sentiment(text: str) -> tuple[str, float]:
    """Return a conservative, local sentiment estimate and heuristic confidence."""
    normalized = re.sub(r"\s+", " ", text.casefold()).strip()
    if not normalized:
        return "neutral", 0.5

    if any(_contains(normalized, phrase) for phrase in _FRUSTRATED):
        return "frustrated", 0.8

    positive = sum(_contains(normalized, phrase) for phrase in _POSITIVE)
    negative = sum(_contains(normalized, phrase) for phrase in _NEGATIVE)
    if any(_contains(normalized, phrase) for phrase in _NEGATIONS):
        positive, negative = negative, positive
    if positive > negative:
        return "positive", min(0.9, 0.65 + 0.1 * (positive - negative - 1))
    if negative > positive:
        return "negative", min(0.9, 0.65 + 0.1 * (negative - positive - 1))
    if positive and negative:
        return "neutral", 0.55
    return "neutral", 0.6
