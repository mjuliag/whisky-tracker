"""Shared validation for product identifiers."""

import re

_GTIN_LENGTHS = {8, 12, 13, 14}


def normalize_gtin(value: str | None) -> str | None:
    """Return a valid GTIN as digits, or ``None`` for malformed input."""
    if not value:
        return None
    digits = re.sub(r"\D", "", value)
    if len(digits) not in _GTIN_LENGTHS or not is_valid_gtin(digits):
        return None
    return digits


def is_valid_gtin(digits: str) -> bool:
    """Validate a GTIN-8, UPC-12, EAN-13, or GTIN-14 check digit."""
    if not digits.isdigit() or len(digits) not in _GTIN_LENGTHS or len(set(digits)) == 1:
        return False
    total = sum(
        int(character) * (3 if index % 2 == 0 else 1)
        for index, character in enumerate(reversed(digits[:-1]))
    )
    return (10 - total % 10) % 10 == int(digits[-1])
