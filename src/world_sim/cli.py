from __future__ import annotations

import argparse
import json
import os
import tempfile
from collections.abc import Mapping
from decimal import Decimal
from pathlib import Path
from typing import Any, Sequence, TextIO

from .experiment import run_counterfactual_pair, run_pilot
from .metrics import calculate_metrics
from .model_host import (
    DEFAULT_LIVE_MAX_CALLS,
    DEFAULT_LIVE_MAX_COMPLETION_TOKENS,
    DEFAULT_LIVE_REASONING_EFFORT,
    DEFAULT_LIVE_TEMPERATURE,
    DEFAULT_LIVE_TIMEOUT_SECONDS,
    LIVE_REASONING_EFFORTS,
    POSTMORTEM_MAX_COMPLETION_TOKENS,
    run_live_postmortem,
    run_live_survival,
    run_live_survival_continuation,
    run_paid_adapter_qualification,
)
from .models import SelectionMode, VerificationMode, WorldConfig
from .selection import (
    LineageConfig,
    LineageExperiment,
    run_lineage_experiment,
    run_selection_matrix,
)
from .survival.calibration import CALIBRATION_NAMES, LEAN_CAMP_V1
from .survival.demo import result_sha256, run_survival_demo, survival_metrics
from .survival.models import GLOBAL_BEATS_V2, SEQUENTIAL_DIALOGUE_V3


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run closed-world social-survival experiments.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    survive = subparsers.add_parser(
        "survive",
        help="run the deterministic named-survivor reference population",
    )
    survive.add_argument("--seed", type=int, default=17)
    survive.add_argument("--cycles", "--days", dest="cycles", type=int, default=8)
    survive.add_argument("--preset", choices=(LEAN_CAMP_V1,), default=LEAN_CAMP_V1)
    survive.add_argument(
        "--population",
        type=int,
        choices=(len(CALIBRATION_NAMES),),
        default=len(CALIBRATION_NAMES),
        metavar="4",
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
        help="assign one model to the next hidden seat; repeat exactly four times",
    )
    survive_live.add_argument("--seed", type=int, default=17)
    survive_live.add_argument(
        "--cycles", "--days", dest="cycles", type=int, default=1
    )
    survive_live.add_argument(
        "--preset", choices=(LEAN_CAMP_V1,), default=LEAN_CAMP_V1
    )
    survive_live.add_argument("--max-calls", type=int, default=DEFAULT_LIVE_MAX_CALLS)
    survive_live.add_argument(
        "--require-complete-budget",
        action="store_true",
        help="reject before transport unless max-calls covers every chance",
    )
    survive_live.add_argument(
        "--max-completion-tokens",
        type=int,
        default=DEFAULT_LIVE_MAX_COMPLETION_TOKENS,
    )
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

    continue_live = subparsers.add_parser(
        "continue-live",
        help="continue a verified completed live survival artifact",
    )
    continue_live.add_argument("--parent", type=Path, required=True)
    continue_live.add_argument("--parent-sha256", required=True)
    continue_live.add_argument(
        "--ancestor",
        action="append",
        type=Path,
        default=[],
        help=(
            "verified ancestor path; repeat oldest to newest, excluding the "
            "direct parent"
        ),
    )
    continue_live.add_argument(
        "--cycles", "--days", dest="cycles", type=int, default=1
    )
    continue_live.add_argument(
        "--interaction-protocol",
        choices=(GLOBAL_BEATS_V2, SEQUENTIAL_DIALOGUE_V3),
        default=GLOBAL_BEATS_V2,
    )
    continue_live.add_argument(
        "--initiative-phase",
        type=int,
        choices=range(4),
        default=0,
        help="rotate v3 initiative without moving identities, seats, or state",
    )
    continue_live.add_argument(
        "--replace-model",
        action="append",
        default=[],
        metavar="PUBLIC_NAME=PROVIDER/MODEL",
        help="replace exactly one verified seat assignment for sequential-dialogue-v3",
    )
    continue_live.add_argument(
        "--replacement-reason",
        help="required audit reason when --replace-model is supplied",
    )
    continue_live.add_argument("--shared-wood-stock", type=int, default=0)
    continue_live.add_argument(
        "--transition-id",
        required=True,
        help="lowercase audit identifier for the between-cycle adjustment",
    )
    continue_live.add_argument(
        "--max-calls", type=int, default=DEFAULT_LIVE_MAX_CALLS
    )
    continue_live.add_argument(
        "--require-complete-budget",
        action="store_true",
        help="reject before transport unless max-calls covers every chance",
    )
    continue_live.add_argument(
        "--max-completion-tokens",
        type=int,
        default=DEFAULT_LIVE_MAX_COMPLETION_TOKENS,
    )
    continue_live.add_argument(
        "--temperature",
        type=float,
        default=DEFAULT_LIVE_TEMPERATURE,
    )
    continue_live.add_argument(
        "--reasoning-effort",
        choices=LIVE_REASONING_EFFORTS,
        default=DEFAULT_LIVE_REASONING_EFFORT,
        help="preserve defaults, request low, or use the paid direct-answer profile",
    )
    continue_live.add_argument(
        "--max-paid-usd",
        type=Decimal,
        help="required conservative authorization ceiling for opencode-paid models",
    )
    continue_live.add_argument(
        "--timeout-seconds",
        type=float,
        default=DEFAULT_LIVE_TIMEOUT_SECONDS,
    )
    continue_live.add_argument("--show-transcript", action="store_true")
    continue_live.add_argument("--output", type=Path, required=True)

    qualify_live = subparsers.add_parser(
        "qualify-live",
        help="qualify paid model adapters without eliciting survival behavior",
    )
    qualify_live.add_argument(
        "--model",
        action="append",
        dest="models",
        required=True,
        metavar="opencode-paid/MODEL",
        help="assign one paid model to qualify; repeat up to four times",
    )
    qualify_live.add_argument(
        "--max-completion-tokens",
        type=int,
        default=10_000,
    )
    qualify_live.add_argument(
        "--temperature",
        type=float,
        default=DEFAULT_LIVE_TEMPERATURE,
    )
    qualify_live.add_argument(
        "--reasoning-effort",
        choices=("provider-default", "low"),
        default=DEFAULT_LIVE_REASONING_EFFORT,
    )
    qualify_live.add_argument(
        "--max-paid-usd",
        type=Decimal,
        required=True,
        help="conservative authorization ceiling for all qualification calls",
    )
    qualify_live.add_argument(
        "--timeout-seconds",
        type=float,
        default=300.0,
    )
    qualify_live.add_argument("--output", type=Path, required=True)

    postmortem_live = subparsers.add_parser(
        "postmortem-live",
        help="send quarantined notices for deaths in a completed live artifact",
    )
    postmortem_live.add_argument("--world-artifact", type=Path, required=True)
    postmortem_live.add_argument("--world-artifact-sha256", required=True)
    postmortem_live.add_argument(
        "--ancestor",
        action="append",
        type=Path,
        default=[],
        help=(
            "verified ancestor path; repeat oldest to newest, excluding the "
            "world artifact"
        ),
    )
    postmortem_live.add_argument(
        "--max-completion-tokens",
        type=int,
        default=POSTMORTEM_MAX_COMPLETION_TOKENS,
    )
    postmortem_live.add_argument("--temperature", type=float, default=0.0)
    postmortem_live.add_argument(
        "--reasoning-effort",
        choices=LIVE_REASONING_EFFORTS,
        default="low",
    )
    postmortem_live.add_argument(
        "--max-paid-usd",
        type=Decimal,
        help="separate conservative authorization for postmortem calls",
    )
    postmortem_live.add_argument(
        "--timeout-seconds",
        type=float,
        default=DEFAULT_LIVE_TIMEOUT_SECONDS,
    )
    postmortem_live.add_argument("--output", type=Path, required=True)

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
    live_output: TextIO | None = None
    live_commands = {
        "survive-live",
        "continue-live",
        "qualify-live",
        "postmortem-live",
    }
    if args.command in live_commands:
        if args.command == "survive-live" and len(args.models) != len(CALIBRATION_NAMES):
            parser.error("survive-live requires exactly four model assignments")
        if args.command == "qualify-live" and not 1 <= len(args.models) <= 4:
            parser.error("qualify-live requires between one and four model assignments")
        try:
            live_output = _reserve_live_output(args.output)
        except FileExistsError:
            parser.error(f"live output already exists: {args.output}")
        except OSError as error:
            parser.error(f"cannot reserve live output: {error}")
        live_output.close()
        live_output = None
    exit_code = 0
    if args.command == "survive":
        result = run_survival_demo(
            seed=args.seed,
            days=args.cycles,
            names=CALIBRATION_NAMES,
            preset=args.preset,
        )
        payload = result.to_dict()
        summary = {
            "mode": "named_survival_reference",
            "seed": args.seed,
            "world_preset": args.preset,
            "cycles_completed": payload["final_state"]["cycle"],
            "finished_reason": payload["final_state"]["finished_reason"],
            "canonical_sha256": result_sha256(result),
            "metrics": survival_metrics(result),
        }
    elif args.command == "survive-live":
        try:
            payload = run_live_survival(
                model_refs=args.models,
                seed=args.seed,
                days=args.cycles,
                max_calls=args.max_calls,
                max_completion_tokens=args.max_completion_tokens,
                temperature=args.temperature,
                reasoning_effort=args.reasoning_effort,
                max_paid_usd=args.max_paid_usd,
                timeout_seconds=args.timeout_seconds,
                world_preset=args.preset,
                require_complete_budget=args.require_complete_budget,
                checkpoint=lambda current: _replace_reserved_live_output(
                    args.output, current
                ),
            )
        except ValueError as error:
            if _is_reserved_live_output(args.output):
                args.output.unlink()
            parser.error(str(error))
        if payload["status"] == "completed":
            result_payload = payload["result"]
            summary = {
                "mode": payload["mode"],
                "status": payload["status"],
                "seed": args.seed,
                "world_preset": args.preset,
                "cycles_completed": result_payload["final_state"]["cycle"],
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
                "world_preset": args.preset,
                "cycles_completed": payload["partial_state"]["cycle"],
                "failure": payload["failure"],
                "provider_summary": payload["provider_summary"],
            }
    elif args.command == "continue-live":
        try:
            payload = run_live_survival_continuation(
                parent_path=args.parent,
                expected_parent_sha256=args.parent_sha256,
                ancestor_paths=args.ancestor,
                additional_cycles=args.cycles,
                shared_resource="wood",
                shared_stock=args.shared_wood_stock,
                transition_reason=args.transition_id,
                interaction_protocol=args.interaction_protocol,
                initiative_phase=args.initiative_phase,
                model_replacements=args.replace_model,
                replacement_reason=args.replacement_reason,
                max_calls=args.max_calls,
                max_completion_tokens=args.max_completion_tokens,
                temperature=args.temperature,
                reasoning_effort=args.reasoning_effort,
                max_paid_usd=args.max_paid_usd,
                timeout_seconds=args.timeout_seconds,
                require_complete_budget=args.require_complete_budget,
                checkpoint=lambda current: _replace_reserved_live_output(
                    args.output, current
                ),
            )
        except ValueError as error:
            if _is_reserved_live_output(args.output):
                args.output.unlink()
            parser.error(str(error))
        result_payload = (
            payload["result"]
            if payload["status"] == "completed"
            else payload["partial_state"]
        )
        if payload["status"] != "completed":
            exit_code = 1
        summary = {
            "mode": payload["mode"],
            "status": payload["status"],
            "seed": payload["config"]["seed"],
            "starting_cycle": payload["config"]["starting_cycle"],
            "cycles_completed": (
                result_payload["final_state"]["cycle"]
                if payload["status"] == "completed"
                else result_payload["cycle"]
            ),
            "provider_summary": payload["provider_summary"],
        }
        if payload["status"] == "completed":
            summary.update(
                {
                    "finished_reason": result_payload["final_state"][
                        "finished_reason"
                    ],
                    "canonical_sha256": payload["canonical_result_sha256"],
                    "metrics": payload["metrics"],
                    "session_outcomes": payload["session_outcomes"],
                }
            )
        else:
            summary["failure"] = payload["failure"]
    elif args.command == "qualify-live":
        try:
            payload = run_paid_adapter_qualification(
                model_refs=args.models,
                max_completion_tokens=args.max_completion_tokens,
                temperature=args.temperature,
                reasoning_effort=args.reasoning_effort,
                max_paid_usd=args.max_paid_usd,
                timeout_seconds=args.timeout_seconds,
                checkpoint=lambda current: _replace_reserved_live_output(
                    args.output, current
                ),
            )
        except ValueError as error:
            if _is_reserved_live_output(args.output):
                args.output.unlink()
            parser.error(str(error))
        if payload["status"] != "passed":
            exit_code = 1
        summary = {
            "mode": payload["mode"],
            "status": payload["status"],
            "qualification_id": payload["qualification_id"],
            **payload["summary"],
        }
    elif args.command == "postmortem-live":
        try:
            payload = run_live_postmortem(
                world_artifact_path=args.world_artifact,
                expected_world_artifact_sha256=args.world_artifact_sha256,
                ancestor_paths=args.ancestor,
                max_completion_tokens=args.max_completion_tokens,
                temperature=args.temperature,
                reasoning_effort=args.reasoning_effort,
                max_paid_usd=args.max_paid_usd,
                timeout_seconds=args.timeout_seconds,
                checkpoint=lambda current: _replace_reserved_live_output(
                    args.output, current
                ),
            )
        except ValueError as error:
            if _is_reserved_live_output(args.output):
                args.output.unlink()
            parser.error(str(error))
        if payload["summary"]["calls_failed"] or payload["summary"][
            "calls_skipped"
        ]:
            exit_code = 1
        summary = {
            "mode": payload["mode"],
            "status": payload["status"],
            **payload["summary"],
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

    if args.output is not None and args.command not in live_commands:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        summary["output"] = str(args.output)
    elif args.command in live_commands:
        summary["output"] = str(args.output)
    if args.command in {"survive-live", "continue-live"} and args.show_transcript:
        for call in payload["calls"]:
            _print_live_call(
                call,
                interaction_protocol=payload["config"]["interaction_protocol"],
            )
    print(json.dumps(summary, sort_keys=True))
    return exit_code


def _reserve_live_output(path: Path) -> TextIO:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("x+", encoding="utf-8", newline="\n")
    handle.write('{"status":"reserved"}\n')
    handle.flush()
    os.fsync(handle.fileno())
    return handle


def _write_reserved_live_output(
    handle: TextIO,
    payload: Mapping[str, object],
) -> None:
    handle.seek(0)
    json.dump(payload, handle, indent=2, sort_keys=True)
    handle.write("\n")
    handle.truncate()
    handle.flush()
    os.fsync(handle.fileno())
    handle.close()


def _replace_reserved_live_output(
    path: Path,
    payload: Mapping[str, object],
) -> None:
    descriptor, raw_temp_path = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
        text=True,
    )
    temp_path = Path(raw_temp_path)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
    finally:
        temp_path.unlink(missing_ok=True)


def _is_reserved_live_output(path: Path) -> bool:
    return path.read_text(encoding="utf-8") == '{"status":"reserved"}\n'


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


def _print_live_call(
    record: Mapping[str, Any],
    *,
    interaction_protocol: str,
) -> None:
    unit = "day" if interaction_protocol == GLOBAL_BEATS_V2 else "cycle"
    step = "beat" if interaction_protocol == GLOBAL_BEATS_V2 else "slot"
    if record["status"] == "failed":
        error = record["error"]
        print(
            f"{unit} {record['cycle']} {step} {record['slot']} | "
            f"{record['public_name']} | "
            f"provider failure: {error['kind']}"
        )
        return
    parsed = record["parsed_choice"]
    action = parsed["action"]
    line = (
        f"{unit} {record['cycle']} {step} {record['slot']} | "
        f"{record['public_name']} | {action['kind']}"
    )
    speech = parsed["say"]
    if isinstance(speech, Mapping):
        line += f" | to {speech['to']}: {speech['text']}"
    print(line)
