"""Minimal FastAPI shell for frontend integration."""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import ValidationError

from . import __version__
from .agent import run_agent_assessment
from .core import MONTE_CARLO_PROFILES, build_assessment, list_scenarios, load_scenario
from .mock_order import SETTINGS as MOCK_ORDER_SETTINGS
from .mock_order import process_mock_order
from .schemas import Assessment, MockOrderRequest, SimulationRequest
from .settings import SETTINGS

app = FastAPI(
    title="OpeningGuard AI Prototype",
    version=__version__,
    description="Synthetic FIFO order-capacity simulator; not a production trading service.",
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=SETTINGS.api.cors_origins,
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)


@app.get("/api/health")
def health() -> dict[str, Any]:
    return {
        "status": "ok",
        "version": __version__,
        "prototype": True,
        "openai_api_key_present": bool(SETTINGS.agent.openai_api_key),
        "scenarios": list_scenarios(),
        "mock_order_concurrency": MOCK_ORDER_SETTINGS.concurrency,
    }


@app.post("/api/simulate")
def simulate(body: SimulationRequest) -> Assessment:
    try:
        scenario = load_scenario(body.scenario)
        operation_note = body.operation_note or scenario.operation_note
        runs = body.runs or MONTE_CARLO_PROFILES[body.profile]
        run_profile = "custom" if body.runs is not None else body.profile
        if body.use_agent:
            return run_agent_assessment(
                body.scenario,
                operation_note,
                runs,
                body.seed,
                run_profile=run_profile,
            )
        return build_assessment(
            body.scenario,
            runs,
            body.seed,
            run_profile=run_profile,
        )
    except (ValueError, ValidationError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.post("/api/mock-orders")
async def mock_order(body: MockOrderRequest) -> dict[str, Any]:
    """Measure a local mock pipeline; never sends an order to a real market."""
    result = await process_mock_order(body.order_id)
    return {"order_id": str(body.order_id), **result}


def main() -> None:
    import uvicorn

    uvicorn.run("main:app", host="127.0.0.1", port=8000, reload=False)


if __name__ == "__main__":
    main()
