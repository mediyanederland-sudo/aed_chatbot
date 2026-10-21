"""Gated semantic extraction of customer-provided lead information.

Read the current message and previous assistant turn to distinguish offered
contact details, third-party contacts, corrections, refusals, and typed sales
facts. Validate extracted values against customer text before storage.
Deterministic parsing remains the fallback when extraction is unavailable."""

import re

from llm import call_llm_structured
from leads import (normalize_contact_text, validate_llm_name, _EMAIL_RE, _valid_phone,
                   ASK_EMAIL_RE, ASK_NAME_RE, OFFER_PHONE_RE, _NAME_STOPWORDS)

# Store only the enumerated sales-fact types.


FACT_KINDS = {
    "team_size":   "how many people / units / seats (\"40 staff\", \"3 AEDs\")",
    "environment": "the place or setting the purchase is for (\"warehouse\", \"school\", \"daycare\")",
    "timeline":    "when they need it (\"before Q4\", \"by September 1\")",
    "budget":      "money signals (\"budget approved\", \"under $1500\", \"no budget limit\")",
    "scheduling":  "when/how to contact them (\"after 3pm\", \"mornings\")",
    "buying_for":  "who the purchase is for when not themselves (\"their boss John\", \"a client\")",
    # Keep product questions and general interests out of stored sales notes.


}

LEAD_SCHEMA = {
    "type": "object",
    "properties": {
        "name": {"type": "string", "description": "the customer's OWN name if they state it in this message, else empty"},
        "company": {"type": "string", "description": "the customer's organization if stated, else empty"},
        "email": {"type": "string", "description": "an email the customer PROVIDES as a way to reach them (self or the named contact person), else empty"},
        "phone": {"type": "string", "description": "a phone number the customer PROVIDES as a way to reach them, else empty"},
        "contact_belongs_to": {"type": "string", "description": "'self' when the email/phone is the customer's own; otherwise the NAME of the person it belongs to (e.g. their boss); empty if no contact given"},
        "facts": {"type": "array", "maxItems": 3,
                  "items": {"type": "object",
                            "properties": {
                                "kind": {"type": "string", "enum": list(FACT_KINDS)},
                                "text": {"type": "string", "description": "one short phrase, the customer's meaning"}},
                            "required": ["kind", "text"]},
                  "description": "concrete facts a salesperson needs that no field holds, each typed by kind. Only kinds in the enum exist; a statement that fits no kind is NOT a fact."},
        "question": {"type": "string",
                     "description": "the question or request the customer is making of US in this message (what they want to know or get), else empty"},
        "forget": {"type": "array", "items": {"type": "string", "enum": ["email", "phone", "name"]},
                   "description": "fields the customer asks us to drop or stop using"},
        "refused": {"type": "boolean", "description": "true if the customer declines to share contact details or objects to being contacted"},
        "hypothetical": {"type": "boolean", "description": "true if any contact detail in the message is an example, a question, or otherwise NOT actually offered as their contact"},
    },
    "required": ["name", "company", "email", "phone", "contact_belongs_to", "facts", "question",
                 "forget", "refused", "hypothetical"],
}
# The question field absorbs customer requests and is never stored as a sales fact.


_FACT_KIND_LINES = "\n".join(f"  - {k}: {v}" for k, v in FACT_KINDS.items())

EXTRACTOR_PROMPT = """You extract lead details from ONE customer message in a store-chat conversation.
You are given what the assistant said just before (so you know what was asked) and the customer's message.

Rules:
- Extract ONLY what the customer explicitly PROVIDES in THIS message. Never infer, never guess, never reuse earlier turns.
- A bare reply to the assistant's question is an answer to that question: if the assistant asked for a name or email and the customer replies "shahin" or "shahin@acme.com", that is their name / email. If the assistant offered "reply here with your phone number" and the customer replies with digits (in any language or spelled out), that is their phone.
- Company names ("Acme Rail"), product or brand words, and the assistant's own name are NEVER the customer's name.
- "do NOT contact me at X" / "not X anymore, use Y" → X is NOT a contact (put it in forget if we might hold it); Y is.
- An email or number mentioned as an example, a question, or a hypothetical ("if I gave you test@example.com…") is NOT a contact: set hypothetical=true and leave the field empty.
- A contact that belongs to someone else ("my boss John's email is john@acme.com") → email=john@acme.com, contact_belongs_to="John".
- A refusal or objection ("no thanks", "why do you need it", "I'd rather not") → refused=true, no fields.
- company: the NAME of their organization as they typed it ("Acme Rail"). A kind of place ("a school", "our office") is an environment fact, never a company.
- facts: concrete facts a salesperson needs that no field holds, each with a kind from this list and nothing else:
""" + _FACT_KIND_LINES + """
  A remark with no kind on this list is not a fact — leave it out. Never their general interest, never something already in a field.
- question: what the customer is asking US for or wants to know. Their question is NEVER a fact — it goes here and nowhere else.
- Leave every field empty when nothing applies. Never invent."""

_LEAD_WORDS_RE = re.compile(
    r"\b(email|e-?mail|phone|number|call|contact|reach|text|name|company|boss|manager|"
    r"staff|people|team|employees|budget|quote|schedule|after|before|morning|afternoon|"
    r"pm|am|forget|remove|delete|don'?t|not|instead|wrong|use|correo|tel[eé]fono|ll[aá]mame|@)\b",
    re.IGNORECASE)


def is_lead_bearing(message: str, last_bot: str | None, regex_found: dict | None) -> bool:
    """Gate: only turns that could carry lead information reach the LLM."""
    if regex_found:
        return True
    if last_bot and (ASK_EMAIL_RE.search(last_bot) or ASK_NAME_RE.search(last_bot)
                     or OFFER_PHONE_RE.search(last_bot)):
        return True
    return bool(_LEAD_WORDS_RE.search(message or "")) or bool(re.search(r"\d{7,}", re.sub(r"\D", "", message or "")))


def _present(words: str, message_low: str) -> bool:
    """Require each significant word to occur as a whole word in the message."""
    toks = [t for t in re.findall(r"[^\W_]+", (words or "").lower()) if len(t) > 2]
    return bool(toks) and all(re.search(r"(?<![^\W_])" + re.escape(t) + r"(?![^\W_])", message_low) for t in toks)


def _validate(raw: dict, message: str, lead_on_file: dict | None) -> dict:
    """Content validation: every stored value must be anchored in what the
    customer typed. The LLM decides meaning; the code decides truth."""
    msg_norm = normalize_contact_text(message or "")
    low = msg_norm.lower()
    out: dict = {"notes": [], "forget": [], "refused": bool(raw.get("refused")),
                 "hypothetical": bool(raw.get("hypothetical"))}
    if out["hypothetical"]:
        return out
    email = (raw.get("email") or "").strip().lower()
    if email and _EMAIL_RE.fullmatch(email) and email in low:
        out["email"] = email
    phone = _valid_phone(raw.get("phone") or "") if raw.get("phone") else None
    if phone and re.sub(r"\D", "", phone)[-7:] in re.sub(r"\D", "", msg_norm):
        out["phone"] = phone
    # Extract names from typed words rather than the local part of an email address.


    msg_no_email = _EMAIL_RE.sub(" ", msg_norm)
    owner = (raw.get("contact_belongs_to") or "").strip()
    if owner and owner.lower() not in ("self", "me", "myself", "customer", ""):
        owner_name = validate_llm_name(owner, msg_no_email)
        if owner_name:
            out["contact_belongs_to"] = owner_name
    name = validate_llm_name(raw.get("name"), msg_no_email) if raw.get("name") else None
    if name and name.lower() not in _NAME_STOPWORDS:
        out["name"] = name
    company = (raw.get("company") or "").strip()
    # Require an explicitly typed organization name rather than an inferred email domain.


    domainish = "." in company or "@" in company or (
        out.get("email") and re.sub(r"\W", "", company.lower()) in out["email"].lower())
    typed_as_name = " " in company or bool(
        re.search(r"\b" + re.escape(company[:1].upper() + company[1:]) + r"\b", message or ""))
    if company and not domainish and typed_as_name and _present(company, low) \
            and company.lower() != (name or "").lower():
        out["company"] = company[:80]
    for f in (raw.get("facts") or []):
        if len(out["notes"]) >= 3:
            break
        if not isinstance(f, dict) or f.get("kind") not in FACT_KINDS:
            continue  # only typed facts exist
        text = (f.get("text") or "").strip()
        if not text or len(text) > 200:
            continue
        # entity-anchored: every number / capitalized token must come from the
        # message (free words unconstrained)
        ents = re.findall(r"\b(?:\d[\d:.,]*|[A-Z][a-z]+)\b", text)
        if all(e.lower() in low for e in ents):
            out["notes"].append({"kind": f["kind"], "text": text})
    held = lead_on_file or {}
    out["forget"] = [f for f in (raw.get("forget") or []) if f in ("email", "phone", "name") and held.get(f)]
    return out


def extract(message: str, last_bot: str | None, lead_on_file: dict | None = None,
            regex_found: dict | None = None) -> dict | None:
    """Run the gated extractor. Returns a validated dict or None (not run /
    failed). Never raises into the chat path."""
    if not is_lead_bearing(message, last_bot, regex_found):
        return None
    context = f"ASSISTANT (previous turn): {last_bot[-600:] if last_bot else '(none)'}\n\nCUSTOMER: {message}"
    try:
        raw = call_llm_structured(EXTRACTOR_PROMPT, context, LEAD_SCHEMA, temperature=0.0,
                                  max_tokens=400)
    except Exception as e:  # pragma: no cover - network/provider
        print(f"  [Lead-extractor] failed ({type(e).__name__}): {e}")
        return None
    if not isinstance(raw, dict):
        return None
    result = _validate(raw, message, lead_on_file)
    keys = [k for k in ("name", "company", "email", "phone", "contact_belongs_to") if result.get(k)]
    print(f"  [Lead-extractor] fields={keys} notes={len(result['notes'])} "
          f"forget={result['forget']} refused={result['refused']} hypothetical={result['hypothetical']}")
    return result
