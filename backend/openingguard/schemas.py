"""API request schemas used by the packaged backend."""

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field


class SimulationRequest(BaseModel):
    scenario: str = "normal"
    runs: int = Field(default=30, ge=1, le=500)
    operation_note: str | None = None
    use_agent: bool = False
    seed: int = 20260904


class MockOrderRequest(BaseModel):
    order_id: UUID
    account_id: str = Field(min_length=1, max_length=64)
    symbol: str = Field(min_length=1, max_length=32)
    side: Literal["buy", "sell"]
    quantity: int = Field(gt=0, le=1_000_000)
