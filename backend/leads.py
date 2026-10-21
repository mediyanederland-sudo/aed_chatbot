"""Deterministic lead parsing, validation, and response-state assembly.

Normalize contact text, validate supplied fields, track corrections and
deletions, and construct truthful acknowledgements from stored values.
Phone-shaped part numbers remain product identifiers unless contact context
or a pending contact request establishes that they are phone numbers."""

import re
import time

# Shapes

_EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")

# NANP shapes: (619) 555-0142 | 619-555-0142 | 619.555.0142 | 619 555 0142 |
# 6195550142 | +1 variants. Validated to exactly 10 digits (11 with leading 1).
_PHONE_RE = re.compile(
    r"(?<!\d)(?:\+?1[-. ]?)?\(?\d{3}\)?[-. ]?\d{3}[-. ]?\d{4}(?!\d)"
)

# Require explicit introductions and Unicode name-shaped words; exclude general first-person statements.


_L = r"[^\W\d_]"
_NAME_RE = re.compile(
    rf"\b(?:my name is|name is|name:|this is|it'?s|i am|i'm|i’m|call me)\s+"
    rf"({_L}[{_L[1:-1]}'.-]*(?:\s+{_L}[{_L[1:-1]}'.-]*){{0,2}})",
    re.IGNORECASE,
)
# Recognize a name supplied as a closing signature.
_SIGNATURE_RE = re.compile(
    rf"(?:thanks|thank you|regards|cheers|best regards|all the best|best,)[,!.]?\s+({_L}[{_L[1:-1]}'.-]*(?:\s+{_L}[{_L[1:-1]}'.-]*)?)\s*[.!]?\s*$",
    re.IGNORECASE,
)

_NAME_STOPWORDS = {
    "a", "an", "the", "not", "regarding", "about", "urgent", "important",
    "rescue", "rita", "aed", "test", "asking", "calling", "writing", "looking",
    "interested", "here", "back",
    # sentence words that a lowercase-name regex would otherwise swallow
    "on", "in", "is", "it", "and", "or", "my", "your", "for", "to", "of",
    "with", "at", "so", "no", "yes", "me", "we", "our", "just", "now",
    "by", "way", "btw", "again", "actually",
    # bot persona names must never be stored as the customer
    "coach", "casey", "compliance", "claire", "preparedness", "paige",
}
# Connector words terminate the customer name.

_NAME_CONNECTORS = {"from", "at", "with", "of", "company", "inc", "llc", "@", "w/"}

# Reject product and flow vocabulary as bare customer names.

_NOT_A_NAME = {
    "zoll", "philips", "defibtech", "heartsine", "lifepak", "stryker",
    "cardiac", "science", "physio", "control",
    "aed", "aeds", "training", "course", "courses", "class", "classes",
    "pads", "pad", "battery", "batteries", "cabinet", "cabinets", "package",
    "packages", "quote", "price", "prices", "pricing", "rental", "rentals",
    "cheapest", "best", "help", "hello", "hi", "hey", "thanks", "thank",
    "ok", "okay", "sure", "later", "maybe", "nope", "none", "nothing",
    "email", "phone", "number", "office", "school", "gym", "home", "church",
    "first", "aid", "cpr", "equipment", "product", "products", "order",
} | _NAME_STOPWORDS

# Use contact and correction context to distinguish phone numbers from product identifiers.


_CONTACT_CONTEXT_RE = re.compile(
    r"\b(call me|call us back|call back|reach me|contact me|my number|my phone|"
    r"phone number|my cell|my mobile|text me|my email|email me|email is|"
    r"reach out to me|get back to me|"
    r"wrong number|instead|use this|use that|correct number|should be|"
    r"actually it'?s|update (?:my|the) (?:number|phone|email)|"
    r"new (?:number|phone|email)|ll[aá]mame|mi n[uú]mero|mi correo|mi tel[eé]fono)\b",
    re.IGNORECASE,
)

# A number the customer NAMES as a part is never a phone, even right after our
# ask/offer ("part number 8900-0802-01", "sku 619-555-0142").
_PART_WORDS_RE = re.compile(r"\b(?:part|parts|sku|model|item|catalog|serial|p/n)\b|#\s*\d", re.IGNORECASE)

# Our own asks / offers — a bare reply to these IS the answer (any language).
ASK_EMAIL_RE = re.compile(r"(best email|may I have your (?:name and )?email|your email address|what'?s your email)", re.I)
ASK_NAME_RE = re.compile(r"(may I have your name|what'?s your name|and your name|and what'?s your name)", re.I)
OFFER_PHONE_RE = re.compile(r"(reply here with your phone number|share your phone number|type it as digits|best (?:phone )?number)", re.I)

_FORGET_RE = re.compile(
    r"\b(forget|remove|delete|disregard|don'?t (?:use|keep|save|store)|erase|scrap)\b[^.]{0,40}?"
    r"\b(my )?(e-?mail|email address|number|phone|cell|mobile|details|info)\b",
    re.IGNORECASE,
)
_REFUSAL_RE = re.compile(
    r"^\W*(no|nope|no thanks|no thank you|not now|later|maybe later|rather not|i'?d rather not|skip|pass|why)\b",
    re.IGNORECASE,
)

_DIGIT_WORDS = {
    "zero": "0", "oh": "0", "o": "0", "one": "1", "two": "2", "three": "3",
    "four": "4", "five": "5", "six": "6", "seven": "7", "eight": "8", "nine": "9",
}

# Normalization

_OBFUSCATED_EMAIL_RE = re.compile(
    r"([A-Za-z0-9._%+-]+)\s*(?:\(at\)|\[at\]|\{at\}|\bat\b)\s*"
    r"([A-Za-z0-9-]+(?:\s*(?:\(dot\)|\[dot\]|\bdot\b|\.)\s*[A-Za-z0-9-]+)+)",
    re.IGNORECASE,
)


def normalize_contact_text(text: str) -> str:
    """Normalize obfuscated email addresses and spelled-out digit sequences."""
    if not text:
        return text

    def _email(m):
        domain = re.sub(r"\s*(?:\(dot\)|\[dot\]|\bdot\b|\.)\s*", ".", m.group(2), flags=re.I)
        return f"{m.group(1)}@{domain}"

    out = _OBFUSCATED_EMAIL_RE.sub(_email, text)

    # spelled digits: a run of ≥10 digit-words becomes a number
    words = re.split(r"(\s+)", out)
    run, res = [], []
    def flush():
        if len(run) >= 10:
            res.append("".join(_DIGIT_WORDS[w.lower()] for w in run))
        else:
            res.extend(run)
        run.clear()
    for tok in words:
        if tok.strip() == "":
            if run:
                continue  # swallow whitespace inside a digit run
            res.append(tok)
        elif tok.lower().strip(",.") in _DIGIT_WORDS:
            run.append(tok.strip(",."))
        else:
            flush()
            res.append(tok)
    flush()
    # re-space: digit runs were joined, other tokens keep original spacing
    return "".join(res) if len(res) != len(words) else out if all(
        r == w for r, w in zip(res, words)) else " ".join(x for x in res if x.strip())


# Validation

def _valid_phone(raw: str) -> str | None:
    digits = re.sub(r"\D", "", raw)
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    if len(digits) != 10:
        return None
    if digits[0] in "01":  # NANP area codes never start with 0/1
        return None
    return f"{digits[0:3]}-{digits[3:6]}-{digits[6:10]}"


def flag_suspicious(field: str, value: str) -> str | None:
    """Junk hints for the Leads tab — NOT rejections (we cannot know)."""
    if field == "phone":
        d = re.sub(r"\D", "", value)
        if d[3:6] == "555" or len(set(d)) <= 2 or d in ("1234567890", "0123456789"):
            return "looks like a placeholder number"
    if field == "email":
        dom = value.rsplit("@", 1)[-1].lower()
        typos = {"gmial.com": "gmail.com", "gmal.com": "gmail.com", "gamil.com": "gmail.com",
                 "hotmial.com": "hotmail.com", "yaho.com": "yahoo.com", "outlok.com": "outlook.com"}
        if dom in typos:
            return f"domain may be a typo (did they mean {typos[dom]}?)"
        if dom.split(".")[0] in ("test", "example", "asdf", "abc", "aaa"):
            return "looks like a test address"
    return None


def _title_if_flat(name: str) -> str:
    """Title-case only all-lower/all-upper input; preserve typed casing."""
    return name.title() if (name.islower() or name.isupper()) else name


def _clean_name_words(candidate: str) -> list[str] | None:
    """Split, cut at the first connector, reject stopwords/non-letters."""
    words = candidate.strip().rstrip(".,!?").split()
    kept = []
    for w in words:
        lw = w.lower().strip(",.")
        # Connectors and sentence words terminate a name; sentence words cannot start one.

        if lw in _NAME_CONNECTORS or lw in _NAME_STOPWORDS:
            break
        kept.append(w)
    if not 1 <= len(kept) <= 3:
        return None
    for w in kept:
        if not re.fullmatch(rf"{_L}[{_L[1:-1]}'.-]*", w):
            return None
    return kept


def validate_llm_name(candidate, message: str) -> str | None:
    """Accept only name-shaped words present in the customer message; reject
    instruction fragments, connector suffixes, and unrelated prose."""
    if not candidate or not isinstance(candidate, str) or not message:
        return None
    kept = _clean_name_words(candidate)
    if not kept:
        return None
    low_msg = message.lower()
    for w in kept:
        if w.lower() not in low_msg:
            return None
    return _title_if_flat(" ".join(kept))


# Extraction

def extract_lead(text: str, lead_on_file: dict | None = None) -> dict:
    """Extract contact fields from a chat message (after normalization).

    Returns {} or a dict with any of email/phone/name plus:
      confident: True when contact context (or contact-only shape, or a
      correction against a field on file) makes the phone/email reading
      authoritative — the caller suppresses SKU handling for this turn only
      when confident.
    """
    if not text or len(text) > 2000:
        return {}
    text = normalize_contact_text(text)
    on_file = lead_on_file or {}
    found: dict = {}

    m = _EMAIL_RE.search(text)
    if m:
        found["email"] = m.group(0).lower()

    has_context = bool(_CONTACT_CONTEXT_RE.search(text))

    m = _PHONE_RE.search(text)
    if m:
        phone = _valid_phone(m.group(0))
        # A different valid phone while one is on file is a correction —
        # the customer is fixing our record, no context words needed.
        correction = bool(phone and on_file.get("phone") and phone != on_file.get("phone")
                          and len(text) <= 80)
        if phone and (has_context or correction or _is_bare_contact(text)):
            found["phone"] = phone

    m = _NAME_RE.search(text) or _SIGNATURE_RE.search(text)
    if m:
        kept = _clean_name_words(m.group(1))
        if kept:
            found["name"] = _title_if_flat(" ".join(kept))

    if found:
        found["confident"] = has_context or "email" in found or _is_bare_contact(text) \
            or ("phone" in found)
    return found


def capture_ask_reply(text: str, last_bot_message: str | None, lead_on_file: dict | None = None,
                      intent: str | None = None) -> dict:
    """Interpret bare replies to a pending name, email, or phone request.
    Single-token names require an appropriate analysis intent; part numbers
    retain SKU precedence."""
    if not text or not last_bot_message:
        return {}
    raw = text.strip()
    if _REFUSAL_RE.search(raw):
        return {}
    asked_email = bool(ASK_EMAIL_RE.search(last_bot_message))
    asked_name = bool(ASK_NAME_RE.search(last_bot_message))
    offered_phone = bool(OFFER_PHONE_RE.search(last_bot_message))
    if not (asked_email or asked_name or offered_phone):
        return {}
    norm = normalize_contact_text(raw)
    found: dict = {}
    if asked_email or asked_name or offered_phone:
        m = _EMAIL_RE.search(norm)
        if m:
            found["email"] = m.group(0).lower()
        # Require a single phone-shaped token and preserve explicit SKU or model references.


        m = _PHONE_RE.search(norm)
        phone = _valid_phone(m.group(0)) if m else None
        if phone and not _PART_WORDS_RE.search(norm) \
                and (offered_phone or (asked_email and _is_bare_contact(norm))):
            found["phone"] = phone
    if (asked_email or asked_name) and "email" not in found and "phone" not in found \
            and not (lead_on_file or {}).get("name"):
        words = raw.rstrip(".!,").split()
        if 1 <= len(words) <= 3 and all(re.fullmatch(rf"{_L}[{_L[1:-1]}'.-]*", w) for w in words):
            lows = [w.lower() for w in words]
            if not any(w in _NOT_A_NAME for w in lows):
                product_intent = intent in ("configurator", "find_specific", "compare", "browse")
                if len(words) > 1 or not product_intent:
                    found["name"] = _title_if_flat(" ".join(words))
    if found:
        found["confident"] = True
        found["source"] = "ask_reply"
    return found


def detect_forget(text: str) -> list[str]:
    """Fields the customer asked us to drop ('forget my email')."""
    out = []
    for m in _FORGET_RE.finditer(text or ""):
        what = m.group(3).lower()
        if "mail" in what:
            out.append("email")
        elif what in ("number", "phone", "cell", "mobile"):
            out.append("phone")
        elif what in ("details", "info"):
            out.extend(["email", "phone", "name"])
    return sorted(set(out))


def invalid_attempt(text: str, found: dict) -> str | None:
    """Detect invalid contact attempts so the reply requests correction rather
    than claiming that an invalid value was saved."""
    t = normalize_contact_text(text or "")
    # email-ish shape only (word@word) — "call me @ 3pm" is not an attempt
    if re.search(r"\w@\w", t) and "email" not in found:
        return "email"
    if "phone" not in found:
        digits_runs = re.findall(r"\d[\d\s().-]{5,}\d", t)
        for run in digits_runs:
            d = re.sub(r"\D", "", run)
            if 7 <= len(d) <= 12 and (_CONTACT_CONTEXT_RE.search(t) or _is_bare_contact(t)):
                return "phone"
    return None


# Contact-only shape

def _is_bare_contact(text: str, found: dict | None = None) -> bool:
    """True when the message is essentially ONLY contact info (± pleasantries)."""
    stripped = normalize_contact_text(text)
    stripped = _EMAIL_RE.sub(" ", stripped)
    stripped = _PHONE_RE.sub(" ", stripped)
    stripped = _NAME_RE.sub(" ", stripped)
    stripped = _CONTACT_CONTEXT_RE.sub(" ", stripped)
    for v in (found or {}).values():
        if isinstance(v, str) and v:
            stripped = re.sub(re.escape(v), " ", stripped, flags=re.IGNORECASE)
    stripped = re.sub(
        r"\b(hi|hello|hey|thanks|thank you|please|ok|okay|sure|here|is|it's|its|and|or|the|my|you can|use|btw|by the way)\b",
        " ", stripped, flags=re.IGNORECASE)
    return len(re.sub(r"[^A-Za-z0-9]", "", stripped)) <= 4


def is_contact_only(text: str, found: dict) -> bool:
    """Message is just contact details → deterministic ack path (no LLM/search)."""
    if not found or not (found.get("email") or found.get("phone") or found.get("name")):
        return False
    return _is_bare_contact(text, found)


# Lead record helpers

def contactable(lead: dict | None) -> bool:
    """The ONLY bar that makes a lead a lead: a way to reach them."""
    lead = lead or {}
    return bool(lead.get("email") or lead.get("phone"))


def lead_history_append(lead: dict, turn: int, field: str, old, new, source: str) -> None:
    hist = lead.setdefault("history", [])
    hist.append({"ts": time.time(), "turn": turn, "field": field,
                 "old": old, "new": new, "source": source})
    del hist[:-30]


def lead_add_note(lead: dict, text: str, turn: int, kind: str | None = None) -> bool:
    """Accumulate a salesworthy note (dedupe, cap). Returns True if added."""
    text = (text or "").strip()[:200]
    if not text:
        return False
    notes = lead.setdefault("notes", [])
    low = text.lower()
    for n in notes:
        if n.get("text", "").lower() == low:
            return False
    note = {"ts": time.time(), "turn": turn, "text": text}
    if kind:
        note["kind"] = kind
    notes.append(note)
    del notes[:-10]
    return True


# LEAD STATE (what the model is allowed to say)

def build_lead_state(lead: dict | None, captured: list[str] | None = None,
                     invalid: str | None = None, deleted: list[str] | None = None) -> str:
    """Deterministic per-turn truth for the reply model."""
    lead = lead or {}
    lines = ["## LEAD STATE (system of record — this is ALL you know about the customer's contact details)"]
    on_file = [f"{k}: {lead[k]}" for k in ("name", "company", "email", "phone") if lead.get(k)]
    lines.append("- On file: " + (", ".join(on_file) if on_file else "nothing"))
    if lead.get("notes"):
        lines.append("- Notes on file: " + " | ".join(n.get("text", "") for n in lead["notes"]))
    lines.append("- Contactable: " + ("YES" if contactable(lead) else "NO (no email or phone)"))
    if captured:
        lines.append("- Captured THIS turn: " + ", ".join(captured))
    if invalid:
        lines.append(f"- ATTEMPTED this turn but NOT valid: {invalid} — do NOT confirm it; the "
                     f"system already asked them to re-type it.")
    if deleted:
        lines.append("- Deleted this turn: " + ", ".join(deleted))
    lines.append("RULES: The system has ALREADY written any confirmation of contact details "
                 "this turn — do NOT confirm, repeat, or mention email/phone/name changes "
                 "yourself. Never say noted/updated/removed/added about anything not listed "
                 "above. Never say our team will reach out / contact / follow up with them "
                 "unless Contactable is YES. Never ask for contact details. You cannot send, "
                 "email, or forward anything — never say something was or will be sent; a human "
                 "may follow up only if Contactable is YES.")
    return "\n".join(lines)


def contact_sentence(lead: dict | None, captured: list[str] | None, invalid: str | None,
                     deleted: list[str] | None, store_phone: str) -> str:
    """The code-owned sentence(s) about contact for this turn (truthful by
    construction). Empty when nothing contact-related happened."""
    lead = lead or {}
    parts = []
    first = f", {lead['name'].split()[0]}" if lead.get("name") else ""
    if captured:
        vals = [lead[k] for k in ("email", "phone") if k in captured and lead.get(k)]
        if vals:
            parts.append(f"Thanks{first} — I've noted {' and '.join(vals)}.")
        elif "name" in captured:
            parts.append(f"Thanks{first}!")
    if deleted:
        parts.append("Done — I've removed your " + " and ".join(deleted) + " from our record.")
    if invalid == "email":
        parts.append("I couldn't read that email address — could you type it plainly, like name@example.com?")
    elif invalid == "phone":
        parts.append("I couldn't read that number — could you type it as digits, like 619-555-0142?")
    return " ".join(parts)


# Mechanical guard on the model's words

_CLAIM_RE = re.compile(
    r"[^.!?\n]*\b(?:reach (?:you|out)|be in touch|contact you|follow up with you|"
    r"get back to you|call you back|(?:noted|updated|removed|disregard|added) your|"
    r"(?:will|can) (?:email|call|text) you|on file)\b[^.!?\n]*[.!?]?",
    re.IGNORECASE,
)
_ASK_RE = re.compile(
    r"[^.!?\n]*\b(?:may I have|could I (?:get|have)|what'?s (?:your|the best)|can I get|"
    r"share your)\b[^.!?\n]*\b(?:email|e-mail|phone|number|name)\b[^.!?\n]*\?",
    re.IGNORECASE,
)


# On a turn where the SYSTEM wrote the contact sentence, every model sentence
# that confirms the record is redundant ("Our system has it on file.",
# "Thanks for confirming your email!") — the system owns confirmation on
# capture turns; recall answers on other turns ("I have x on file for you")
# are untouched.
_CONFIRM_RE = re.compile(
    r"[^.!?\n]*\b(?:(?:noted|updated|removed|saved|recorded|got|have) (?:your|that|it)|on file|"
    r"in our (?:system|records)|thanks? for (?:confirming|sharing|providing|letting)|"
    r"thank you for (?:confirming|sharing|providing|letting))\b[^.!?\n]*[.!?]?",
    re.IGNORECASE,
)

# The chatbot does not send messages; remove claims that an email has already been sent.


_SENT_CLAIM_RE = re.compile(
    r"[^.!?\n]*\b(?:I|I've|I have|we've|we have|has been|have been|are being|is being|"
    r"I'll make sure|I've made sure|I made sure)\b[^.!?\n]*\b(?:sent|emailed|forwarded|mailed|sending)\b"
    r"[^.!?\n]*[.!?]?",
    re.IGNORECASE,
)


def strip_contact_claims(text: str, lead: dict | None, store_values: tuple[str, ...] = (),
                         strip_asks: bool = False, replacement: str | None = None
                         ) -> tuple[str, list[str], bool]:
    """Remove sentences where the MODEL talks about the customer's contact:
    any email/phone value not on file (store values exempt), any outreach or
    noted/updated claim while not contactable, and — after the once-ever ask is
    spent — any model-initiated contact ask.

    `replacement`: when given, the FIRST outreach claim removed while not
    contactable is replaced in place by this truthful sentence instead of
    vanishing — deleting a load-bearing sentence leaves the next one hanging
    ("Otherwise, you can call…"). Returns (text, removed, replaced)."""
    lead = lead or {}
    removed: list[str] = []
    replaced = False
    out = text or ""
    known = {v.lower() for v in (lead.get("email"), lead.get("phone")) if v}
    known |= {v.lower() for v in store_values if v}

    def _drop(pattern, substitute: str | None = None):
        nonlocal out, replaced
        first = True
        for m in list(pattern.finditer(out)):
            s = m.group(0)
            if s and s.strip():
                removed.append(s.strip())
        def _sub(m):
            nonlocal first, replaced
            if substitute and first and m.group(0).strip():
                first = False
                replaced = True
                return " " + substitute + " "
            return ""
        out = pattern.sub(_sub, out)

    # values not on file
    for m in list(_EMAIL_RE.finditer(out)):
        if m.group(0).lower() not in known:
            _drop(re.compile(r"[^.!?\n]*" + re.escape(m.group(0)) + r"[^.!?\n]*[.!?]?"))
    for m in list(_PHONE_RE.finditer(out)):
        val = _valid_phone(m.group(0))
        if val and val not in known and m.group(0).lower() not in known:
            _drop(re.compile(r"[^.!?\n]*" + re.escape(m.group(0)) + r"[^.!?\n]*[.!?]?"))
    if not contactable(lead):
        _drop(_CLAIM_RE, replacement)
    _drop(_SENT_CLAIM_RE)  # "I've made sure the details are being sent" — nothing is ever sent by the bot
    if strip_asks:
        _drop(_ASK_RE)
    out = re.sub(r"[ \t]{2,}", " ", out)
    out = re.sub(r"\n{3,}", "\n\n", out).strip()
    return out, removed, replaced
