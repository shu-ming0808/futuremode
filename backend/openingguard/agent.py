"""OpenAI tool-calling agent plus an explicitly labelled offline fallback."""

from __future__ import annotations

import json
import os
from typing import Any

from .core import DATA_DIR, build_assessment, load_risk_catalog, load_scenario


DEFAULT_MODEL = "gpt-5.1"
PROMPT_VERSION = "openingguard-agent-v1"
KB_VERSION = "risk-catalog-v1"


def _catalog_for_prompt() -> list[dict[str, Any]]:
    fields = (
        "risk_id",
        "title",
        "trigger_signals",
        "affected_components",
        "failure_mechanism",
        "evidence_strength",
    )
    return [{key: item[key] for key in fields} for item in load_risk_catalog()]


def _tool_schema() -> dict[str, Any]:
    risk_ids = [item["risk_id"] for item in load_risk_catalog()]
    return {
        "type": "function",
        "name": "run_capacity_assessment",
        "description": (
            "Run the deterministic FIFO queue simulation after selecting up to three "
            "relevant risk IDs from the supplied catalog. Never invent a risk ID."
        ),
        "strict": True,
        "parameters": {
            "type": "object",
            "properties": {
                "risk_matches": {
                    "type": "array",
                    "maxItems": 3,
                    "items": {
                        "type": "object",
                        "properties": {
                            "risk_id": {"type": "string", "enum": risk_ids},
                            "relevance_score": {"type": "integer", "minimum": 0, "maximum": 100},
                            "matched_input_text": {"type": "string"},
                            "reason": {"type": "string"},
                        },
                        "required": ["risk_id", "relevance_score", "matched_input_text", "reason"],
                        "additionalProperties": False,
                    },
                },
                "no_confident_match": {"type": "boolean"},
            },
            "required": ["risk_matches", "no_confident_match"],
            "additionalProperties": False,
        },
    }

def _offline_select(operation_note: str) -> dict[str, Any]:
    """Demo continuity only. This is not presented as an LLM result."""
    lowered = operation_note.lower()
    if any(phrase in lowered for phrase in ("沒有部署或已知", "沒有回報任何", "例行營運")):
        return {"risk_matches": [], "no_confident_match": True}
    scored: list[tuple[int, dict[str, Any], str]] = []
    for item in load_risk_catalog():
        hits = [signal for signal in item["trigger_signals"] if signal.lower() in lowered]
        if hits:
            scored.append((len(hits), item, hits[0]))
    scored.sort(key=lambda row: (-row[0], row[1]["risk_id"]))
    matches = [
        {
            "risk_id": item["risk_id"],
            "relevance_score": min(90, 50 + score * 12),
            "matched_input_text": hit,
            "reason": f"離線備援以觸發詞「{hit}」匹配：{item['title']}",
        }
        for score, item, hit in scored[:3]
    ]
    return {"risk_matches": matches, "no_confident_match": not bool(matches)}


def _enrich_matches(matches: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Join trusted metadata by risk_id; never accept model-generated URLs."""
    catalog = {item["risk_id"]: item for item in load_risk_catalog()}
    return [
        {
            **match,
            "title": catalog[match["risk_id"]]["title"],
            "source_url": catalog[match["risk_id"]]["source_url"],
            "evidence_strength": catalog[match["risk_id"]]["evidence_strength"],
        }
        for match in matches
        if match["risk_id"] in catalog
    ]


def _llm_select(operation_note: str, scenario_id: str) -> tuple[dict[str, Any], Any]:
    from openai import OpenAI

    scenario = load_scenario(scenario_id)
    client = OpenAI()
    model = os.getenv("OPENAI_MODEL", DEFAULT_MODEL)
    payload = {
        "structured_scenario": {
            "scenario_id": scenario_id,
            "traffic": scenario["traffic"],
            "capacity": scenario["capacity"],
            "slo": scenario["slo"],
            "retry": scenario["retry"],
        },
        "operation_note": operation_note,
        "risk_catalog": _catalog_for_prompt(),
    }
    response = client.responses.create(
        model=model,
        instructions=(
            "你是券商內部容量規劃 Agent。先閱讀自然語言營運備註，再從提供的風險目錄選最多三個相關 risk_id，"
            "然後必須呼叫 run_capacity_assessment。relevance_score 只表示本次排序，不是發生機率。"
            "matched_input_text 必須逐字來自 operation_note。沒有可信匹配時傳空陣列並將 no_confident_match 設為 true。"
            "不可修改結構化數值、不可創造 risk_id、不可聲稱委託已成交。"
        ),
        input=json.dumps(payload, ensure_ascii=False),
        tools=[_tool_schema()],
        tool_choice={"type": "function", "name": "run_capacity_assessment"},
        parallel_tool_calls=False,
    )
    calls = [item for item in response.output if item.type == "function_call"]
    if len(calls) != 1 or calls[0].name != "run_capacity_assessment":
        raise RuntimeError("Agent 未依規格呼叫容量評估工具")
    args = json.loads(calls[0].arguments)
    known = {item["risk_id"] for item in load_risk_catalog()}
    for match in args["risk_matches"]:
        if match["risk_id"] not in known:
            raise ValueError(f"Agent 回傳未知 risk_id：{match['risk_id']}")
        if match["matched_input_text"] not in operation_note:
            raise ValueError("Agent 的 matched_input_text 不是營運備註原文")
    return args, response


def run_agent_assessment(
    scenario_id: str,
    operation_note: str,
    runs: int = 30,
    seed: int = 20260904,
    allow_offline_fallback: bool = True,
) -> dict[str, Any]:
    model = os.getenv("OPENAI_MODEL", DEFAULT_MODEL)
    if not os.getenv("OPENAI_API_KEY"):
        if not allow_offline_fallback:
            raise RuntimeError("缺少 OPENAI_API_KEY，不能執行 LLM Agent")
        selection = _offline_select(operation_note)
        matches = _enrich_matches(selection["risk_matches"])
        assessment = build_assessment(scenario_id, runs, seed, matches, apply_risks=True)
        assessment["agent"] = {
            "mode": "offline_fallback",
            "is_llm_result": False,
            "reason": "OPENAI_API_KEY 未設定；此結果不能用作 Agent Demo 或評測證據。",
            "prompt_version": PROMPT_VERSION,
            "knowledge_base_version": KB_VERSION,
        }
        return assessment

    try:
        selection, first_response = _llm_select(operation_note, scenario_id)
        matches = _enrich_matches(selection["risk_matches"])
        assessment = build_assessment(scenario_id, runs, seed, matches, apply_risks=True)
        call = next(item for item in first_response.output if item.type == "function_call")
        compact_tool_output = {
            "scenario": assessment["scenario"],
            "scenarios": assessment["scenarios"],
            "recommended": assessment["recommended"],
            "warning": assessment["warning"],
            "approval_status": assessment["approval_status"],
            "synthetic_assumption": True,
        }
        from openai import OpenAI

        client = OpenAI()
        followup_input = [
            {
                "type": "function_call_output",
                "call_id": call.call_id,
                "output": json.dumps(compact_tool_output, ensure_ascii=False),
            }
        ]
        final_response = client.responses.create(
            model=model,
            previous_response_id=first_response.id,
            instructions=(
                "用繁體中文簡短說明工具結果。必須標示 Synthetic、引用確切數值與風險 ID、"
                "區分委託已接受與已成交，最後說明等待工程師人工核准。不要提出工具未計算的 worker 數。"
            ),
            input=followup_input,
            tools=[_tool_schema()],
            tool_choice="none",
        )
        assessment["agent"] = {
            "mode": "openai_responses_tool_calling",
            "is_llm_result": True,
            "model": model,
            "prompt_version": PROMPT_VERSION,
            "knowledge_base_version": KB_VERSION,
            "tool_called": "run_capacity_assessment",
            "tool_arguments": selection,
            "final_explanation": final_response.output_text,
            "response_ids": [first_response.id, final_response.id],
        }
        return assessment
    except Exception as exc:
        if not allow_offline_fallback:
            raise
        selection = _offline_select(operation_note)
        matches = _enrich_matches(selection["risk_matches"])
        assessment = build_assessment(scenario_id, runs, seed, matches, apply_risks=True)
        assessment["agent"] = {
            "mode": "offline_fallback_after_api_error",
            "is_llm_result": False,
            "reason": f"OpenAI API 呼叫失敗：{type(exc).__name__}: {exc}",
            "prompt_version": PROMPT_VERSION,
            "knowledge_base_version": KB_VERSION,
        }
        return assessment


def evaluate_llm() -> dict[str, Any]:
    if not os.getenv("OPENAI_API_KEY"):
        raise RuntimeError("缺少 OPENAI_API_KEY；離線備援不能冒充 LLM 評測")
    cases = json.loads((DATA_DIR / "eval_cases.json").read_text(encoding="utf-8"))
    top1_hits = 0
    top3_hits = 0
    no_match_total = 0
    no_match_false_positives = 0
    details = []
    for case in cases:
        selection, _ = _llm_select(case["operation_note"], "normal")
        predicted = [item["risk_id"] for item in selection["risk_matches"]]
        expected = case["expected_risk_ids"]
        if expected:
            top1_hits += int(bool(predicted) and predicted[0] in expected)
            top3_hits += int(any(risk_id in predicted[:3] for risk_id in expected))
        else:
            no_match_total += 1
            no_match_false_positives += int(bool(predicted) or not selection["no_confident_match"])
        details.append({**case, "predicted_risk_ids": predicted, "raw_selection": selection})
    matched_cases = sum(bool(case["expected_risk_ids"]) for case in cases)
    return {
        "model": os.getenv("OPENAI_MODEL", DEFAULT_MODEL),
        "prompt_version": PROMPT_VERSION,
        "knowledge_base_version": KB_VERSION,
        "cases": len(cases),
        "top1_accuracy": top1_hits / matched_cases if matched_cases else 0.0,
        "top3_recall": top3_hits / matched_cases if matched_cases else 0.0,
        "no_match_false_positive_rate": (
            no_match_false_positives / no_match_total if no_match_total else 0.0
        ),
        "details": details,
        "scope_warning": "只代表此固定知識庫與小型人工測試集，不是一般化準確率。",
    }
