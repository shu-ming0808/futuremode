"""Local-only mock order service used for capacity calibration."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import hashlib
import math
import os
from statistics import NormalDist
from time import perf_counter
from uuid import UUID


def _positive_float(name: str, default: float) -> float:
    value = float(os.getenv(name, default))
    if value <= 0:
        raise ValueError(f"{name} must be greater than zero")
    return value


def _positive_int(name: str, default: int) -> int:
    value = int(os.getenv(name, default))
    if value <= 0:
        raise ValueError(f"{name} must be greater than zero")
    return value


@dataclass(frozen=True)
class MockOrderSettings:
    concurrency: int
    validation_mean_ms: float
    validation_sigma: float
    database_mean_ms: float
    database_sigma: float
    gateway_mean_ms: float
    gateway_sigma: float

    @classmethod
    def from_environment(cls) -> "MockOrderSettings":
        return cls(
            concurrency=_positive_int("MOCK_ORDER_CONCURRENCY", 20),
            validation_mean_ms=_positive_float("MOCK_VALIDATION_MEAN_MS", 10.0),
            validation_sigma=_positive_float("MOCK_VALIDATION_SIGMA", 0.15),
            database_mean_ms=_positive_float("MOCK_DATABASE_MEAN_MS", 90.0),
            database_sigma=_positive_float("MOCK_DATABASE_SIGMA", 0.35),
            gateway_mean_ms=_positive_float("MOCK_GATEWAY_MEAN_MS", 100.0),
            gateway_sigma=_positive_float("MOCK_GATEWAY_SIGMA", 0.45),
        )


SETTINGS = MockOrderSettings.from_environment()
CAPACITY_GATE = asyncio.Semaphore(SETTINGS.concurrency)


def _deterministic_lognormal_ms(
    order_id: UUID, stage: str, mean_ms: float, sigma: float
) -> float:
    """Generate a repeatable positive delay whose arithmetic mean is mean_ms."""
    digest = hashlib.sha256(f"{order_id}:{stage}".encode()).digest()
    raw = int.from_bytes(digest[:8], "big")
    uniform = min(max((raw + 0.5) / (2**64), 1e-12), 1 - 1e-12)
    normal = NormalDist().inv_cdf(uniform)
    mu = math.log(mean_ms) - 0.5 * sigma**2
    return math.exp(mu + sigma * normal)


async def process_mock_order(order_id: UUID) -> dict[str, float | int | str]:
    """Simulate validation, database, and gateway work without a real trade."""
    request_started = perf_counter()
    wait_started = request_started
    async with CAPACITY_GATE:
        acquired = perf_counter()
        queue_wait_ms = (acquired - wait_started) * 1000

        validation_ms = _deterministic_lognormal_ms(
            order_id,
            "validation",
            SETTINGS.validation_mean_ms,
            SETTINGS.validation_sigma,
        )
        await asyncio.sleep(validation_ms / 1000)

        database_ms = _deterministic_lognormal_ms(
            order_id,
            "database",
            SETTINGS.database_mean_ms,
            SETTINGS.database_sigma,
        )
        await asyncio.sleep(database_ms / 1000)

        gateway_ms = _deterministic_lognormal_ms(
            order_id,
            "gateway",
            SETTINGS.gateway_mean_ms,
            SETTINGS.gateway_sigma,
        )
        await asyncio.sleep(gateway_ms / 1000)

    total_ms = (perf_counter() - request_started) * 1000
    return {
        "status": "accepted",
        "mock_only": True,
        "server_concurrency": SETTINGS.concurrency,
        "queue_wait_ms": round(queue_wait_ms, 3),
        "validation_ms": round(validation_ms, 3),
        "database_ms": round(database_ms, 3),
        "gateway_ms": round(gateway_ms, 3),
        "simulated_work_ms": round(validation_ms + database_ms + gateway_ms, 3),
        "server_total_ms": round(total_ms, 3),
    }
