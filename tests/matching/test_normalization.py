import pytest

from whisky_tracker.identifiers import normalize_gtin
from whisky_tracker.matching.normalization import (
    extract_age_statement,
    extract_known_expression,
    extract_pack_count,
    extract_volume_ml,
    normalize_brand,
    normalize_text,
)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("750 ml", 750),
        ("750ml", 750),
        ("750 cc", 750),
        ("0.75 L", 750),
        ("1 litro", 1000),
        ("1000 ml", 1000),
        ("700ml", 700),
    ],
)
def test_extracts_volume_in_milliliters(text: str, expected: int) -> None:
    assert extract_volume_ml(text) == expected


def test_multiple_different_volumes_are_ambiguous() -> None:
    assert extract_volume_ml("botella 750 ml, también disponible 1 L") is None


@pytest.mark.parametrize("text", ["x6", "pack x 6", "6 unidades", "caja de 6"])
def test_extracts_pack_count(text: str) -> None:
    assert extract_pack_count(text) == 6


@pytest.mark.parametrize("text", ["12 años", "12 years", "12 yo", "12 y.o."])
def test_extracts_explicit_age(text: str) -> None:
    assert extract_age_statement(text) == 12


def test_age_does_not_confuse_volume_pack_or_edition() -> None:
    assert extract_age_statement("Edition 18, 750 ml, pack x6") is None


def test_text_and_brand_normalization_is_conservative() -> None:
    assert normalize_brand(" JOHNNIE-WALKER ") == "johnnie walker"
    assert normalize_text("  Black   Label! ") == "black label"


def test_degenerate_gtin_is_rejected() -> None:
    assert normalize_gtin("00000000") is None


def test_live_tennesse_fire_spelling_normalizes_to_known_variant() -> None:
    assert (
        extract_known_expression("Whisky importado Jack Daniels tennesse fire en botella 750 cc.")
        == "tennessee fire"
    )
