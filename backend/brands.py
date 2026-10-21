"""Canonical brand vocabulary shared by indexing and search filters.

Normalize catalog taxonomy and model aliases consistently so exact brand
filters match indexed payloads. Unknown brands pass through unchanged."""

CANONICAL_BRANDS = {
    # dirty source-taxonomy forms (aedleader)
    "heart sine": "HeartSine",
    "cardiac science": "Cardiac Science",
    # Normalize provider-generated aliases to the catalog vocabulary.
    "physio-control": "Lifepak",
    "physio control": "Lifepak",
    "physio-control/lifepak": "Lifepak",
    "stryker": "Lifepak",
    # case-variant self-maps (idempotence for already-clean values)
    "heartsine": "HeartSine",
    "zoll": "Zoll",
    "philips": "Philips",
    "defibtech": "Defibtech",
    "lifepak": "Lifepak",
}


def canonical_brand(raw):
    """Canonical form of a brand string; unknown brands pass through unchanged
    (cpr1 legitimately carries e.g. 'First Aid Only')."""
    if not raw:
        return raw
    return CANONICAL_BRANDS.get(raw.strip().lower(), raw.strip())
