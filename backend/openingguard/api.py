"""Minimal FastAPI shell for frontend integration."""

from __future__ import annotations

import os
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from . import __version__
from .agent import run_agent_assessment
from .core import MONTE_CARLO_PROFILES, build_assessment, list_scenarios, load_scenario
from .mock_order import SETTINGS as MOCK_ORDER_SETTINGS, process_mock_order
from .schemas import MockOrderRequest, SimulationRequest


DEFAULT_LOCAL_ORIGINS = ",".join(
    (
        "http://127.0.0.1:3000",
        "http://localhost:3000",
        "http://127.0.0.1:5173",
        "http://localhost:5173",
    )
)


def allowed_origins() -> list[str]:
    return [
        origin.strip()
        for origin in os.getenv("CORS_ORIGINS", DEFAULT_LOCAL_ORIGINS).split(",")
        if origin.strip()
    ]


app = FastAPI(
    title="OpeningGuard AI Prototype",
    version=__version__,
    description="Synthetic FIFO order-capacity simulator; not a production trading service.",
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins(),
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)


@app.get("/api/health")
def health() -> dict[str, Any]:
    return {
        "status": "ok",
        "version": __version__,
        "prototype": True,
        "openai_api_key_present": bool(os.getenv("OPENAI_API_KEY")),
        "scenarios": list_scenarios(),
        "mock_order_concurrency": MOCK_ORDER_SETTINGS.concurrency,
    }


@app.post("/api/simulate")
def simulate(body: SimulationRequest) -> dict[str, Any]:
    try:
        scenario = load_scenario(body.scenario)
        operation_note = body.operation_note or scenario["operation_note"]
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
    except ValueError as exc:
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
