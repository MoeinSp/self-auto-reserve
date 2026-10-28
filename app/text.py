"""Persian text helpers: the site mixes Arabic/Persian letters and digits."""
import re

_TRANS = str.maketrans({
    "ي": "ی", "ى": "ی", "ك": "ک", "ة": "ه", "أ": "ا", "إ": "ا", "ۀ": "ه",
    **{chr(0x06F0 + i): str(i) for i in range(10)},  # Persian digits
    **{chr(0x0660 + i): str(i) for i in range(10)},  # Arabic digits
    "‌": " ", "‏": "", "‎": "", "ـ": "",
})


def norm(text: str) -> str:
    """Normalize for matching: unify letters/digits, collapse spaces."""
    return re.sub(r"\s+", " ", (text or "").translate(_TRANS)).strip()


def matches(option_text: str, wanted: str) -> bool:
    """True if every word of `wanted` appears in the option text; a word starting with "-" must not."""
    opt = norm(option_text)
    words = norm(wanted).split()
    return any(not w.startswith("-") for w in words) and all(
        (w[1:] not in opt) if w.startswith("-") and len(w) > 1 else (w in opt) for w in words
    )


def parse_foods(raw: str) -> list[str]:
    return [line.strip() for line in (raw or "").splitlines() if line.strip()]
