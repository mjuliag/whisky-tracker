"""Small application configuration loader with no retailer-side environment access."""

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path

from whisky_tracker.alerts import AlertConfig
from whisky_tracker.persistence.repository import DEFAULT_DATABASE_PATH


class ConfigurationError(ValueError):
    """Application configuration is malformed."""


@dataclass(frozen=True, slots=True)
class AppConfig:
    database_path: Path = DEFAULT_DATABASE_PATH
    telegram_bot_token: str | None = None
    telegram_chat_id: str | None = None
    carrefour_postal_code: str | None = None
    user_latitude: float | None = None
    user_longitude: float | None = None
    user_postal_code: str | None = None
    mercadolibre_access_token: str | None = None
    mercadolibre_refresh_token: str | None = None
    mercadolibre_client_id: str | None = None
    mercadolibre_client_secret: str | None = None
    alert_config: AlertConfig = field(default_factory=AlertConfig)
    max_notifications_per_run: int = 10
    search_query: str = "whisky"

    @property
    def telegram_enabled(self) -> bool:
        return bool(self.telegram_bot_token and self.telegram_chat_id)

    @property
    def mercadolibre_enabled(self) -> bool:
        return bool(self.mercadolibre_access_token)

    @property
    def user_coordinates(self) -> tuple[float, float] | None:
        """Return longitude/latitude in RetailerContext order."""
        if self.user_latitude is None or self.user_longitude is None:
            return None
        return (self.user_longitude, self.user_latitude)


def load_config(
    *,
    env_file: str | Path = ".env",
    environment: Mapping[str, str] | None = None,
) -> AppConfig:
    """Load a local dotenv file, then overlay process environment values."""
    values = _read_dotenv(Path(env_file))
    values.update(os.environ if environment is None else environment)
    database = _optional(values, "WHISKY_TRACKER_DB_PATH")
    postcode = _optional(values, "CARREFOUR_POSTAL_CODE") or _optional(
        values, "WHISKY_TRACKER_CARREFOUR_POSTAL_CODE"
    )
    latitude = _coordinate(values, "USER_LATITUDE", minimum=-90, maximum=90)
    longitude = _coordinate(values, "USER_LONGITUDE", minimum=-180, maximum=180)
    if (latitude is None) != (longitude is None):
        raise ConfigurationError("USER_LATITUDE and USER_LONGITUDE must be configured together")
    user_postcode = _optional(values, "USER_POSTAL_CODE")
    return AppConfig(
        database_path=Path(database) if database else DEFAULT_DATABASE_PATH,
        telegram_bot_token=_optional(values, "TELEGRAM_BOT_TOKEN"),
        telegram_chat_id=_optional(values, "TELEGRAM_CHAT_ID"),
        carrefour_postal_code=user_postcode or postcode,
        user_latitude=latitude,
        user_longitude=longitude,
        user_postal_code=user_postcode,
        mercadolibre_access_token=_optional(values, "MERCADOLIBRE_ACCESS_TOKEN"),
        mercadolibre_refresh_token=_optional(values, "MERCADOLIBRE_REFRESH_TOKEN"),
        mercadolibre_client_id=_optional(values, "MERCADOLIBRE_CLIENT_ID"),
        mercadolibre_client_secret=_optional(values, "MERCADOLIBRE_CLIENT_SECRET"),
        alert_config=AlertConfig(
            minimum_price_drop_percentage=_decimal(
                values, "MINIMUM_PRICE_DROP_PERCENTAGE", Decimal("10")
            ),
            minimum_cross_retailer_difference_percentage=_decimal(
                values, "MINIMUM_CROSS_RETAILER_DIFFERENCE_PERCENTAGE", Decimal("10")
            ),
            minimum_promotion_discount_percentage=_decimal(
                values, "MINIMUM_PROMOTION_DISCOUNT_PERCENTAGE", Decimal("25")
            ),
        ),
        max_notifications_per_run=_positive_int(values, "MAX_NOTIFICATIONS_PER_RUN", 10),
        search_query=_optional(values, "WHISKY_TRACKER_SEARCH_QUERY") or "whisky",
    )


def _read_dotenv(path: Path) -> dict[str, str]:
    if not path.is_file():
        return {}
    result: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        if key:
            result[key] = value
    return result


def _optional(values: Mapping[str, str], key: str) -> str | None:
    value = values.get(key, "").strip()
    return value or None


def _decimal(values: Mapping[str, str], key: str, default: Decimal) -> Decimal:
    value = _optional(values, key)
    if value is None:
        return default
    try:
        return Decimal(value)
    except InvalidOperation as exc:
        raise ConfigurationError(f"{key} must be a decimal number") from exc


def _positive_int(values: Mapping[str, str], key: str, default: int) -> int:
    value = _optional(values, key)
    if value is None:
        return default
    try:
        parsed = int(value)
    except ValueError as exc:
        raise ConfigurationError(f"{key} must be a positive integer") from exc
    if parsed < 1:
        raise ConfigurationError(f"{key} must be a positive integer")
    return parsed


def _coordinate(
    values: Mapping[str, str], key: str, *, minimum: float, maximum: float
) -> float | None:
    value = _optional(values, key)
    if value is None:
        return None
    try:
        parsed = float(value)
    except ValueError as exc:
        raise ConfigurationError(f"{key} must be a decimal coordinate") from exc
    if not minimum <= parsed <= maximum:
        raise ConfigurationError(f"{key} must be between {minimum:g} and {maximum:g}")
    return parsed
