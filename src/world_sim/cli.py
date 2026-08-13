from __future__ import annotations

import argparse
import json
from collections.abc import Mapping
from decimal import Decimal
from pathlib import Path
from typing import Any, Sequence

from .experiment import run_counterfactual_pair, run_pilot
from .metrics import calculate_metrics
from .model_host import (
    DEFAULT_LIVE_MAX_CALLS,
    DEFAULT_LIVE_REASONING_EFFORT,
    DEFAULT_LIVE_TEMPERATURE,
    DEFAULT_LIVE_TIMEOUT_SECONDS,
    LIVE_REASONING_EFFORTS,
    run_live_survival,
)
from .models import SelectionMode, VerificationMode, WorldConfig
from .selection import LineageConfig, LineageExperiment, run_lineage_experiment, run_selection_matrix
from .survival.demo import result_sha256, run_survival_demo, survival_metrics
from .survival.models import DEFAULT_SURVIVOR_NAMES


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run closed-world social-survival experiments.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    survive = subparsers.add_parser(
        "survive",
        help="run the deterministic named-survivor reference population",
    )
    survive.add_argument("--seed", type=int, default=17)
    survive.add_argument("--days", type=int, default=10)
    survive.add_argument(
        "--population",
        type=int,
        choices=range(2, len(DEFAULT_SURVIVOR_NAMES) + 1),
        default=len(DEFAULT_SURVIVOR_NAMES),
        metavar="2..8",
    )
    survive.add_argument("--output", type=Path)

    survive_live = subparsers.add_parser(
        "survive-live",
        help="run named survivors using direct OpenCode model calls",
    )
    survive_live.add_argument(
        "--model",
        action="append",
        dest="models",
        required=True,
        metavar="PROVIDER/MODEL",
        help="assign one model to the next hidden seat; repeat 2-8 times",
    )
    survive_live.add_argument("--seed", type=int, default=17)
    survive_live.add_argument("--days", type=int, default=3)
    survive_live.add_argument("--max-calls", type=int, default=DEFAULT_LIVE_MAX_CALLS)
    survive_live.add_argument("--max-completion-tokens", type=int, default=4096)
    survive_live.add_argument(
        "--temperature",
        type=float,
        default=DEFAULT_LIVE_TEMPERATURE,
    )
    survive_live.add_argument(
        "--reasoning-effort",
        choices=LIVE_REASONING_EFFORTS,
        default=DEFAULT_LIVE_REASONING_EFFORT,
        help="preserve defaults, request low, or use the paid direct-answer profile",
    )
    survive_live.add_argument(
        "--max-paid-usd",
        type=Decimal,
        help="required conservative authorization ceiling for opencode-paid models",
    )
    survive_live.add_argument(
        "--timeout-seconds",
        type=float,
        default=DEFAULT_LIVE_TIMEOUT_SECONDS,
    )
    survive_live.add_argument("--show-transcript", action="store_true")
    survive_live.add_argument("--output", type=Path, required=True)

    pilot = subparsers.add_parser("pilot", help="run the Blind Commons calibration population")
    _add_run_arguments(pilot)
    pilot.add_argument("--verification", choices=[mode.value for mode in VerificationMode], required=True)

    compare = subparsers.add_parser("compare", help="run the proxy and receipt treatments from the same seed")
    _add_run_arguments(compare)

    evolve = subparsers.add_parser(
        "evolve",
        help="run deterministic inherited policy bundles in one selection treatment",
    )
    _add_lineage_arguments(evolve)
    evolve.add_argument("--verification", choices=[mode.value for mode in VerificationMode], required=True)
    evolve.add_argument(
        "--selection",
        choices=[mode.value for mode in SelectionMode],
        default=SelectionMode.INDIVIDUAL.value,
    )

    matrix = subparsers.add_parser(
        "matrix",
        help="run the 2x2 individual-versus-none and proxy-versus-receipts matrix",
    )
    _add_lineage_arguments(matrix)

    args = parser.parse_args(argv)
    exit_code = 0
    if args.command == "survive":
        names = DEFAULT_SURVIVOR_NAMES[: args.population]
        result = run_survival_demo(seed=args.seed, days=args.days, names=names)
        payload = result.to_dict()
        summary = {
            "mode": "named_survival_reference",
            "seed": args.seed,
            "days_completed": payload["final_state"]["day"],
            "finished_reason": payload["final_state"]["finished_reason"],
            "canonical_sha256": result_sha256(result),
            "metrics": survival_metrics(result),
        }
    elif args.command == "survive-live":
        try:
            payload = run_live_survival(
                model_refs=args.models,
                seed=args.seed,
                days=args.days,
                max_calls=args.max_calls,
                max_completion_tokens=args.max_completion_tokens,
                temperature=args.temperature,
                reasoning_effort=args.reasoning_effort,
                max_paid_usd=args.max_paid_usd,
                timeout_seconds=args.timeout_seconds,
            )
        except ValueError as error:
            parser.error(str(error))
        if payload["status"] == "completed":
            result_payload = payload["result"]
            summary = {
                "mode": payload["mode"],
                "status": payload["status"],
                "seed": args.seed,
                "days_completed": result_payload["final_state"]["day"],
                "finished_reason": result_payload["final_state"]["finished_reason"],
                "canonical_sha256": payload["canonical_result_sha256"],
                "metrics": payload["metrics"],
                "provider_summary": payload["provider_summary"],
            }
        else:
            exit_code = 1
            summary = {
                "mode": payload["mode"],
                "status": payload["status"],
                "seed": args.seed,
                "days_completed": payload["partial_state"]["day"],
                "failure": payload["failure"],
                "provider_summary": payload["provider_summary"],
            }
    elif args.command == "pilot":
        run = run_pilot(
            verification_mode=VerificationMode(args.verification),
            seed=args.seed,
            turns=args.turns,
        )
        payload: dict[str, Any] = run.to_dict()
        summary = {
            "verification": args.verification,
            "seed": args.seed,
            "turns_completed": payload["result"]["final_state"]["turn"],
            "finished_reason": payload["result"]["final_state"]["finished_reason"],
            "metrics": payload["metrics"],
        }
    elif args.command == "compare":
        pair = run_counterfactual_pair(seed=args.seed, turns=args.turns)
        payload = pair.to_dict()
        summary = {
            "seed": args.seed,
            "proxy": payload["proxy"]["metrics"],
            "receipts": payload["receipts"]["metrics"],
            "receipts_minus_proxy": payload["receipts_minus_proxy"],
        }
    elif args.command == "evolve":
        experiment = run_lineage_experiment(
            seed=args.seed,
            config=_lineage_config(
                args,
                selection_mode=SelectionMode(args.selection),
                verification_mode=VerificationMode(args.verification),
            ),
        )
        payload = experiment.to_dict()
        summary = _lineage_summary(experiment)
    elif args.command == "matrix":
        matrix_run = run_selection_matrix(
            seed=args.seed,
            generations=args.generations,
            turns_per_generation=args.turns,
            population_size=args.population,
            parent_count=args.parent_count,
            mutation_rate=args.mutation_rate,
            memory_limit=args.memory_limit,
            world_config=_world_config(args),
        )
        payload = matrix_run.to_dict()
        summary = {
            "mode": "selection_matrix",
            "seed": args.seed,
            "canonical_sha256": matrix_run.content_sha256,
            "conditions": {
                key: _lineage_summary(experiment)
                for key, experiment in sorted(matrix_run.conditions.items())
            },
        }
    else:
        raise RuntimeError(f"unsupported command {args.command!r}")

    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        summary["output"] = str(args.output)
    if args.command == "survive-live" and args.show_transcript:
        for call in payload["calls"]:
            _print_live_call(call)
    print(json.dumps(summary, sort_keys=True))
    return exit_code


def _add_run_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--seed", type=int, default=17)
    parser.add_argument("--turns", type=int, default=12)
    parser.add_argument("--output", type=Path)


def _add_lineage_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--seed", type=int, default=17)
    parser.add_argument("--generations", type=int, default=5)
    parser.add_argument("--turns", type=int, default=12, help="turns per generation")
    parser.add_argument("--population", type=int, default=8)
    parser.add_argument("--parent-count", type=int, default=2)
    parser.add_argument("--mutation-rate", type=float, default=0.15)
    parser.add_argument("--memory-limit", type=int, default=3)
    parser.add_argument("--no-messages", action="store_true")
    parser.add_argument("--no-pacts", action="store_true")
    parser.add_argument(
        "--show-verification",
        action="store_true",
        help="expose the verification treatment to controllers; off in the core treatment",
    )
    parser.add_argument("--output", type=Path)


def _lineage_config(
    args: argparse.Namespace,
    *,
    selection_mode: SelectionMode,
    verification_mode: VerificationMode,
) -> LineageConfig:
    return LineageConfig(
        selection_mode=selection_mode,
        generations=args.generations,
        turns_per_generation=args.turns,
        population_size=args.population,
        parent_count=args.parent_count,
        mutation_rate=args.mutation_rate,
        memory_limit=args.memory_limit,
        world_config=_world_config(args, verification_mode=verification_mode),
    )


def _world_config(
    args: argparse.Namespace,
    *,
    verification_mode: VerificationMode = VerificationMode.PROXY,
) -> WorldConfig:
    return WorldConfig(
        verification_mode=verification_mode,
        messages_enabled=not args.no_messages,
        pacts_enabled=not args.no_pacts,
        verification_visible=args.show_verification,
    )


def _lineage_summary(experiment: LineageExperiment) -> dict[str, Any]:
    generation_metrics: list[dict[str, int | float]] = []
    for generation in experiment.generations:
        metrics = calculate_metrics(generation.result)
        alive_agent_turns = metrics["alive_agent_turns"]
        unverified_claims = metrics["false_claims"]
        generation_metrics.append(
            {
                "generation": generation.generation,
                "alive_agent_turns": alive_agent_turns,
                "unverified_claim_attempts": unverified_claims,
                "unverified_claim_attempts_per_alive_agent_turn": (
                    unverified_claims / alive_agent_turns if alive_agent_turns else 0.0
                ),
                "false_claims_paid": metrics["false_claims_paid"],
                "receipt_backed_claims_paid": metrics["receipt_backed_claims_paid"],
                "extractions": metrics["extractions"],
                "restorations": metrics["restorations"],
                "final_commons_damage": metrics["final_commons_damage"],
                "living_agents": metrics["living_agents"],
                "total_final_energy": metrics["total_final_energy"],
            }
        )
    return {
        "mode": "lineage",
        "seed": experiment.seed,
        "selection": experiment.config.selection_mode.value,
        "verification": experiment.config.world_config.verification_mode.value,
        "canonical_sha256": experiment.content_sha256,
        "generation_metrics": generation_metrics,
        "selection_records": [record.to_dict() for record in experiment.selections],
        "final_strategy_distribution": experiment.to_dict()["final_strategy_distribution"],
    }


def _print_live_call(record: Mapping[str, Any]) -> None:
    if record["status"] == "failed":
        error = record["error"]
        print(
            f"day {record['day']} | {record['public_name']} | "
            f"provider failure: {error['kind']}"
        )
        return
    parsed = record["parsed_choice"]
    action = parsed["action"]
    line = f"day {record['day']} | {record['public_name']} | {action['kind']}"
    speech = parsed["say"]
    if isinstance(speech, Mapping):
        line += f" | to {speech['to']}: {speech['text']}"
    print(line)
