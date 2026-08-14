from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections.abc import Mapping, Sequence
from decimal import Decimal
from pathlib import Path
from typing import Any


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = REPOSITORY_ROOT / "src"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from world_sim.model_host import (  # noqa: E402
    LIVE_REASONING_EFFORTS,
    POSTMORTEM_PROTOCOL,
    _PaidBudget,
    _build_postmortem_request,
    _calculate_usage_cost,
    _load_verified_parent_artifact,
    _postmortem_paid_preflight,
    _postmortem_summary,
    _postmortem_targets,
    _parse_postmortem_reflection,
    _recorded_cost_decimal,
    _strict_json,
    _verify_artifact_source_provenance,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Verify a postmortem artifact and its completed world link."
    )
    parser.add_argument("artifact", type=Path)
    parser.add_argument("--artifact-sha256")
    parser.add_argument("--world-artifact", type=Path, required=True)
    parser.add_argument(
        "--ancestor",
        action="append",
        type=Path,
        default=[],
        help="world ancestor path; repeat oldest to newest",
    )
    return parser


def verify_postmortem_artifact(
    path: Path,
    *,
    world_artifact_path: Path,
    expected_artifact_sha256: str | None = None,
    ancestor_paths: Sequence[Path] = (),
) -> dict[str, object]:
    raw = path.read_bytes()
    artifact_sha256 = hashlib.sha256(raw).hexdigest()
    if (
        expected_artifact_sha256 is not None
        and artifact_sha256 != expected_artifact_sha256.casefold()
    ):
        raise ValueError(
            "postmortem artifact SHA-256 mismatch: expected "
            f"{expected_artifact_sha256}, got {artifact_sha256}"
        )
    loaded = _strict_json(raw.decode("utf-8"))
    if not isinstance(loaded, Mapping):
        raise ValueError("postmortem artifact must be an object")
    artifact = dict(loaded)
    expected_top_level = {
        "format_version",
        "mode",
        "protocol",
        "source",
        "world_link",
        "config",
        "paid_preflight",
        "targets",
        "calls",
        "status",
        "summary",
        "causal_boundary",
    }
    if set(artifact) != expected_top_level:
        raise ValueError("postmortem artifact has unexpected fields")
    if (
        artifact.get("format_version") != 1
        or artifact.get("mode") != "live_postmortem_reflection"
        or artifact.get("protocol") != POSTMORTEM_PROTOCOL
        or artifact.get("status") != "completed"
    ):
        raise ValueError("postmortem artifact identity or status is invalid")
    _verify_artifact_source_provenance(artifact)

    link = artifact.get("world_link")
    if not isinstance(link, Mapping) or set(link) != {
        "artifact_name",
        "artifact_sha256",
        "canonical_result_sha256",
        "format_version",
        "mode",
    }:
        raise ValueError("postmortem world_link is invalid")
    world_artifact, world_result, world_sha256 = _load_verified_parent_artifact(
        world_artifact_path,
        expected_sha256=str(link["artifact_sha256"]),
        ancestor_paths=ancestor_paths,
    )
    expected_link = {
        "artifact_name": world_artifact_path.name,
        "artifact_sha256": world_sha256,
        "canonical_result_sha256": world_artifact[
            "canonical_result_sha256"
        ],
        "format_version": world_artifact["format_version"],
        "mode": world_artifact["mode"],
    }
    if dict(link) != expected_link:
        raise ValueError("postmortem world_link does not match the verified world")

    assignments = _verified_targets(world_artifact, world_result)
    expected_targets = [target for target, _ in assignments]
    targets = artifact.get("targets")
    if not isinstance(targets, list) or targets != expected_targets:
        raise ValueError("postmortem targets do not match deaths in this world artifact")
    config = _postmortem_config(artifact.get("config"))
    requests = [
        (
            target,
            assignment,
            _strict_json(
                json.dumps(
                    _build_postmortem_request(
                        assignment,
                        target,
                        max_completion_tokens=config["max_completion_tokens"],
                        temperature=config["temperature"],
                        reasoning_effort=(
                            None
                            if config["reasoning_effort"] == "provider-default"
                            else config["reasoning_effort"]
                        ),
                    ),
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
            ),
        )
        for target, assignment in assignments
    ]
    paid_limit, expected_preflight = _postmortem_paid_preflight(
        requests,
        max_paid_usd=config["max_paid_usd"],
    )
    if artifact.get("paid_preflight") != expected_preflight:
        raise ValueError("postmortem paid_preflight does not reconstruct exactly")
    budget = _PaidBudget(paid_limit) if paid_limit is not None else None

    calls = artifact.get("calls")
    if not isinstance(calls, list) or len(calls) != len(requests):
        raise ValueError("postmortem must contain one terminal call per death target")
    for sequence, (call, request_row) in enumerate(
        zip(calls, requests, strict=True),
        start=1,
    ):
        if not isinstance(call, Mapping):
            raise ValueError("postmortem call must be an object")
        target, assignment, expected_request = request_row
        _verify_postmortem_call(
            call,
            sequence=sequence,
            target=target,
            assignment=assignment,
            expected_request=expected_request,
            budget=budget,
        )
    expected_summary = _postmortem_summary(calls, len(requests), budget)
    if artifact.get("summary") != expected_summary:
        raise ValueError("postmortem summary does not reconstruct exactly")
    expected_boundary = (
        "postmortem calls occurred after the linked world artifact was completed; "
        "their replies are absent from world state and replay"
    )
    if artifact.get("causal_boundary") != expected_boundary:
        raise ValueError("postmortem causal boundary is invalid")
    return {
        "artifact_sha256": artifact_sha256,
        "status": "verified",
        "world_artifact_sha256": world_sha256,
        "death_targets": len(requests),
        "calls_succeeded": expected_summary["calls_succeeded"],
        "calls_failed": expected_summary["calls_failed"],
        "calls_skipped": expected_summary["calls_skipped"],
        "world_replay_verified": True,
        "postmortem_is_causally_separate": True,
    }


def _verified_targets(
    world_artifact: Mapping[str, object],
    world_result: object,
) -> list[tuple[dict[str, object], Any]]:
    from world_sim.model_host import _verified_parent_assignments

    assignments = _verified_parent_assignments(world_artifact, world_result)
    return _postmortem_targets(world_result, assignments)


def _postmortem_config(value: object) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != {
        "max_completion_tokens",
        "max_reflection_chars",
        "temperature",
        "reasoning_effort",
        "max_paid_usd",
        "timeout_seconds",
        "attempts_per_death",
        "retry_policy",
    }:
        raise ValueError("postmortem config is invalid")
    config = dict(value)
    if (
        type(config["max_completion_tokens"]) is not int
        or not 1 <= config["max_completion_tokens"] <= 1_024
        or config["max_reflection_chars"] != 500
        or config["reasoning_effort"] not in LIVE_REASONING_EFFORTS
        or config["attempts_per_death"] != 1
        or config["retry_policy"] != "none"
    ):
        raise ValueError("postmortem config limits are invalid")
    if isinstance(config["temperature"], bool):
        raise ValueError("postmortem temperature is invalid")
    try:
        config["temperature"] = float(config["temperature"])
    except (TypeError, ValueError) as error:
        raise ValueError("postmortem temperature is invalid") from error
    if not 0.0 <= config["temperature"] <= 2.0:
        raise ValueError("postmortem temperature is invalid")
    if isinstance(config["timeout_seconds"], bool):
        raise ValueError("postmortem timeout is invalid")
    try:
        timeout_seconds = float(config["timeout_seconds"])
    except (TypeError, ValueError) as error:
        raise ValueError("postmortem timeout is invalid") from error
    if not 1.0 <= timeout_seconds <= 300.0:
        raise ValueError("postmortem timeout is invalid")
    return config


def _verify_postmortem_call(
    call: Mapping[str, object],
    *,
    sequence: int,
    target: Mapping[str, object],
    assignment: Any,
    expected_request: Mapping[str, object],
    budget: _PaidBudget | None,
) -> None:
    death = target["death_event"]
    assert isinstance(death, Mapping)
    expected_identity = {
        "sequence": sequence,
        "death_event_sequence": death["sequence"],
        "cycle": death["cycle"],
        "slot": death["slot"],
        "seat_id": assignment.seat_id,
        "public_name": assignment.public_name,
        "model": assignment.model_ref,
        "endpoint": assignment.endpoint.url,
    }
    if any(call.get(key) != expected for key, expected in expected_identity.items()):
        raise ValueError("postmortem call identity does not match its death target")
    if call.get("request") != expected_request:
        raise ValueError("postmortem call request does not reconstruct exactly")
    if any(key in call for key in ("action", "speech", "parsed_choice")):
        raise ValueError("postmortem call contains a world-action field")
    status = call.get("status")
    if status not in {"succeeded", "failed", "skipped"}:
        raise ValueError("postmortem call must have terminal status")
    cost_state = _verify_postmortem_cost(
        call,
        assignment=assignment,
        request=expected_request,
        budget=budget,
    )
    base_keys = {*expected_identity, "request", "status", "response"}
    is_paid = assignment.endpoint.provider == "opencode-paid"
    if is_paid:
        base_keys.add("cost_authorization")
    if status == "succeeded":
        expected_cost_state = "accounted_within" if is_paid else "non_paid"
        if cost_state != expected_cost_state:
            raise ValueError("successful postmortem cost state is invalid")
        expected_keys = base_keys | {"reflection", "validation"}
        if set(call) != expected_keys:
            raise ValueError("successful postmortem call has unexpected fields")
        response = call.get("response")
        if not isinstance(response, Mapping):
            raise ValueError("successful postmortem call has no response")
        _verify_postmortem_full_response(response, assignment=assignment)
        if response.get("provider_model") != assignment.model_id:
            raise ValueError("postmortem response model does not match assignment")
        model_reply = response.get("model_reply")
        if not isinstance(model_reply, str):
            raise ValueError("successful postmortem call has no raw reply")
        reflection = _parse_postmortem_reflection(model_reply)
        if call.get("reflection") != reflection:
            raise ValueError("postmortem parsed reflection does not match raw reply")
        if call.get("validation") != {
            "strict_json": True,
            "exact_schema": True,
            "within_character_cap": True,
        }:
            raise ValueError("postmortem success validation is invalid")
        return
    expected_keys = base_keys | {"error"}
    if set(call) != expected_keys:
        raise ValueError("non-successful postmortem call has unexpected fields")
    error = call.get("error")
    if not isinstance(error, Mapping) or set(error) != {
        "kind",
        "message",
        "http_status",
    }:
        raise ValueError("postmortem error receipt is invalid")
    if status == "skipped":
        if (
            call.get("response") is not None
            or error.get("kind") != "paid_budget_exhausted"
            or error.get("message")
            != "postmortem paid authorization exhausted before request"
            or error.get("http_status") is not None
            or not is_paid
            or cost_state != "skipped_exhausted"
        ):
            raise ValueError("postmortem skipped receipt is invalid")
        return
    _verify_failed_postmortem_call(
        call,
        error=error,
        assignment=assignment,
        cost_state=cost_state,
    )


def _verify_failed_postmortem_call(
    call: Mapping[str, object],
    *,
    error: Mapping[str, object],
    assignment: Any,
    cost_state: str,
) -> None:
    kind = error.get("kind")
    message = error.get("message")
    http_status = error.get("http_status")
    if not isinstance(kind, str) or not kind:
        raise ValueError("postmortem failure kind is invalid")
    if not isinstance(message, str) or not message:
        raise ValueError("postmortem failure message is invalid")
    if http_status is not None and (
        isinstance(http_status, bool) or not isinstance(http_status, int)
    ):
        raise ValueError("postmortem failure HTTP status is invalid")

    response = call.get("response")
    if response is None:
        transport_kinds = {
            "invalid_http_encoding",
            "network_error",
            "oversized_http_response",
            "transport_error",
        }
        if (
            kind not in transport_kinds
            or http_status is not None
            or cost_state
            != (
                "reserved_failure"
                if assignment.endpoint.provider == "opencode-paid"
                else "non_paid"
            )
        ):
            raise ValueError(
                "postmortem failure kind is incompatible with a missing response"
            )
        if kind == "transport_error" and not message.startswith(
            "transport raised "
        ):
            raise ValueError("postmortem transport failure message is invalid")
        return
    if not isinstance(response, Mapping):
        raise ValueError("failed postmortem response must be an object or null")

    response_status = response.get("http_status")
    if (
        isinstance(response_status, bool)
        or not isinstance(response_status, int)
        or not 100 <= response_status <= 599
        or http_status != response_status
    ):
        raise ValueError("postmortem failure HTTP status does not match response")
    if response_status != 200:
        _verify_postmortem_error_response(response, assignment=assignment)
        if (
            kind != "http_error"
            or message != f"provider returned HTTP {response_status}"
            or cost_state
            != (
                "reserved_failure"
                if assignment.endpoint.provider == "opencode-paid"
                else "non_paid"
            )
        ):
            raise ValueError("postmortem HTTP failure receipt is inconsistent")
        return
    if kind == "http_error":
        raise ValueError("postmortem HTTP failure cannot report status 200")

    model_reply = response.get("model_reply")
    if not isinstance(model_reply, str):
        _verify_postmortem_error_response(response, assignment=assignment)
        body_failure_kinds = {
            "completion_budget_exhausted",
            "paid_cost_bound_breached",
            "provider_cost_error",
            "provider_envelope_error",
        }
        if kind not in body_failure_kinds:
            raise ValueError(
                "postmortem failure kind requires a parsed provider response"
            )
        if kind == "completion_budget_exhausted" and message != (
            "model exhausted its completion budget before finishing an answer"
        ):
            raise ValueError("postmortem completion-budget failure is invalid")
        if kind == "paid_cost_bound_breached":
            if (
                assignment.endpoint.provider != "opencode-paid"
                or cost_state != "accounted_breached"
                or message
                != "provider cost exceeded the authorized request bound"
            ):
                raise ValueError("postmortem paid-cost failure is fabricated")
        elif cost_state != (
            "reserved_failure"
            if assignment.endpoint.provider == "opencode-paid"
            else "non_paid"
        ):
            raise ValueError("postmortem pre-accounting cost state is invalid")
        return

    _verify_postmortem_full_response(response, assignment=assignment)
    if cost_state == "accounted_breached":
        if (
            kind != "paid_cost_bound_breached"
            or assignment.endpoint.provider != "opencode-paid"
            or message != "provider cost exceeded the authorized request bound"
        ):
            raise ValueError("postmortem paid-cost failure is fabricated")
        return
    expected_cost_state = (
        "accounted_within"
        if assignment.endpoint.provider == "opencode-paid"
        else "non_paid"
    )
    if cost_state != expected_cost_state:
        raise ValueError("postmortem parsed failure cost state is invalid")
    if kind == "response_validation_error":
        if response.get("provider_model") != assignment.model_id:
            raise ValueError(
                "postmortem validation failure has the wrong provider model"
            )
        try:
            _parse_postmortem_reflection(model_reply)
        except ValueError as parse_error:
            if str(parse_error) != message:
                raise ValueError(
                    "postmortem validation failure does not match raw reply"
                ) from parse_error
        else:
            raise ValueError("postmortem validation failure parsed successfully")
    elif kind == "provider_model_error":
        if (
            response.get("provider_model") == assignment.model_id
            or message
            != "provider model identity does not match the requested model"
        ):
            raise ValueError("postmortem provider-model failure is fabricated")
    else:
        raise ValueError("postmortem failure kind is incompatible with raw reply")


def _verify_postmortem_error_response(
    response: Mapping[str, object], *, assignment: Any
) -> None:
    required = {"http_status", "request_id", "body_bytes", "body_sha256"}
    optional = {"provider_reported_cost_usd", "uncached_calculated_cost_usd"}
    if assignment.endpoint.provider != "opencode-paid":
        optional.clear()
    if not required <= set(response) or set(response) - required - optional:
        raise ValueError("postmortem error response has unexpected fields")
    has_provider_cost = "provider_reported_cost_usd" in response
    has_calculated_cost = "uncached_calculated_cost_usd" in response
    if has_calculated_cost and not has_provider_cost:
        raise ValueError("postmortem error response has calculated cost only")
    if has_provider_cost:
        _recorded_cost_decimal(
            response["provider_reported_cost_usd"],
            name="provider_reported_cost_usd",
        )
    if has_calculated_cost:
        _recorded_cost_decimal(
            response["uncached_calculated_cost_usd"],
            name="uncached_calculated_cost_usd",
        )
    request_id = response.get("request_id")
    if request_id is not None and not isinstance(request_id, str):
        raise ValueError("postmortem error response request ID is invalid")
    body_bytes = response.get("body_bytes")
    body_sha256 = response.get("body_sha256")
    if (
        isinstance(body_bytes, bool)
        or not isinstance(body_bytes, int)
        or body_bytes < 0
        or not isinstance(body_sha256, str)
        or len(body_sha256) != 64
        or any(character not in "0123456789abcdef" for character in body_sha256)
    ):
        raise ValueError("postmortem error response body receipt is invalid")


def _verify_postmortem_full_response(
    response: Mapping[str, object], *, assignment: Any
) -> None:
    expected = {
        "http_status",
        "request_id",
        "provider_model",
        "finish_reason",
        "usage",
        "provider_reported_cost_usd",
        "model_reply",
    }
    if assignment.endpoint.provider == "opencode-paid":
        expected.add("uncached_calculated_cost_usd")
    if set(response) != expected:
        raise ValueError("postmortem provider response has unexpected fields")
    if response.get("http_status") != 200:
        raise ValueError("postmortem parsed provider response must have status 200")
    if response.get("request_id") is not None and not isinstance(
        response.get("request_id"), str
    ):
        raise ValueError("postmortem provider response request ID is invalid")
    if response.get("provider_model") is not None and not isinstance(
        response.get("provider_model"), str
    ):
        raise ValueError("postmortem provider model is invalid")
    if response.get("finish_reason") is not None and not isinstance(
        response.get("finish_reason"), str
    ):
        raise ValueError("postmortem finish reason is invalid")
    if not isinstance(response.get("usage"), Mapping):
        raise ValueError("postmortem provider usage is invalid")
    provider_cost = response.get("provider_reported_cost_usd")
    if provider_cost is not None and not isinstance(provider_cost, str):
        raise ValueError("postmortem provider cost is invalid")
    if assignment.endpoint.provider == "opencode-paid" and not isinstance(
        response.get("uncached_calculated_cost_usd"), str
    ):
        raise ValueError("postmortem calculated cost is invalid")


def _verify_postmortem_cost(
    call: Mapping[str, object],
    *,
    assignment: Any,
    request: Mapping[str, object],
    budget: _PaidBudget | None,
) -> str:
    raw_authorization = call.get("cost_authorization")
    if assignment.endpoint.provider != "opencode-paid":
        if raw_authorization is not None:
            raise ValueError("non-paid postmortem contains cost authorization")
        return "non_paid"
    if budget is None or not isinstance(raw_authorization, Mapping):
        raise ValueError("paid postmortem has no cost authorization")
    expected = budget.quote(assignment, request)
    cumulative_bound = Decimal(str(expected["cumulative_cost_bound_usd"]))
    is_skipped = call.get("status") == "skipped"
    if is_skipped and cumulative_bound <= budget.limit:
        raise ValueError("postmortem skipped call did not exceed the paid budget")
    if not is_skipped and cumulative_bound > budget.limit:
        raise ValueError("postmortem attempted call exceeded the paid budget")
    basis = raw_authorization.get("accounting_basis")
    state: str
    if basis == "provider_or_calculated_cost":
        response = call.get("response")
        if not isinstance(response, Mapping):
            raise ValueError("accounted postmortem has no response cost")
        provider_cost = _recorded_cost_decimal(
            response.get("provider_reported_cost_usd"),
            name="provider_reported_cost_usd",
        )
        usage = response.get("usage")
        if isinstance(usage, Mapping):
            calculated = _recorded_cost_decimal(
                _calculate_usage_cost(assignment.model_id, usage),
                name="calculated usage cost",
            )
        else:
            calculated = _recorded_cost_decimal(
                response.get("uncached_calculated_cost_usd"),
                name="uncached_calculated_cost_usd",
            )
        if response.get("uncached_calculated_cost_usd") != str(calculated):
            raise ValueError("postmortem calculated cost is inconsistent")
        within_bound = budget.account(
            expected,
            provider_cost=provider_cost,
            calculated_cost=calculated,
        )
        state = "accounted_within" if within_bound else "accounted_breached"
    elif basis == "authorized_bound_after_failure":
        budget.reserve_failed_request(expected)
        state = "reserved_failure"
    elif is_skipped:
        state = "skipped_exhausted"
    else:
        raise ValueError("paid postmortem cost accounting is missing")
    if dict(raw_authorization) != expected:
        raise ValueError("postmortem cost authorization does not reconstruct exactly")
    return state


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    receipt = verify_postmortem_artifact(
        args.artifact,
        world_artifact_path=args.world_artifact,
        expected_artifact_sha256=args.artifact_sha256,
        ancestor_paths=args.ancestor,
    )
    print(json.dumps(receipt, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
