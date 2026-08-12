"""Retailer adapters."""

from whisky_tracker.retailers.base import RetailerAdapter, RetailerError
from whisky_tracker.retailers.carrefour import CarrefourAdapter, CarrefourLocationError
from whisky_tracker.retailers.coto import CotoAdapter, CotoSchemaError
from whisky_tracker.retailers.jumbo import JumboAdapter
from whisky_tracker.retailers.mercadolibre import (
    MercadoLibreAdapter,
    MercadoLibreAuth,
    MercadoLibreCredentialsError,
    MercadoLibreError,
    MercadoLibreSchemaError,
)
from whisky_tracker.retailers.vtex import VtexSchemaError

__all__ = [
    "CarrefourAdapter",
    "CarrefourLocationError",
    "CotoAdapter",
    "CotoSchemaError",
    "JumboAdapter",
    "MercadoLibreAdapter",
    "MercadoLibreAuth",
    "MercadoLibreCredentialsError",
    "MercadoLibreError",
    "MercadoLibreSchemaError",
    "RetailerAdapter",
    "RetailerError",
    "VtexSchemaError",
]
