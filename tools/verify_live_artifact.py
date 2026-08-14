from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from collections.abc import Mapping, Sequence
from copy import deepcopy
from decimal import Decimal, InvalidOperation
from functools import lru_cache
from pathlib import Path
from typing import Any, NoReturn


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = REPOSITORY_ROOT / "src"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from world_sim.survival.demo import result_sha256  # noqa: E402
from world_sim.model_host import (  # noqa: E402
    _Assignment,
    _build_request,
    _parse_model_ref,
    _recorded_paid_budget,
    _verify_recorded_cost_authorization,
)
from world_sim.survival.engine import (  # noqa: E402
    adjust_shared_resource,
    continue_survival_world,
    replay_survival,
    run_survival,
)
from world_sim.survival.models import (  # noqa: E402
    SEQUENTIAL_DIALOGUE_V3,
    SurvivalResult,
    SurvivalWorld,
)
from world_sim.survival.protocol import parse_model_response  # noqa: E402
SOURCE_FILES = {
    "cli_sha256": SOURCE_ROOT / "world_sim" / "cli.py",
    "model_host_sha256": SOURCE_ROOT / "world_sim" / "model_host.py",
    "demo_sha256": SOURCE_ROOT / "world_sim" / "survival" / "demo.py",
    "engine_sha256": SOURCE_ROOT / "world_sim" / "survival" / "engine.py",
    "models_sha256": SOURCE_ROOT / "world_sim" / "survival" / "models.py",
    "prompt_sha256": SOURCE_ROOT / "world_sim" / "survival" / "prompt.py",
    "protocol_sha256": SOURCE_ROOT / "world_sim" / "survival" / "protocol.py",
    "calibration_sha256": SOURCE_ROOT / "world_sim" / "survival" / "calibration.py",
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
        help="direct parent artifact for a continuation",
    )
    parser.add_argument(
        "--ancestor",
        action="append",
        default=[],
        type=Path,
        help=(
            "ancestor before --parent, repeat oldest to newest; exclude the "
            "direct parent"
        ),
    )
    return parser


def verify_live_artifact(
    path: Path,
    *,
    expected_artifact_sha256: str | None = None,
    parent_path: Path | None = None,
    ancestor_paths: Sequence[Path] = (),
) -> dict[str, Any]:
    ancestors = tuple(ancestor_paths)
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

    artifact = _mapping(_strict_json_bytes(raw), name="artifact")
    if "ancestor_chain" in artifact:
        raise ValueError("artifact must not store a redundant ancestor_chain")
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
            "assignment_transition_receipts",
        )
    )
    if (
        format_version in {4, 5, 6}
        or mode == "live_named_survival_continuation"
        or continuation_fields_present
        or parent_path is not None
        or ancestors
    ):
        if format_version not in {4, 5, 6} or mode != (
            "live_named_survival_continuation"
        ):
            raise ValueError(
                "continuation artifact must use format_version 4, 5, or 6 and "
                "mode live_named_survival_continuation"
            )
        if parent_path is None:
            raise ValueError(
                f"format-v{format_version} continuation verification requires --parent"
            )
        if format_version == 4 and ancestors:
            raise ValueError("format-v4 continuation does not accept --ancestor")
        return _verify_continuation_artifact(
            artifact,
            parent_path=parent_path,
            ancestor_paths=ancestors,
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
            "continuation_depth": 0,
            "root_artifact_sha256": artifact_sha256,
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
                failure.get(left) != error.get(right) for left, right in error_pairs
            ):
                raise ValueError("failure error does not match its call receipt")
        return {
            **base,
            "status": status,
            "failure_kind": failure.get("kind"),
            "failure_call_receipt_consistent": True,
            "exact_replay": None,
            "continuation_depth": 0,
            "root_artifact_sha256": artifact_sha256,
        }
    raise ValueError("artifact status must be completed or failed")


def _verify_continuation_artifact(
    artifact: Mapping[str, Any],
    *,
    parent_path: Path,
    ancestor_paths: Sequence[Path],
    base: Mapping[str, Any],
) -> dict[str, Any]:
    link = _mapping(artifact.get("continuation_link"), name="continuation_link")
    try:
        parent_raw = parent_path.read_bytes()
    except OSError as error:
        raise ValueError(
            f"cannot read continuation parent from {parent_path}"
        ) from error
    parent_artifact_sha256 = hashlib.sha256(parent_raw).hexdigest()
    if link.get("parent_artifact_sha256") != parent_artifact_sha256:
        raise ValueError(
            "continuation parent artifact SHA-256 mismatch: "
            f"expected {link.get('parent_artifact_sha256')}, "
            f"got {parent_artifact_sha256}"
        )

    parent_artifact = _mapping(
        _strict_json_bytes(parent_raw),
        name="parent artifact",
    )
    child_format = artifact.get("format_version")
    parent_format = parent_artifact.get("format_version")
    parent_mode = parent_artifact.get("mode")
    if child_format == 4:
        valid_parent = parent_format == 3 and parent_mode == "live_named_survival"
        if not valid_parent:
            raise ValueError(
                "format-v4 continuation parent must use format_version 3 and "
                "mode live_named_survival"
            )
    elif child_format == 5:
        valid_parent = parent_format in {4, 5} and parent_mode == (
            "live_named_survival_continuation"
        )
        if not valid_parent:
            raise ValueError(
                "format-v5 continuation parent must use format_version 4 or 5 "
                "and mode live_named_survival_continuation"
            )
    else:
        valid_parent = parent_format in {4, 5, 6} and parent_mode == (
            "live_named_survival_continuation"
        )
        if not valid_parent:
            raise ValueError(
                "format-v6 continuation parent must use format_version 4, 5, "
                "or 6 and mode live_named_survival_continuation"
            )
    if parent_artifact.get("status") != "completed":
        raise ValueError("continuation parent must be completed")
    if (
        link.get("parent_format_version") != parent_format
        or link.get("parent_mode") != parent_mode
    ):
        raise ValueError("continuation parent format or mode does not match its link")

    if parent_format == 3:
        if ancestor_paths:
            raise ValueError("format-v3 root must be the oldest supplied artifact")
        parent_receipt = verify_live_artifact(
            parent_path,
            expected_artifact_sha256=parent_artifact_sha256,
        )
    else:
        if not ancestor_paths:
            raise ValueError(
                "format-v5 continuation requires its complete ancestor chain"
            )
        parent_receipt = verify_live_artifact(
            parent_path,
            expected_artifact_sha256=parent_artifact_sha256,
            parent_path=ancestor_paths[-1],
            ancestor_paths=ancestor_paths[:-1],
        )
    parent_canonical_sha256 = parent_receipt.get("canonical_result_sha256")
    if link.get("parent_canonical_result_sha256") != parent_canonical_sha256:
        raise ValueError("continuation parent canonical result SHA-256 mismatch")
    parent_result = _survival_result(
        _mapping(parent_artifact.get("result"), name="parent result")
    )

    config = _mapping(artifact.get("config"), name="config")
    if child_format == 6:
        if config.get("interaction_protocol") != SEQUENTIAL_DIALOGUE_V3:
            raise ValueError(
                "format-v6 continuation must use interaction_protocol "
                "sequential-dialogue-v3"
            )
        _verify_assignment_transitions(
            parent_assignments=_sequence(
                parent_artifact.get("seat_assignments"),
                name="parent seat_assignments",
            ),
            child_assignments=_sequence(
                artifact.get("seat_assignments"),
                name="seat_assignments",
            ),
            receipts=_sequence(
                artifact.get("assignment_transition_receipts"),
                name="assignment_transition_receipts",
            ),
        )
    elif "assignment_transition_receipts" in artifact:
        raise ValueError(
            "assignment_transition_receipts requires format_version 6"
        )
    additional_cycles = config.get("cycles_requested")
    if (
        isinstance(additional_cycles, bool)
        or not isinstance(additional_cycles, int)
        or additional_cycles < 1
    ):
        raise ValueError("config cycles_requested must be a positive integer")
    continuation_options: dict[str, Any] = {}
    if "interaction_protocol" in config:
        continuation_options["interaction_protocol"] = config["interaction_protocol"]
    expected_world = continue_survival_world(
        parent_result,
        additional_cycles=additional_cycles,
        **continuation_options,
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
    if not _reconstructed_equal(
        dict(transition),
        expected_transition,
        strict_json_types=child_format == 6,
    ):
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
    if not _reconstructed_equal(
        dict(actual_public_record_receipt),
        expected_public_record_receipt,
        strict_json_types=child_format == 6,
    ):
        raise ValueError(
            "public record receipt does not match the verified parent result"
        )

    strict_json_types = child_format == 6
    if not _reconstructed_equal(
        config.get("seed"),
        expected_world.seed,
        strict_json_types=strict_json_types,
    ):
        raise ValueError("continuation config seed does not match the parent")
    if not _reconstructed_equal(
        config.get("starting_cycle"),
        expected_world.day + 1,
        strict_json_types=strict_json_types,
    ):
        raise ValueError("continuation config starting_cycle is invalid")
    if not _reconstructed_equal(
        config.get("ending_cycle"),
        expected_world.day + additional_cycles,
        strict_json_types=strict_json_types,
    ):
        raise ValueError("continuation config ending_cycle is invalid")
    if not _reconstructed_equal(
        config.get("world_config"),
        expected_world.config.to_dict(),
        strict_json_types=strict_json_types,
    ):
        raise ValueError("continuation world_config does not match reconstruction")

    status = artifact.get("status")
    if status == "completed":
        result = _survival_result(_mapping(artifact.get("result"), name="result"))
        expected_initial_state = _continued_initial_state(
            expected_world,
            include_observation_history=True,
        )
        if not _reconstructed_equal(
            result.initial_state,
            expected_initial_state,
            strict_json_types=strict_json_types,
        ):
            raise ValueError(
                "child initial state does not match the reconstructed continuation"
            )
        expected_event_sequence_base = expected_world.event_sequence_offset + len(
            expected_world.events
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
        if child_format == 6:
            _verify_completed_sequential_result(
                expected_world,
                result,
                additional_cycles=additional_cycles,
                calls=_sequence(artifact.get("calls"), name="calls"),
                assignments=_sequence(
                    artifact.get("seat_assignments"),
                    name="seat_assignments",
                ),
                config=config,
            )
        if not _reconstructed_equal(
            replay_survival(result).to_dict(),
            result.to_dict(),
            strict_json_types=strict_json_types,
        ):
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
            "continuation_depth": int(parent_receipt["continuation_depth"]) + 1,
            "root_artifact_sha256": parent_receipt["root_artifact_sha256"],
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
        if not _reconstructed_equal(
            dict(initial_state),
            expected_initial_state,
            strict_json_types=strict_json_types,
        ):
            raise ValueError(
                "child initial state does not match the reconstructed continuation"
            )
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
        partial_state = _mapping(
            artifact.get("partial_state"),
            name="partial_state",
        )
        _verify_failed_partial_state(
            expected_world,
            partial_state,
            failure=failure,
            calls=calls,
            assignments=_sequence(
                artifact.get("seat_assignments"),
                name="seat_assignments",
            ),
            config=config,
            verify_call_assignments=child_format == 6,
            verify_request_view=child_format == 6,
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
            "continuation_depth": int(parent_receipt["continuation_depth"]) + 1,
            "root_artifact_sha256": parent_receipt["root_artifact_sha256"],
        }
    raise ValueError("artifact status must be completed or failed")


def _survival_result(payload: Mapping[str, Any]) -> SurvivalResult:
    return SurvivalResult(
        initial_state=dict(
            _mapping(payload.get("initial_state"), name="initial_state")
        ),
        final_state=dict(_mapping(payload.get("final_state"), name="final_state")),
        events=tuple(_sequence(payload.get("events"), name="events")),
        choice_tape=tuple(_sequence(payload.get("choice_tape"), name="choice_tape")),
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


def _assignment_map(
    assignments: Sequence[Any],
    *,
    name: str,
) -> dict[str, dict[str, str]]:
    by_public_name: dict[str, dict[str, str]] = {}
    seat_ids: set[str] = set()
    for index, raw_assignment in enumerate(assignments):
        assignment = _mapping(
            raw_assignment,
            name=f"{name}[{index}]",
        )
        if set(assignment) != {"seat_id", "public_name", "model"}:
            raise ValueError(
                f"{name}[{index}] must contain exactly seat_id, public_name, and model"
            )
        values = {
            key: assignment.get(key)
            for key in ("seat_id", "public_name", "model")
        }
        if any(not isinstance(value, str) or not value for value in values.values()):
            raise ValueError(f"{name}[{index}] fields must be non-empty strings")
        seat_id = str(values["seat_id"])
        public_name = str(values["public_name"])
        if seat_id in seat_ids:
            raise ValueError(f"{name} contains duplicate seat_id {seat_id!r}")
        if public_name in by_public_name:
            raise ValueError(
                f"{name} contains duplicate public_name {public_name!r}"
            )
        seat_ids.add(seat_id)
        by_public_name[public_name] = {
            "seat_id": seat_id,
            "public_name": public_name,
            "model": str(values["model"]),
        }
    if not by_public_name:
        raise ValueError(f"{name} must not be empty")
    return by_public_name


def _verify_assignment_transitions(
    *,
    parent_assignments: Sequence[Any],
    child_assignments: Sequence[Any],
    receipts: Sequence[Any],
) -> None:
    parent = _assignment_map(parent_assignments, name="parent seat_assignments")
    child = _assignment_map(child_assignments, name="seat_assignments")
    if set(parent) != set(child):
        raise ValueError(
            "child seat assignments must preserve every parent public name"
        )
    for public_name, parent_assignment in parent.items():
        if child[public_name]["seat_id"] != parent_assignment["seat_id"]:
            raise ValueError(
                "child seat assignments must preserve the parent seat_id for "
                f"{public_name}"
            )

    if len(receipts) > 1:
        raise ValueError(
            "format-v6 continuation accepts at most one "
            "assignment_transition_receipt"
        )
    receipt_by_public_name: dict[str, Mapping[str, Any]] = {}
    receipt_seat_ids: set[str] = set()
    expected_keys = {
        "seat_id",
        "public_name",
        "previous_model",
        "replacement_model",
        "reason",
    }
    for index, raw_receipt in enumerate(receipts):
        receipt = _mapping(
            raw_receipt,
            name=f"assignment_transition_receipts[{index}]",
        )
        if set(receipt) != expected_keys:
            raise ValueError(
                "assignment transition receipt must contain exactly seat_id, "
                "public_name, previous_model, replacement_model, and reason"
            )
        if any(
            not isinstance(receipt.get(key), str) or not receipt.get(key)
            for key in expected_keys
        ):
            raise ValueError(
                "assignment transition receipt fields must be non-empty strings"
            )
        reason = str(receipt["reason"])
        if (
            reason != reason.strip()
            or len(reason) > 500
            or not reason.isprintable()
        ):
            raise ValueError(
                "assignment transition reason must be 1-500 printable characters "
                "without outer whitespace"
            )
        public_name = str(receipt["public_name"])
        seat_id = str(receipt["seat_id"])
        if public_name in receipt_by_public_name or seat_id in receipt_seat_ids:
            raise ValueError("assignment transition receipts contain a duplicate seat")
        parent_assignment = parent.get(public_name)
        if parent_assignment is None:
            raise ValueError(
                "assignment transition receipt identifies an unknown public name"
            )
        if seat_id != parent_assignment["seat_id"]:
            raise ValueError(
                "assignment transition receipt seat_id does not match the parent"
            )
        receipt_by_public_name[public_name] = receipt
        receipt_seat_ids.add(seat_id)

    changed_names = {
        public_name
        for public_name in parent
        if parent[public_name]["model"] != child[public_name]["model"]
    }
    if set(receipt_by_public_name) != changed_names:
        raise ValueError(
            "assignment transition receipts do not exactly explain the model changes"
        )
    for public_name, receipt in receipt_by_public_name.items():
        parent_assignment = parent[public_name]
        child_assignment = child[public_name]
        if receipt["previous_model"] != parent_assignment["model"]:
            raise ValueError(
                "assignment transition receipt previous_model does not match the parent"
            )
        if receipt["replacement_model"] != child_assignment["model"]:
            raise ValueError(
                "assignment transition receipt replacement_model does not match the child"
            )


def _verify_call_request_view(
    call: Mapping[str, Any],
    view: Any,
    *,
    assignment: Mapping[str, str],
    config: Mapping[str, Any],
) -> None:
    try:
        request = _mapping(call.get("request"), name="continuation call request")
        recorded_assignment = _assignment_from_receipt(assignment)
        expected_request = _build_request(
            recorded_assignment,
            view,
            max_completion_tokens=int(config["max_completion_tokens"]),
            temperature=config["temperature"],
            reasoning_effort=(
                None
                if config.get("reasoning_effort") == "provider-default"
                else str(config["reasoning_effort"])
            ),
        )
    except (KeyError, TypeError, ValueError) as error:
        raise _ReceiptMismatch(
            "sequential continuation request cannot be reconstructed"
        ) from error
    if call.get("endpoint") != recorded_assignment.endpoint.url:
        raise _ReceiptMismatch(
            "continuation call endpoint does not match its seat assignment"
        )
    if dict(request) != expected_request:
        raise _ReceiptMismatch(
            "continuation call request does not match the reconstructed view"
        )


def _assignment_from_receipt(assignment: Mapping[str, str]) -> _Assignment:
    endpoint, model_id = _parse_model_ref(assignment["model"])
    return _Assignment(
        seat_id=assignment["seat_id"],
        public_name=assignment["public_name"],
        model_ref=assignment["model"],
        endpoint=endpoint,
        model_id=model_id,
    )


def _verify_successful_sequential_call(
    call: Mapping[str, Any],
    view: Any,
    *,
    assignment: Mapping[str, str],
    config: Mapping[str, Any],
    paid_budget: Any,
) -> dict[str, Any]:
    try:
        recorded_assignment = _assignment_from_receipt(assignment)
        response = _mapping(call.get("response"), name="continuation call response")
        if response.get("provider_model") != recorded_assignment.model_id:
            raise ValueError("provider model does not match its seat assignment")
        model_reply = response.get("model_reply")
        if not isinstance(model_reply, str):
            raise ValueError("successful continuation call has no model reply")
        parsed = parse_model_response(
            model_reply,
            actor=view.name,
            living_peers=tuple(str(peer["name"]) for peer in view.others),
            max_food_eaten=int(view.rules["max_food_eaten"]),
            max_speech_chars=int(view.rules["max_speech_chars"]),
            interaction_protocol=view.interaction_protocol,
        )
        parsed_choice = parsed.to_dict()
        if call.get("parsed_choice") != parsed_choice:
            raise ValueError("parsed choice does not match the raw model reply")
        if call.get("validation") != {
            "action_error": parsed.action_error,
            "speech_error": parsed.speech_error,
        }:
            raise ValueError("validation does not match the raw model reply")
        request = _mapping(call.get("request"), name="continuation call request")
        _verify_recorded_cost_authorization(
            assignment=recorded_assignment,
            request=request,
            record=call,
            paid_budget=paid_budget,
            successful=True,
        )
    except (KeyError, TypeError, ValueError) as error:
        raise _ReceiptMismatch(
            f"successful continuation call receipt is inconsistent: {error}"
        ) from error
    return parsed_choice


def _verify_completed_sequential_result(
    expected_world: SurvivalWorld,
    result: SurvivalResult,
    *,
    additional_cycles: int,
    calls: Sequence[Any],
    assignments: Sequence[Any],
    config: Mapping[str, Any],
) -> None:
    replay = _CompletedReceiptReplay(
        calls=calls,
        assignments=assignments,
        config=config,
    )
    replayed_world = deepcopy(expected_world)
    providers = {name: replay for name in replayed_world.alive_names()}
    try:
        replayed_result = run_survival(
            replayed_world,
            providers,
            days=additional_cycles,
        )
    except RuntimeError as error:
        if isinstance(error.__cause__, _ReceiptMismatch):
            raise ValueError(str(error.__cause__)) from error
        raise ValueError(
            "completed sequential result could not be reconstructed from calls"
        ) from error
    if replay.consumed != len(calls):
        raise ValueError("completed sequential calls continue after world completion")
    if not _reconstructed_equal(
        replayed_result.to_dict(),
        result.to_dict(),
        strict_json_types=True,
    ):
        raise ValueError(
            "completed sequential calls do not reconstruct the recorded result"
        )


def _verify_failed_partial_state(
    expected_world: SurvivalWorld,
    partial_state: Mapping[str, Any],
    *,
    failure: Mapping[str, Any],
    calls: Sequence[Any],
    assignments: Sequence[Any],
    config: Mapping[str, Any],
    verify_call_assignments: bool,
    verify_request_view: bool,
) -> None:
    replay = _FailedReceiptReplay(
        failure=failure,
        calls=calls,
        assignments=assignments,
        config=config,
        verify_call_assignments=verify_call_assignments,
        verify_request_view=verify_request_view,
    )
    replayed_world = deepcopy(expected_world)
    providers = {name: replay for name in replayed_world.alive_names()}
    try:
        run_survival(replayed_world, providers)
    except RuntimeError as error:
        if isinstance(error.__cause__, _ReceiptMismatch):
            raise ValueError(str(error.__cause__)) from error
        if not isinstance(error.__cause__, _RecordedFailure):
            raise ValueError(
                "failed partial state could not be reconstructed"
            ) from error
    else:
        raise ValueError("failure receipt did not interrupt reconstructed execution")
    if replay.consumed != len(calls):
        raise ValueError("failure call receipts continue after the recorded failure")
    if not _reconstructed_equal(
        replayed_world.to_dict(),
        dict(partial_state),
        strict_json_types=verify_call_assignments,
    ):
        raise ValueError(
            "failed partial state does not match reconstructed recorded calls"
        )


class _RecordedFailure(Exception):
    pass


class _ReceiptMismatch(Exception):
    pass


class _CompletedReceiptReplay:
    def __init__(
        self,
        *,
        calls: Sequence[Any],
        assignments: Sequence[Any],
        config: Mapping[str, Any],
    ) -> None:
        self.calls = calls
        self.assignments = _assignment_map(assignments, name="seat_assignments")
        self.config = config
        self.paid_budget = _recorded_paid_budget(config)
        self.consumed = 0

    def decide(self, view: Any) -> Mapping[str, Any]:
        if self.consumed >= len(self.calls):
            raise _ReceiptMismatch(
                "completed sequential call receipt is missing"
            )
        call = _mapping(
            self.calls[self.consumed],
            name="continuation call",
        )
        expected_sequence = self.consumed + 1
        _verify_sequential_call_context(
            call,
            view,
            expected_sequence=expected_sequence,
            assignments=self.assignments,
            config=self.config,
        )
        if call.get("status") != "succeeded":
            raise _ReceiptMismatch(
                "completed sequential call status must be succeeded"
            )
        assignment = self.assignments.get(view.name)
        if assignment is None:
            raise _ReceiptMismatch(
                "continuation call identifies an unknown public name"
            )
        parsed_choice = _verify_successful_sequential_call(
            call,
            view,
            assignment=assignment,
            config=self.config,
            paid_budget=self.paid_budget,
        )
        self.consumed += 1
        return parsed_choice


class _FailedReceiptReplay:
    def __init__(
        self,
        *,
        failure: Mapping[str, Any],
        calls: Sequence[Any],
        assignments: Sequence[Any],
        config: Mapping[str, Any],
        verify_call_assignments: bool,
        verify_request_view: bool,
    ) -> None:
        self.failure = failure
        self.calls = calls
        self.assignments = (
            _assignment_map(assignments, name="seat_assignments")
            if verify_call_assignments
            else {}
        )
        self.verify_call_assignments = verify_call_assignments
        self.verify_request_view = verify_request_view
        self.config = config
        self.paid_budget = (
            _recorded_paid_budget(config) if verify_call_assignments else None
        )
        raw_max_calls = config.get("max_calls")
        if (
            verify_call_assignments
            and (type(raw_max_calls) is not int or raw_max_calls < 1)
        ):
            raise ValueError("format-v6 max_calls must be a positive integer")
        self.max_calls = int(raw_max_calls) if verify_call_assignments else None
        self.consumed = 0

    def decide(self, view: Any) -> Mapping[str, Any]:
        if self.consumed < len(self.calls):
            call = _mapping(
                self.calls[self.consumed],
                name="continuation call",
            )
            expected_sequence = self.consumed + 1
            if call.get("sequence") != expected_sequence:
                raise _ReceiptMismatch(
                    "continuation calls must have contiguous sequences"
                )
            expected_context = {
                "day": view.day,
                "cycle": view.day,
                "slot": view.slot,
                "public_name": view.name,
            }
            if any(call.get(key) != value for key, value in expected_context.items()):
                raise _ReceiptMismatch(
                    "continuation call order does not match reconstructed execution"
                )
            if self.verify_call_assignments:
                _verify_sequential_call_context(
                    call,
                    view,
                    expected_sequence=expected_sequence,
                    assignments=self.assignments,
                    config=self.config,
                    verify_order=False,
                    verify_request=self.verify_request_view,
                )
            elif self.verify_request_view:
                raise _ReceiptMismatch(
                    "request verification requires verified call assignments"
                )
            self.consumed += 1
            status = call.get("status")
            if status == "succeeded":
                if not self.verify_call_assignments:
                    return dict(
                        _mapping(call.get("parsed_choice"), name="parsed_choice")
                    )
                assignment = self.assignments.get(view.name)
                if assignment is None:
                    raise _ReceiptMismatch(
                        "continuation call identifies an unknown public name"
                    )
                return _verify_successful_sequential_call(
                    call,
                    view,
                    assignment=assignment,
                    config=self.config,
                    paid_budget=self.paid_budget,
                )
            if status == "failed":
                if self.failure.get("call_sequence") != expected_sequence:
                    raise _ReceiptMismatch(
                        "failed call does not match the failure receipt"
                    )
                if self.verify_call_assignments:
                    assignment = self.assignments.get(view.name)
                    if assignment is None:
                        raise _ReceiptMismatch(
                            "continuation call identifies an unknown public name"
                        )
                    try:
                        _verify_recorded_cost_authorization(
                            assignment=_assignment_from_receipt(assignment),
                            request=_mapping(
                                call.get("request"),
                                name="continuation call request",
                            ),
                            record=call,
                            paid_budget=self.paid_budget,
                            successful=False,
                        )
                    except (KeyError, TypeError, ValueError) as error:
                        raise _ReceiptMismatch(
                            "failed continuation call cost receipt is inconsistent: "
                            f"{error}"
                        ) from error
                raise _RecordedFailure
            raise _ReceiptMismatch("continuation call status is invalid")

        if self.failure.get("call_sequence") is not None:
            raise _ReceiptMismatch("failure call receipt is missing")
        expected_failure = {
            "day": view.day,
            "cycle": view.day,
            "slot": view.slot,
            "public_name": view.name,
        }
        if any(
            self.failure.get(key) != value for key, value in expected_failure.items()
        ):
            raise _ReceiptMismatch(
                "failure boundary does not match reconstructed execution"
            )
        if self.verify_call_assignments:
            self._verify_no_call_failure(view)
        raise _RecordedFailure

    def _verify_no_call_failure(self, view: Any) -> None:
        kind = self.failure.get("kind")
        if kind == "call_cap_reached":
            if self.max_calls is None or self.consumed < self.max_calls:
                raise _ReceiptMismatch(
                    "call_cap_reached was recorded before the configured cap"
                )
            if "cost_authorization" in self.failure:
                raise _ReceiptMismatch(
                    "call_cap_reached cannot contain cost authorization"
                )
            if self.failure.get("message") != (
                "live model call cap reached before request"
            ):
                raise _ReceiptMismatch("call_cap_reached message is not canonical")
            return
        if kind != "paid_budget_exhausted":
            raise _ReceiptMismatch("failure without a call has an invalid kind")
        if self.max_calls is None or self.consumed >= self.max_calls:
            raise _ReceiptMismatch(
                "paid_budget_exhausted cannot supersede the configured call cap"
            )
        assignment = self.assignments.get(view.name)
        if assignment is None:
            raise _ReceiptMismatch(
                "failure identifies an unknown public name"
            )
        try:
            recorded_assignment = _assignment_from_receipt(assignment)
            request = _build_request(
                recorded_assignment,
                view,
                max_completion_tokens=int(self.config["max_completion_tokens"]),
                temperature=self.config["temperature"],
                reasoning_effort=(
                    None
                    if self.config.get("reasoning_effort") == "provider-default"
                    else str(self.config["reasoning_effort"])
                ),
            )
            _verify_recorded_cost_authorization(
                assignment=recorded_assignment,
                request=request,
                record=self.failure,
                paid_budget=self.paid_budget,
                successful=False,
            )
            authorization = _mapping(
                self.failure.get("cost_authorization"),
                name="failure cost_authorization",
            )
            cumulative = Decimal(str(authorization["cumulative_cost_bound_usd"]))
            maximum = Decimal(str(authorization["max_paid_usd"]))
        except (KeyError, TypeError, ValueError, InvalidOperation) as error:
            raise _ReceiptMismatch(
                f"paid_budget_exhausted receipt is inconsistent: {error}"
            ) from error
        if cumulative <= maximum:
            raise _ReceiptMismatch(
                "paid_budget_exhausted was recorded within the authorized budget"
            )
        if self.failure.get("message") != (
            "paid cost authorization exhausted before request"
        ):
            raise _ReceiptMismatch("paid_budget_exhausted message is not canonical")


def _verify_sequential_call_context(
    call: Mapping[str, Any],
    view: Any,
    *,
    expected_sequence: int,
    assignments: Mapping[str, Mapping[str, str]],
    config: Mapping[str, Any],
    verify_order: bool = True,
    verify_request: bool = True,
) -> None:
    if verify_order:
        if call.get("sequence") != expected_sequence:
            raise _ReceiptMismatch(
                "continuation calls must have contiguous sequences"
            )
        expected_context = {
            "day": view.day,
            "cycle": view.day,
            "slot": view.slot,
            "public_name": view.name,
        }
        if any(call.get(key) != value for key, value in expected_context.items()):
            raise _ReceiptMismatch(
                "continuation call order does not match reconstructed execution"
            )
    assignment = assignments.get(view.name)
    if assignment is None:
        raise _ReceiptMismatch(
            "continuation call identifies an unknown public name"
        )
    if call.get("seat_id") != assignment["seat_id"] or call.get(
        "model"
    ) != assignment["model"]:
        raise _ReceiptMismatch(
            "continuation call identity does not match its seat assignment"
        )
    if verify_request:
        _verify_call_request_view(
            call,
            view,
            assignment=assignment,
            config=config,
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
    if failure.get("seat_id") != assignment.get("seat_id") or failure.get(
        "model"
    ) != assignment.get("model"):
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


@lru_cache(maxsize=None)
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


def _reconstructed_equal(
    actual: object,
    expected: object,
    *,
    strict_json_types: bool,
) -> bool:
    if not strict_json_types:
        return actual == expected
    return json.dumps(
        actual,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ) == json.dumps(
        expected,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _strict_json_bytes(raw: bytes) -> object:
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ValueError("artifact is not valid UTF-8") from error
    return json.loads(
        text,
        object_pairs_hook=_unique_object,
        parse_float=str,
        parse_constant=_raise_invalid_constant,
    )


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate object key {key!r}")
        result[key] = value
    return result


def _raise_invalid_constant(value: str) -> NoReturn:
    raise ValueError(f"invalid JSON constant {value!r}")


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
        ancestor_paths=args.ancestor,
    )
    print(json.dumps(receipt, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
