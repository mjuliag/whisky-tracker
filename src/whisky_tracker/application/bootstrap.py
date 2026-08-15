"""Construct concrete one-shot application dependencies from configuration."""

from dataclasses import dataclass

from whisky_tracker.alerts import AlertEngine
from whisky_tracker.application.config import AppConfig
from whisky_tracker.application.runner import RetailerCollection, WhiskyTrackerRunner
from whisky_tracker.matching import ProductMatcher
from whisky_tracker.models import FulfillmentMode, RetailerContext
from whisky_tracker.notifications.telegram import TelegramConfig, TelegramNotifier
from whisky_tracker.persistence import SQLiteRepository
from whisky_tracker.retailers.carrefour import CarrefourAdapter
from whisky_tracker.retailers.coto import CotoAdapter
from whisky_tracker.retailers.jumbo import JumboAdapter
from whisky_tracker.retailers.mercadolibre import MercadoLibreAdapter, MercadoLibreAuth

_RETAILERS = ("carrefour", "coto", "jumbo", "mercadolibre")


@dataclass(slots=True)
class ApplicationRuntime:
    runner: WhiskyTrackerRunner
    repository: SQLiteRepository
    closeables: tuple[object, ...]

    async def close(self) -> None:
        for resource in reversed(self.closeables):
            close = getattr(resource, "aclose", None)
            if close is not None:
                await close()
        self.repository.close()


def build_runtime(
    config: AppConfig, *, selected_retailers: frozenset[str] | None = None
) -> ApplicationRuntime:
    selected = selected_retailers or frozenset(_RETAILERS)
    collections: list[RetailerCollection] = []
    closeables: list[object] = []

    if "carrefour" in selected:
        if config.carrefour_postal_code:
            adapter = CarrefourAdapter()
            closeables.append(adapter)
            collections.append(
                RetailerCollection(
                    "Carrefour",
                    adapter,
                    RetailerContext(
                        fulfillment_mode=FulfillmentMode.SCHEDULED_DELIVERY,
                        postal_code=config.carrefour_postal_code,
                    ),
                )
            )
        else:
            collections.append(
                RetailerCollection("Carrefour", None, skip_reason="postcode_not_configured")
            )
    if "coto" in selected:
        if config.user_coordinates:
            adapter = CotoAdapter()
            closeables.append(adapter)
            collections.append(
                RetailerCollection(
                    "Coto",
                    adapter,
                    RetailerContext(
                        fulfillment_mode=FulfillmentMode.DELIVERY,
                        postal_code=config.user_postal_code,
                        coordinates=config.user_coordinates,
                    ),
                )
            )
        else:
            collections.append(
                RetailerCollection("Coto", None, skip_reason="coordinates_not_configured")
            )
    if "jumbo" in selected:
        if config.user_coordinates:
            adapter = JumboAdapter()
            closeables.append(adapter)
            collections.append(
                RetailerCollection(
                    "Jumbo",
                    adapter,
                    RetailerContext(
                        fulfillment_mode=FulfillmentMode.DELIVERY,
                        postal_code=config.user_postal_code,
                        coordinates=config.user_coordinates,
                    ),
                )
            )
        else:
            collections.append(
                RetailerCollection("Jumbo", None, skip_reason="coordinates_not_configured")
            )
    if "mercadolibre" in selected:
        if config.mercadolibre_enabled:
            adapter = MercadoLibreAdapter(
                auth=MercadoLibreAuth(
                    access_token=config.mercadolibre_access_token,
                    refresh_token=config.mercadolibre_refresh_token,
                    client_id=config.mercadolibre_client_id,
                    client_secret=config.mercadolibre_client_secret,
                )
            )
            closeables.append(adapter)
            collections.append(
                RetailerCollection("Mercado Libre", adapter, adapter.default_context())
            )
        else:
            collections.append(
                RetailerCollection("Mercado Libre", None, skip_reason="credentials_not_configured")
            )

    repository = SQLiteRepository(config.database_path)
    repository.initialize()
    notifier = None
    if config.telegram_enabled:
        notifier = TelegramNotifier(
            config=TelegramConfig(config.telegram_bot_token, config.telegram_chat_id)
        )
        closeables.append(notifier)
    runner = WhiskyTrackerRunner(
        collections=tuple(collections),
        matcher=ProductMatcher(),
        repository=repository,
        alert_engine=AlertEngine(repository, config=config.alert_config),
        notifier=notifier,
        database_path=str(config.database_path),
        query=config.search_query,
        max_notifications_per_run=config.max_notifications_per_run,
    )
    return ApplicationRuntime(runner, repository, tuple(closeables))
