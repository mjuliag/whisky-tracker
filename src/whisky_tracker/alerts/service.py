"""Policy service that evaluates persisted observations for alert signals."""

import hashlib
import json
from decimal import Decimal

from whisky_tracker.alerts.models import (
    Alert,
    AlertConfig,
    AlertType,
    ComparisonPrice,
    PromotionEvidence,
)
from whisky_tracker.matching.models import CanonicalProduct
from whisky_tracker.matching.normalization import pack_from_observation, volume_from_observation
from whisky_tracker.models.product import ProductObservation
from whisky_tracker.models.promotion import DiscountType, PromotionKind
from whisky_tracker.persistence import HistoryFilter, ListingKey, SQLiteRepository
from whisky_tracker.persistence.models import StoredObservation

_CONDITIONAL_PROMOTIONS = {
    PromotionKind.LOYALTY,
    PromotionKind.PAYMENT_METHOD,
    PromotionKind.QUANTITY,
    PromotionKind.COUPON,
    PromotionKind.SHIPPING,
}


class AlertEngine:
    """Evaluate one already-persisted observation without sending notifications."""

    def __init__(self, repository: SQLiteRepository, *, config: AlertConfig | None = None) -> None:
        self.repository = repository
        self.config = config or AlertConfig()

    def evaluate_observation(
        self,
        observation: ProductObservation,
        *,
        canonical_product: CanonicalProduct | None = None,
    ) -> Alert | None:
        if self.config.require_in_stock and not observation.in_stock:
            return None

        listing = ListingKey(
            observation.retailer,
            observation.retailer_product_id,
            observation.retailer_sku_id,
        )
        history = self.repository.get_price_history(
            HistoryFilter(listing=listing, context=observation.context)
        )
        prior = tuple(item for item in history if item.observed_at < observation.observed_at)
        comparable_prior = tuple(item for item in prior if item.currency == observation.currency)
        previous = comparable_prior[-1] if comparable_prior else None
        prior_minimum = (
            min((item.current_price for item in comparable_prior), default=None)
            if comparable_prior
            else None
        )

        signals: set[AlertType] = set()
        reasons: list[str] = []
        price_change = None
        percentage_change = None
        if previous is not None:
            price_change = observation.current_price - previous.current_price
            if previous.current_price != 0:
                percentage_change = price_change / previous.current_price * Decimal(100)
            drop = -percentage_change if percentage_change is not None else Decimal(0)
            if price_change < 0 and drop >= self.config.minimum_price_drop_percentage:
                signals.add(AlertType.PRICE_DROP)
                reasons.append(f"price fell {drop:.2f}% from the previous comparable observation")

        if prior_minimum is not None and observation.current_price < prior_minimum:
            signals.add(AlertType.HISTORICAL_LOW)
            reasons.append("price is below every prior comparable observation")

        comparison_prices = self._cross_retailer_prices(observation, canonical_product)
        other_prices = [
            item.price for item in comparison_prices if item.retailer != observation.retailer
        ]
        if other_prices:
            next_best = min(other_prices)
            difference = (next_best - observation.current_price) / next_best * Decimal(100)
            own_prices = [
                item.price for item in comparison_prices if item.retailer == observation.retailer
            ]
            if (
                observation.current_price <= min(own_prices, default=observation.current_price)
                and observation.current_price < next_best
                and difference >= self.config.minimum_cross_retailer_difference_percentage
            ):
                signals.add(AlertType.CROSS_RETAILER_DEAL)
                reasons.append(f"price is {difference:.2f}% below the next-best retailer")

        qualifying_promotions = self._promotion_evidence(observation)
        if any(
            item.discount_percentage >= self.config.minimum_promotion_discount_percentage
            for item in qualifying_promotions
        ):
            signals.add(AlertType.PROMOTION)
            reasons.append("structured promotion meets the configured discount threshold")

        if not signals:
            return None
        fingerprint = self._fingerprint(
            observation, canonical_product, signals, qualifying_promotions
        )
        if self.repository.is_alert_sent(fingerprint):
            return None
        alert = Alert(
            canonical_product=canonical_product,
            observation=observation,
            alert_types=frozenset(signals),
            current_price=observation.current_price,
            previous_price=previous.current_price if previous else None,
            historical_minimum=prior_minimum,
            price_change=price_change,
            percentage_change=percentage_change,
            comparison_prices=comparison_prices,
            qualifying_promotions=qualifying_promotions,
            reason="; ".join(reasons),
            fingerprint=fingerprint,
        )
        self.repository.record_alert_candidate(
            fingerprint=fingerprint,
            listing=listing,
            observed_at=observation.observed_at,
            context=observation.context,
            canonical_id=canonical_product.canonical_id if canonical_product else None,
            alert_types=tuple(sorted(signal.value for signal in signals)),
            price=observation.current_price,
            currency=observation.currency,
        )
        return alert

    def mark_sent(self, alert: Alert) -> None:
        self.repository.mark_alert_sent(alert.fingerprint)

    def _cross_retailer_prices(
        self,
        observation: ProductObservation,
        canonical_product: CanonicalProduct | None,
    ) -> tuple[ComparisonPrice, ...]:
        volume = volume_from_observation(observation)
        pack = pack_from_observation(observation)
        if (
            canonical_product is None
            or volume is None
            or pack is None
            or canonical_product.volume_ml != volume
            or canonical_product.pack_count != pack
        ):
            return ()
        latest = self.repository.get_latest_canonical_observations(canonical_product.canonical_id)
        compatible = [
            item
            for item in latest
            if item.in_stock
            and item.currency == observation.currency
            and item.volume_ml == volume
            and item.pack_count == pack
        ]
        best_by_retailer: dict[str, StoredObservation] = {}
        for item in compatible:
            current = best_by_retailer.get(item.listing.retailer)
            if current is None or item.current_price < current.current_price:
                best_by_retailer[item.listing.retailer] = item
        return tuple(
            ComparisonPrice(
                retailer=item.listing.retailer,
                price=item.current_price,
                currency=item.currency,
                context=item.context,
                product_url=item.product_url,
            )
            for item in sorted(best_by_retailer.values(), key=lambda value: value.current_price)
        )

    def _promotion_evidence(self, observation: ProductObservation) -> tuple[PromotionEvidence, ...]:
        result = []
        for promotion in observation.promotions:
            percentage = None
            if (
                promotion.discount_type is DiscountType.PERCENTAGE
                and promotion.discount_value is not None
            ):
                percentage = promotion.discount_value
            elif (
                promotion.discount_type is DiscountType.FIXED_AMOUNT
                and promotion.discount_value is not None
                and observation.regular_price is not None
                and observation.regular_price > 0
            ):
                percentage = promotion.discount_value / observation.regular_price * Decimal(100)
            if percentage is not None:
                result.append(
                    PromotionEvidence(
                        promotion=promotion,
                        discount_percentage=percentage,
                        conditional=promotion.kind in _CONDITIONAL_PROMOTIONS,
                    )
                )
        return tuple(result)

    @staticmethod
    def _fingerprint(
        observation: ProductObservation,
        canonical_product: CanonicalProduct | None,
        signals: set[AlertType],
        promotions: tuple[PromotionEvidence, ...],
    ) -> str:
        context = observation.context
        promotion_identity = sorted(
            (
                item.promotion.kind.value,
                item.promotion.name,
                str(item.discount_percentage.normalize()),
                item.promotion.conditions,
            )
            for item in promotions
        )
        payload = (
            canonical_product.canonical_id if canonical_product else None,
            observation.retailer,
            observation.retailer_product_id,
            observation.retailer_sku_id,
            context.fulfillment_mode.value,
            context.context_resolution.value,
            context.postal_code,
            context.sales_channel,
            context.region_id,
            context.seller_id,
            context.store_id,
            str(observation.current_price.normalize()),
            observation.currency,
            sorted(signal.value for signal in signals),
            promotion_identity,
        )
        encoded = json.dumps(payload, ensure_ascii=True, separators=(",", ":"))
        return hashlib.sha256(encoded.encode()).hexdigest()
