"""Deterministic human-handoff and conversation-closer detection.

These checks complement model intent classification and keep contact
information and acknowledgements consistent."""

HANDOFF_REPLY = (
    "Of course — you can reach our (human) team directly at **855-888-2771** "
    "(Mon–Fri, 8 AM–4:30 PM PST) or by email at **info@aedleader.com**. They can help with quotes, orders, "
    "technical questions, and anything else I can't. Or just reply here with "
    "your phone number and our team will call you back."
)

_HANDOFF_PHRASES = (
    "real agent", "live agent", "human agent", "real person", "a human",
    "humain",  # Include common spelling variants of human requests.
    "speak to a person", "talk to a person", "speak to someone", "talk to someone",
    "customer service", "representative", "speak with someone", "talk with someone",
)

_CLOSER_MARKERS = ("thank", "thx", "bye", "appreciate")
_CLOSER_EXACT = ("ok", "okay", "got it", "great", "perfect", "cool", "k", "ty")


def wants_human(text: str) -> bool:
    """Detect explicit requests for a human representative, including common typos.
    Product questions alone are not handoff requests."""
    low = f" {(text or '').lower()} "
    return any(p in low for p in _HANDOFF_PHRASES)


def is_closer(text: str) -> bool:
    """Detect acknowledgements and conversation closers while excluding greetings."""
    low = (text or "").lower().strip(" !.?,")
    return any(m in low for m in _CLOSER_MARKERS) or low in _CLOSER_EXACT
