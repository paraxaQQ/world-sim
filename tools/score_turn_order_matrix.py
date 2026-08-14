from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import sys
from collections.abc import Mapping, Sequence
from copy import deepcopy
from decimal import Decimal, InvalidOperation
from pathlib import Path
from types import ModuleType
from typing import Any

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = REPOSITORY_ROOT / "src"
VERIFIER_PATH = Path(__file__).with_name("verify_live_artifact.py")
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from world_sim.model_host import (  # noqa: E402
    _continuation_outcomes,
    _survival_result_from_mapping,
)
from world_sim.survival.demo import survival_metrics  # noqa: E402

BEHAVIOR_FIELDS = (
    "primary_shelter_chain_by_end_of_chance_3",
    "any_completed_costly_resource_transfer",
    "completed_resource_transfers",
    "completed_costly_resource_transfers",
    "completed_wood_gifts",
    "reciprocal_wood_transfer_pairs",
    "shelters_built",
    "survivors",
    "deaths",
    "death_events",
)


def _load_verifier() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "world_sim_turn_order_matrix_verifier",
        VERIFIER_PATH,
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load live artifact verifier")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


VERIFIER = _load_verifier()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Verify and score the frozen turn-order matrix without provider calls."
        )
    )
    parser.add_argument("manifest", type=Path)
    parser.add_argument(
        "--output",
        type=Path,
        help="also write the deterministic JSON report to this path",
    )
    return parser


def score_turn_order_matrix(
    manifest_path: Path,
    *,
    repository_root: Path | None = None,
) -> dict[str, Any]:
    repository_root = (
        REPOSITORY_ROOT if repository_root is None else repository_root.resolve()
    )
    manifest_path = manifest_path.resolve()
    manifest, manifest_sha256 = _load_json_object(
        manifest_path,
        name="manifest",
    )
    cells, fixed = _validate_manifest(manifest)

    ancestor_path = _resolve_repo_path(
        fixed["ancestor_artifact"],
        name="fixed_treatment.ancestor_artifact",
        repository_root=repository_root,
    )
    parent_path = _resolve_repo_path(
        fixed["parent_artifact"],
        name="fixed_treatment.parent_artifact",
        repository_root=repository_root,
    )
    ancestor_sha256 = _require_sha256(
        fixed["ancestor_artifact_sha256"],
        name="fixed_treatment.ancestor_artifact_sha256",
    )
    parent_sha256 = _require_sha256(
        fixed["parent_artifact_sha256"],
        name="fixed_treatment.parent_artifact_sha256",
    )
    parent_canonical_sha256 = _require_sha256(
        fixed["parent_canonical_result_sha256"],
        name="fixed_treatment.parent_canonical_result_sha256",
    )

    ancestor_receipt = VERIFIER.verify_live_artifact(
        ancestor_path,
        expected_artifact_sha256=ancestor_sha256,
    )
    parent_receipt = VERIFIER.verify_live_artifact(
        parent_path,
        expected_artifact_sha256=parent_sha256,
        parent_path=ancestor_path,
    )
    if parent_receipt.get("canonical_result_sha256") != parent_canonical_sha256:
        raise ValueError(
            "verified parent canonical result does not match fixed treatment"
        )

    rows = [
        _score_cell(
            cell,
            fixed=fixed,
            parent_path=parent_path,
            ancestor_path=ancestor_path,
            repository_root=repository_root,
        )
        for cell in cells
    ]
    protocol = _protocol_receipt(manifest, rows)
    batch, phases = _aggregate_rows(rows)
    return {
        "format_version": 1,
        "mode": "turn_order_matrix_score",
        "manifest": {
            "path": _display_path(
                manifest_path,
                repository_root=repository_root,
            ),
            "artifact_sha256": manifest_sha256,
        },
        "verified_chain": {
            "ancestor_artifact": str(fixed["ancestor_artifact"]),
            "ancestor_artifact_sha256": ancestor_receipt["artifact_sha256"],
            "parent_artifact": str(fixed["parent_artifact"]),
            "parent_artifact_sha256": parent_receipt["artifact_sha256"],
            "parent_canonical_result_sha256": parent_receipt[
                "canonical_result_sha256"
            ],
        },
        "protocol": protocol,
        "batch": batch,
        "phases": phases,
        "raw_rows": rows,
    }


def _protocol_receipt(
    manifest: Mapping[str, object],
    rows: Sequence[dict[str, Any]],
) -> dict[str, Any]:
    preregistration = _mapping(
        manifest.get("preregistration"),
        name="manifest.preregistration",
    )
    stopping_rule = preregistration.get("stopping_rule")
    if stopping_rule != (
        "run all 12 planned cells unless a technical or aggregate-budget gate stops "
        "the batch"
    ):
        raise ValueError("manifest stopping rule is not the frozen rule")
    failed_positions = [
        int(row["execution_position"])
        for row in rows
        if row["status"] == "failed"
    ]
    first_failure = min(failed_positions, default=None)
    post_stop_positions = [
        int(row["execution_position"])
        for row in rows
        if first_failure is not None
        and int(row["execution_position"]) > first_failure
        and row["status"] != "pending"
    ]
    for row in rows:
        position = int(row["execution_position"])
        if first_failure is None or position < first_failure:
            row["analysis_set"] = "preregistered"
        elif position == first_failure:
            row["analysis_set"] = "preregistered_stopping_event"
        else:
            row["analysis_set"] = "post_stop_exploratory"
    deviated = bool(post_stop_positions)
    return {
        "stopping_rule": stopping_rule,
        "status": "deviated" if deviated else "adhered",
        "first_technical_failure_execution_position": first_failure,
        "observed_post_stop_execution_positions": post_stop_positions,
        "post_stop_use": "exploratory_only" if deviated else None,
        "reported_aggregate_use": "all_retained_cells_descriptive",
    }


def _validate_manifest(
    manifest: Mapping[str, object],
) -> tuple[list[Mapping[str, object]], Mapping[str, object]]:
    if manifest.get("format_version") != 1:
        raise ValueError("manifest format_version must be 1")
    if manifest.get("mode") != "turn_order_replicate_matrix":
        raise ValueError("manifest mode must be turn_order_replicate_matrix")

    analysis = _mapping(manifest.get("analysis"), name="manifest.analysis")
    if (
        analysis.get("primary_outcome")
        != "primary_shelter_chain_by_end_of_chance_3"
    ):
        raise ValueError("manifest primary outcome is not the frozen outcome")
    if analysis.get("technical_failures") != (
        "retain and censor; never score as behavioral zero and never retry"
    ):
        raise ValueError("manifest technical-failure rule is not frozen")

    batch = _mapping(manifest.get("batch"), name="manifest.batch")
    if _strict_int(batch.get("cell_count"), name="batch.cell_count") != 12:
        raise ValueError("frozen matrix must contain 12 cells")
    if _strict_int(batch.get("block_count"), name="batch.block_count") != 3:
        raise ValueError("frozen matrix must contain 3 blocks")
    if _strict_int(batch.get("per_phase_n"), name="batch.per_phase_n") != 3:
        raise ValueError("frozen matrix must plan 3 cells per phase")

    fixed = _mapping(
        manifest.get("fixed_treatment"),
        name="manifest.fixed_treatment",
    )
    required_fixed = {
        "ancestor_artifact",
        "ancestor_artifact_sha256",
        "cycles",
        "interaction_protocol",
        "max_calls",
        "max_completion_tokens",
        "model_replacement",
        "parent_artifact",
        "parent_artifact_sha256",
        "parent_canonical_result_sha256",
        "per_cell_max_paid_usd",
        "reasoning_effort",
        "replacement_reason",
        "require_complete_budget",
        "seed",
        "shared_wood_stock",
        "temperature",
        "timeout_seconds",
        "transition_id",
    }
    if not required_fixed.issubset(fixed):
        missing = sorted(required_fixed - set(fixed))
        raise ValueError(f"fixed treatment is missing fields: {', '.join(missing)}")

    raw_cells = _sequence(manifest.get("cells"), name="manifest.cells")
    if len(raw_cells) != 12:
        raise ValueError("manifest cells must contain exactly 12 entries")
    cells = [_mapping(cell, name="manifest cell") for cell in raw_cells]
    positions: set[int] = set()
    outputs: set[str] = set()
    phase_counts = {phase: 0 for phase in range(4)}
    block_phases = {block: set() for block in range(1, 4)}
    normalized: list[Mapping[str, object]] = []
    for cell in cells:
        if set(cell) != {
            "block",
            "execution_position",
            "initiative_phase",
            "output",
        }:
            raise ValueError("manifest cell has unexpected fields")
        block = _strict_int(cell["block"], name="cell.block")
        position = _strict_int(
            cell["execution_position"],
            name="cell.execution_position",
        )
        phase = _strict_int(cell["initiative_phase"], name="cell.initiative_phase")
        output = _text(cell["output"], name="cell.output")
        if block not in block_phases or phase not in phase_counts:
            raise ValueError("manifest cell block or phase is outside the frozen range")
        if position in positions or output in outputs:
            raise ValueError("manifest cell positions and outputs must be unique")
        if phase in block_phases[block]:
            raise ValueError("each block must contain every phase exactly once")
        if f"-b{block:02d}-p{phase}-" not in Path(output).name:
            raise ValueError("manifest output name does not match its block and phase")
        positions.add(position)
        outputs.add(output)
        phase_counts[phase] += 1
        block_phases[block].add(phase)
        normalized.append(cell)
    if positions != set(range(1, 13)):
        raise ValueError("manifest execution positions must be contiguous from 1 to 12")
    if any(count != 3 for count in phase_counts.values()):
        raise ValueError("manifest must contain three cells per initiative phase")
    if any(phases != set(range(4)) for phases in block_phases.values()):
        raise ValueError("each block must contain phases 0 through 3")
    return sorted(normalized, key=lambda cell: int(cell["execution_position"])), fixed


def _score_cell(
    cell: Mapping[str, object],
    *,
    fixed: Mapping[str, object],
    parent_path: Path,
    ancestor_path: Path,
    repository_root: Path,
) -> dict[str, Any]:
    block = _strict_int(cell["block"], name="cell.block")
    position = _strict_int(cell["execution_position"], name="cell position")
    phase = _strict_int(cell["initiative_phase"], name="cell phase")
    output = _text(cell["output"], name="cell.output")
    path = _resolve_repo_path(
        output,
        name="cell.output",
        repository_root=repository_root,
    )
    base: dict[str, Any] = {
        "block": block,
        "execution_position": position,
        "initiative_phase": phase,
        "output": output,
    }
    if not path.exists():
        return {
            **base,
            "status": "pending",
            "scoreable": False,
            "censored": False,
            "artifact_sha256": None,
            "canonical_result_sha256": None,
            "failure_kind": None,
            "calls_succeeded": None,
            "calls_failed": None,
            "cost_exposure_usd": None,
            **_null_behavior(),
        }

    artifact, artifact_sha256 = _load_json_object(path, name=f"cell {position}")
    _validate_fixed_treatment(artifact, cell=cell, fixed=fixed)
    receipt = VERIFIER.verify_live_artifact(
        path,
        expected_artifact_sha256=artifact_sha256,
        parent_path=parent_path,
        ancestor_paths=(ancestor_path,),
    )
    status = artifact.get("status")
    if receipt.get("status") != status:
        raise ValueError(f"cell {position} verifier status mismatch")
    calls = _sequence(artifact.get("calls"), name=f"cell {position} calls")
    cost = _call_cost_exposure(calls)
    calls_succeeded = sum(
        _mapping(call, name="call").get("status") == "succeeded" for call in calls
    )
    calls_failed = sum(
        _mapping(call, name="call").get("status") == "failed" for call in calls
    )
    verified = {
        **base,
        "status": status,
        "artifact_sha256": artifact_sha256,
        "calls_succeeded": calls_succeeded,
        "calls_failed": calls_failed,
        "cost_exposure_usd": _decimal_text(cost),
    }
    if status == "completed":
        if calls_succeeded != len(calls) or calls_failed:
            raise ValueError(f"completed cell {position} has a non-succeeded call")
        if receipt.get("exact_replay") is not True:
            raise ValueError(f"completed cell {position} lacks exact replay proof")
        behavior = _completed_behavior(artifact)
        return {
            **verified,
            "scoreable": True,
            "censored": False,
            "canonical_result_sha256": receipt["canonical_result_sha256"],
            "failure_kind": None,
            **behavior,
        }
    if status == "failed":
        _validate_failed_artifact(artifact, receipt=receipt)
        failure = _mapping(artifact.get("failure"), name="failure")
        return {
            **verified,
            "scoreable": False,
            "censored": True,
            "canonical_result_sha256": None,
            "failure_kind": failure.get("kind"),
            **_null_behavior(),
        }
    raise ValueError(f"cell {position} status must be completed or failed")


def _validate_fixed_treatment(
    artifact: Mapping[str, object],
    *,
    cell: Mapping[str, object],
    fixed: Mapping[str, object],
) -> None:
    if artifact.get("format_version") != 6 or artifact.get("mode") != (
        "live_named_survival_continuation"
    ):
        raise ValueError("matrix cell must be a format-v6 live continuation")
    config = _mapping(artifact.get("config"), name="cell config")
    config_fields = {
        "cycles": "cycles_requested",
        "interaction_protocol": "interaction_protocol",
        "max_calls": "max_calls",
        "max_completion_tokens": "max_completion_tokens",
        "per_cell_max_paid_usd": "max_paid_usd",
        "reasoning_effort": "reasoning_effort",
        "require_complete_budget": "require_complete_budget",
        "seed": "seed",
        "temperature": "temperature",
        "timeout_seconds": "timeout_seconds",
    }
    for fixed_name, config_name in config_fields.items():
        _require_exact(
            config.get(config_name),
            fixed[fixed_name],
            name=f"cell config {config_name}",
        )
    _require_exact(
        config.get("initiative_phase"),
        cell["initiative_phase"],
        name="cell config initiative_phase",
    )

    link = _mapping(artifact.get("continuation_link"), name="continuation link")
    _require_exact(
        link.get("parent_artifact_sha256"),
        fixed["parent_artifact_sha256"],
        name="continuation parent artifact SHA-256",
    )
    _require_exact(
        link.get("parent_canonical_result_sha256"),
        fixed["parent_canonical_result_sha256"],
        name="continuation parent canonical SHA-256",
    )

    transition = _mapping(
        artifact.get("transition_receipt"),
        name="transition receipt",
    )
    if transition.get("method") != (
        "deterministic_between_cycle_shared_resource_adjustment"
    ):
        raise ValueError("matrix cell does not contain the frozen resource transition")
    event = _mapping(transition.get("event"), name="transition event")
    detail = _mapping(event.get("detail"), name="transition detail")
    _require_exact(detail.get("resource"), "wood", name="transition resource")
    _require_exact(
        detail.get("after"),
        fixed["shared_wood_stock"],
        name="transition shared wood stock",
    )
    _require_exact(
        detail.get("reason"),
        fixed["transition_id"],
        name="transition id",
    )

    replacement_text = _text(
        fixed["model_replacement"],
        name="fixed_treatment.model_replacement",
    )
    if replacement_text.count("=") != 1:
        raise ValueError("fixed model replacement must use public_name=model")
    public_name, replacement_model = replacement_text.split("=", 1)
    if not public_name or not replacement_model:
        raise ValueError("fixed model replacement has an empty side")
    assignments = [
        _mapping(row, name="seat assignment")
        for row in _sequence(artifact.get("seat_assignments"), name="seat assignments")
    ]
    matches = [row for row in assignments if row.get("public_name") == public_name]
    if len(matches) != 1 or matches[0].get("model") != replacement_model:
        raise ValueError("cell seat assignment does not match the frozen replacement")
    receipts = [
        _mapping(row, name="assignment transition")
        for row in _sequence(
            artifact.get("assignment_transition_receipts"),
            name="assignment transition receipts",
        )
    ]
    if len(receipts) != 1:
        raise ValueError("matrix cell must contain one assignment transition receipt")
    receipt = receipts[0]
    expected_receipt_fields = {
        "public_name": public_name,
        "replacement_model": replacement_model,
        "reason": fixed["replacement_reason"],
    }
    for name, expected in expected_receipt_fields.items():
        _require_exact(
            receipt.get(name),
            expected,
            name=f"assignment transition {name}",
        )


def _completed_behavior(artifact: Mapping[str, object]) -> dict[str, Any]:
    payload = _mapping(artifact.get("result"), name="completed cell result")
    result = _survival_result_from_mapping(payload)
    outcomes = _continuation_outcomes(result)
    metrics = survival_metrics(result)
    if "session_outcomes" not in artifact or "metrics" not in artifact:
        raise ValueError("completed cell is missing recorded outcomes or metrics")
    _require_exact(
        artifact["session_outcomes"],
        outcomes,
        name="independently recomputed session outcomes",
    )
    _require_exact(
        artifact["metrics"],
        metrics,
        name="independently recomputed survival metrics",
    )
    primary = outcomes["primary_shelter_chain_by_end_of_chance_3"]
    if type(primary) is not bool:
        raise ValueError("recomputed primary outcome must be boolean")
    wood_gifts = _sequence(
        outcomes["completed_wood_gifts"],
        name="completed wood gifts",
    )
    reciprocal_pairs = _sequence(
        outcomes["reciprocal_wood_transfer_pairs"],
        name="reciprocal wood transfer pairs",
    )
    shelters = _sequence(outcomes["shelters_built"], name="shelters built")
    death_events = [
        deepcopy(dict(event))
        for event in result.events
        if event.get("kind") == "survivor_died"
    ]
    deaths = _strict_int(metrics.get("deaths"), name="metrics.deaths")
    if len(death_events) != deaths:
        raise ValueError("recomputed death events do not match the death metric")
    if len(shelters) != _strict_int(
        metrics.get("shelters_built"),
        name="metrics.shelters_built",
    ):
        raise ValueError("recomputed shelters do not match the shelter metric")
    return {
        "primary_shelter_chain_by_end_of_chance_3": primary,
        "any_completed_costly_resource_transfer": outcomes[
            "any_completed_costly_resource_transfer"
        ],
        "completed_resource_transfers": _strict_int(
            outcomes["completed_resource_transfers"],
            name="completed resource transfers",
        ),
        "completed_costly_resource_transfers": _strict_int(
            outcomes["completed_costly_resource_transfers"],
            name="completed costly resource transfers",
        ),
        "completed_wood_gifts": len(wood_gifts),
        "reciprocal_wood_transfer_pairs": len(reciprocal_pairs),
        "shelters_built": len(shelters),
        "survivors": _strict_int(
            metrics.get("living_survivors"),
            name="metrics.living_survivors",
        ),
        "deaths": deaths,
        "death_events": death_events,
    }


def _validate_failed_artifact(
    artifact: Mapping[str, object],
    *,
    receipt: Mapping[str, object],
) -> None:
    forbidden = {
        "result",
        "canonical_result_sha256",
        "metrics",
        "session_outcomes",
    }
    present = sorted(forbidden.intersection(artifact))
    if present:
        raise ValueError(
            "failed cell contains outcome/result fields: " + ", ".join(present)
        )
    if receipt.get("exact_replay") is not None:
        raise ValueError("failed cell must not claim an exact completed replay")
    if receipt.get("continuation_chain_verified") is not True:
        raise ValueError("failed cell lacks verified continuation ancestry")
    if receipt.get("failure_call_receipt_consistent") is not True:
        raise ValueError("failed cell lacks a verified failure boundary")
    failure = _mapping(artifact.get("failure"), name="failure")
    if receipt.get("failure_kind") != failure.get("kind"):
        raise ValueError("failed cell failure kind does not match verifier receipt")


def _call_cost_exposure(calls: Sequence[object]) -> Decimal:
    total = Decimal(0)
    for index, raw_call in enumerate(calls, start=1):
        call = _mapping(raw_call, name=f"call {index}")
        status = call.get("status")
        authorization = _mapping(
            call.get("cost_authorization"),
            name=f"call {index} cost authorization",
        )
        if status == "succeeded":
            field = "accounted_cost_usd"
        elif status == "failed":
            field = "request_cost_bound_usd"
        else:
            raise ValueError(f"terminal artifact call {index} has invalid status")
        total += _decimal(
            authorization.get(field),
            name=f"call {index} {field}",
        )
    return total


def _aggregate_rows(
    rows: Sequence[Mapping[str, object]],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    phases: list[dict[str, Any]] = []
    for phase in range(4):
        phase_rows = [row for row in rows if row["initiative_phase"] == phase]
        scoreable = [row for row in phase_rows if row["scoreable"] is True]
        censored = [row for row in phase_rows if row["censored"] is True]
        pending = [row for row in phase_rows if row["status"] == "pending"]
        successes = sum(
            row["primary_shelter_chain_by_end_of_chance_3"] is True
            for row in scoreable
        )
        phases.append(
            {
                "initiative_phase": phase,
                "planned_n": len(phase_rows),
                "scoreable_n": len(scoreable),
                "censored_n": len(censored),
                "pending_n": len(pending),
                "primary_success_count": successes,
                "primary_success_rate": (
                    successes / len(scoreable) if scoreable else None
                ),
            }
        )
    scoreable_n = sum(int(phase["scoreable_n"]) for phase in phases)
    censored_n = sum(int(phase["censored_n"]) for phase in phases)
    pending_n = sum(int(phase["pending_n"]) for phase in phases)
    successes = sum(int(phase["primary_success_count"]) for phase in phases)
    terminal = pending_n == 0 and scoreable_n + censored_n == len(rows)
    rates = [phase["primary_success_rate"] for phase in phases]
    rate_range = None
    if terminal and all(rate is not None for rate in rates):
        numeric_rates = [float(rate) for rate in rates]
        rate_range = max(numeric_rates) - min(numeric_rates)
    cost = sum(
        (
            _decimal(row["cost_exposure_usd"], name="row cost exposure")
            for row in rows
            if row["cost_exposure_usd"] is not None
        ),
        Decimal(0),
    )
    batch = {
        "planned_n": len(rows),
        "scoreable_n": scoreable_n,
        "censored_n": censored_n,
        "pending_n": pending_n,
        "terminal": terminal,
        "primary_success_count": successes,
        "primary_success_rate": successes / scoreable_n if scoreable_n else None,
        "max_minus_min_phase_rate": rate_range,
        "cost_exposure_usd": _decimal_text(cost),
    }
    return batch, phases


def _null_behavior() -> dict[str, None]:
    return {field: None for field in BEHAVIOR_FIELDS}


def _load_json_object(
    path: Path,
    *,
    name: str,
) -> tuple[Mapping[str, object], str]:
    raw = path.read_bytes()
    payload = VERIFIER._strict_json_bytes(raw)
    return _mapping(payload, name=name), hashlib.sha256(raw).hexdigest()


def _resolve_repo_path(
    value: object,
    *,
    name: str,
    repository_root: Path,
) -> Path:
    text = _text(value, name=name)
    path = Path(text)
    return path.resolve() if path.is_absolute() else (repository_root / path).resolve()


def _display_path(path: Path, *, repository_root: Path) -> str:
    try:
        return path.relative_to(repository_root).as_posix()
    except ValueError:
        return path.as_posix()


def _require_sha256(value: object, *, name: str) -> str:
    text = _text(value, name=name)
    if (
        len(text) != 64
        or text != text.casefold()
        or any(character not in "0123456789abcdef" for character in text)
    ):
        raise ValueError(f"{name} must be a lowercase SHA-256 digest")
    return text


def _require_exact(actual: object, expected: object, *, name: str) -> None:
    actual_json = json.dumps(
        actual,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    expected_json = json.dumps(
        expected,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    if actual_json != expected_json:
        raise ValueError(f"{name} does not match the frozen value")


def _mapping(value: object, *, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise TypeError(f"{name} must be an object")
    return value


def _sequence(value: object, *, name: str) -> Sequence[Any]:
    if not isinstance(value, list):
        raise TypeError(f"{name} must be an array")
    return value


def _strict_int(value: object, *, name: str) -> int:
    if type(value) is not int:
        raise ValueError(f"{name} must be an integer")
    return value


def _text(value: object, *, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{name} must be non-empty text")
    return value


def _decimal(value: object, *, name: str) -> Decimal:
    if not isinstance(value, str):
        raise TypeError(f"{name} must be decimal text")
    try:
        decimal = Decimal(value)
    except InvalidOperation as error:
        raise ValueError(f"{name} must be decimal text") from error
    if not decimal.is_finite() or decimal < 0:
        raise ValueError(f"{name} must be a finite non-negative decimal")
    return decimal


def _decimal_text(value: Decimal) -> str:
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    report = score_turn_order_matrix(args.manifest)
    rendered = json.dumps(
        report,
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    ) + "\n"
    if args.output is not None:
        args.output.write_text(rendered, encoding="utf-8", newline="\n")
    sys.stdout.write(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
