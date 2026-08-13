from whisky_tracker.product_filter import is_obvious_non_whisky_title


def test_filters_glassware_returned_by_whisky_search() -> None:
    assert is_obvious_non_whisky_title("Vaso Whisky Transparente Cristal 410 Ml")
    assert is_obvious_non_whisky_title("Set de vasos para whisky")


def test_does_not_filter_whisky_gift_that_includes_a_glass() -> None:
    assert not is_obvious_non_whisky_title("Whisky Old N°7 con vaso Jack Daniels 700 ml")


def test_filters_liqueur_returned_by_whisky_search() -> None:
    assert is_obvious_non_whisky_title("Tres Plumas Licor de Café 750 ml")
