"""Small shared filters for unmistakable non-whisky search results."""

import re

_GLASSWARE_TITLE = re.compile(
    r"^\s*(?:set\s+(?:de\s+)?|juego\s+(?:de\s+)?)?(?:vasos?|copas?)\b",
    re.IGNORECASE,
)
_NON_WHISKY_PRODUCT = re.compile(r"\blicor\b", re.IGNORECASE)


def is_obvious_non_whisky_title(title: str) -> bool:
    """Reject accessories only when the title clearly identifies glassware first."""
    return bool(_GLASSWARE_TITLE.search(title) or _NON_WHISKY_PRODUCT.search(title))
