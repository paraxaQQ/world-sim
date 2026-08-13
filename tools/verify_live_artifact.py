from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = REPOSITORY_ROOT / "src"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from world_sim.survival.demo import result_sha256  # noqa: E402
from world_sim.survival.engine import (  # noqa: E402
    adjust_shared_resource,
    continue_survival_world,
    replay_survival,
)
from world_sim.survival.models import (  # noqa: E402
    SurvivalResult,
    SurvivalWorld,
)


SOURCE_FILES = {
    "cli_sha256": SOURCE_ROOT / "world_sim" / "cli.py",
    "model_host_sha256": SOURCE_ROOT / "world_sim" / "model_host.py",
    "demo_sha256": SOURCE_ROOT / "world_sim" / "survival" / "demo.py",
    "engine_sha256": SOURCE_ROOT / "world_sim" / "survival" / "engine.py",
    "models_sha256": SOURCE_ROOT / "world_sim" / "survival" / "models.py",
    "prompt_sha256": SOURCE_ROOT / "world_sim" / "survival" / "prompt.py",
    "protocol_sha256": SOURCE_ROOT / "world_sim" / "survival" / "protocol.py",
    "calibration_sha256": SOURCE_ROOT
    / "world_sim"
    / "survival"
    / "calibration.py",
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Verify a retained live artifact without provider calls."
    )
    parser.add_argument("artifact", type=Path)
    parser.add_argument("--artifact-sha256")
    parser.add_argument(
        "--parent",
        type=Path,
        help="actual format-v3 parent artifact for a format-v4 continuation",
    )
    return parser


def verify_live_artifact(
    path: Path,
    *,
    expected_artifact_sha256: str | None = None,
    parent_path: Path | None = None,
) -> dict[str, Any]:
    raw = path.read_bytes()
    artifact_sha256 = hashlib.sha256(raw).hexdigest()
    if (
        expected_artifact_sha256 is not None
        and artifact_sha256 != expected_artifact_sha256.casefold()
    ):
        raise ValueError(
            f"artifact SHA-256 mismatch: expected {expected_artifact_sha256}, "
            f"got {artifact_sha256}"
        )

    artifact = _mapping(json.loads(raw), name="artifact")
    source = _mapping(artifact.get("source"), name="source")
    source_receipt = _verify_source_receipt(source)

    base = {
        "artifact_sha256": artifact_sha256,
        **source_receipt,
    }
    format_version = artifact.get("format_version")
    mode = artifact.get("mode")
    continuation_fields_present = any(
        key in artifact
        for key in (
            "continuation_link",
            "transition_receipt",
            "public_record_receipt",
        )
    )
    if (
        format_version == 4
        or mode == "live_named_survival_continuation"
        or continuation_fields_present
        or parent_path is not None
    ):
        if format_version != 4 or mode != "live_named_survival_continuation":
            raise ValueError(
                "continuation artifact must use format_version 4 and "
                "mode live_named_survival_continuation"
            )
        if parent_path is None:
            raise ValueError("format-v4 continuation verification requires --parent")
        return _verify_continuation_artifact(
            artifact,
            parent_path=parent_path,
            base=base,
        )

    status = artifact.get("status")
    if status == "completed":
        payload = _mapping(artifact.get("result"), name="result")
        result = SurvivalResult(
            initial_state=dict(
                _mapping(payload.get("initial_state"), name="initial_state")
            ),
            final_state=dict(_mapping(payload.get("final_state"), name="final_state")),
            events=tuple(_sequence(payload.get("events"), name="events")),
            choice_tape=tuple(
                _sequence(payload.get("choice_tape"), name="choice_tape")
            ),
            event_sequence_base=int(payload.get("event_sequence_base", 0)),
        )
        if replay_survival(result).to_dict() != result.to_dict():
            raise ValueError("exact replay mismatch")
        canonical_result_sha256 = result_sha256(result)
        if artifact.get("canonical_result_sha256") != canonical_result_sha256:
            raise ValueError("canonical result SHA-256 mismatch")
        return {
            **base,
            "status": status,
            "canonical_result_sha256": canonical_result_sha256,
            "exact_replay": True,
        }
    if status == "failed":
        failure = _mapping(artifact.get("failure"), name="failure")
        _mapping(artifact.get("initial_state"), name="initial_state")
        _mapping(artifact.get("partial_state"), name="partial_state")
        calls = _sequence(artifact.get("calls"), name="calls")
        call_sequence = failure.get("call_sequence")
        if call_sequence is not None:
            if type(call_sequence) is not int or call_sequence < 1:
                raise ValueError("failure call_sequence must be positive or null")
            matching = [
                _mapping(call, name="call")
                for call in calls
                if isinstance(call, Mapping) and call.get("sequence") == call_sequence
            ]
            if len(matching) != 1 or matching[0].get("status") != "failed":
                raise ValueError("failure does not identify one failed call")
            error = _mapping(matching[0].get("error"), name="call error")
            call = matching[0]
            pairs = (
                ("day", "day"),
                ("cycle", "cycle"),
                ("slot", "slot"),
                ("seat_id", "seat_id"),
                ("public_name", "public_name"),
                ("model", "model"),
            )
            if any(failure.get(left) != call.get(right) for left, right in pairs):
                raise ValueError("failure identity does not match its call receipt")
            error_pairs = (
                ("kind", "kind"),
                ("message", "message"),
                ("http_status", "http_status"),
            )
            if any(
                failure.get(left) != error.get(right)
                for left, right in error_pairs
            ):
                raise ValueError("failure error does not match its call receipt")
        return {
            **base,
            "status": status,
            "failure_kind": failure.get("kind"),
            "failure_call_receipt_consistent": True,
            "exact_replay": None,
        }
    raise ValueError("artifact status must be completed or failed")


def _verify_continuation_artifact(
    artifact: Mapping[str, Any],
    *,
    parent_path: Path,
    base: Mapping[str, Any],
) -> dict[str, Any]:
    link = _mapping(artifact.get("continuation_link"), name="continuation_link")
    try:
        parent_raw = parent_path.read_bytes()
    except OSError as error:
        raise ValueError(f"cannot read continuation parent from {parent_path}") from error
    parent_artifact_sha256 = hashlib.sha256(parent_raw).hexdigest()
    if link.get("parent_artifact_sha256") != parent_artifact_sha256:
        raise ValueError(
            "continuation parent artifact SHA-256 mismatch: "
            f"expected {link.get('parent_artifact_sha256')}, "
            f"got {parent_artifact_sha256}"
        )

    parent_artifact = _mapping(
        json.loads(parent_raw),
        name="parent artifact",
    )
    if (
        parent_artifact.get("format_version") != 3
        or parent_artifact.get("mode") != "live_named_survival"
    ):
        raise ValueError(
            "continuation parent must use format_version 3 and mode "
            "live_named_survival"
        )
    if parent_artifact.get("status") != "completed":
        raise ValueError("continuation parent must be completed")
    if (
        link.get("parent_format_version") != parent_artifact.get("format_version")
        or link.get("parent_mode") != parent_artifact.get("mode")
    ):
        raise ValueError("continuation parent format or mode does not match its link")

    parent_receipt = verify_live_artifact(
        parent_path,
        expected_artifact_sha256=parent_artifact_sha256,
    )
    parent_canonical_sha256 = parent_receipt.get("canonical_result_sha256")
    if link.get("parent_canonical_result_sha256") != parent_canonical_sha256:
        raise ValueError("continuation parent canonical result SHA-256 mismatch")
    parent_result = _survival_result(
        _mapping(parent_artifact.get("result"), name="parent result")
    )

    config = _mapping(artifact.get("config"), name="config")
    additional_cycles = config.get("cycles_requested")
    if (
        isinstance(additional_cycles, bool)
        or not isinstance(additional_cycles, int)
        or additional_cycles < 1
    ):
        raise ValueError("config cycles_requested must be a positive integer")
    expected_world = continue_survival_world(
        parent_result,
        additional_cycles=additional_cycles,
    )

    transition_receipt = _mapping(
        artifact.get("transition_receipt"),
        name="transition_receipt",
    )
    if transition_receipt.get("method") != (
        "deterministic_between_cycle_shared_resource_adjustment"
    ):
        raise ValueError("transition receipt method is invalid")
    transition = _mapping(
        transition_receipt.get("event"),
        name="transition event",
    )
    transition_detail = _mapping(
        transition.get("detail"),
        name="transition event detail",
    )
    resource = transition_detail.get("resource")
    stock = transition_detail.get("after")
    reason = transition_detail.get("reason")
    if not isinstance(resource, str):
        raise ValueError("transition resource must be a string")
    if isinstance(stock, bool) or not isinstance(stock, int):
        raise ValueError("transition after stock must be an integer")
    if not isinstance(reason, str):
        raise ValueError("transition reason must be a string")
    expected_transition = adjust_shared_resource(
        expected_world,
        resource=resource,
        stock=stock,
        reason=reason,
    ).to_dict()
    if dict(transition) != expected_transition:
        raise ValueError("transition receipt does not match the reconstructed event")

    public_record = expected_world.prior_public_record
    if public_record is None:
        raise ValueError("reconstructed continuation has no prior public record")
    expected_public_record_receipt = {
        "method": "final_public_broadcast_per_identity_verbatim",
        "statement_status": "unverified",
        "objective_totals_source": "verified_parent_engine_events",
        "record": public_record.to_dict(),
    }
    actual_public_record_receipt = _mapping(
        artifact.get("public_record_receipt"),
        name="public_record_receipt",
    )
    if dict(actual_public_record_receipt) != expected_public_record_receipt:
        raise ValueError(
            "public record receipt does not match the verified parent result"
        )

    if config.get("seed") != expected_world.seed:
        raise ValueError("continuation config seed does not match the parent")
    if config.get("starting_cycle") != expected_world.day + 1:
        raise ValueError("continuation config starting_cycle is invalid")
    if config.get("ending_cycle") != expected_world.day + additional_cycles:
        raise ValueError("continuation config ending_cycle is invalid")
    if config.get("world_config") != expected_world.config.to_dict():
        raise ValueError("continuation world_config does not match reconstruction")

    status = artifact.get("status")
    if status == "completed":
        result = _survival_result(
            _mapping(artifact.get("result"), name="result")
        )
        expected_initial_state = _continued_initial_state(
            expected_world,
            include_observation_history=True,
        )
        if result.initial_state != expected_initial_state:
            raise ValueError(
                "child initial state does not match the reconstructed continuation"
            )
        expected_event_sequence_base = (
            expected_world.event_sequence_offset + len(expected_world.events)
        )
        if result.event_sequence_base != expected_event_sequence_base:
            raise ValueError("child event_sequence_base is invalid")
        _verify_contiguous_events(
            result.initial_state["observation_history"],
            offset=expected_world.event_sequence_offset,
            name="child observation history",
        )
        _verify_contiguous_events(
            result.events,
            offset=result.event_sequence_base,
            name="child result events",
        )
        if replay_survival(result).to_dict() != result.to_dict():
            raise ValueError("exact replay mismatch")
        canonical_result_sha256 = result_sha256(result)
        if artifact.get("canonical_result_sha256") != canonical_result_sha256:
            raise ValueError("canonical result SHA-256 mismatch")
        return {
            **base,
            "status": status,
            "canonical_result_sha256": canonical_result_sha256,
            "exact_replay": True,
            "continuation_chain_verified": True,
            "parent_artifact_sha256": parent_artifact_sha256,
            "parent_canonical_result_sha256": parent_canonical_sha256,
        }
    if status == "failed":
        expected_initial_state = _continued_initial_state(
            expected_world,
            include_observation_history=False,
        )
        initial_state = _mapping(
            artifact.get("initial_state"),
            name="initial_state",
        )
        if dict(initial_state) != expected_initial_state:
            raise ValueError(
                "child initial state does not match the reconstructed continuation"
            )
        partial_state = _mapping(
            artifact.get("partial_state"),
            name="partial_state",
        )
        _verify_failed_partial_state(expected_world, partial_state)
        failure = _mapping(artifact.get("failure"), name="failure")
        calls = _sequence(artifact.get("calls"), name="calls")
        _verify_continuation_failure_receipt(
            failure,
            calls=calls,
            assignments=_sequence(
                artifact.get("seat_assignments"),
                name="seat_assignments",
            ),
        )
        return {
            **base,
            "status": status,
            "failure_kind": failure.get("kind"),
            "failure_call_receipt_consistent": True,
            "exact_replay": None,
            "continuation_chain_verified": True,
            "parent_artifact_sha256": parent_artifact_sha256,
            "parent_canonical_result_sha256": parent_canonical_sha256,
        }
    raise ValueError("artifact status must be completed or failed")


def _survival_result(payload: Mapping[str, Any]) -> SurvivalResult:
    return SurvivalResult(
        initial_state=dict(
            _mapping(payload.get("initial_state"), name="initial_state")
        ),
        final_state=dict(_mapping(payload.get("final_state"), name="final_state")),
        events=tuple(_sequence(payload.get("events"), name="events")),
        choice_tape=tuple(
            _sequence(payload.get("choice_tape"), name="choice_tape")
        ),
        event_sequence_base=int(payload.get("event_sequence_base", 0)),
    )


def _continued_initial_state(
    world: SurvivalWorld,
    *,
    include_observation_history: bool,
) -> dict[str, Any]:
    state = world.to_dict(include_events=False)
    if include_observation_history:
        state["event_sequence_offset"] = world.event_sequence_offset
        state["observation_history"] = [event.to_dict() for event in world.events]
    return state


def _verify_contiguous_events(
    events: Sequence[Any],
    *,
    offset: int,
    name: str,
) -> None:
    expected = list(range(offset + 1, offset + len(events) + 1))
    actual = [
        int(_mapping(event, name=f"{name} event").get("sequence", -1))
        for event in events
    ]
    if actual != expected:
        raise ValueError(f"{name} must have contiguous event sequences")


def _verify_failed_partial_state(
    expected_world: SurvivalWorld,
    partial_state: Mapping[str, Any],
) -> None:
    if partial_state.get("config") != expected_world.config.to_dict():
        raise ValueError("failed partial state config does not match reconstruction")
    if partial_state.get("seed") != expected_world.seed:
        raise ValueError("failed partial state seed does not match reconstruction")
    expected_record = expected_world.prior_public_record
    if expected_record is None or partial_state.get(
        "prior_public_record"
    ) != expected_record.to_dict():
        raise ValueError(
            "failed partial state public record does not match reconstruction"
        )
    events = _sequence(partial_state.get("events"), name="partial_state events")
    expected_prefix = [event.to_dict() for event in expected_world.events]
    if list(events[: len(expected_prefix)]) != expected_prefix:
        raise ValueError("failed partial state does not retain the continuation history")
    _verify_contiguous_events(
        events,
        offset=expected_world.event_sequence_offset,
        name="failed partial state events",
    )


def _verify_continuation_failure_receipt(
    failure: Mapping[str, Any],
    *,
    calls: Sequence[Any],
    assignments: Sequence[Any],
) -> None:
    if failure.get("day") != failure.get("cycle"):
        raise ValueError("failure day and cycle aliases disagree")
    for key in ("cycle", "slot"):
        value = failure.get(key)
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ValueError(f"failure {key} must be a positive integer")
    call_sequence = failure.get("call_sequence")
    if call_sequence is not None:
        if type(call_sequence) is not int or call_sequence < 1:
            raise ValueError("failure call_sequence must be positive or null")
        matching = [
            _mapping(call, name="call")
            for call in calls
            if isinstance(call, Mapping) and call.get("sequence") == call_sequence
        ]
        if len(matching) != 1 or matching[0].get("status") != "failed":
            raise ValueError("failure does not identify one failed call")
        call = matching[0]
        error = _mapping(call.get("error"), name="call error")
        pairs = (
            ("day", "day"),
            ("cycle", "cycle"),
            ("slot", "slot"),
            ("seat_id", "seat_id"),
            ("public_name", "public_name"),
            ("model", "model"),
        )
        if any(failure.get(left) != call.get(right) for left, right in pairs):
            raise ValueError("failure identity does not match its call receipt")
        error_pairs = (
            ("kind", "kind"),
            ("message", "message"),
            ("http_status", "http_status"),
        )
        if any(failure.get(left) != error.get(right) for left, right in error_pairs):
            raise ValueError("failure error does not match its call receipt")
        return

    if failure.get("kind") not in {"call_cap_reached", "paid_budget_exhausted"}:
        raise ValueError("failure without a call receipt has an invalid kind")
    matching_assignments = [
        _mapping(assignment, name="seat assignment")
        for assignment in assignments
        if isinstance(assignment, Mapping)
        and assignment.get("public_name") == failure.get("public_name")
    ]
    if len(matching_assignments) != 1:
        raise ValueError("failure does not identify one seat assignment")
    assignment = matching_assignments[0]
    if (
        failure.get("seat_id") != assignment.get("seat_id")
        or failure.get("model") != assignment.get("model")
    ):
        raise ValueError("failure identity does not match its seat assignment")
    if not isinstance(failure.get("message"), str):
        raise ValueError("failure message must be a string")
    if failure.get("http_status") is not None:
        raise ValueError("failure without a provider call cannot have an HTTP status")


def _verify_source_receipt(source: Mapping[str, Any]) -> dict[str, Any]:
    recorded = {key: source.get(key) for key in SOURCE_FILES}
    current = {
        key: hashlib.sha256(path.read_bytes()).hexdigest()
        for key, path in SOURCE_FILES.items()
    }
    if recorded == current:
        return {
            "source_hashes_matched": len(SOURCE_FILES),
            "source_match": "working_tree",
            "source_commit": None,
        }

    marker = SOURCE_FILES["model_host_sha256"].relative_to(REPOSITORY_ROOT)
    history = subprocess.run(
        ("git", "log", "--all", "--format=%H", "--", marker.as_posix()),
        cwd=REPOSITORY_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    if history.returncode == 0:
        for commit in history.stdout.splitlines():
            if _commit_source_hashes(commit) == recorded:
                return {
                    "source_hashes_matched": len(SOURCE_FILES),
                    "source_match": "git_commit",
                    "source_commit": commit,
                }

    mismatched_key = next(key for key in SOURCE_FILES if recorded[key] != current[key])
    source_path = SOURCE_FILES[mismatched_key]
    raise ValueError(
        f"source SHA-256 mismatch for {source_path.relative_to(REPOSITORY_ROOT)}: "
        f"expected {recorded[mismatched_key]}, got {current[mismatched_key]}"
    )


def _commit_source_hashes(commit: str) -> dict[str, str] | None:
    result: dict[str, str] = {}
    for key, source_path in SOURCE_FILES.items():
        relative = source_path.relative_to(REPOSITORY_ROOT).as_posix()
        blob = subprocess.run(
            ("git", "show", f"{commit}:{relative}"),
            cwd=REPOSITORY_ROOT,
            check=False,
            capture_output=True,
        )
        if blob.returncode != 0:
            return None
        result[key] = hashlib.sha256(blob.stdout).hexdigest()
    return result


def _mapping(value: object, *, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{name} must be an object")
    return value


def _sequence(value: object, *, name: str) -> Sequence[Any]:
    if not isinstance(value, list):
        raise ValueError(f"{name} must be an array")
    return value


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    receipt = verify_live_artifact(
        args.artifact,
        expected_artifact_sha256=args.artifact_sha256,
        parent_path=args.parent,
    )
    print(json.dumps(receipt, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
