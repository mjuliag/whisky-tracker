"""Policy service that evaluates persisted observations for alert signals."""

import hashlib
import json
from dataclasses import replace
from decimal import Decimal

from whisky_tracker.alerts.models import (
    Alert,
    AlertConfig,
    AlertType,
    ComparisonPrice,
    ProductAlert,
    ProductOffer,
    PromotionEvidence,
)
from whisky_tracker.matching.models import CanonicalProduct
from whisky_tracker.matching.normalization import (
    extract_age_statement,
    extract_known_expression,
    pack_from_observation,
    volume_from_observation,
)
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

    def evaluate_product(
        self,
        canonical_product: CanonicalProduct,
        observations: tuple[ProductObservation, ...],
    ) -> ProductAlert | None:
        """Consolidate a canonical product's current retailer offers into one candidate."""
        selected = self._select_current_offers(canonical_product, observations)
        if not selected:
            return None

        analyzed = [self._analyze_offer(item) for item in selected]
        analyzed = [item for item in analyzed if item is not None]
        if not analyzed:
            return None

        currency_counts: dict[str, int] = {}
        for offer in analyzed:
            currency = offer.observation.currency
            currency_counts[currency] = currency_counts.get(currency, 0) + 1
        primary_currency = min(
            currency_counts,
            key=lambda currency: (-currency_counts[currency], currency),
        )
        comparable = [offer for offer in analyzed if offer.observation.currency == primary_currency]
        comparable.sort(
            key=lambda offer: (
                offer.observation.current_price,
                offer.observation.retailer.casefold(),
                offer.observation.retailer_product_id,
                offer.observation.retailer_sku_id,
            )
        )
        best = comparable[0]
        second = comparable[1] if len(comparable) > 1 else None
        if second is not None and second.observation.current_price > best.observation.current_price:
            difference = second.observation.current_price - best.observation.current_price
            percentage = difference / second.observation.current_price * Decimal(100)
            if percentage >= self.config.minimum_cross_retailer_difference_percentage:
                best = replace(
                    best,
                    alert_types=best.alert_types | {AlertType.CROSS_RETAILER_DEAL},
                    reasons=(
                        *best.reasons,
                        f"price is {percentage:.2f}% below the next-best retailer",
                    ),
                    is_best_price=True,
                )
            else:
                best = replace(best, is_best_price=True)
        else:
            difference = None
            percentage = None
            best = replace(best, is_best_price=True)
        comparable[0] = best

        signals = frozenset(signal for offer in comparable for signal in offer.alert_types)
        if not signals:
            return None
        fingerprint = self._product_fingerprint(canonical_product, comparable, best)
        if self.repository.is_alert_sent(fingerprint, model_version=2):
            return None
        alert = ProductAlert(
            canonical_product=canonical_product,
            current_observations=observations,
            offers=tuple(comparable),
            alert_types=signals,
            best_offer=best,
            second_best_offer=second,
            savings_amount=difference,
            savings_percentage=percentage,
            reason="; ".join(reason for offer in comparable for reason in offer.reasons),
            fingerprint=fingerprint,
        )
        observation = best.observation
        self.repository.record_alert_candidate(
            fingerprint=fingerprint,
            listing=ListingKey(
                observation.retailer,
                observation.retailer_product_id,
                observation.retailer_sku_id,
            ),
            observed_at=observation.observed_at,
            context=observation.context,
            canonical_id=canonical_product.canonical_id,
            alert_types=tuple(sorted(signal.value for signal in signals)),
            price=observation.current_price,
            currency=observation.currency,
            model_version=2,
        )
        return alert

    def _select_current_offers(
        self,
        canonical_product: CanonicalProduct,
        observations: tuple[ProductObservation, ...],
    ) -> tuple[ProductObservation, ...]:
        best_by_retailer_currency: dict[tuple[str, str], ProductObservation] = {}
        canonical_expression = extract_known_expression(canonical_product.expression or "")
        for observation in observations:
            if self.config.require_in_stock and not observation.in_stock:
                continue
            if (
                volume_from_observation(observation) != canonical_product.volume_ml
                or pack_from_observation(observation) != canonical_product.pack_count
            ):
                continue
            observed_expression = extract_known_expression(observation.title)
            if (
                canonical_expression
                and observed_expression
                and canonical_expression != observed_expression
            ):
                continue
            observed_age = extract_age_statement(observation.title)
            if (
                canonical_product.age_statement is not None
                and observed_age is not None
                and canonical_product.age_statement != observed_age
            ):
                continue
            key = (observation.retailer, observation.currency)
            current = best_by_retailer_currency.get(key)
            if current is None or (
                observation.current_price,
                -observation.observed_at.timestamp(),
                observation.retailer_product_id,
                observation.retailer_sku_id,
            ) < (
                current.current_price,
                -current.observed_at.timestamp(),
                current.retailer_product_id,
                current.retailer_sku_id,
            ):
                best_by_retailer_currency[key] = observation
        return tuple(best_by_retailer_currency.values())

    def _analyze_offer(self, observation: ProductObservation) -> ProductOffer | None:
        listing = ListingKey(
            observation.retailer,
            observation.retailer_product_id,
            observation.retailer_sku_id,
        )
        history = self.repository.get_price_history(
            HistoryFilter(listing=listing, context=observation.context)
        )
        prior = tuple(
            item
            for item in history
            if item.observed_at < observation.observed_at and item.currency == observation.currency
        )
        previous = prior[-1] if prior else None
        prior_minimum = min((item.current_price for item in prior), default=None)
        signals: set[AlertType] = set()
        reasons: list[str] = []
        price_change = None
        percentage_change = None
        if previous is not None:
            price_change = observation.current_price - previous.current_price
            if previous.current_price:
                percentage_change = price_change / previous.current_price * Decimal(100)
            drop = -percentage_change if percentage_change is not None else Decimal(0)
            if price_change < 0 and drop >= self.config.minimum_price_drop_percentage:
                signals.add(AlertType.PRICE_DROP)
                reasons.append(f"{observation.retailer} price fell {drop:.2f}%")
        if prior_minimum is not None and observation.current_price < prior_minimum:
            signals.add(AlertType.HISTORICAL_LOW)
            reasons.append(f"{observation.retailer} reached a historical low")
        promotions = self._promotion_evidence(observation)
        if any(
            evidence.discount_percentage >= self.config.minimum_promotion_discount_percentage
            for evidence in promotions
        ):
            signals.add(AlertType.PROMOTION)
            reasons.append(f"{observation.retailer} has a qualifying promotion")
        return ProductOffer(
            observation=observation,
            alert_types=frozenset(signals),
            previous_price=previous.current_price if previous else None,
            historical_minimum=prior_minimum,
            price_change=price_change,
            percentage_change=percentage_change,
            qualifying_promotions=promotions,
            reasons=tuple(reasons),
        )

    @staticmethod
    def _product_fingerprint(
        canonical_product: CanonicalProduct,
        offers: list[ProductOffer],
        best_offer: ProductOffer,
    ) -> str:
        offer_state = []
        for offer in offers:
            observation = offer.observation
            context = observation.context
            promotions = sorted(
                (
                    evidence.promotion.kind.value,
                    evidence.promotion.name,
                    str(evidence.discount_percentage.normalize()),
                    evidence.promotion.conditions,
                )
                for evidence in offer.qualifying_promotions
            )
            offer_state.append(
                (
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
                    sorted(signal.value for signal in offer.alert_types),
                    promotions,
                )
            )
        payload = (
            2,
            canonical_product.canonical_id,
            offer_state,
            best_offer.observation.retailer,
            best_offer.observation.retailer_product_id,
            best_offer.observation.retailer_sku_id,
        )
        encoded = json.dumps(payload, ensure_ascii=True, separators=(",", ":"))
        return "v2:" + hashlib.sha256(encoded.encode()).hexdigest()

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
