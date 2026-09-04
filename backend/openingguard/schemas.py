"""API request schemas used by the packaged backend."""

from pydantic import BaseModel, Field


class SimulationRequest(BaseModel):
    scenario: str = "normal"
    runs: int = Field(default=30, ge=1, le=500)
    operation_note: str | None = None
    use_agent: bool = False
    seed: int = 20260904
