from __future__ import annotations

import re

_FRUSTRATED = (
    # Direct frustration and anger
    "frustrated",
    "frustrating",
    "annoyed",
    "annoying",
    "angry",
    "furious",
    "mad",
    "irritated",
    "irritating",
    "fed up",
    "sick of this",
    "had enough",
    "lost my patience",
    # Strong dissatisfaction
    "ridiculous",
    "unacceptable",
    "pathetic",
    "useless",
    "waste of time",
    "complete waste",
    "terrible experience",
    "worst experience",
    "disgusted",
    "outraged",
    # Repeated problems
    "again and again",
    "over and over",
    "same problem",
    "same issue",
    "still not resolved",
    "happening again",
    "every single time",
    "keep having this problem",
    # Lack of support
    "nobody is helping",
    "no one is helping",
    "nobody cares",
    "no one cares",
    "not getting any help",
    "not helping at all",
    "where is the support",
    "no response from your team",
    "ignored my request",
    # Waiting and delays
    "still waiting",
    "waiting forever",
    "taking too long",
    "been waiting for hours",
    "no update",
    "how long will this take",
    "why the delay",
    "it's taking ages",
    "delayed again",
    # Escalation and threats
    "want to speak to a manager",
    "escalate this",
    "file a complaint",
    "report this",
    "cancel my account",
    "never using this again",
    "taking my business elsewhere",
)

_POSITIVE = (
    # General appreciation
    "great",
    "excellent",
    "wonderful",
    "amazing",
    "awesome",
    "brilliant",
    "fantastic",
    "superb",
    "outstanding",
    "love it",
    "love this",
    "really appreciate",
    # Satisfaction
    "happy",
    "satisfied",
    "pleased",
    "delighted",
    "very happy",
    "quite happy",
    "fully satisfied",
    "completely satisfied",
    "works perfectly",
    "exactly what I wanted",
    "just what I needed",
    # Gratitude
    "thank you",
    "thanks",
    "many thanks",
    "thanks a lot",
    "thank you so much",
    "really appreciate it",
    "much appreciated",
    "grateful",
    "appreciate your help",
    # Approval
    "helpful",
    "perfect",
    "good",
    "looks good",
    "sounds good",
    "that's good",
    "that is good",
    "looks great",
    "sounds great",
    "looks nice",
    "very nice",
    "not bad",
    "that's perfect",
    "works for me",
    "that works",
    "all good",
    "no problem",
    "no issues",
    "well done",
    # Interest and excitement
    "excited",
    "interested",
    "like it",
    "love it",
    "looking forward to it",
    "can't wait",
    "happy with the result",
    "impressed",
)

_NEGATIVE = (
    # General dissatisfaction
    "bad",
    "poor",
    "unhappy",
    "disappointed",
    "disappointing",
    "upset",
    "terrible",
    "awful",
    "horrible",
    "worst",
    "unsatisfactory",
    "not acceptable",
    "below expectations",
    # Problems
    "problem",
    "problems",
    "issue",
    "issues",
    "complaint",
    "complaints",
    "wrong",
    "incorrect",
    "broken",
    "failed",
    "failure",
    "not working",
    "doesn't work",
    "does not work",
    "not resolved",
    "still an issue",
    "unexpected error",
    # Delays
    "delay",
    "delayed",
    "late",
    "taking too long",
    "long wait",
    "waiting too long",
    "no response",
    "no update",
    "haven't received",
    "not received",
    # Pricing and affordability
    "too expensive",
    "overpriced",
    "can't afford",
    "cannot afford",
    "not worth it",
    "costly",
    "price is too high",
    "out of my budget",
    # Disinterest and rejection
    "not interested",
    "don't want",
    "do not want",
    "don't like",
    "do not like",
    "not happy",
    "not satisfied",
    "not helpful",
    "not good",
    "not impressed",
    "not what I expected",
    # Confusion and difficulty
    "confusing",
    "difficult",
    "complicated",
    "hard to understand",
    "unclear",
    "misleading",
    # Negative experiences
    "bad experience",
    "poor service",
    "waste",
    "unfortunately",
    "inconvenient",
    "annoying",
    "frustrating",
    "regret",
    "dissatisfied",
)

_NEGATED_POSITIVE = (
    "not good",
    "not great",
    "not happy",
    "not helpful",
    "not satisfied",
    "not perfect",
    "not pleased",
    "not impressed",
    "not working",
    "never liked",
    "don't like",
    "do not like",
    "not what I wanted",
    "not what I expected",
)

_NEGATED_NEGATIVE = (
    "not bad",
    "not poor",
    "not terrible",
    "not awful",
    "not disappointed",
    "no problem",
    "no problems",
    "no issue",
    "no issues",
    "nothing wrong",
    "not a problem",
    "not too expensive",
    "not overpriced",
)


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
    positive -= sum(_contains(normalized, phrase) for phrase in _NEGATED_POSITIVE)
    negative -= sum(_contains(normalized, phrase) for phrase in _NEGATED_NEGATIVE)
    if positive > negative:
        return "positive", min(0.9, 0.65 + 0.1 * (positive - negative - 1))
    if negative > positive:
        return "negative", min(0.9, 0.65 + 0.1 * (negative - positive - 1))
    if positive and negative:
        return "neutral", 0.55
    return "neutral", 0.6
