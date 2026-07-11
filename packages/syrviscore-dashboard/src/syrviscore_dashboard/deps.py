"""FastAPI dependency providers backed by ``app.state``."""

from fastapi import Request

from .aggregator import HealthAggregator
from .settings import DashboardSettings


def get_settings_dep(request: Request) -> DashboardSettings:
    return request.app.state.settings


def get_aggregator(request: Request) -> HealthAggregator:
    return request.app.state.aggregator
