"""Thin interactive shell for the OpeningGuard logic prototype."""

from __future__ import annotations

import argparse
import json
import os
import sys

from .agent import evaluate_llm, run_agent_assessment
from .core import MONTE_CARLO_PROFILES, build_assessment, list_scenarios, load_scenario
from .schemas import Assessment

BOLD = "\033[1m"
DIM = "\033[2m"
RESET = "\033[0m"


def _compact(result: Assessment | None) -> str:
    if not result:
        return "尚未執行"
    lines = [
        f"run_id: {result.run_id}",
        f"scenario: {result.scenario} ({result.label})",
        f"runs / seed: {result.runs} / {result.random_seed}",
        f"approval: {result.approval_status}",
    ]
    if result.agent:
        agent = result.agent
        lines.append(f"agent_mode: {agent['mode']} (is_llm={agent['is_llm_result']})")
    if result.risk_matches:
        lines.append(
            "risks: "
            + ", ".join(
                f"{item.risk_id}={item.severity}" for item in result.risk_matches
            )
        )
    lines.append("")
    lines.append(
        "strategy                 congestion   CI95-high   p95(ms)   timeout    max_queue   worker-min"
    )
    for item in result.scenarios:
        lines.append(
            f"{item.name:<24} {item.congestion_probability:>9.1%} "
            f"{item.congestion_probability_ci95[1]:>10.1%} "
            f"{item.p95_latency_ms:>9.0f} {item.timeout_rate:>9.2%} "
            f"{item.max_queue:>11} {item.total_worker_minutes:>12.1f}"
        )
    if result.recommended:
        lines.append(
            f"\nrecommendation: 08:50 預熱至 {result.recommended.workers} workers"
        )
    else:
        lines.append(f"\nwarning: {result.warning}")
    return "\n".join(lines)


def _run(scenario_id: str, runs: int, use_agent: bool, run_profile: str) -> Assessment:
    scenario = load_scenario(scenario_id)
    if use_agent:
        return run_agent_assessment(
            scenario_id=scenario_id,
            operation_note=scenario.operation_note,
            runs=runs,
            run_profile=run_profile,
        )
    return build_assessment(scenario_id, runs=runs, run_profile=run_profile)


def interactive(runs: int, run_profile: str) -> None:
    last: Assessment | None = None
    status = "選擇情境開始。"
    while True:
        print("\033[2J\033[H", end="")
        print(f"{BOLD}OpeningGuard AI — PROTOTYPE / SYNTHETIC{RESET}")
        print(f"{DIM}單一 FIFO Queue；所有決策等待人工核准。{RESET}\n")
        print(f"{BOLD}目前狀態{RESET}")
        print(_compact(last))
        print(f"\n{DIM}{status}{RESET}")
        print(
            f"\n{BOLD}[1]{RESET} normal  {BOLD}[2]{RESET} high pressure  "
            f"{BOLD}[3]{RESET} downstream bottleneck"
        )
        print(
            f"{BOLD}[a]{RESET} 用 Agent 跑 high pressure  "
            f"{BOLD}[e]{RESET} 執行 LLM gold-set 評測  {BOLD}[q]{RESET} 離開"
        )
        choice = input("> ").strip().lower()
        try:
            if choice == "q":
                return
            if choice in {"1", "2", "3"}:
                scenario_id = {
                    "1": "normal",
                    "2": "high_pressure",
                    "3": "downstream_bottleneck",
                }[choice]
                status = f"正在執行 {scenario_id}..."
                last = _run(scenario_id, runs, False, run_profile)
                status = "完成確定性模擬。"
            elif choice == "a":
                status = "正在執行 Agent 與容量工具..."
                last = _run("high_pressure", runs, True, run_profile)
                status = "Agent 流程完成；請檢查 agent_mode 是否為真正 LLM。"
            elif choice == "e":
                report = evaluate_llm()
                status = json.dumps(report, ensure_ascii=False, indent=2)
            else:
                status = "未知按鍵。"
        except Exception as exc:
            status = f"錯誤：{type(exc).__name__}: {exc}"


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(
        description="OpeningGuard AI throwaway backend prototype"
    )
    parser.add_argument("--scenario", choices=list_scenarios())
    parser.add_argument(
        "--profile",
        choices=MONTE_CARLO_PROFILES,
        default="demo",
        help="demo=500 次；evidence=2,000 次",
    )
    parser.add_argument(
        "--runs",
        type=int,
        help="自訂 1-2,000 次；指定後覆蓋 profile",
    )
    parser.add_argument(
        "--agent", action="store_true", help="使用 OpenAI Agent；無 key 時明確降級"
    )
    parser.add_argument(
        "--eval", action="store_true", help="執行 15 筆 LLM gold-set 評測"
    )
    parser.add_argument("--json", action="store_true", help="輸出完整 JSON")
    args = parser.parse_args()
    runs = args.runs or MONTE_CARLO_PROFILES[args.profile]
    if not 1 <= runs <= 2_000:
        parser.error("runs 必須介於 1～2,000")
    run_profile = "custom" if args.runs is not None else args.profile
    if args.eval:
        print(json.dumps(evaluate_llm(), ensure_ascii=False, indent=2))
        return
    if args.scenario:
        result = _run(args.scenario, runs, args.agent, run_profile)
        if args.json:
            print(result.model_dump_json(indent=2))
        else:
            print(_compact(result))
        return
    interactive(runs, run_profile)


if __name__ == "__main__":
    main()
