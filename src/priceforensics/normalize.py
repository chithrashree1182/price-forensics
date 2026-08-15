"""Product normalisation: matching the same physical product across retailers.

This is the load-bearing part of the whole study. The MRP-contradiction
analysis asks "do sellers agree on this product's original price?", which is
only meaningful if we are genuinely looking at the same product. Retailers
title things very differently:

    Flipkart : "SAMSUNG Galaxy S24 5G (Onyx Black, 128 GB)  (8 GB RAM)"
    Croma    : "Samsung Galaxy S24 5G (8GB RAM, 128GB, Onyx Black)"
    Amazon   : "Samsung Galaxy S24 5G AI Smartphone (Onyx Black, 8GB, 128GB Storage)"

All three are the same SKU. We reduce each to a match key:

    samsung|galaxy s24 5g|8gb-128gb

Colour is deliberately dropped: colour variants share an MRP, so keeping it
would fragment the group and hide genuine contradictions. Storage and RAM are
kept because they genuinely price differently.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

# Brands seen in the categories under study. Order matters: longer names first
# so "OnePlus" is not swallowed by a substring match on "One".
KNOWN_BRANDS = [
    "samsung", "oneplus", "xiaomi", "redmi", "realme", "motorola", "nothing",
    "google", "apple", "iphone", "vivo", "oppo", "poco", "infinix", "tecno",
    "lava", "iqoo", "honor", "asus", "lenovo", "acer", "hp", "dell", "msi",
    "microsoft", "lg", "sony", "boat", "jbl", "sennheiser", "bose", "noise",
    "skullcandy", "marshall", "beats", "soundcore", "anker",
]

# Marketing noise that carries no identity information.
NOISE_TOKENS = {
    "smartphone", "mobile", "phone", "5g", "4g", "lte", "ai", "new", "latest",
    "with", "and", "the", "for", "buy", "online", "best", "price", "in", "india",
    "laptop", "notebook", "headphone", "headphones", "earbuds", "earphone",
    "wireless", "bluetooth", "true", "tws", "gaming", "official", "warranty",
    "launched", "edition",
}

COLOUR_WORDS = {
    "black", "white", "blue", "green", "red", "grey", "gray", "silver", "gold",
    "purple", "pink", "yellow", "orange", "titanium", "graphite", "midnight",
    "starlight", "onyx", "marble", "cream", "lavender", "mint", "obsidian",
    "phantom", "cosmic", "aurora", "twilight", "charcoal", "platinum", "sage",
    "navy", "teal", "bronze", "copper", "ivory", "beige", "violet", "amber",
}

_STORAGE_RE = re.compile(r"(\d+)\s*(gb|tb)\b", re.IGNORECASE)
_RAM_HINT_RE = re.compile(r"(\d+)\s*gb\s*(?:ram)", re.IGNORECASE)
_STORAGE_HINT_RE = re.compile(r"(\d+)\s*(gb|tb)\s*(?:rom|storage|internal)", re.IGNORECASE)
_PAREN_RE = re.compile(r"[\(\[\{].*?[\)\]\}]")
_NON_ALNUM_RE = re.compile(r"[^a-z0-9\s\-\+]")
_WS_RE = re.compile(r"\s+")


@dataclass(frozen=True)
class ParsedProduct:
    match_key: str
    brand: str | None
    model: str | None
    variant: str | None
    canonical_title: str

    @property
    def is_confident(self) -> bool:
        """Whether we trust this enough to use in cross-seller comparison.

        Without a brand we cannot safely group listings, so those products are
        still tracked longitudinally (price history is per-listing anyway) but
        excluded from the MRP-contradiction analysis.
        """
        return self.brand is not None and bool(self.model)


def _strip_accents(text: str) -> str:
    return "".join(
        c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c)
    )


def extract_variant(title: str) -> str | None:
    """Pull the RAM/storage configuration out of a title.

    Prefers explicitly labelled values ("8 GB RAM", "128GB Storage"). Falls
    back to positional heuristics: when two bare capacities appear, the smaller
    is RAM and the larger is storage, which holds for essentially every consumer
    device on the market.
    """
    ram = _RAM_HINT_RE.search(title)
    storage = _STORAGE_HINT_RE.search(title)

    ram_gb: int | None = int(ram.group(1)) if ram else None
    storage_gb: int | None = None
    if storage:
        val, unit = int(storage.group(1)), storage.group(2).lower()
        storage_gb = val * 1024 if unit == "tb" else val

    if ram_gb is None or storage_gb is None:
        capacities: list[int] = []
        for m in _STORAGE_RE.finditer(title):
            val, unit = int(m.group(1)), m.group(2).lower()
            capacities.append(val * 1024 if unit == "tb" else val)
        capacities = sorted(set(capacities))

        if storage_gb is None and capacities:
            storage_gb = capacities[-1]
        if ram_gb is None and len(capacities) >= 2:
            ram_gb = capacities[0]

    if ram_gb and storage_gb and ram_gb != storage_gb:
        return f"{ram_gb}gb-{_fmt_capacity(storage_gb)}"
    if storage_gb:
        return _fmt_capacity(storage_gb)
    return None


def _fmt_capacity(gb: int) -> str:
    if gb >= 1024 and gb % 1024 == 0:
        return f"{gb // 1024}tb"
    return f"{gb}gb"


def extract_brand(title_lower: str) -> str | None:
    """Find the brand, preferring the earliest mention in the title.

    Position beats length. Retailers put the brand first, while later words are
    feature copy that can collide with other brands' names: "Sony ULT WEAR ...
    Noise Cancelling" contains both "sony" and "noise", and a longest-match rule
    picks Noise — the wrong company. A wrong brand corrupts the match key, which
    silently breaks cross-seller comparison, so this is worth getting right.

    Length is kept only as a tie-break, so "oneplus" still beats "one" when both
    start at the same offset.
    """
    best: tuple[int, int, str] | None = None
    for brand in KNOWN_BRANDS:
        m = re.search(rf"\b{re.escape(brand)}\b", title_lower)
        if m is None:
            continue
        candidate = (m.start(), -len(brand), brand)
        if best is None or candidate < best:
            best = candidate
    if best is None:
        return None
    # "iPhone" implies Apple; normalise so the two forms group together.
    return "apple" if best[2] == "iphone" else best[2]


def parse_title(title: str, category: str = "") -> ParsedProduct:
    """Reduce a retailer's product title to a stable cross-seller match key."""
    original = title.strip()
    variant = extract_variant(original)

    text = _strip_accents(original.lower())
    # Parenthetical blocks are almost always colour/variant chatter, and we have
    # already pulled the capacities we need out of the full string.
    text = _PAREN_RE.sub(" ", text)
    text = _NON_ALNUM_RE.sub(" ", text)
    text = _WS_RE.sub(" ", text).strip()

    brand = extract_brand(text)

    tokens = []
    for tok in text.split():
        if tok in NOISE_TOKENS or tok in COLOUR_WORDS:
            continue
        if brand and tok == brand:
            continue
        if tok in {"iphone"} and brand == "apple":
            tokens.append(tok)          # "iPhone" is part of the model name
            continue
        if _STORAGE_RE.fullmatch(tok) or tok in {"gb", "tb", "ram", "rom"}:
            continue
        if tok.isdigit() and len(tok) > 4:   # stray SKU numbers
            continue
        tokens.append(tok)

    # Model names are short; anything past ~4 tokens is marketing copy.
    model = " ".join(tokens[:4]).strip() or None

    parts = [brand or "unknown", model or "unknown"]
    if variant:
        parts.append(variant)
    match_key = "|".join(parts)

    canonical_bits = [b for b in (brand, model, variant) if b]
    canonical = " ".join(canonical_bits).title() if canonical_bits else original

    return ParsedProduct(
        match_key=match_key,
        brand=brand,
        model=model,
        variant=variant,
        canonical_title=canonical,
    )


def parse_price(raw: str | None) -> float | None:
    """Parse an Indian-format price string into a float.

    Handles the shapes that actually appear on these sites:
        "₹1,29,999"  "Rs. 45,990"  "₹1,299.00"  "1,29,999"  "MRP ₹59,999"
    """
    if raw is None:
        return None
    text = str(raw)
    text = text.replace("₹", " ").replace("Rs.", " ").replace("Rs", " ")
    text = re.sub(r"(?i)\b(mrp|m\.r\.p\.?|price|from|starting|at)\b", " ", text)
    text = re.sub(r"[^\d.,]", "", text)
    # Strip orphaned separators. "M.R.P.: ₹79,900" leaves a leading "." once the
    # label is removed, which would otherwise parse as ₹0.79.
    text = text.strip(".,")
    if not text:
        return None

    # Indian grouping (1,29,999) and decimals both use ',' and '.'. Strip commas,
    # then keep only a trailing 2-dp decimal if one is present.
    text = text.replace(",", "")
    if text.count(".") > 1:
        head, _, tail = text.rpartition(".")
        text = head.replace(".", "") + "." + tail
    try:
        value = float(text)
    except ValueError:
        return None

    # Sanity bounds: below ₹1 or above ₹100,00,000 is a parse error, not a price.
    if value < 1 or value > 10_000_000:
        return None
    return round(value, 2)


def parse_int(raw: str | None) -> int | None:
    if raw is None:
        return None
    m = re.search(r"\d[\d,]*", str(raw))
    if not m:
        return None
    try:
        return int(m.group(0).replace(",", ""))
    except ValueError:
        return None
