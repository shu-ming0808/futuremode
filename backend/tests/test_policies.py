"""Regression tests for the agreed risk and statistical decision policies."""

import json
import unittest
from argparse import Namespace
from copy import deepcopy

from pydantic_ai.messages import ModelResponse, ToolCallPart
from pydantic_ai.models.function import FunctionModel

from openingguard.agent import (
    _aggregate_judgments,
    _judge_agent,
    _judge_instructions,
    load_prompt_examples,
)
from openingguard.core import (
    DATA_DIR,
    _passes_constraints,
    apply_risk_assumptions,
    load_scenario,
)
from openingguard.schemas import JudgeVote, StrategySummary
from tools.calibrate import CALIBRATION_PROFILES, _passes_rate_rule


def _vote(judge_id: str, severity: str) -> JudgeVote:
    bits = {
        "low": (0, 0),
        "medium": (1, 0),
        "high": (1, 1),
    }[severity]
    return JudgeVote(
        judge_id=judge_id,
        no_confident_match=False,
        risk_matches=[
            {
                "risk_id": "market_volatility_order_spike",
                "is_at_least_medium": bits[0],
                "is_high": bits[1],
                "matched_input_text": "大量送單",
                "reason": "測試票",
            }
        ],
    )


class RiskPolicyTests(unittest.TestCase):
    def test_prompt_examples_cover_rubric_and_are_held_out(self) -> None:
        prompt_document = load_prompt_examples()
        examples = prompt_document["examples"]
        severities = {
            (match["is_at_least_medium"], match["is_high"])
            for example in examples
            for match in example["risk_matches"]
        }
        self.assertTrue({(0, 0), (1, 0), (1, 1)}.issubset(severities))
        self.assertTrue(any(example["no_confident_match"] for example in examples))

        eval_cases = json.loads(
            (DATA_DIR / "eval_cases.json").read_text(encoding="utf-8")
        )
        prompt_notes = {example["operation_note"] for example in examples}
        eval_notes = {case["operation_note"] for case in eval_cases}
        self.assertTrue(prompt_notes.isdisjoint(eval_notes))

    def test_prompt_defines_capacity_impact_and_forbids_numeric_outputs(self) -> None:
        prompt = _judge_instructions("test_judge", "測試觀點。")
        self.assertIn("severity 代表容量影響，不代表事件發生機率", prompt)
        self.assertIn("人工標記範例是分級示範", prompt)
        self.assertIn("不可產生倍率、RPS、worker 數", prompt)

    def test_three_way_severity_split_stays_uncertain_and_previews_high(self) -> None:
        selection = _aggregate_judgments(
            [
                _vote("market", "low"),
                _vote("capacity", "medium"),
                _vote("audit", "high"),
            ]
        )
        match = selection.risk_matches[0]
        self.assertEqual(match.severity, "uncertain")
        self.assertEqual(match.simulation_assumption, "high")
        self.assertTrue(selection.requires_human_review)
        self.assertFalse(selection.auto_approved)

    def test_two_high_votes_form_high_majority(self) -> None:
        selection = _aggregate_judgments(
            [
                _vote("market", "high"),
                _vote("capacity", "high"),
                _vote("audit", "medium"),
            ]
        )
        self.assertEqual(selection.risk_matches[0].severity, "high")
        self.assertEqual(selection.decision_status, "confirmed")

    def test_consensus_exposes_selected_risks_for_the_simulator(self) -> None:
        selection = _aggregate_judgments(
            [_vote("market", "high"), _vote("capacity", "high"), _vote("audit", "high")]
        )
        risks = selection.selected_risks
        self.assertEqual(
            [item.risk_id for item in risks], ["market_volatility_order_spike"]
        )
        self.assertEqual(risks[0].severity, "high")
        self.assertEqual(risks[0].matched_input_text, "大量送單")

    def test_uncertain_risk_uses_catalog_high_effect_without_relabelling(self) -> None:
        scenario = load_scenario("normal")
        adjusted, audit = apply_risk_assumptions(
            scenario,
            [{"risk_id": "market_volatility_order_spike", "severity": "uncertain"}],
        )
        self.assertEqual(
            adjusted.traffic.scenario_multiplier,
            scenario.traffic.scenario_multiplier * 2.0,
        )
        self.assertEqual(audit[0].agent_severity, "uncertain")
        self.assertEqual(audit[0].simulation_assumption, "high")


def _judgment_response(info, quote: str) -> ModelResponse:
    return ModelResponse(
        parts=[
            ToolCallPart(
                tool_name=info.output_tools[0].name,
                args={
                    "risk_matches": [
                        {
                            "risk_id": "market_volatility_order_spike",
                            "is_at_least_medium": 1,
                            "is_high": 0,
                            "matched_input_text": quote,
                            "reason": "測試票",
                        }
                    ],
                    "no_confident_match": False,
                },
            )
        ]
    )


class JudgeOutputTests(unittest.TestCase):
    NOTE = "夜盤大跌，預期大量送單"

    def test_hallucinated_quote_is_sent_back_to_the_model(self) -> None:
        attempts = []

        def flaky(messages, info) -> ModelResponse:
            attempts.append(len(messages))
            quote = "備註中不存在的字" if len(attempts) == 1 else "大量送單"
            return _judgment_response(info, quote)

        agent = _judge_agent("test_judge", "測試觀點。", self.NOTE)
        with agent.override(model=FunctionModel(flaky, model_name="flaky")):
            output = agent.run_sync("{}").output
        self.assertEqual(len(attempts), 2)
        self.assertEqual(output.risk_matches[0].matched_input_text, "大量送單")
        self.assertEqual(output.risk_matches[0].severity, "medium")

    def test_no_confident_match_is_derived_not_trusted(self) -> None:
        def liar(_messages, info) -> ModelResponse:
            return ModelResponse(
                parts=[
                    ToolCallPart(
                        tool_name=info.output_tools[0].name,
                        args={"risk_matches": [], "no_confident_match": False},
                    )
                ]
            )

        agent = _judge_agent("test_judge", "測試觀點。", self.NOTE)
        with agent.override(model=FunctionModel(liar, model_name="liar")):
            output = agent.run_sync("{}").output
        self.assertTrue(output.no_confident_match)


class StatisticalPolicyTests(unittest.TestCase):
    def test_capacity_gate_uses_wilson_upper_bound(self) -> None:
        scenario = load_scenario("normal")
        result = StrategySummary(
            name="predictive_prewarm",
            p95_latency_ms=1000.0,
            timeout_rate=0.0,
            congestion_probability=0.04,
            congestion_probability_ci95=[0.025, 0.049],
            database_peak_utilization=0.5,
            gateway_peak_utilization=0.5,
            cost=0.0,
        )
        self.assertTrue(_passes_constraints(result, scenario))
        failed = deepcopy(result)
        failed.congestion_probability_ci95 = [0.026, 0.061]
        self.assertFalse(_passes_constraints(failed, scenario))

    def test_monte_carlo_profiles_are_fixed(self) -> None:
        from openingguard.core import MONTE_CARLO_PROFILES

        self.assertEqual(MONTE_CARLO_PROFILES, {"demo": 500, "evidence": 2_000})

    def test_calibration_profiles_capture_warmup_measurement_and_drain(self) -> None:
        evidence = CALIBRATION_PROFILES["evidence"]
        self.assertEqual(evidence["warmup_seconds"], 5.0)
        self.assertEqual(evidence["measurement_seconds"], 30.0)
        self.assertEqual(evidence["drain_seconds"], 2.0)
        self.assertEqual(evidence["repetitions"], 5)

    def test_rate_gate_requires_999_within_slo_and_drained_queue(self) -> None:
        args = Namespace(
            slo_ms=2000.0,
            min_accepted_within_slo_rate=0.999,
            max_error_rate=0.001,
        )
        row = {
            "p95_end_to_end_ms": 500.0,
            "accepted_within_slo_rate": 0.999,
            "error_rate": 0.0,
            "client_rejections": 0,
            "queue_drained_within_seconds": True,
        }
        self.assertTrue(_passes_rate_rule(row, args))
        row["queue_drained_within_seconds"] = False
        self.assertFalse(_passes_rate_rule(row, args))


if __name__ == "__main__":
    unittest.main()
