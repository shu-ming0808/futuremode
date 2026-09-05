"""Environment-driven deployment configuration.

Only values that vary between environments belong here. Code-level constants
that define simulation semantics (Monte Carlo profiles, judge roles, prompt
versions) stay as module constants in core.py / agent.py.
"""

from __future__ import annotations

from typing import Annotated

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class MockOrderSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="MOCK_")

    order_concurrency: int = Field(default=20, gt=0, alias="MOCK_ORDER_CONCURRENCY")
    validation_mean_ms: float = Field(default=10.0, gt=0)
    validation_sigma: float = Field(default=0.15, gt=0)
    database_mean_ms: float = Field(default=90.0, gt=0)
    database_sigma: float = Field(default=0.35, gt=0)
    gateway_mean_ms: float = Field(default=100.0, gt=0)
    gateway_sigma: float = Field(default=0.45, gt=0)

    @property
    def concurrency(self) -> int:
        return self.order_concurrency


class APISettings(BaseSettings):
    cors_origins: Annotated[list[str], NoDecode] = Field(
        default=[
            "http://127.0.0.1:3000",
            "http://localhost:3000",
            "http://127.0.0.1:5173",
            "http://localhost:5173",
        ]
    )

    @field_validator("cors_origins", mode="before")
    @classmethod
    def _split_comma_separated(cls, value: object) -> object:
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        return value


class AgentSettings(BaseSettings):
    openai_api_key: str | None = Field(default=None)
    openai_model: str = Field(default="gpt-5.1")
    openai_judge_models: Annotated[list[str], NoDecode] = Field(default_factory=list)

    @field_validator("openai_judge_models", mode="before")
    @classmethod
    def _split_comma_separated(cls, value: object) -> object:
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        return value


class Settings(BaseSettings):
    """Root settings; each field is populated from its own nested model's
    environment variables (no shared prefix, since MOCK_/CORS_/OPENAI_ vars
    are already unambiguous on their own)."""

    model_config = SettingsConfigDict(extra="ignore")

    mock_order: MockOrderSettings = Field(default_factory=MockOrderSettings)
    api: APISettings = Field(default_factory=APISettings)
    agent: AgentSettings = Field(default_factory=AgentSettings)


SETTINGS = Settings()
