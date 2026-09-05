"""OpenAI multi-judge risk agent with an explicitly labelled offline fallback."""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from functools import lru_cache
from typing import Any, Literal

from pydantic import create_model
from pydantic_ai import Agent, ModelRetry
from pydantic_ai.messages import (
    ModelMessage,
    ModelResponse,
    ToolCallPart,
    UserPromptPart,
)
from pydantic_ai.models.function import AgentInfo, FunctionModel

from .core import (
    DATA_DIR,
    PACKAGE_DIR,
    build_assessment,
    load_risk_catalog,
    load_scenario,
)
from .schemas import (
    AggregatedRiskMatch,
    Assessment,
    JudgeConsensus,
    JudgeVote,
    SubmitRiskJudgment,
    SubmitRiskJudgmentMatch,
)
from .settings import SETTINGS

PROMPT_VERSION = "openingguard-agent-v3-few-shot"
KB_VERSION = "risk-catalog-v2"
PROMPT_EXAMPLES_FILE = DATA_DIR / "prompt_examples.json"
JUDGE_INSTRUCTIONS_FILE = PACKAGE_DIR / "prompts" / "judge_instructions.txt"
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
JUDGE_RETRIES = 2


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


@lru_cache
def load_prompt_examples() -> dict[str, Any]:
    """Load and validate human-approved few-shot examples separately from eval data."""
    payload = json.loads(PROMPT_EXAMPLES_FILE.read_text(encoding="utf-8"))
    examples = payload.get("examples")
    if not payload.get("prompt_examples_version") or not isinstance(examples, list):
        raise ValueError("prompt_examples.json 缺少版本或 examples")
    known_risks = {item["risk_id"] for item in load_risk_catalog()}
    seen_ids: set[str] = set()
    seen_notes: set[str] = set()
    for example in examples:
        example_id = str(example.get("example_id", ""))
        operation_note = str(example.get("operation_note", ""))
        matches = example.get("risk_matches", [])
        if not example_id or example_id in seen_ids:
            raise ValueError("prompt example_id 必須存在且不可重複")
        if not operation_note or operation_note in seen_notes:
            raise ValueError("prompt operation_note 必須存在且不可重複")
        if bool(example.get("no_confident_match")) == bool(matches):
            raise ValueError(
                "prompt example 的 no_confident_match 與 risk_matches 不一致"
            )
        seen_ids.add(example_id)
        seen_notes.add(operation_note)
        seen_risks: set[str] = set()
        for match in matches:
            risk_id = str(match.get("risk_id", ""))
            quote = str(match.get("matched_input_text", ""))
            if risk_id not in known_risks or risk_id in seen_risks:
                raise ValueError(f"prompt example 含未知或重複 risk_id：{risk_id}")
            if not quote or quote not in operation_note:
                raise ValueError(
                    "prompt example 的 matched_input_text 必須逐字出現在備註"
                )
            # Validate examples through the judges' own output schema: an example
            # the schema would reject is an example teaching the model to fail.
            SubmitRiskJudgmentMatch.model_validate(match)
            seen_risks.add(risk_id)
    return payload


@lru_cache
def _judgment_model(risk_ids: tuple[str, ...]) -> type[SubmitRiskJudgment]:
    """Subclass with risk_id narrowed to the current catalog; cached per catalog snapshot."""
    scoped_match = create_model(
        "ScopedSubmitRiskJudgmentMatch",
        __base__=SubmitRiskJudgmentMatch,
        risk_id=(Literal[risk_ids], ...),
    )
    return create_model(
        "ScopedSubmitRiskJudgment",
        __base__=SubmitRiskJudgment,
        risk_matches=(list[scoped_match], ...),
    )


def _aggregate_judgments(judgments: list[JudgeVote]) -> JudgeConsensus:
    by_risk: dict[str, list[tuple[str, SubmitRiskJudgmentMatch]]] = defaultdict(list)
    for judgment in judgments:
        for match in judgment.risk_matches:
            by_risk[match.risk_id].append((judgment.judge_id, match))

    aggregated: list[AggregatedRiskMatch] = []
    for risk_id, votes in by_risk.items():
        severity_counts: Counter[Literal["low", "medium", "high"]] = Counter(
            match.severity for _, match in votes
        )
        majority: list[Literal["low", "medium", "high"]] = [
            name for name, count in severity_counts.items() if count >= 2
        ]
        risk_has_majority = len(votes) >= 2
        severity: Literal["low", "medium", "high", "uncertain"] = (
            majority[0] if risk_has_majority and len(majority) == 1 else "uncertain"
        )
        simulation_assumption: Literal["low", "medium", "high"] = (
            "high" if severity == "uncertain" else severity
        )
        aggregated.append(
            AggregatedRiskMatch(
                risk_id=risk_id,
                severity=severity,
                simulation_assumption=simulation_assumption,
                selected_by_judges=len(votes),
                judge_count=len(judgments),
                severity_votes={
                    label: severity_counts.get(label, 0) for label in SEVERITY_ORDER
                },
                binary_vote_sums={
                    "is_at_least_medium": sum(
                        match.is_at_least_medium for _, match in votes
                    ),
                    "is_high": sum(match.is_high for _, match in votes),
                },
                matched_input_text=votes[0][1].matched_input_text,
                evidence_quotes=list(
                    dict.fromkeys(match.matched_input_text for _, match in votes)
                ),
                reason="；".join(
                    f"{judge_id}: {match.reason}" for judge_id, match in votes
                ),
            )
        )

    aggregated.sort(
        key=lambda item: (
            -item.selected_by_judges,
            -(item.severity_votes["high"] * 2 + item.severity_votes["medium"]),
            item.risk_id,
        )
    )
    aggregated = aggregated[:3]
    has_uncertain = any(item.severity == "uncertain" for item in aggregated)
    confirmed = [item for item in aggregated if item.severity != "uncertain"]
    if has_uncertain:
        decision_status = "uncertain"
    elif confirmed:
        decision_status = "confirmed"
    else:
        decision_status = "no_match"
    return JudgeConsensus(
        risk_matches=aggregated,
        no_confident_match=not bool(confirmed),
        decision_status=decision_status,
        requires_human_review=has_uncertain,
        judge_count=len(judgments),
        judgments=judgments,
    )


@lru_cache
def _judge_instructions_template() -> str:
    return JUDGE_INSTRUCTIONS_FILE.read_text(encoding="utf-8")


def _judge_instructions(judge_id: str, perspective: str) -> str:
    """Stable rubric prompt; the strict output schema remains in the tools field."""
    return _judge_instructions_template().format(
        judge_id=judge_id, perspective=perspective
    )


def _mock_judgment(operation_note: str) -> dict[str, Any]:
    """Deterministic stand-in for a judge's tool call; keyword-matched, not LLM reasoning."""
    lowered = operation_note.lower()
    matches = []
    for item in load_risk_catalog():
        hits = [
            signal for signal in item["trigger_signals"] if signal.lower() in lowered
        ]
        if hits and len(matches) < 3:
            matches.append(
                {
                    "risk_id": item["risk_id"],
                    "is_at_least_medium": 1,
                    "is_high": 0,
                    "matched_input_text": hits[0],
                    "reason": f"mock judge：關鍵字「{hits[0]}」命中 {item['risk_id']}",
                }
            )
    return {"risk_matches": matches, "no_confident_match": not matches}


def _mock_model_function(
    messages: list[ModelMessage], info: AgentInfo
) -> ModelResponse:
    user_part = messages[-1].parts[-1]
    assert isinstance(user_part, UserPromptPart)
    assert isinstance(user_part.content, str)
    note = json.loads(user_part.content)["target_operation_note"]
    tool = info.output_tools[0]
    return ModelResponse(
        parts=[ToolCallPart(tool_name=tool.name, args=_mock_judgment(note))]
    )


def _judge_agent(
    judge_id: str, perspective: str, operation_note: str
) -> Agent[None, Any]:
    """Build a judge whose output validator can reject a quote against this note."""
    risk_ids = tuple(item["risk_id"] for item in load_risk_catalog())
    agent = Agent(
        model=f"openai:{SETTINGS.agent.openai_model}",
        output_type=_judgment_model(risk_ids),
        instructions=_judge_instructions(judge_id, perspective),
        retries=JUDGE_RETRIES,
        # Resolve the provider lazily so the offline mock can override the model
        # without an OpenAI key present at construction time.
        defer_model_check=True,
    )

    @agent.output_validator
    def _quotes_must_come_from_the_note(
        judgment: SubmitRiskJudgment,
    ) -> SubmitRiskJudgment:
        # A quote the note does not contain is a fabricated citation; hand it back
        # to the judge instead of letting it reach the capacity assumptions.
        for match in judgment.risk_matches:
            if match.matched_input_text not in operation_note:
                raise ModelRetry(
                    f"matched_input_text 必須逐字出現在營運備註中："
                    f"{match.matched_input_text!r} 不在備註裡。"
                )
        return judgment

    return agent


def _run_judge(agent: Agent[None, Any], payload: str) -> SubmitRiskJudgment:
    """Run one judge in its own thread.

    `Agent.override` is contextvar-based, so the offline stand-in must be entered
    inside the worker thread rather than around the whole fan-out.
    """
    override = (
        agent.override(
            model=FunctionModel(_mock_model_function, model_name="mock-judge")
        )
        if SETTINGS.agent.provider == "mock"
        else nullcontext()
    )
    with override:
        return agent.run_sync(payload).output


def _judge_payload(operation_note: str, scenario_id: str) -> str:
    scenario = load_scenario(scenario_id)
    prompt_examples = load_prompt_examples()
    return json.dumps(
        {
            "risk_catalog": _catalog_for_prompt(),
            "human_labeled_examples": {
                "prompt_examples_version": prompt_examples["prompt_examples_version"],
                "labeling_policy_version": prompt_examples["labeling_policy_version"],
                "examples": prompt_examples["examples"],
            },
            "structured_scenario": {
                "scenario_id": scenario_id,
                **scenario.model_dump(
                    include={"scenario_version", "traffic", "capacity", "slo", "retry"}
                ),
            },
            "target_operation_note": operation_note,
        },
        ensure_ascii=False,
    )


def _run_judges(operation_note: str, payload: str) -> JudgeConsensus:
    agents = [
        _judge_agent(judge_id, perspective, operation_note)
        for judge_id, perspective in JUDGE_ROLES
    ]
    with ThreadPoolExecutor(max_workers=len(agents)) as executor:
        outputs = [
            future.result()
            for future in [
                executor.submit(_run_judge, agent, payload) for agent in agents
            ]
        ]
    judgments = [
        JudgeVote(
            judge_id=judge_id,
            risk_matches=output.risk_matches,
            no_confident_match=output.no_confident_match,
        )
        for (judge_id, _), output in zip(JUDGE_ROLES, outputs, strict=True)
    ]
    consensus = _aggregate_judgments(judgments)
    consensus.model = SETTINGS.agent.openai_model
    return consensus


def _llm_select(operation_note: str, scenario_id: str) -> JudgeConsensus:
    return _run_judges(operation_note, _judge_payload(operation_note, scenario_id))


def _public_event_payload(
    operation_note: str, event_metadata: dict[str, Any]
) -> dict[str, Any]:
    """Build the Agent input without simulation truth or hidden future traffic."""
    prompt_examples = load_prompt_examples()
    allowed_metadata = {
        key: event_metadata[key]
        for key in (
            "event_id",
            "published_at",
            "available_at",
            "expected_start",
            "source",
        )
        if key in event_metadata
    }
    return {
        "risk_catalog": _catalog_for_prompt(),
        "human_labeled_examples": {
            "prompt_examples_version": prompt_examples["prompt_examples_version"],
            "labeling_policy_version": prompt_examples["labeling_policy_version"],
            "examples": prompt_examples["examples"],
        },
        "public_event_metadata": allowed_metadata,
        "target_operation_note": operation_note,
        "information_boundary": (
            "只能使用截至 available_at 已公開的事件資料；沒有模擬真值、未來 RPS 或實際尖峰結果。"
        ),
    }


def select_public_event(
    operation_note: str, event_metadata: dict[str, Any]
) -> JudgeConsensus:
    """Run real OpenAI judges using only timestamped public event information."""
    if SETTINGS.agent.provider != "openai":
        raise RuntimeError("公開事件的 OpenAI 評測不可使用 mock provider")
    if not SETTINGS.agent.openai_api_key:
        raise RuntimeError("缺少 OPENAI_API_KEY，不能將 fixture 冒充成 LLM Agent 結果")
    return _run_judges(
        operation_note,
        json.dumps(
            _public_event_payload(operation_note, event_metadata), ensure_ascii=False
        ),
    )


def run_agent_assessment(
    scenario_id: str,
    operation_note: str,
    runs: int = 500,
    seed: int = 20260904,
) -> Assessment:
    if SETTINGS.agent.provider != "mock" and not SETTINGS.agent.openai_api_key:
        raise RuntimeError("缺少 OPENAI_API_KEY；離線 demo 請設定 AGENT_PROVIDER=mock")
    consensus = _llm_select(operation_note, scenario_id)
    return build_assessment(
        scenario_id,
        runs,
        seed,
        consensus.selected_risks,
        apply_risks=True,
        requires_human_review=consensus.requires_human_review,
    )


def _macro_f1(expected: list[str], predicted: list[str]) -> float:
    scores = []
    for label in SEVERITY_ORDER:
        tp = sum(
            e == label and p == label for e, p in zip(expected, predicted, strict=True)
        )
        fp = sum(
            e != label and p == label for e, p in zip(expected, predicted, strict=True)
        )
        fn = sum(
            e == label and p != label for e, p in zip(expected, predicted, strict=True)
        )
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
    if not SETTINGS.agent.openai_api_key:
        raise RuntimeError("缺少 OPENAI_API_KEY；離線備援不能冒充 LLM 評測")
    prompt_examples = load_prompt_examples()
    cases = json.loads((DATA_DIR / "eval_cases.json").read_text(encoding="utf-8"))
    prompt_notes = {
        " ".join(example["operation_note"].split()).casefold()
        for example in prompt_examples["examples"]
    }
    eval_notes = {" ".join(case["operation_note"].split()).casefold() for case in cases}
    overlap = prompt_notes & eval_notes
    if overlap:
        raise RuntimeError("prompt examples 與 held-out eval cases 發生資料洩漏")
    top1_hits = 0
    top3_hits = 0
    no_match_total = 0
    no_match_false_positives = 0
    expected_severities: list[str] = []
    predicted_severities: list[str] = []
    details = []
    for case in cases:
        selection = _llm_select(case["operation_note"], "normal")
        predicted = [item.risk_id for item in selection.risk_matches]
        predicted_by_id = {
            item.risk_id: item.severity for item in selection.risk_matches
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
                bool(predicted) or not selection.no_confident_match
            )
        details.append(
            {
                **case,
                "predicted_risk_ids": predicted,
                "predicted_severities": predicted_by_id,
                "raw_selection": selection.model_dump(),
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
        "model": SETTINGS.agent.openai_model,
        "prompt_version": PROMPT_VERSION,
        "prompt_examples_version": prompt_examples["prompt_examples_version"],
        "prompt_example_count": len(prompt_examples["examples"]),
        "evaluation_split": "held_out_not_in_prompt",
        "knowledge_base_version": KB_VERSION,
        "cases": len(cases),
        "top1_accuracy": top1_hits / matched_cases if matched_cases else 0.0,
        "top3_recall": top3_hits / matched_cases if matched_cases else 0.0,
        "no_match_false_positive_rate": (
            no_match_false_positives / no_match_total if no_match_total else 0.0
        ),
        "severity_accuracy": correct / len(expected_severities)
        if expected_severities
        else 0.0,
        "severity_macro_f1": (
            _macro_f1(expected_severities, predicted_severities)
            if expected_severities
            else 0.0
        ),
        "severity_coverage": covered / len(expected_severities)
        if expected_severities
        else 0.0,
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
