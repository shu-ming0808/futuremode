"""OpenAI multi-judge risk agent with an explicitly labelled offline fallback."""

from __future__ import annotations

from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
import json
import os
from typing import Any

from .core import DATA_DIR, build_assessment, load_risk_catalog, load_scenario


DEFAULT_MODEL = "gpt-5.1"
PROMPT_VERSION = "openingguard-agent-v2"
KB_VERSION = "risk-catalog-v2"
JUDGE_ROLES = (
    (
        "market_evidence",
        "從市場事件與下單需求的角度檢查證據，但仍須依相同容量影響標準分級。",
    ),
    (
        "system_capacity",
        "從 Queue、worker、Database 與交易閘道容量的角度檢查證據，但仍須依相同容量影響標準分級。",
    ),
    (
        "risk_auditor",
        "採懷疑態度檢查是否有足夠原文證據，避免將模糊敘述過度解讀。",
    ),
)
SEVERITY_ORDER = ("low", "medium", "high")


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


def _judge_tool_schema() -> dict[str, Any]:
    risk_ids = [item["risk_id"] for item in load_risk_catalog()]
    return {
        "type": "function",
        "name": "submit_risk_judgment",
        "description": (
            "Select up to three catalog risk IDs and classify capacity impact using "
            "two ordered binary decisions. Never invent numeric multipliers or capacity."
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
                            "is_at_least_medium": {"type": "integer", "enum": [0, 1]},
                            "is_high": {"type": "integer", "enum": [0, 1]},
                            "matched_input_text": {"type": "string"},
                            "reason": {"type": "string"},
                        },
                        "required": [
                            "risk_id",
                            "is_at_least_medium",
                            "is_high",
                            "matched_input_text",
                            "reason",
                        ],
                        "additionalProperties": False,
                    },
                },
                "no_confident_match": {"type": "boolean"},
            },
            "required": ["risk_matches", "no_confident_match"],
            "additionalProperties": False,
        },
    }


def _bits_to_severity(is_at_least_medium: int, is_high: int) -> str:
    if is_high and not is_at_least_medium:
        raise ValueError("is_high=1 時，is_at_least_medium 必須為 1")
    if is_high:
        return "high"
    if is_at_least_medium:
        return "medium"
    return "low"


def _validate_judgment(
    judgment: dict[str, Any], operation_note: str, judge_id: str
) -> dict[str, Any]:
    known = {item["risk_id"] for item in load_risk_catalog()}
    matches = judgment.get("risk_matches", [])
    if bool(judgment.get("no_confident_match")) == bool(matches):
        raise ValueError("no_confident_match 必須與 risk_matches 是否為空一致")
    seen: set[str] = set()
    validated: list[dict[str, Any]] = []
    for match in matches:
        risk_id = str(match["risk_id"])
        if risk_id not in known:
            raise ValueError(f"Agent 回傳未知 risk_id：{risk_id}")
        if risk_id in seen:
            raise ValueError(f"同一 judge 重複回傳 risk_id：{risk_id}")
        seen.add(risk_id)
        quote = str(match["matched_input_text"])
        if not quote or quote not in operation_note:
            raise ValueError("Agent 的 matched_input_text 必須是營運備註的非空原文")
        severity = _bits_to_severity(
            int(match["is_at_least_medium"]), int(match["is_high"])
        )
        validated.append({**match, "severity": severity, "judge_id": judge_id})
    return {
        "judge_id": judge_id,
        "risk_matches": validated,
        "no_confident_match": not bool(validated),
    }


def _aggregate_judgments(judgments: list[dict[str, Any]]) -> dict[str, Any]:
    by_risk: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for judgment in judgments:
        for match in judgment["risk_matches"]:
            by_risk[match["risk_id"]].append(match)

    aggregated: list[dict[str, Any]] = []
    for risk_id, votes in by_risk.items():
        severity_counts = Counter(vote["severity"] for vote in votes)
        majority = [name for name, count in severity_counts.items() if count >= 2]
        risk_has_majority = len(votes) >= 2
        severity = majority[0] if risk_has_majority and len(majority) == 1 else "uncertain"
        aggregated.append(
            {
                "risk_id": risk_id,
                "severity": severity,
                "simulation_assumption": "high" if severity == "uncertain" else severity,
                "selected_by_judges": len(votes),
                "judge_count": len(judgments),
                "severity_votes": {
                    label: severity_counts.get(label, 0) for label in SEVERITY_ORDER
                },
                "binary_vote_sums": {
                    "is_at_least_medium": sum(
                        int(vote["is_at_least_medium"]) for vote in votes
                    ),
                    "is_high": sum(int(vote["is_high"]) for vote in votes),
                },
                "matched_input_text": votes[0]["matched_input_text"],
                "evidence_quotes": list(
                    dict.fromkeys(vote["matched_input_text"] for vote in votes)
                ),
                "reason": "；".join(
                    f"{vote['judge_id']}: {vote['reason']}" for vote in votes
                ),
            }
        )

    aggregated.sort(
        key=lambda item: (
            -item["selected_by_judges"],
            -(item["severity_votes"]["high"] * 2 + item["severity_votes"]["medium"]),
            item["risk_id"],
        )
    )
    aggregated = aggregated[:3]
    has_uncertain = any(item["severity"] == "uncertain" for item in aggregated)
    confirmed = [item for item in aggregated if item["severity"] != "uncertain"]
    if has_uncertain:
        decision_status = "uncertain"
    elif confirmed:
        decision_status = "confirmed"
    else:
        decision_status = "no_match"
    return {
        "risk_matches": aggregated,
        "no_confident_match": not bool(confirmed),
        "decision_status": decision_status,
        "requires_human_review": has_uncertain,
        "auto_approved": False,
        "judge_count": len(judgments),
        "judgments": judgments,
    }


def _offline_select(operation_note: str) -> dict[str, Any]:
    """Demo continuity only. This is not presented as an LLM result."""
    lowered = operation_note.lower()
    if any(phrase in lowered for phrase in ("沒有部署或已知", "沒有回報任何", "例行營運")):
        return {
            "risk_matches": [],
            "no_confident_match": True,
            "decision_status": "no_match",
            "requires_human_review": False,
            "auto_approved": False,
        }
    scored: list[tuple[int, dict[str, Any], str]] = []
    for item in load_risk_catalog():
        hits = [signal for signal in item["trigger_signals"] if signal.lower() in lowered]
        if hits:
            scored.append((len(hits), item, hits[0]))
    scored.sort(key=lambda row: (-row[0], row[1]["risk_id"]))
    matches = [
        {
            "risk_id": item["risk_id"],
            "severity": "uncertain",
            "simulation_assumption": "high",
            "selected_by_judges": 0,
            "judge_count": 0,
            "severity_votes": {"low": 0, "medium": 0, "high": 0},
            "matched_input_text": hit,
            "evidence_quotes": [hit],
            "reason": f"離線關鍵字只辨識到「{hit}」，不能代替 LLM 嚴重度判斷。",
        }
        for _, item, hit in scored[:3]
    ]
    return {
        "risk_matches": matches,
        "no_confident_match": True,
        "decision_status": "uncertain" if matches else "no_match",
        "requires_human_review": bool(matches),
        "auto_approved": False,
    }


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


def _judge_models() -> list[str]:
    configured = [
        item.strip()
        for item in os.getenv("OPENAI_JUDGE_MODELS", "").split(",")
        if item.strip()
    ]
    if not configured:
        configured = [os.getenv("OPENAI_MODEL", DEFAULT_MODEL)]
    if len(configured) == 1:
        return configured * len(JUDGE_ROLES)
    if len(configured) != len(JUDGE_ROLES):
        raise ValueError("OPENAI_JUDGE_MODELS 必須提供一個模型，或依三個 judge 提供三個模型")
    return configured


def _llm_judge(
    operation_note: str,
    scenario_id: str,
    judge_id: str,
    perspective: str,
    model: str,
) -> tuple[dict[str, Any], Any]:
    from openai import OpenAI

    scenario = load_scenario(scenario_id)
    payload = {
        "structured_scenario": {
            "scenario_id": scenario_id,
            "scenario_version": scenario["scenario_version"],
            "traffic": scenario["traffic"],
            "capacity": scenario["capacity"],
            "slo": scenario["slo"],
            "retry": scenario["retry"],
        },
        "operation_note": operation_note,
        "risk_catalog": _catalog_for_prompt(),
    }
    response = OpenAI().responses.create(
        model=model,
        instructions=(
            f"你是券商內部容量風險 judge，角色為 {judge_id}。{perspective} "
            "severity 只代表事件對系統容量的影響，不代表發生機率。low 表示容量影響有限；"
            "medium 表示可能明顯增加 Queue 或資源使用；high 表示可能造成逾時、下游飽和或違反 SLO。"
            "從目錄選最多三個 risk_id，並用兩個有序 0/1 欄位表達 severity。"
            "is_high=1 時 is_at_least_medium 必須為 1。matched_input_text 必須逐字來自 operation_note。"
            "沒有可信證據時傳空陣列並將 no_confident_match 設為 true。"
            "不可產生倍率、RPS 或 worker 數，不可創造目錄以外的 risk_id，也不可聲稱委託已成交。"
            "最後必須呼叫 submit_risk_judgment。"
        ),
        input=json.dumps(payload, ensure_ascii=False),
        tools=[_judge_tool_schema()],
        tool_choice={"type": "function", "name": "submit_risk_judgment"},
        parallel_tool_calls=False,
    )
    calls = [item for item in response.output if item.type == "function_call"]
    if len(calls) != 1 or calls[0].name != "submit_risk_judgment":
        raise RuntimeError(f"{judge_id} 未依規格提交風險判斷")
    raw = json.loads(calls[0].arguments)
    return _validate_judgment(raw, operation_note, judge_id), response


def _llm_select(operation_note: str, scenario_id: str) -> tuple[dict[str, Any], list[Any]]:
    models = _judge_models()
    with ThreadPoolExecutor(max_workers=len(JUDGE_ROLES)) as executor:
        futures = [
            executor.submit(
                _llm_judge,
                operation_note,
                scenario_id,
                judge_id,
                perspective,
                model,
            )
            for (judge_id, perspective), model in zip(JUDGE_ROLES, models, strict=True)
        ]
        results = [future.result() for future in futures]
    judgments = [result[0] for result in results]
    responses = [result[1] for result in results]
    selection = _aggregate_judgments(judgments)
    selection["models"] = models
    selection["committee_mode"] = (
        "same_model_multi_judge" if len(set(models)) == 1 else "multi_model_openai_judges"
    )
    return selection, responses


def _deterministic_explanation(assessment: dict[str, Any]) -> str:
    risks = assessment.get("risk_matches", [])
    if risks:
        labels = "、".join(
            f"{item['risk_id']}（{item['severity']}）" for item in risks
        )
    else:
        labels = "沒有形成可信風險匹配"
    if assessment["recommended"]:
        plan = f"預熱至 {assessment['recommended']['workers']} 個 worker"
    else:
        plan = assessment["warning"]
    return (
        f"Synthetic 容量評估：{labels}。保守模擬結果建議：{plan}。"
        "本結果只代表交易閘道回傳委託已接受，不代表訂單已成交；等待工程師人工核准。"
    )


def run_agent_assessment(
    scenario_id: str,
    operation_note: str,
    runs: int = 500,
    seed: int = 20260904,
    allow_offline_fallback: bool = True,
    run_profile: str = "custom",
) -> dict[str, Any]:
    if not os.getenv("OPENAI_API_KEY"):
        if not allow_offline_fallback:
            raise RuntimeError("缺少 OPENAI_API_KEY，不能執行 LLM Agent")
        selection = _offline_select(operation_note)
        matches = _enrich_matches(selection["risk_matches"])
        assessment = build_assessment(
            scenario_id,
            runs,
            seed,
            matches,
            apply_risks=True,
            run_profile=run_profile,
        )
        assessment["agent"] = {
            "mode": "offline_fallback",
            "is_llm_result": False,
            "decision_status": selection["decision_status"],
            "reason": "OPENAI_API_KEY 未設定；此結果不能用作 Agent Demo 或評測證據。",
            "prompt_version": PROMPT_VERSION,
            "knowledge_base_version": KB_VERSION,
        }
        return assessment

    configured_models = _judge_models()
    try:
        selection, judge_responses = _llm_select(operation_note, scenario_id)
        matches = _enrich_matches(selection["risk_matches"])
        assessment = build_assessment(
            scenario_id,
            runs,
            seed,
            matches,
            apply_risks=True,
            run_profile=run_profile,
        )
        assessment["agent"] = {
            "mode": "openai_multi_judge_tool_calling",
            "is_llm_result": True,
            "committee_mode": selection["committee_mode"],
            "models": selection["models"],
            "judge_count": selection["judge_count"],
            "decision_status": selection["decision_status"],
            "requires_human_review": selection["requires_human_review"],
            "auto_approved": False,
            "prompt_version": PROMPT_VERSION,
            "knowledge_base_version": KB_VERSION,
            "tool_called": "submit_risk_judgment",
            "judge_outputs": selection["judgments"],
            "final_explanation": _deterministic_explanation(assessment),
            "response_ids": [response.id for response in judge_responses],
        }
        return assessment
    except Exception as exc:
        if not allow_offline_fallback:
            raise
        selection = _offline_select(operation_note)
        matches = _enrich_matches(selection["risk_matches"])
        assessment = build_assessment(
            scenario_id,
            runs,
            seed,
            matches,
            apply_risks=True,
            run_profile=run_profile,
        )
        assessment["agent"] = {
            "mode": "offline_fallback_after_api_error",
            "is_llm_result": False,
            "models_requested": configured_models,
            "decision_status": selection["decision_status"],
            "reason": f"OpenAI API 呼叫失敗：{type(exc).__name__}: {exc}",
            "prompt_version": PROMPT_VERSION,
            "knowledge_base_version": KB_VERSION,
        }
        return assessment


def _macro_f1(expected: list[str], predicted: list[str]) -> float:
    scores = []
    for label in SEVERITY_ORDER:
        tp = sum(e == label and p == label for e, p in zip(expected, predicted, strict=True))
        fp = sum(e != label and p == label for e, p in zip(expected, predicted, strict=True))
        fn = sum(e == label and p != label for e, p in zip(expected, predicted, strict=True))
        denominator = 2 * tp + fp + fn
        scores.append((2 * tp / denominator) if denominator else 0.0)
    return sum(scores) / len(scores)


def _weighted_kappa(expected: list[str], predicted: list[str]) -> float | None:
    covered = [
        (e, p)
        for e, p in zip(expected, predicted, strict=True)
        if e in SEVERITY_ORDER and p in SEVERITY_ORDER
    ]
    if not covered:
        return None
    size = len(SEVERITY_ORDER)
    observed = [[0.0] * size for _ in range(size)]
    expected_counts = Counter(e for e, _ in covered)
    predicted_counts = Counter(p for _, p in covered)
    for expected_label, predicted_label in covered:
        observed[SEVERITY_ORDER.index(expected_label)][
            SEVERITY_ORDER.index(predicted_label)
        ] += 1
    n = len(covered)
    observed_disagreement = 0.0
    expected_disagreement = 0.0
    for i, expected_label in enumerate(SEVERITY_ORDER):
        for j, predicted_label in enumerate(SEVERITY_ORDER):
            weight = ((i - j) / (size - 1)) ** 2
            observed_disagreement += weight * observed[i][j] / n
            expected_disagreement += (
                weight
                * expected_counts[expected_label]
                * predicted_counts[predicted_label]
                / (n * n)
            )
    if expected_disagreement == 0:
        return 1.0 if observed_disagreement == 0 else 0.0
    return 1 - observed_disagreement / expected_disagreement


def evaluate_llm() -> dict[str, Any]:
    if not os.getenv("OPENAI_API_KEY"):
        raise RuntimeError("缺少 OPENAI_API_KEY；離線備援不能冒充 LLM 評測")
    cases = json.loads((DATA_DIR / "eval_cases.json").read_text(encoding="utf-8"))
    top1_hits = 0
    top3_hits = 0
    no_match_total = 0
    no_match_false_positives = 0
    expected_severities: list[str] = []
    predicted_severities: list[str] = []
    details = []
    for case in cases:
        selection, _ = _llm_select(case["operation_note"], "normal")
        predicted = [item["risk_id"] for item in selection["risk_matches"]]
        predicted_by_id = {
            item["risk_id"]: item["severity"] for item in selection["risk_matches"]
        }
        expected = case["expected_risk_ids"]
        if expected:
            top1_hits += int(bool(predicted) and predicted[0] in expected)
            top3_hits += int(any(risk_id in predicted[:3] for risk_id in expected))
            for risk_id, severity in case.get("expected_severities", {}).items():
                expected_severities.append(severity)
                predicted_severities.append(predicted_by_id.get(risk_id, "uncertain"))
        else:
            no_match_total += 1
            no_match_false_positives += int(
                bool(predicted) or not selection["no_confident_match"]
            )
        details.append(
            {
                **case,
                "predicted_risk_ids": predicted,
                "predicted_severities": predicted_by_id,
                "raw_selection": selection,
            }
        )
    matched_cases = sum(bool(case["expected_risk_ids"]) for case in cases)
    covered = sum(value in SEVERITY_ORDER for value in predicted_severities)
    correct = sum(
        expected == predicted
        for expected, predicted in zip(
            expected_severities, predicted_severities, strict=True
        )
    )
    confusion_labels = (*SEVERITY_ORDER, "uncertain")
    confusion_matrix = {
        expected_label: {
            predicted_label: sum(
                expected == expected_label and predicted == predicted_label
                for expected, predicted in zip(
                    expected_severities, predicted_severities, strict=True
                )
            )
            for predicted_label in confusion_labels
        }
        for expected_label in SEVERITY_ORDER
    }
    return {
        "models": _judge_models(),
        "prompt_version": PROMPT_VERSION,
        "knowledge_base_version": KB_VERSION,
        "cases": len(cases),
        "top1_accuracy": top1_hits / matched_cases if matched_cases else 0.0,
        "top3_recall": top3_hits / matched_cases if matched_cases else 0.0,
        "no_match_false_positive_rate": (
            no_match_false_positives / no_match_total if no_match_total else 0.0
        ),
        "severity_accuracy": correct / len(expected_severities) if expected_severities else 0.0,
        "severity_macro_f1": (
            _macro_f1(expected_severities, predicted_severities)
            if expected_severities
            else 0.0
        ),
        "severity_coverage": covered / len(expected_severities) if expected_severities else 0.0,
        "severity_confusion_matrix": confusion_matrix,
        "severity_quadratic_weighted_kappa_on_covered": _weighted_kappa(
            expected_severities, predicted_severities
        ),
        "details": details,
        "scope_warning": (
            "只代表此固定知識庫與小型人工測試集，不是一般化準確率；"
            "同模型 judge 具有相關性，票數不是獨立統計樣本。"
        ),
    }
