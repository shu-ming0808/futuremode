"""Regression tests for the agreed risk and statistical decision policies."""

from argparse import Namespace
from copy import deepcopy
from dataclasses import asdict
import json
import unittest

import numpy as np
from pydantic_ai.messages import ModelResponse, ToolCallPart
from pydantic_ai.models.function import FunctionModel

from openingguard.agent import (
    _agent_instructions,
    _build_decision,
    _public_event_payload,
    _risk_agent,
    load_prompt_examples,
)
from openingguard.budget_experiment import (
    Decision,
    PublicEvent,
    Truth,
    fixture_decision,
    generate_truth,
    load_config,
    make_cases,
    simulate,
)
from openingguard.core import (
    DATA_DIR,
    _passes_constraints,
    apply_risk_assumptions,
    load_scenario,
)
from openingguard.schemas import SubmitRiskJudgment, StrategySummary
from tools.calibrate import CALIBRATION_PROFILES, _passes_rate_rule


def _judgment(
    severity: str, decision_status: str = "confirmed"
) -> SubmitRiskJudgment:
    bits = {
        "low": (0, 0),
        "medium": (1, 0),
        "high": (1, 1),
    }[severity]
    return SubmitRiskJudgment(
        decision_status=decision_status,
        no_confident_match=False,
        risk_matches=[
            {
                "risk_id": "market_volatility_order_spike",
                "is_at_least_medium": bits[0],
                "is_high": bits[1],
                "matched_input_text": "大量送單",
                "reason": "測試判斷",
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

    def test_real_incident_labels_are_user_approved_and_internally_consistent(self) -> None:
        document = json.loads(
            (DATA_DIR / "manual_label_candidates.json").read_text(encoding="utf-8")
        )
        records = document["records"]
        catalog_risk_ids = {
            item["risk_id"]
            for item in json.loads(
                (DATA_DIR / "risk_catalog.json").read_text(encoding="utf-8")
            )
        }
        expected_bits = {
            "low": (0, 0),
            "medium": (1, 0),
            "high": (1, 1),
        }

        self.assertEqual(document["record_count"], 14)
        self.assertEqual(len(records), 14)
        self.assertEqual(
            {
                severity: sum(
                    record["annotation"]["event_rating"]["severity"] == severity
                    for record in records
                )
                for severity in expected_bits
            },
            {"low": 2, "medium": 6, "high": 6},
        )

        for record in records:
            annotation = record["annotation"]
            severity = annotation["event_rating"]["severity"]
            self.assertEqual(annotation["reviewer"], "user")
            self.assertEqual(
                annotation["review_status"], "user_approved_first_version"
            )
            self.assertTrue(annotation["risk_matches"])
            self.assertFalse(annotation["no_confident_match"])
            for match in annotation["risk_matches"]:
                self.assertIn(match["risk_id"], catalog_risk_ids)
                self.assertEqual(
                    (match["is_at_least_medium"], match["is_high"]),
                    expected_bits[severity],
                )
                self.assertIn(match["matched_input_text"], record["annotation_text_zh"])

    def test_prompt_defines_capacity_impact_and_forbids_numeric_outputs(self) -> None:
        prompt = _agent_instructions()
        self.assertIn("severity 代表容量影響，不代表事件發生機率", prompt)
        self.assertIn("人工標記範例是分級示範", prompt)
        self.assertIn("不可產生倍率、RPS、worker 數", prompt)

    def test_uncertain_single_judgment_previews_high(self) -> None:
        selection = _build_decision(_judgment("medium", "uncertain"))
        match = selection.risk_matches[0]
        self.assertEqual(match.severity, "uncertain")
        self.assertEqual(match.simulation_assumption, "high")
        self.assertTrue(selection.requires_human_review)

    def test_confirmed_single_judgment_keeps_high(self) -> None:
        selection = _build_decision(_judgment("high"))
        self.assertEqual(selection.risk_matches[0].severity, "high")
        self.assertEqual(selection.decision_status, "confirmed")
        self.assertFalse(selection.requires_human_review)

    def test_single_decision_exposes_selected_risks_for_the_simulator(self) -> None:
        selection = _build_decision(_judgment("high"))
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
                            "reason": "測試判斷",
                        }
                    ],
                    "decision_status": "confirmed",
                    "no_confident_match": False,
                },
            )
        ]
    )


class AgentOutputTests(unittest.TestCase):
    NOTE = "夜盤大跌，預期大量送單"

    def test_hallucinated_quote_is_sent_back_to_the_model(self) -> None:
        attempts = []

        def flaky(messages, info) -> ModelResponse:
            attempts.append(len(messages))
            quote = "備註中不存在的字" if len(attempts) == 1 else "大量送單"
            return _judgment_response(info, quote)

        agent = _risk_agent(self.NOTE)
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
                        args={
                            "risk_matches": [],
                            "decision_status": "no_match",
                            "no_confident_match": False,
                        },
                    )
                ]
            )

        agent = _risk_agent(self.NOTE)
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


class EqualBudgetExperimentTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config = load_config()
        self.config.update(
            horizon_seconds=60,
            arrival_guard_seconds=2,
            budget_worker_minutes=12,
            warmup_seconds=2,
            scheduled_peak_seconds=20,
            reactive_poll_seconds=1,
            baseline_rps=100,
            peak_rps=900,
            peak_duration_seconds=20,
            fixture_judgment_seconds=1,
        )
        self.truth = Truth("test_peak", 20, 20, 900)
        self.event = PublicEvent(
            "event-1", 10, 12, 20, "公開資訊顯示開盤可能大量送單", "test_fixture"
        )
        self.decision = fixture_decision(self.config, self.event)
        self.arrivals, self.peak = generate_truth(self.config, self.truth, 123)

    def test_public_event_payload_excludes_hidden_simulation_truth(self) -> None:
        payload = _public_event_payload(
            self.event.operation_note,
            {**asdict(self.event), "future_peak_rps": 9999, "truth": "must_not_leak"},
        )
        self.assertEqual(payload["target_operation_note"], self.event.operation_note)
        self.assertNotIn("future_peak_rps", payload["public_event_metadata"])
        self.assertNotIn("truth", payload["public_event_metadata"])
        self.assertNotIn("structured_scenario", payload)

    def test_fixed_and_burst_strategies_share_same_budget_cap(self) -> None:
        fixed = simulate(self.config, self.truth, self.arrivals, self.peak, "fixed12")
        event = simulate(
            self.config, self.truth, self.arrivals, self.peak, "event", self.decision
        )
        self.assertAlmostEqual(fixed["worker_minutes"], 12)
        self.assertLessEqual(event["worker_minutes"], 12)
        self.assertAlmostEqual(event["worker_minutes"], 12)
        self.assertGreater(event["warmup_worker_minutes"], 0)

    def test_event_scales_down_through_warm_pool_after_confirmed_low_load(self) -> None:
        config = load_config()
        truth, decision = {
            truth.case_id: (truth, decision)
            for truth, decision in make_cases(config)
        }["early_signal"]
        arrivals, peak = generate_truth(config, truth, 2026090500)
        result = simulate(
            config, truth, arrivals, peak, "event", decision, keep_series=True
        )
        workers = [point["allocated_workers"] for point in result["series"]]

        self.assertIn(config["burst_workers"], workers)
        self.assertIn(config["warm_pool_workers"], workers)
        self.assertEqual(workers[-1], config["base_workers"])
        self.assertGreaterEqual(
            result["scale_down_first_step_seconds"],
            truth.peak_start
            + truth.peak_duration
            + config["scale_down_confirmation_seconds"],
        )
        self.assertLess(result["worker_minutes"], config["budget_worker_minutes"])
        self.assertFalse(result["budget_exhausted_warning"])
        self.assertTrue(result["event_signal_applied"])
        self.assertEqual(result["first_scale_trigger"], "event_signal")
        self.assertEqual(result["queue_fallback_trigger_count"], 0)

    def test_event_strategy_uses_queue_fallback_when_signal_is_missing(self) -> None:
        config = load_config()
        truth, decision = {
            truth.case_id: (truth, decision)
            for truth, decision in make_cases(config)
        }["missed_signal"]
        self.assertIsNone(decision)
        arrivals, peak = generate_truth(config, truth, 2026091500)

        event = simulate(config, truth, arrivals, peak, "event", None)
        reactive = simulate(config, truth, arrivals, peak, "reactive")

        self.assertEqual(event["first_scale_trigger"], "queue_fallback")
        self.assertGreaterEqual(event["queue_fallback_trigger_count"], 1)
        self.assertFalse(event["event_signal_applied"])
        self.assertEqual(
            event["accepted_within_slo"], reactive["accepted_within_slo"]
        )
        self.assertEqual(event["worker_minutes"], reactive["worker_minutes"])

    def test_rebound_uses_warm_pool_while_extra_workers_warm_up(self) -> None:
        config = load_config()
        config.update(
            horizon_seconds=60,
            arrival_guard_seconds=2,
            fixed_workers=16,
            budget_worker_minutes=16,
            warmup_seconds=2,
            scheduled_peak_seconds=20,
            controller_poll_seconds=1,
            reactive_poll_seconds=1,
            scale_down_min_high_seconds=2,
            scale_down_observation_seconds=1,
            scale_down_confirmation_seconds=3,
            warm_pool_hold_seconds=6,
            baseline_rps=100,
            fixture_judgment_seconds=1,
        )
        event = PublicEvent(
            "rebound", 10, 12, 20, "公開資訊顯示可能出現兩段集中送單"
        )
        decision = fixture_decision(config, event)
        truth = Truth("rebound", 20, 20, 1800)
        steps = round(config["horizon_seconds"] / config["dt_seconds"])
        arrivals = np.full(steps, 10, dtype=int)
        peak = np.zeros(steps, dtype=bool)
        for start, end in ((20, 25), (34, 39)):
            first = round(start / config["dt_seconds"])
            last = round(end / config["dt_seconds"])
            arrivals[first:last] = 180
            peak[first:last] = True
        arrivals[-round(config["arrival_guard_seconds"] / config["dt_seconds"]):] = 0

        result = simulate(
            config, truth, arrivals, peak, "event", decision, keep_series=True
        )
        rebound_warmup = [
            point
            for point in result["series"]
            if point["seconds"] >= 34
            and point["controller_state"] == "warming"
            and point["allocated_workers"] == config["burst_workers"]
            and point["ready_workers"] == config["warm_pool_workers"]
        ]

        self.assertGreaterEqual(result["rebound_scale_up_count"], 1)
        self.assertTrue(rebound_warmup)
        self.assertFalse(result["budget_exhausted_warning"])

    def test_same_arrival_trace_is_reused_across_strategies(self) -> None:
        hashes = {
            simulate(self.config, self.truth, self.arrivals, self.peak, strategy,
                     self.decision if strategy == "event" else None)["trace_sha256"]
            for strategy in ("fixed12", "scheduled", "reactive", "event", "perfect_timing")
        }
        self.assertEqual(len(hashes), 1)

    def test_decision_cannot_precede_available_information(self) -> None:
        with self.assertRaises(ValueError):
            Decision(self.event, 11, "market_volatility_order_spike", "high", "test")


if __name__ == "__main__":
    unittest.main()
