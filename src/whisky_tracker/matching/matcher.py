"""Conservative hierarchical product grouping."""

from collections import defaultdict
from collections.abc import Iterable
from difflib import SequenceMatcher
from hashlib import sha256

from whisky_tracker.matching.models import (
    CanonicalProduct,
    ListingIdentity,
    ManualOverrides,
    MatchConfidence,
    MatchingResult,
    ProductMatchGroup,
)
from whisky_tracker.matching.normalization import (
    NormalizedObservation,
    extract_known_expression,
    normalize_observation,
)
from whisky_tracker.models.product import ProductObservation

_RANK = {
    MatchConfidence.EXACT_GTIN: 0,
    MatchConfidence.STRONG_ATTRIBUTES: 1,
    MatchConfidence.FUZZY_SUPPORTED: 2,
    MatchConfidence.MANUAL: 3,
}


class ProductMatcher:
    """Group observations only when identity evidence is strong and compatible."""

    def __init__(
        self, *, overrides: ManualOverrides | None = None, fuzzy_threshold: float = 0.90
    ) -> None:
        if not 0.0 <= fuzzy_threshold <= 1.0:
            raise ValueError("fuzzy_threshold must be between zero and one")
        self.overrides = overrides or ManualOverrides()
        self.fuzzy_threshold = fuzzy_threshold

    def match(self, observations: Iterable[ProductObservation]) -> MatchingResult:
        normalized = sorted(
            (normalize_observation(item) for item in observations),
            key=lambda item: ListingIdentity.from_observation(item.observation),
        )
        parent = list(range(len(normalized)))
        group_methods: dict[int, set[MatchConfidence]] = defaultdict(set)

        def root(index: int) -> int:
            while parent[index] != index:
                parent[index] = parent[parent[index]]
                index = parent[index]
            return index

        def members(index: int) -> list[int]:
            target = root(index)
            return [candidate for candidate in range(len(parent)) if root(candidate) == target]

        def merge(left: int, right: int, confidence: MatchConfidence) -> None:
            left_root, right_root = root(left), root(right)
            if left_root == right_root:
                group_methods[left_root].add(confidence)
                return
            parent[right_root] = left_root
            group_methods[left_root] |= group_methods.pop(right_root, set())
            group_methods[left_root].add(confidence)

        # Manual force rules are intentional exceptions and therefore run before evidence rules.
        for left in range(len(normalized)):
            for right in range(left + 1, len(normalized)):
                if self._manual_pair(normalized[left], normalized[right]) is True and all(
                    self._manual_pair(normalized[a], normalized[b]) is not False
                    for a in members(left)
                    for b in members(right)
                ):
                    merge(left, right, MatchConfidence.MANUAL)

        candidates: list[tuple[int, int, int, MatchConfidence]] = []
        for left in range(len(normalized)):
            for right in range(left + 1, len(normalized)):
                confidence = self._classify(normalized[left], normalized[right])
                if confidence is not None:
                    candidates.append((_RANK[confidence], left, right, confidence))

        for _rank, left, right, confidence in sorted(candidates):
            if root(left) == root(right):
                continue
            if all(
                self._groups_compatible(normalized[a], normalized[b])
                for a in members(left)
                for b in members(right)
            ):
                merge(left, right, confidence)

        grouped: dict[int, list[NormalizedObservation]] = defaultdict(list)
        for index, item in enumerate(normalized):
            grouped[root(index)].append(item)

        groups: list[ProductMatchGroup] = []
        unmatched: list[ProductObservation] = []
        for group_root, items in grouped.items():
            if len(items) == 1:
                unmatched.append(items[0].observation)
                continue
            methods = group_methods[group_root]
            confidence = max(methods, key=_RANK.__getitem__)
            groups.append(
                ProductMatchGroup(
                    canonical_product=self._canonical(items),
                    observations=tuple(item.observation for item in items),
                    match_confidence=confidence,
                    match_reason=self._reason(methods),
                )
            )
        groups.sort(key=lambda group: group.canonical_product.canonical_id)
        return MatchingResult(tuple(groups), tuple(unmatched))

    def _classify(
        self, left: NormalizedObservation, right: NormalizedObservation
    ) -> MatchConfidence | None:
        manual = self._manual_pair(left, right)
        if manual is False or not self._structured_compatible(left, right):
            return None
        if left.gtin and left.gtin == right.gtin:
            return MatchConfidence.EXACT_GTIN
        if self._strong_attributes(left, right):
            return MatchConfidence.STRONG_ATTRIBUTES
        if self._fuzzy_supported(left, right):
            return MatchConfidence.FUZZY_SUPPORTED
        return None

    def _manual_pair(
        self, left: NormalizedObservation, right: NormalizedObservation
    ) -> bool | None:
        left_identity = ListingIdentity.from_observation(left.observation)
        right_identity = ListingIdentity.from_observation(right.observation)
        if left_identity == right_identity:
            return None
        pair = ManualOverrides.pair(left_identity, right_identity)
        if pair in self.overrides.force_non_match:
            return False
        if pair in self.overrides.force_match:
            return True
        return None

    def _groups_compatible(self, left: NormalizedObservation, right: NormalizedObservation) -> bool:
        manual = self._manual_pair(left, right)
        return manual is not False and (manual is True or self._classify(left, right) is not None)

    @staticmethod
    def _structured_compatible(left: NormalizedObservation, right: NormalizedObservation) -> bool:
        for attribute in ("volume_ml", "pack_count", "age_statement"):
            left_value = getattr(left, attribute)
            right_value = getattr(right, attribute)
            if left_value is not None and right_value is not None and left_value != right_value:
                return False
        return True

    @staticmethod
    def _strong_attributes(left: NormalizedObservation, right: NormalizedObservation) -> bool:
        required = ("brand", "expression", "volume_ml", "pack_count")
        return all(
            getattr(left, attribute) is not None
            and getattr(left, attribute) == getattr(right, attribute)
            for attribute in required
        )

    def _fuzzy_supported(self, left: NormalizedObservation, right: NormalizedObservation) -> bool:
        left_known = extract_known_expression(left.observation.title)
        right_known = extract_known_expression(right.observation.title)
        if (
            not left.brand
            or left.brand != right.brand
            or not left.expression
            or not right.expression
            or (left_known and right_known and left_known != right_known)
        ):
            return False
        if left.pack_count is None or left.pack_count != right.pack_count:
            return False
        if left.volume_ml is None and right.volume_ml is None:
            return False
        similarity = SequenceMatcher(None, left.title, right.title, autojunk=False).ratio()
        expression_similarity = SequenceMatcher(
            None, left.expression, right.expression, autojunk=False
        ).ratio()
        return similarity >= self.fuzzy_threshold and expression_similarity >= self.fuzzy_threshold

    @staticmethod
    def _canonical(items: list[NormalizedObservation]) -> CanonicalProduct:
        def consensus(attribute: str):
            values = {
                getattr(item, attribute) for item in items if getattr(item, attribute) is not None
            }
            return next(iter(values)) if len(values) == 1 else None

        known_expressions = [extract_known_expression(item.observation.title) for item in items]
        known_expressions = [value for value in known_expressions if value]
        expressions = [item.expression for item in items if item.expression]
        expression = _shared_expression(known_expressions) or _shared_expression(expressions)
        gtins = frozenset(item.gtin for item in items if item.gtin)
        parts = (
            consensus("brand") or "unknown-brand",
            expression or "unknown-expression",
            str(consensus("age_statement") or "unknown-age"),
            str(consensus("volume_ml") or "unknown-volume"),
            str(consensus("pack_count") or "unknown-pack"),
            ",".join(sorted(gtins)),
        )
        digest = sha256("|".join(parts).encode()).hexdigest()[:16]
        return CanonicalProduct(
            canonical_id=f"whisky-{digest}",
            brand=consensus("brand"),
            expression=expression,
            age_statement=consensus("age_statement"),
            volume_ml=consensus("volume_ml"),
            pack_count=consensus("pack_count"),
            gtins=gtins,
        )

    @staticmethod
    def _reason(methods: set[MatchConfidence]) -> str:
        ordered = sorted(methods, key=_RANK.__getitem__)
        labels = {
            MatchConfidence.EXACT_GTIN: "shared valid GTIN",
            MatchConfidence.STRONG_ATTRIBUTES: "brand, expression, volume, and pack agree",
            MatchConfidence.FUZZY_SUPPORTED: (
                "compatible attributes with conservative title similarity"
            ),
            MatchConfidence.MANUAL: "explicit manual force-match rule",
        }
        return "; ".join(labels[method] for method in ordered)


def _shared_expression(expressions: list[str]) -> str | None:
    if not expressions:
        return None
    if len(set(expressions)) == 1:
        return expressions[0]
    first = expressions[0].split()
    shared = [token for token in first if all(token in value.split() for value in expressions[1:])]
    return " ".join(dict.fromkeys(shared)) or None
