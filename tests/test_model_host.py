from __future__ import annotations

import hashlib
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from collections.abc import Mapping, Sequence
from contextlib import redirect_stdout
from copy import deepcopy
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from world_sim.cli import (
    _print_live_call,
    _reserve_live_output,
    _write_reserved_live_output,
    main,
)
from world_sim.model_host import (
    ChatTransport,
    EndpointSpec,
    TransportResponse,
    load_opencode_go_api_key,
    run_live_postmortem,
    run_live_survival,
    run_live_survival_continuation,
)
from world_sim.survival.calibration import LEAN_CAMP_V1, survival_preset
from world_sim.survival.engine import (
    make_survival_world,
    replay_survival,
    survival_view_for,
)
from world_sim.survival.models import (
    GLOBAL_BEATS_V2,
    SEQUENTIAL_DIALOGUE_V3,
    SLOTS_V1,
    SurvivalResult,
)
from world_sim.survival.prompt import response_schema

TEST_MODEL_NAMES = ("alpha", "beta", "gamma", "delta")
FREE_MODELS = tuple(f"opencode/{name}-free" for name in TEST_MODEL_NAMES)
GO_MODELS = tuple(f"opencode-go/{name}" for name in TEST_MODEL_NAMES)
PAID_MODELS = tuple(
    f"opencode-paid/{name}"
    for name in ("deepseek-v4-flash", "grok-4.5", "kimi-k2.6", "glm-5.2")
)
GROK_MODELS = ("opencode-paid/grok-4.6",) * 4
REST_REPLY = '{"action":{"kind":"rest"},"say":null}'
WAIT_REPLY = '{"action":{"kind":"wait"},"say":null}'


def response(
    content: str,
    *,
    request_id: str = "request-1",
    cost: str | None = None,
    cost_in_usage: bool = False,
) -> TransportResponse:
    usage: dict[str, object] = {
        "prompt_tokens": 100,
        "completion_tokens": 20,
        "total_tokens": 120,
        "completion_tokens_details": {"reasoning_tokens": 15},
    }
    if cost is not None and cost_in_usage:
        usage["cost"] = cost
    payload: dict[str, object] = {
        "model": "fake-model",
        "choices": [
            {
                "message": {"content": content},
                "finish_reason": "stop",
            }
        ],
        "usage": usage,
    }
    if cost is not None and not cost_in_usage:
        payload["cost"] = cost
    return TransportResponse(
        status=200,
        headers={"x-request-id": request_id},
        body=json.dumps(payload),
    )


def responses_response(
    content: str,
    *,
    request_id: str = "request-1",
    cost: str | None = None,
    cost_in_usage: bool = False,
    status: str = "completed",
) -> TransportResponse:
    usage: dict[str, object] = {
        "input_tokens": 100,
        "output_tokens": 20,
        "total_tokens": 120,
        "output_tokens_details": {"reasoning_tokens": 15},
    }
    if cost is not None and cost_in_usage:
        usage["cost"] = cost
    payload: dict[str, object] = {
        "object": "response",
        "model": "fake-model",
        "status": status,
        "error": None,
        "output": [
            {"type": "reasoning", "status": "completed"},
            {
                "type": "message",
                "role": "assistant",
                "status": "completed",
                "content": [{"type": "output_text", "text": content}],
            },
        ],
        "usage": usage,
        "incomplete_details": None,
    }
    if cost is not None and not cost_in_usage:
        payload["cost"] = cost
    return TransportResponse(
        status=200,
        headers={"x-request-id": request_id},
        body=json.dumps(payload),
    )


def paid_panel_response(
    index: int,
    content: str,
    *,
    request_id: str = "request-1",
    cost: str | None = None,
    cost_in_usage: bool = False,
) -> TransportResponse:
    factory = responses_response if index % len(PAID_MODELS) == 1 else response
    return factory(
        content,
        request_id=request_id,
        cost=cost,
        cost_in_usage=cost_in_usage,
    )


class FakeTransport(ChatTransport):
    def __init__(
        self,
        responses: Sequence[TransportResponse],
        *,
        align_provider_model: bool = True,
    ) -> None:
        self.responses = list(responses)
        self.requests: list[dict[str, object]] = []
        self.align_provider_model = align_provider_model

    def post(
        self,
        endpoint: EndpointSpec,
        request_body: Mapping[str, object],
        *,
        api_key: str | None,
        timeout_seconds: float,
    ) -> TransportResponse:
        self.requests.append(
            {
                "endpoint": endpoint,
                "body": deepcopy(dict(request_body)),
                "api_key": api_key,
                "timeout_seconds": timeout_seconds,
            }
        )
        if not self.responses:
            raise AssertionError("unexpected extra model request")
        response_receipt = self.responses.pop(0)
        if not self.align_provider_model or response_receipt.status != 200:
            return response_receipt
        model_text = json.dumps(str(request_body["model"]))
        body, replacements = re.subn(
            r'("model"\s*:\s*)"(?:\\.|[^"\\])*"',
            lambda match: match.group(1) + model_text,
            response_receipt.body,
            count=1,
        )
        if replacements != 1:
            return response_receipt
        return TransportResponse(
            status=response_receipt.status,
            headers=response_receipt.headers,
            body=body,
        )


class BrokenTransport(ChatTransport):
    def post(
        self,
        endpoint: EndpointSpec,
        request_body: Mapping[str, object],
        *,
        api_key: str | None,
        timeout_seconds: float,
    ) -> TransportResponse:
        del endpoint, request_body, api_key, timeout_seconds
        raise LookupError("secret diagnostic")


class TimeoutTransport(ChatTransport):
    def __init__(self) -> None:
        self.requests: list[dict[str, object]] = []

    def post(
        self,
        endpoint: EndpointSpec,
        request_body: Mapping[str, object],
        *,
        api_key: str | None,
        timeout_seconds: float,
    ) -> TransportResponse:
        self.requests.append(
            {
                "endpoint": endpoint,
                "body": deepcopy(dict(request_body)),
                "api_key": api_key,
                "timeout_seconds": timeout_seconds,
            }
        )
        raise TimeoutError("provider response deadline elapsed")


def result_from(artifact: Mapping[str, object]) -> SurvivalResult:
    payload = artifact["result"]
    if not isinstance(payload, Mapping):
        raise AssertionError("completed artifact has no result")
    return SurvivalResult(
        initial_state=dict(payload["initial_state"]),
        final_state=dict(payload["final_state"]),
        events=tuple(payload["events"]),
        choice_tape=tuple(payload["choice_tape"]),
        event_sequence_base=int(payload["event_sequence_base"]),
    )


class ModelHostTests(unittest.TestCase):
    def _continuation_parent(self) -> dict[str, object]:
        replies = (
            '{"action":{"kind":"rest"},"say":{"to":"everyone","text":"Aster final public note"}}',
            '{"action":{"kind":"rest"},"say":{"to":"everyone","text":"Birch final public note"}}',
            '{"action":{"kind":"gather_wood"},"say":{"to":"everyone","text":"Cinder first note"}}',
            '{"action":{"kind":"gather_wood"},"say":{"to":"everyone","text":"Lumen first note"}}',
            '{"action":{"kind":"rest"},"say":{"to":"everyone","text":"Cinder final public note"}}',
            '{"action":{"kind":"rest"},"say":{"to":"everyone","text":"Lumen final public note"}}',
        )
        artifact = run_live_survival(
            model_refs=FREE_MODELS,
            seed=29_993,
            days=1,
            max_calls=16,
            require_complete_budget=True,
            transport=FakeTransport([response(reply) for reply in replies]),
            environ={},
        )
        self.assertEqual(artifact["status"], "completed")
        self.assertEqual(artifact["result"]["final_state"]["resources"]["wood"], 2)
        return artifact

    @staticmethod
    def _write_parent(
        directory: str,
        artifact: Mapping[str, object],
        name: str = "parent.json",
    ) -> tuple[Path, str]:
        raw = json.dumps(
            artifact,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        path = Path(directory) / name
        path.write_bytes(raw)
        return path, hashlib.sha256(raw).hexdigest()

    def _free_continuation_chain(
        self,
        directory: str,
        *,
        through_format: int = 4,
    ) -> list[tuple[Path, str, dict[str, object]]]:
        root = self._continuation_parent()
        root_path, root_sha256 = self._write_parent(
            directory,
            root,
            "session-001.json",
        )
        chain = [(root_path, root_sha256, root)]
        direct_path = root_path
        direct_sha256 = root_sha256
        for session in range(2, through_format - 1):
            replies = tuple(
                '{"action":{"kind":"rest"},"say":{"to":"everyone","text":"'
                + name
                + f" session {session} final note"
                + '"}}'
                for name in ("Aster", "Birch", "Cinder", "Lumen")
            )
            ancestor_paths = tuple(row[0] for row in chain[:-1])
            artifact = run_live_survival_continuation(
                parent_path=direct_path,
                expected_parent_sha256=direct_sha256,
                ancestor_paths=ancestor_paths,
                transition_reason=f"session_{session:03d}_chain_test",
                max_calls=4,
                transport=FakeTransport([response(reply) for reply in replies]),
                environ={},
            )
            direct_path, direct_sha256 = self._write_parent(
                directory,
                artifact,
                f"session-{session:03d}.json",
            )
            chain.append((direct_path, direct_sha256, artifact))
        return chain

    def _paid_v4_parent(
        self,
        directory: str,
    ) -> tuple[Path, Path, str, dict[str, object]]:
        root_replies = tuple(
            '{"action":{"kind":"rest"},"say":{"to":"everyone","text":"'
            + name
            + ' paid root note"}}'
            for name in ("Aster", "Birch", "Cinder", "Lumen")
        )
        root = run_live_survival(
            model_refs=PAID_MODELS,
            seed=29_993,
            days=1,
            max_calls=4,
            max_completion_tokens=1_024,
            reasoning_effort="low",
            max_paid_usd="0.30",
            transport=FakeTransport(
                [
                    paid_panel_response(index, reply, cost="0.001")
                    for index, reply in enumerate(root_replies)
                ]
            ),
            environ={"OPENCODE_ZEN_API_KEY": "test-only-key"},
        )
        root_path, root_sha256 = self._write_parent(
            directory,
            root,
            "paid-session-001.json",
        )
        parent_replies = tuple(
            '{"action":{"kind":"rest"},"say":{"to":"everyone","text":"'
            + name
            + ' paid continuation note"}}'
            for name in ("Aster", "Birch", "Cinder", "Lumen")
        )
        parent = run_live_survival_continuation(
            parent_path=root_path,
            expected_parent_sha256=root_sha256,
            transition_reason="session_002_paid_parent",
            max_calls=4,
            max_completion_tokens=1_024,
            reasoning_effort="low",
            max_paid_usd="0.30",
            transport=FakeTransport(
                [
                    paid_panel_response(index, reply, cost="0.001")
                    for index, reply in enumerate(parent_replies)
                ]
            ),
            environ={"OPENCODE_ZEN_API_KEY": "test-only-key"},
        )
        parent_path, parent_sha256 = self._write_parent(
            directory,
            parent,
            "paid-session-002.json",
        )
        return root_path, parent_path, parent_sha256, parent

    def _paid_v6_parent(
        self,
        directory: str,
    ) -> tuple[Path, Path, Path, str, dict[str, object]]:
        root_path, v4_path, v4_sha256, _ = self._paid_v4_parent(directory)
        replies = {
            name: (
                '{"action":{"kind":"rest"},"say":{"to":"everyone","text":"'
                + name
                + ' sequential note"}}'
            )
            for name in ("Aster", "Birch", "Cinder", "Lumen")
        }
        v6 = run_live_survival_continuation(
            parent_path=v4_path,
            expected_parent_sha256=v4_sha256,
            ancestor_paths=(root_path,),
            transition_reason="session_004_sequential_parent",
            interaction_protocol=SEQUENTIAL_DIALOGUE_V3,
            model_replacements=(
                "Cinder=opencode-paid/gpt-5.6-luna",
            ),
            replacement_reason="replace the adapter that exhausted its budget",
            max_calls=4,
            max_completion_tokens=4_096,
            reasoning_effort="low",
            max_paid_usd="0.30",
            transport=FakeTransport(
                [
                    responses_response(replies["Cinder"], cost="0.001"),
                    response(replies["Lumen"], cost="0.001"),
                    response(replies["Aster"], cost="0.001"),
                    responses_response(replies["Birch"], cost="0.001"),
                ]
            ),
            environ={"OPENCODE_ZEN_API_KEY": "test-only-key"},
        )
        self.assertEqual(v6["status"], "completed")
        v6_path, v6_sha256 = self._write_parent(
            directory,
            v6,
            "paid-session-004.json",
        )
        return root_path, v4_path, v6_path, v6_sha256, v6

    def test_live_first_beat_uses_frozen_views_and_next_beat_hears_speech(
        self,
    ) -> None:
        broadcasts = {
            "Aster": "aster beat-one broadcast",
            "Birch": "birch beat-one broadcast",
            "Cinder": "cinder beat-one broadcast",
            "Lumen": "lumen beat-one broadcast",
        }
        first_beat = (
            '{"action":{"kind":"gather_wood"},"say":{"to":"everyone","text":"aster beat-one broadcast"}}',
            '{"action":{"kind":"forage"},"say":{"to":"everyone","text":"birch beat-one broadcast"}}',
            '{"action":{"kind":"wait"},"say":{"to":"everyone","text":"cinder beat-one broadcast"}}',
            '{"action":{"kind":"wait"},"say":{"to":"everyone","text":"lumen beat-one broadcast"}}',
        )
        transport = FakeTransport(
            [response(reply) for reply in (*first_beat, *(REST_REPLY,) * 4)]
        )

        artifact = run_live_survival(
            model_refs=FREE_MODELS,
            days=1,
            max_calls=8,
            transport=transport,
            environ={},
        )

        self.assertEqual(artifact["status"], "completed")
        self.assertEqual(
            artifact["config"]["interaction_protocol"], GLOBAL_BEATS_V2
        )
        self.assertEqual(
            [call["slot"] for call in artifact["calls"]],
            [1] * 4 + [2] * 4,
        )
        first_prompts = [
            request["body"]["messages"][1]["content"]
            for request in transport.requests[:4]
        ]
        for request, prompt in zip(
            transport.requests[:4], first_prompts, strict=True
        ):
            system_prompt = request["body"]["messages"][0]["content"]
            self.assertIn("Each day unfolds in global beats", system_prompt)
            self.assertIn("day 1, beat 1 of 4", prompt)
            self.assertIn("wood available: 4 of 12", prompt)
            self.assertTrue(
                all(text not in prompt for text in broadcasts.values())
            )

        second_prompts = [
            request["body"]["messages"][1]["content"]
            for request in transport.requests[4:]
        ]
        for public_name, prompt in zip(
            broadcasts, second_prompts, strict=True
        ):
            self.assertIn("day 1, beat 2 of 4", prompt)
            self.assertIn("wood available: 2 of 12", prompt)
            inbox = prompt.split(
                "messages heard since your last active beat:\n", 1
            )[1].split("\n\nobjective outcomes", 1)[0]
            for speaker, text in broadcasts.items():
                if speaker == public_name:
                    self.assertNotIn(text, inbox)
                else:
                    self.assertIn(text, inbox)
        for call in artifact["calls"][2:4]:
            self.assertEqual(call["parsed_choice"]["action"], {"kind": "wait"})
            self.assertIsNone(call["validation"]["action_error"])
        self.assertEqual(
            replay_survival(result_from(artifact)).to_dict(), artifact["result"]
        )

    def test_sequential_continuation_records_independent_initiative_phase(self) -> None:
        transport = FakeTransport(
            [
                response(REST_REPLY, cost="0.001"),
                responses_response(REST_REPLY, cost="0.001"),
                responses_response(REST_REPLY, cost="0.001"),
                response(REST_REPLY, cost="0.001"),
            ]
        )
        with tempfile.TemporaryDirectory() as directory:
            root_path, parent_path, parent_sha256, _ = self._paid_v4_parent(
                directory
            )
            artifact = run_live_survival_continuation(
                parent_path=parent_path,
                expected_parent_sha256=parent_sha256,
                ancestor_paths=(root_path,),
                transition_reason="initiative_phase_control",
                interaction_protocol=SEQUENTIAL_DIALOGUE_V3,
                initiative_phase=2,
                model_replacements=(
                    "Cinder=opencode-paid/gpt-5.6-luna",
                ),
                replacement_reason="replace the adapter that exhausted its budget",
                max_calls=4,
                max_completion_tokens=4_096,
                reasoning_effort="low",
                max_paid_usd="0.30",
                transport=transport,
                environ={"OPENCODE_ZEN_API_KEY": "test-only-key"},
            )

        self.assertEqual(artifact["status"], "completed")
        self.assertEqual(artifact["config"]["initiative_phase"], 2)
        self.assertEqual(artifact["result"]["initial_state"]["initiative_phase"], 2)
        self.assertEqual(
            [call["public_name"] for call in artifact["calls"]],
            ["Aster", "Birch", "Cinder", "Lumen"],
        )
        self.assertEqual(
            artifact["result"]["events"][1]["detail"]["initiative_order"],
            ["Aster", "Birch", "Cinder", "Lumen"],
        )
        self.assertEqual(
            replay_survival(result_from(artifact)).to_dict(), artifact["result"]
        )

    def test_paid_provider_failure_mid_beat_never_resolves_or_retries(self) -> None:
        exhausted_payload = json.loads(
            paid_panel_response(2, WAIT_REPLY, cost="0.001").body
        )
        exhausted_payload["choices"][0]["finish_reason"] = "length"
        transport = FakeTransport(
            [
                paid_panel_response(0, WAIT_REPLY, cost="0.001"),
                paid_panel_response(1, WAIT_REPLY, cost="0.001"),
                TransportResponse(200, {}, json.dumps(exhausted_payload)),
            ]
        )

        artifact = run_live_survival(
            model_refs=PAID_MODELS,
            days=1,
            max_calls=4,
            max_completion_tokens=1_024,
            max_paid_usd="0.11",
            transport=transport,
            environ={"OPENCODE_ZEN_API_KEY": "secret"},
        )

        self.assertEqual(artifact["status"], "failed")
        self.assertEqual(artifact["failure"]["kind"], "completion_budget_exhausted")
        self.assertEqual(len(transport.requests), 3)
        self.assertEqual(
            [call["status"] for call in artifact["calls"]],
            ["succeeded", "succeeded", "failed"],
        )
        self.assertTrue(
            all(call["slot"] == 1 for call in artifact["calls"])
        )
        event_kinds = [
            event["kind"] for event in artifact["partial_state"]["events"]
        ]
        self.assertEqual(event_kinds, ["cycle_started", "slot_started"])
        self.assertEqual(
            artifact["partial_state"]["resources"],
            {"food": 6, "food_capacity": 12, "wood": 4, "wood_capacity": 12},
        )
        self.assertTrue(
            all(
                survivor["energy"] == 16
                for survivor in artifact["partial_state"]["survivors"]
            )
        )

    def test_live_transcript_uses_protocol_specific_labels(self) -> None:
        record = {
            "status": "succeeded",
            "cycle": 2,
            "slot": 3,
            "public_name": "Aster",
            "parsed_choice": {"action": {"kind": "wait"}, "say": None},
        }
        output = io.StringIO()
        with redirect_stdout(output):
            _print_live_call(record, interaction_protocol=GLOBAL_BEATS_V2)
            _print_live_call(record, interaction_protocol=SLOTS_V1)
        self.assertEqual(
            output.getvalue().splitlines(),
            [
                "day 2 beat 3 | Aster | wait",
                "cycle 2 slot 3 | Aster | wait",
            ],
        )

    def test_live_continuation_verifies_parent_and_records_objective_chain(self) -> None:
        parent = self._continuation_parent()
        child_replies = (
            REST_REPLY,
            REST_REPLY,
            '{"action":{"kind":"give_wood","target":"Lumen","amount":2},"say":null}',
            '{"action":{"kind":"build_shelter"},"say":null}',
            REST_REPLY,
            REST_REPLY,
        )
        child_transport = FakeTransport(
            [response(reply) for reply in child_replies]
        )
        checkpoints: list[dict[str, object]] = []
        with tempfile.TemporaryDirectory() as directory:
            parent_path, parent_sha256 = self._write_parent(directory, parent)
            artifact = run_live_survival_continuation(
                parent_path=parent_path,
                expected_parent_sha256=parent_sha256,
                transition_reason="session_002_shelter_dilemma",
                max_calls=16,
                require_complete_budget=True,
                transport=child_transport,
                environ={},
                checkpoint=lambda current: checkpoints.append(
                    deepcopy(dict(current))
                ),
            )

        self.assertEqual(artifact["status"], "completed")
        self.assertEqual(artifact["format_version"], 4)
        self.assertEqual(artifact["mode"], "live_named_survival_continuation")
        self.assertEqual(
            artifact["config"]["interaction_protocol"], GLOBAL_BEATS_V2
        )
        self.assertEqual(
            artifact["continuation_link"]["parent_artifact_sha256"],
            parent_sha256,
        )
        transition = artifact["transition_receipt"]["event"]
        self.assertEqual(transition["kind"], "resource_adjusted")
        self.assertEqual(
            transition["detail"],
            {
                "resource": "wood",
                "before": 2,
                "after": 0,
                "delta": -2,
                "reason": "session_002_shelter_dilemma",
            },
        )
        record = artifact["public_record_receipt"]["record"]
        self.assertEqual(
            [statement["text"] for statement in record["statements"]],
            [
                "Aster final public note",
                "Birch final public note",
                "Cinder final public note",
                "Lumen final public note",
            ],
        )
        self.assertTrue(
            all(
                statement["verification"] == "unverified"
                for statement in record["statements"]
            )
        )
        self.assertEqual(len(artifact["calls"]), 6)
        self.assertTrue(all(call["cycle"] == 2 for call in artifact["calls"]))
        self.assertEqual(checkpoints[0]["calls"], [])
        self.assertTrue(
            any(
                current["calls"]
                and current["calls"][-1]["status"] == "in_flight"
                for current in checkpoints
            )
        )
        self.assertEqual(checkpoints[-1], artifact)
        outcomes = artifact["session_outcomes"]
        self.assertEqual(outcomes["completed_resource_transfers"], 1)
        self.assertTrue(outcomes["any_completed_costly_resource_transfer"])
        self.assertEqual(outcomes["failed_paid_transfer_attempts"], [])
        self.assertEqual(outcomes["reciprocal_wood_transfer_pairs"], [])
        self.assertEqual(outcomes["wood_received"]["Lumen"], 2)
        self.assertTrue(outcomes["primary_shelter_chain_by_end_of_chance_3"])
        self.assertEqual(
            outcomes["primary_shelter_chains"][0]["builder"], "Lumen"
        )
        replayed = replay_survival(result_from(artifact))
        self.assertEqual(replayed.to_dict(), artifact["result"])

        first_request = child_transport.requests[0]["body"]
        prompt = first_request["messages"][1]["content"]
        self.assertIn("day 2, beat 1", prompt)
        self.assertIn("Aster final public note", prompt)
        self.assertIn("Lumen final public note", prompt)
        self.assertIn("wood available: 0 of 12", prompt)
        peer_section = prompt.split("other living people:\n", 1)[1].split(
            "\n\nshared land:", 1
        )[0]
        self.assertNotIn("wood", peer_section)
        self.assertNotIn("food", peer_section)
        for forbidden in (
            "conflict",
            "reputation",
            "cooperative",
            "selfish",
            "trustworthy",
            "promise",
            "betrayal",
            "alliance",
            "session_002",
            "model",
            "provider",
            "seat-",
        ):
            self.assertNotIn(forbidden, prompt.casefold())

    def test_live_continuation_rejects_tamper_before_transport(self) -> None:
        parent = self._continuation_parent()
        parent["result"]["final_state"]["survivors"][0]["energy"] += 1
        transport = FakeTransport([])
        with tempfile.TemporaryDirectory() as directory:
            parent_path, parent_sha256 = self._write_parent(directory, parent)
            with self.assertRaisesRegex(ValueError, "canonical result SHA-256"):
                run_live_survival_continuation(
                    parent_path=parent_path,
                    expected_parent_sha256=parent_sha256,
                    transition_reason="session_002_shelter_dilemma",
                    interaction_protocol=SEQUENTIAL_DIALOGUE_V3,
                    model_replacements=(
                        "Cinder=opencode-paid/gpt-5.6-luna",
                        "Birch=opencode-paid/gpt-5.6-luna",
                    ),
                    replacement_reason="must not be inspected before the parent",
                    max_calls=16,
                    require_complete_budget=True,
                    transport=transport,
                    environ={},
                )
        self.assertEqual(transport.requests, [])

    def test_live_continuation_rejects_wrong_parent_hash_before_transport(self) -> None:
        parent = self._continuation_parent()
        transport = FakeTransport([])
        with tempfile.TemporaryDirectory() as directory:
            parent_path, _ = self._write_parent(directory, parent)
            with self.assertRaisesRegex(ValueError, "artifact SHA-256 mismatch"):
                run_live_survival_continuation(
                    parent_path=parent_path,
                    expected_parent_sha256="0" * 64,
                    transition_reason="session_002_shelter_dilemma",
                    max_calls=16,
                    require_complete_budget=True,
                    transport=transport,
                    environ={},
                )
        self.assertEqual(transport.requests, [])

    def test_live_continuation_rejects_wrong_seat_mapping_before_transport(self) -> None:
        parent = self._continuation_parent()
        parent["seat_assignments"][0]["public_name"] = "Lumen"
        transport = FakeTransport([])
        with tempfile.TemporaryDirectory() as directory:
            parent_path, parent_sha256 = self._write_parent(directory, parent)
            with self.assertRaisesRegex(ValueError, "mapping is not exact"):
                run_live_survival_continuation(
                    parent_path=parent_path,
                    expected_parent_sha256=parent_sha256,
                    transition_reason="session_002_shelter_dilemma",
                    max_calls=16,
                    require_complete_budget=True,
                    transport=transport,
                    environ={},
                )
        self.assertEqual(transport.requests, [])

    def test_sequential_continuation_replaces_one_verified_model_and_emits_v6(
        self,
    ) -> None:
        transport = FakeTransport(
            [
                responses_response(REST_REPLY, cost="0.001"),
                response(REST_REPLY, cost="0.001"),
                response(REST_REPLY, cost="0.001"),
                responses_response(REST_REPLY, cost="0.001"),
            ]
        )
        with tempfile.TemporaryDirectory() as directory:
            root_path, parent_path, parent_sha256, _ = self._paid_v4_parent(
                directory
            )
            artifact = run_live_survival_continuation(
                parent_path=parent_path,
                expected_parent_sha256=parent_sha256,
                ancestor_paths=(root_path,),
                transition_reason="session_004_sequential_branch",
                interaction_protocol=SEQUENTIAL_DIALOGUE_V3,
                model_replacements=(
                    "Cinder=opencode-paid/gpt-5.6-luna",
                ),
                replacement_reason="replace the adapter that exhausted its budget",
                max_calls=4,
                max_completion_tokens=4_096,
                reasoning_effort="low",
                max_paid_usd="0.30",
                transport=transport,
                environ={"OPENCODE_ZEN_API_KEY": "test-only-key"},
            )

        self.assertEqual(artifact["status"], "completed")
        self.assertEqual(artifact["format_version"], 6)
        self.assertEqual(
            artifact["config"]["interaction_protocol"],
            SEQUENTIAL_DIALOGUE_V3,
        )
        self.assertEqual(
            artifact["assignment_transition_receipts"],
            [
                {
                    "seat_id": "seat-003",
                    "public_name": "Cinder",
                    "previous_model": "opencode-paid/kimi-k2.6",
                    "replacement_model": "opencode-paid/gpt-5.6-luna",
                    "reason": "replace the adapter that exhausted its budget",
                }
            ],
        )
        self.assertEqual(
            artifact["seat_assignments"][2],
            {
                "seat_id": "seat-003",
                "public_name": "Cinder",
                "model": "opencode-paid/gpt-5.6-luna",
            },
        )
        cinder_call = next(
            call for call in artifact["calls"] if call["public_name"] == "Cinder"
        )
        self.assertEqual(
            cinder_call["endpoint"],
            "https://opencode.ai/zen/v1/responses",
        )
        cinder_request = cinder_call["request"]
        self.assertEqual(
            set(cinder_request),
            {
                "model",
                "input",
                "max_output_tokens",
                "reasoning",
                "text",
                "stream",
                "store",
            },
        )
        self.assertEqual(cinder_request["max_output_tokens"], 4_096)
        self.assertEqual(cinder_request["reasoning"], {"effort": "low"})
        self.assertTrue(cinder_request["text"]["format"]["strict"])
        cinder_schema = cinder_request["text"]["format"]["schema"]
        serialized_schema = json.dumps(cinder_schema)
        self.assertNotIn('"const"', serialized_schema)
        self.assertNotIn('"oneOf"', serialized_schema)
        self.assertIn("anyOf", cinder_schema["properties"]["action"])
        action_kinds = {
            variant["properties"]["kind"]["enum"][0]
            for variant in cinder_schema["properties"]["action"]["anyOf"]
        }
        self.assertIn("rest", action_kinds)
        self.assertIn("give_wood", action_kinds)
        self.assertNotIn("temperature", cinder_request)
        self.assertEqual(
            artifact["paid_preflight"]["cost_bound_scope"],
            "initial_sequential_dialogue_view_per_paid_model",
        )
        self.assertEqual(
            replay_survival(result_from(artifact)).to_dict(), artifact["result"]
        )

    def test_live_call_rejects_a_provider_model_identity_mismatch(self) -> None:
        transport = FakeTransport(
            [response(REST_REPLY)],
            align_provider_model=False,
        )

        artifact = run_live_survival(
            model_refs=FREE_MODELS,
            days=1,
            max_calls=4,
            transport=transport,
        )

        self.assertEqual(artifact["status"], "failed")
        self.assertEqual(artifact["failure"]["kind"], "provider_model_error")
        self.assertEqual(len(artifact["calls"]), 1)
        self.assertEqual(artifact["calls"][0]["status"], "failed")
        self.assertEqual(artifact["partial_state"]["day"], 0)
        self.assertFalse(
            any(
                event["kind"] == "choice_submitted"
                for event in artifact["partial_state"]["events"]
            )
        )

    def test_sequential_failure_retains_dialogue_without_physical_actions(self) -> None:
        spoken = "Cinder says this before the later provider failure"
        cinder_reply = json.dumps(
            {
                "action": {"kind": "rest"},
                "say": {"to": "everyone", "text": spoken},
            },
            separators=(",", ":"),
        )
        exhausted = json.loads(response(REST_REPLY, cost="0.001").body)
        exhausted["choices"][0]["finish_reason"] = "length"
        transport = FakeTransport(
            [
                responses_response(cinder_reply, cost="0.001"),
                response(REST_REPLY, cost="0.001"),
                TransportResponse(200, {}, json.dumps(exhausted)),
            ]
        )
        with tempfile.TemporaryDirectory() as directory:
            root_path, parent_path, parent_sha256, _ = self._paid_v4_parent(
                directory
            )
            artifact = run_live_survival_continuation(
                parent_path=parent_path,
                expected_parent_sha256=parent_sha256,
                ancestor_paths=(root_path,),
                transition_reason="session_004_sequential_failure",
                interaction_protocol=SEQUENTIAL_DIALOGUE_V3,
                model_replacements=(
                    "Cinder=opencode-paid/gpt-5.6-luna",
                ),
                replacement_reason="replace the adapter that exhausted its budget",
                max_calls=4,
                max_completion_tokens=4_096,
                reasoning_effort="low",
                max_paid_usd="0.30",
                transport=transport,
                environ={"OPENCODE_ZEN_API_KEY": "test-only-key"},
            )

        self.assertEqual(artifact["status"], "failed")
        self.assertEqual(artifact["failure"]["public_name"], "Aster")
        self.assertEqual(
            artifact["failure"]["kind"], "completion_budget_exhausted"
        )
        self.assertEqual(len(transport.requests), 3)
        self.assertIn(
            spoken,
            transport.requests[1]["body"]["messages"][1]["content"],
        )
        submitted = [
            event
            for event in artifact["partial_state"]["events"]
            if event["kind"] == "choice_submitted" and event["cycle"] == 3
        ]
        self.assertEqual([event["actor"] for event in submitted], ["Cinder", "Lumen"])
        self.assertTrue(
            all(
                event["detail"]["initiative_order"]
                == ["Cinder", "Lumen", "Aster", "Birch"]
                for event in submitted
            )
        )
        self.assertTrue(
            any(
                event["kind"] == "speech_sent"
                and event["actor"] == "Cinder"
                and event["detail"]["message"]["text"] == spoken
                for event in artifact["partial_state"]["events"]
            )
        )
        self.assertTrue(
            any(message["text"] == spoken for message in artifact["partial_state"]["messages"])
        )
        initial_survivors = {
            survivor["name"]: survivor
            for survivor in artifact["initial_state"]["survivors"]
        }
        partial_survivors = {
            survivor["name"]: survivor
            for survivor in artifact["partial_state"]["survivors"]
        }
        for name in initial_survivors:
            for field in ("energy", "food", "wood", "shelter", "resting", "alive"):
                self.assertEqual(
                    partial_survivors[name][field],
                    initial_survivors[name][field],
                )
        self.assertEqual(
            artifact["partial_state"]["resources"],
            artifact["initial_state"]["resources"],
        )

    def test_sequential_replacement_validation_precedes_credentials_and_transport(
        self,
    ) -> None:
        cases = (
            (
                (),
                "reason",
                "requires a model replacement",
            ),
            (
                (
                    "Cinder=opencode-paid/gpt-5.6-luna",
                    "Birch=opencode-paid/gpt-5.6-luna",
                ),
                "reason",
                "at most one model replacement",
            ),
            (
                ("Unknown=opencode-paid/gpt-5.6-luna",),
                "reason",
                "unknown identity",
            ),
            (
                ("Cinder=opencode-paid/kimi-k2.6",),
                "reason",
                "must change",
            ),
            (
                ("Cinder=opencode-paid/gpt-5.6-luna",),
                None,
                "replacement_reason",
            ),
        )
        with tempfile.TemporaryDirectory() as directory:
            root_path, parent_path, parent_sha256, _ = self._paid_v4_parent(
                directory
            )
            for replacements, reason, message in cases:
                with self.subTest(message=message):
                    transport = FakeTransport([])
                    with self.assertRaisesRegex(ValueError, message):
                        run_live_survival_continuation(
                            parent_path=parent_path,
                            expected_parent_sha256=parent_sha256,
                            ancestor_paths=(root_path,),
                            transition_reason="session_004_invalid_replacement",
                            interaction_protocol=SEQUENTIAL_DIALOGUE_V3,
                            model_replacements=replacements,
                            replacement_reason=reason,
                            max_calls=4,
                            max_completion_tokens=4_096,
                            reasoning_effort="low",
                            max_paid_usd="0.30",
                            transport=transport,
                            environ={},
                        )
                    self.assertEqual(transport.requests, [])

    def test_v6_parent_continues_without_another_model_replacement(self) -> None:
        transport = FakeTransport(
            [
                response(REST_REPLY, cost="0.001"),
                response(REST_REPLY, cost="0.001"),
                responses_response(REST_REPLY, cost="0.001"),
                responses_response(REST_REPLY, cost="0.001"),
            ]
        )
        with tempfile.TemporaryDirectory() as directory:
            root_path, v4_path, v6_path, v6_sha256, parent = (
                self._paid_v6_parent(directory)
            )
            artifact = run_live_survival_continuation(
                parent_path=v6_path,
                expected_parent_sha256=v6_sha256,
                ancestor_paths=(root_path, v4_path),
                transition_reason="session_005_sequential_continuation",
                interaction_protocol=SEQUENTIAL_DIALOGUE_V3,
                max_calls=4,
                max_completion_tokens=4_096,
                reasoning_effort="low",
                max_paid_usd="0.30",
                transport=transport,
                environ={"OPENCODE_ZEN_API_KEY": "test-only-key"},
            )

        self.assertEqual(artifact["status"], "completed")
        self.assertEqual(artifact["format_version"], 6)
        self.assertEqual(artifact["assignment_transition_receipts"], [])
        self.assertEqual(artifact["seat_assignments"], parent["seat_assignments"])
        self.assertEqual(
            [call["public_name"] for call in artifact["calls"]],
            ["Lumen", "Aster", "Birch", "Cinder"],
        )

    def test_v6_parent_rejects_transition_tamper_before_credentials_or_transport(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root_path, v4_path, _, _, v6 = self._paid_v6_parent(directory)
            tampered = deepcopy(v6)
            tampered["assignment_transition_receipts"][0][
                "previous_model"
            ] = "opencode-paid/glm-5.2"
            tampered_path, tampered_sha256 = self._write_parent(
                directory,
                tampered,
                "tampered-session-004.json",
            )
            transport = FakeTransport([])
            with self.assertRaisesRegex(
                ValueError,
                "receipt does not match the verified parent and child",
            ):
                run_live_survival_continuation(
                    parent_path=tampered_path,
                    expected_parent_sha256=tampered_sha256,
                    ancestor_paths=(root_path, v4_path),
                    transition_reason="session_005_tamper_check",
                    interaction_protocol=SEQUENTIAL_DIALOGUE_V3,
                    max_calls=4,
                    max_completion_tokens=4_096,
                    reasoning_effort="low",
                    max_paid_usd="0.30",
                    transport=transport,
                    environ={},
                )
            self.assertEqual(transport.requests, [])

    def test_v6_parent_rejects_implicit_global_protocol_downgrade(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root_path, v4_path, v6_path, v6_sha256, _ = (
                self._paid_v6_parent(directory)
            )
            transport = FakeTransport([])
            with self.assertRaisesRegex(
                ValueError,
                "format-v6 parent must continue",
            ):
                run_live_survival_continuation(
                    parent_path=v6_path,
                    expected_parent_sha256=v6_sha256,
                    ancestor_paths=(root_path, v4_path),
                    transition_reason="session_005_downgrade_check",
                    max_calls=4,
                    max_completion_tokens=4_096,
                    reasoning_effort="low",
                    max_paid_usd="0.30",
                    transport=transport,
                    environ={},
                )
            self.assertEqual(transport.requests, [])

    def test_v6_parent_rejects_call_view_tamper_before_credentials_or_transport(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root_path, v4_path, _, _, v6 = self._paid_v6_parent(directory)
            tampered = deepcopy(v6)
            tampered["calls"][0]["request"]["max_output_tokens"] = 4_095
            tampered_path, tampered_sha256 = self._write_parent(
                directory,
                tampered,
                "tampered-call-session-004.json",
            )
            transport = FakeTransport([])
            with self.assertRaisesRegex(
                ValueError,
                "request does not match its replay view",
            ):
                run_live_survival_continuation(
                    parent_path=tampered_path,
                    expected_parent_sha256=tampered_sha256,
                    ancestor_paths=(root_path, v4_path),
                    transition_reason="session_005_call_tamper_check",
                    interaction_protocol=SEQUENTIAL_DIALOGUE_V3,
                    max_calls=4,
                    max_completion_tokens=4_096,
                    reasoning_effort="low",
                    max_paid_usd="0.30",
                    transport=transport,
                    environ={},
                )
            self.assertEqual(transport.requests, [])

    def test_live_continuation_extends_v4_parent_as_format_v5(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            chain = self._free_continuation_chain(directory, through_format=4)
            root_path, _, _ = chain[0]
            parent_path, parent_sha256, _ = chain[-1]
            artifact = run_live_survival_continuation(
                parent_path=parent_path,
                expected_parent_sha256=parent_sha256,
                ancestor_paths=(root_path,),
                transition_reason="session_003_recursive_chain",
                max_calls=4,
                transport=FakeTransport(
                    [response(REST_REPLY) for _ in FREE_MODELS]
                ),
                environ={},
            )

        self.assertEqual(artifact["status"], "completed")
        self.assertEqual(artifact["format_version"], 5)
        self.assertEqual(
            artifact["continuation_link"],
            {
                "parent_artifact_name": parent_path.name,
                "parent_artifact_sha256": parent_sha256,
                "parent_canonical_result_sha256": chain[-1][2][
                    "canonical_result_sha256"
                ],
                "parent_format_version": 4,
                "parent_mode": "live_named_survival_continuation",
            },
        )
        self.assertEqual(artifact["result"]["final_state"]["cycle"], 3)
        self.assertEqual(
            replay_survival(result_from(artifact)).to_dict(), artifact["result"]
        )

    def test_recursive_continuation_requires_complete_ancestor_chain(self) -> None:
        transport = FakeTransport([])
        with tempfile.TemporaryDirectory() as directory:
            chain = self._free_continuation_chain(directory, through_format=4)
            parent_path, parent_sha256, _ = chain[-1]
            with self.assertRaisesRegex(ValueError, "requires its ancestor chain"):
                run_live_survival_continuation(
                    parent_path=parent_path,
                    expected_parent_sha256=parent_sha256,
                    transition_reason="session_003_recursive_chain",
                    transport=transport,
                    environ={},
                )
        self.assertEqual(transport.requests, [])

    def test_recursive_continuation_rejects_wrong_ancestor(self) -> None:
        transport = FakeTransport([])
        with tempfile.TemporaryDirectory() as directory:
            chain = self._free_continuation_chain(directory, through_format=4)
            wrong_root = deepcopy(chain[0][2])
            wrong_root["adapter"] = "wrong-adapter"
            wrong_path, _ = self._write_parent(
                directory,
                wrong_root,
                "wrong-session-001.json",
            )
            parent_path, parent_sha256, _ = chain[-1]
            with self.assertRaisesRegex(ValueError, "continuation_link"):
                run_live_survival_continuation(
                    parent_path=parent_path,
                    expected_parent_sha256=parent_sha256,
                    ancestor_paths=(wrong_path,),
                    transition_reason="session_003_recursive_chain",
                    transport=transport,
                    environ={},
                )
        self.assertEqual(transport.requests, [])

    def test_recursive_continuation_rejects_tampered_ancestor(self) -> None:
        transport = FakeTransport([])
        with tempfile.TemporaryDirectory() as directory:
            chain = self._free_continuation_chain(directory, through_format=4)
            tampered_root = deepcopy(chain[0][2])
            tampered_root["result"]["final_state"]["survivors"][0][
                "energy"
            ] += 1
            tampered_path, _ = self._write_parent(
                directory,
                tampered_root,
                "tampered-session-001.json",
            )
            parent_path, parent_sha256, _ = chain[-1]
            with self.assertRaisesRegex(ValueError, "canonical result SHA-256"):
                run_live_survival_continuation(
                    parent_path=parent_path,
                    expected_parent_sha256=parent_sha256,
                    ancestor_paths=(tampered_path,),
                    transition_reason="session_003_recursive_chain",
                    transport=transport,
                    environ={},
                )
        self.assertEqual(transport.requests, [])

    def test_recursive_continuation_rejects_reordered_ancestors(self) -> None:
        transport = FakeTransport([])
        with tempfile.TemporaryDirectory() as directory:
            chain = self._free_continuation_chain(directory, through_format=5)
            parent_path, parent_sha256, _ = chain[-1]
            with self.assertRaisesRegex(ValueError, "ordered oldest to newest"):
                run_live_survival_continuation(
                    parent_path=parent_path,
                    expected_parent_sha256=parent_sha256,
                    ancestor_paths=(chain[1][0], chain[0][0]),
                    transition_reason="session_004_recursive_chain",
                    transport=transport,
                    environ={},
                )
        self.assertEqual(transport.requests, [])

    def test_live_continuation_rejects_unproven_source_before_transport(self) -> None:
        parent = self._continuation_parent()
        parent["source"]["cli_sha256"] = "0" * 64
        transport = FakeTransport([])
        with tempfile.TemporaryDirectory() as directory:
            parent_path, parent_sha256 = self._write_parent(directory, parent)
            with self.assertRaisesRegex(ValueError, "source receipt"):
                run_live_survival_continuation(
                    parent_path=parent_path,
                    expected_parent_sha256=parent_sha256,
                    transition_reason="session_002_shelter_dilemma",
                    transport=transport,
                    environ={},
                )
        self.assertEqual(transport.requests, [])

    def test_format_v5_paid_failure_mid_beat_never_resolves_or_retries(self) -> None:
        root_replies = tuple(
            '{"action":{"kind":"rest"},"say":{"to":"everyone","text":"'
            + name
            + ' paid root note"}}'
            for name in ("Aster", "Birch", "Cinder", "Lumen")
        )
        root = run_live_survival(
            model_refs=PAID_MODELS,
            seed=29_993,
            days=1,
            max_calls=4,
            max_completion_tokens=1_024,
            max_paid_usd="0.30",
            transport=FakeTransport(
                [
                    paid_panel_response(index, reply, cost="0.001")
                    for index, reply in enumerate(root_replies)
                ]
            ),
            environ={"OPENCODE_ZEN_API_KEY": "test-only-key"},
        )
        with tempfile.TemporaryDirectory() as directory:
            root_path, root_sha256 = self._write_parent(
                directory,
                root,
                "paid-session-001.json",
            )
            parent_replies = tuple(
                '{"action":{"kind":"rest"},"say":{"to":"everyone","text":"'
                + name
                + ' paid continuation note"}}'
                for name in ("Aster", "Birch", "Cinder", "Lumen")
            )
            parent = run_live_survival_continuation(
                parent_path=root_path,
                expected_parent_sha256=root_sha256,
                transition_reason="session_002_paid_chain",
                max_calls=4,
                max_completion_tokens=1_024,
                max_paid_usd="0.30",
                transport=FakeTransport(
                    [
                        paid_panel_response(index, reply, cost="0.001")
                        for index, reply in enumerate(parent_replies)
                    ]
                ),
                environ={"OPENCODE_ZEN_API_KEY": "test-only-key"},
            )
            parent_path, parent_sha256 = self._write_parent(
                directory,
                parent,
                "paid-session-002.json",
            )
            exhausted_payload = json.loads(
                paid_panel_response(2, WAIT_REPLY, cost="0.001").body
            )
            exhausted_payload["choices"][0]["finish_reason"] = "length"
            transport = FakeTransport(
                [
                    paid_panel_response(0, WAIT_REPLY, cost="0.001"),
                    paid_panel_response(1, WAIT_REPLY, cost="0.001"),
                    TransportResponse(200, {}, json.dumps(exhausted_payload)),
                ]
            )
            artifact = run_live_survival_continuation(
                parent_path=parent_path,
                expected_parent_sha256=parent_sha256,
                ancestor_paths=(root_path,),
                transition_reason="session_003_paid_chain",
                max_calls=4,
                max_completion_tokens=1_024,
                max_paid_usd="0.30",
                transport=transport,
                environ={"OPENCODE_ZEN_API_KEY": "test-only-key"},
            )

        self.assertEqual(artifact["format_version"], 5)
        self.assertEqual(artifact["status"], "failed")
        self.assertEqual(artifact["failure"]["kind"], "completion_budget_exhausted")
        self.assertEqual(len(transport.requests), 3)
        self.assertEqual(len(artifact["calls"]), 3)
        self.assertEqual(
            [call["status"] for call in artifact["calls"]],
            ["succeeded", "succeeded", "failed"],
        )
        self.assertEqual(
            [
                event["kind"]
                for event in artifact["partial_state"]["events"]
                if event["cycle"] == 3
            ],
            ["resource_adjusted", "cycle_started", "slot_started"],
        )
        self.assertEqual(
            artifact["partial_state"]["resources"],
            artifact["initial_state"]["resources"],
        )

    def test_paid_continuation_preflight_prices_reconstructed_cycle_two_views(self) -> None:
        parent_replies = tuple(
            '{"action":{"kind":"rest"},"say":{"to":"everyone","text":"'
            + name
            + ' paid parent note"}}'
            for name in ("Aster", "Birch", "Cinder", "Lumen")
        )
        parent_transport = FakeTransport(
            [
                paid_panel_response(index, reply, cost="0.001")
                for index, reply in enumerate(parent_replies)
            ]
        )
        parent = run_live_survival(
            model_refs=PAID_MODELS,
            seed=29_993,
            days=1,
            max_calls=4,
            max_paid_usd="0.30",
            transport=parent_transport,
            environ={"OPENCODE_ZEN_API_KEY": "test-only-key"},
        )
        child_transport = FakeTransport(
            [
                paid_panel_response(index, REST_REPLY, cost="0.001")
                for index in range(4)
            ]
        )
        with tempfile.TemporaryDirectory() as directory:
            parent_path, parent_sha256 = self._write_parent(directory, parent)
            artifact = run_live_survival_continuation(
                parent_path=parent_path,
                expected_parent_sha256=parent_sha256,
                transition_reason="session_002_shelter_dilemma",
                max_calls=4,
                max_paid_usd="0.30",
                transport=child_transport,
                environ={"OPENCODE_ZEN_API_KEY": "test-only-key"},
            )

        self.assertEqual(artifact["status"], "completed")
        first_messages = child_transport.requests[0]["body"]["messages"]
        actual_prompt_bytes = len(
            json.dumps(
                first_messages,
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
        )
        self.assertEqual(
            artifact["paid_preflight"]["calls"][0]["prompt_utf8_bytes"],
            actual_prompt_bytes,
        )
        self.assertIn(
            "Aster paid parent note",
            first_messages[1]["content"],
        )
        self.assertEqual(artifact["calls"][0]["cycle"], 2)

    def test_live_cli_rejects_an_existing_output_before_transport(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "existing.json"
            output.write_text("keep me", encoding="utf-8")
            environment = dict(os.environ)
            environment["PYTHONPATH"] = str(
                Path(__file__).resolve().parents[1] / "src"
            )

            completed = subprocess.run(
                (
                    sys.executable,
                    "-m",
                    "world_sim",
                    "survive-live",
                    "--model",
                    "opencode/alpha-free",
                    "--model",
                    "opencode/beta-free",
                    "--model",
                    "opencode/gamma-free",
                    "--model",
                    "opencode/delta-free",
                    "--output",
                    str(output),
                ),
                check=False,
                capture_output=True,
                env=environment,
                text=True,
            )

            self.assertEqual(completed.returncode, 2)
            self.assertIn("live output already exists", completed.stderr)
            self.assertEqual(output.read_text(encoding="utf-8"), "keep me")

    def test_live_output_reservation_is_exclusive_and_writes_through_handle(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "nested" / "receipt.json"
            handle = _reserve_live_output(output)

            with self.assertRaises(FileExistsError):
                _reserve_live_output(output)

            _write_reserved_live_output(handle, {"status": "completed"})
            self.assertEqual(
                json.loads(output.read_text(encoding="utf-8")),
                {"status": "completed"},
            )

    def test_live_cli_removes_a_reservation_after_preflight_rejection(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "rejected.json"
            environment = dict(os.environ)
            environment["PYTHONPATH"] = str(
                Path(__file__).resolve().parents[1] / "src"
            )
            command = [
                sys.executable,
                "-m",
                "world_sim",
                "survive-live",
            ]
            for model in FREE_MODELS:
                command.extend(("--model", model))
            command.extend(
                (
                    "--max-calls",
                    "15",
                    "--require-complete-budget",
                    "--output",
                    str(output),
                )
            )

            completed = subprocess.run(
                command,
                check=False,
                capture_output=True,
                env=environment,
                text=True,
            )

            self.assertEqual(completed.returncode, 2)
            self.assertIn(
                "complete-cycle budget requires at least 16",
                completed.stderr,
            )
            self.assertFalse(output.exists())

    def test_continue_cli_rejects_a_parent_hash_before_credentials_or_transport(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            parent = Path(directory) / "parent.json"
            output = Path(directory) / "continuation.json"
            parent.write_text("{}\n", encoding="utf-8")
            environment = dict(os.environ)
            environment["PYTHONPATH"] = str(
                Path(__file__).resolve().parents[1] / "src"
            )

            completed = subprocess.run(
                (
                    sys.executable,
                    "-m",
                    "world_sim",
                    "continue-live",
                    "--parent",
                    str(parent),
                    "--parent-sha256",
                    "0" * 64,
                    "--transition-id",
                    "session_002_shelter_dilemma",
                    "--output",
                    str(output),
                ),
                check=False,
                capture_output=True,
                env=environment,
                text=True,
            )

            self.assertEqual(completed.returncode, 2)
            self.assertIn("parent artifact SHA-256 mismatch", completed.stderr)
            self.assertFalse(output.exists())

    def test_continue_cli_passes_repeatable_ancestors_oldest_to_newest(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            chain = self._free_continuation_chain(directory, through_format=5)
            output = Path(directory) / "continuation.json"
            environment = dict(os.environ)
            environment["PYTHONPATH"] = str(
                Path(__file__).resolve().parents[1] / "src"
            )
            parent_path, parent_sha256, _ = chain[-1]

            completed = subprocess.run(
                (
                    sys.executable,
                    "-m",
                    "world_sim",
                    "continue-live",
                    "--ancestor",
                    str(chain[0][0]),
                    "--ancestor",
                    str(chain[1][0]),
                    "--parent",
                    str(parent_path),
                    "--parent-sha256",
                    parent_sha256,
                    "--transition-id",
                    "session_004_cli_chain",
                    "--max-calls",
                    "0",
                    "--output",
                    str(output),
                ),
                check=False,
                capture_output=True,
                env=environment,
                text=True,
            )

            self.assertEqual(completed.returncode, 2)
            self.assertIn("requires at least 4 model calls", completed.stderr)
            self.assertFalse(output.exists())

    def test_live_host_rejects_noncalibrated_population_before_transport(self) -> None:
        transport = FakeTransport([])

        with self.assertRaisesRegex(ValueError, "exactly four survivors"):
            run_live_survival(
                model_refs=("opencode/alpha-free", "opencode/beta-free"),
                days=1,
                max_calls=2,
                transport=transport,
                environ={},
            )

        self.assertEqual(transport.requests, [])

    def test_request_is_tool_free_and_credentials_do_not_leak(self) -> None:
        secret = "not-for-the-artifact"
        transport = FakeTransport(
            [response(REST_REPLY) for _ in GO_MODELS]
        )
        artifact = run_live_survival(
            model_refs=GO_MODELS,
            days=1,
            max_calls=4,
            transport=transport,
            environ={"OPENCODE_API_KEY": secret},
        )

        self.assertEqual(artifact["status"], "completed")
        for request, model in zip(
            transport.requests,
            ("alpha", "beta", "gamma", "delta"),
            strict=True,
        ):
            endpoint = request["endpoint"]
            self.assertIsInstance(endpoint, EndpointSpec)
            self.assertEqual(endpoint.url, "https://opencode.ai/zen/go/v1/chat/completions")
            self.assertEqual(request["api_key"], secret)
            body = request["body"]
            self.assertEqual(
                set(body),
                {
                    "model",
                    "messages",
                    "max_tokens",
                    "temperature",
                    "response_format",
                    "stream",
                },
            )
            self.assertEqual(body["model"], model)
            self.assertEqual(body["max_tokens"], 4_096)
            self.assertNotIn("tools", body)
            prompts = json.dumps(body["messages"])
            self.assertNotIn(model, prompts)
            self.assertNotIn("seat-", prompts)
        self.assertNotIn(secret, json.dumps(artifact))

    def test_four_models_complete_three_days_and_replay_without_calls(self) -> None:
        raw = REST_REPLY
        transport = FakeTransport(
            [response(raw, request_id=f"request-{index}") for index in range(12)]
        )
        artifact = run_live_survival(
            model_refs=FREE_MODELS,
            seed=29,
            days=3,
            max_calls=12,
            transport=transport,
            environ={},
        )

        self.assertEqual(artifact["status"], "completed")
        self.assertEqual(len(transport.requests), 12)
        self.assertEqual(
            [(call["day"], call["public_name"]) for call in artifact["calls"]],
            [
                (day, name)
                for day in (1, 2, 3)
                for name in ("Aster", "Birch", "Cinder", "Lumen")
            ],
        )
        self.assertEqual(
            artifact["provider_summary"]["provider_reported_usage"][
                "reasoning_tokens"
            ],
            180,
        )
        original = result_from(artifact)
        self.assertEqual(original.to_dict(), replay_survival(original).to_dict())
        self.assertEqual(len(transport.requests), 12)

    def test_live_survival_checkpoints_before_and_after_every_transport(self) -> None:
        transport = FakeTransport([response(REST_REPLY) for _ in FREE_MODELS])
        checkpoints: list[dict[str, object]] = []

        artifact = run_live_survival(
            model_refs=FREE_MODELS,
            days=1,
            max_calls=4,
            transport=transport,
            environ={},
            checkpoint=lambda current: checkpoints.append(
                deepcopy(dict(current))
            ),
        )

        self.assertEqual(artifact["status"], "completed")
        self.assertEqual(len(checkpoints), 10)
        self.assertEqual(checkpoints[0]["status"], "running")
        self.assertEqual(checkpoints[0]["calls"], [])
        self.assertEqual(checkpoints[-1], artifact)
        in_flight = [
            current
            for current in checkpoints
            if any(call["status"] == "in_flight" for call in current["calls"])
        ]
        self.assertEqual(len(in_flight), 4)
        self.assertEqual(
            [current["calls"][-1]["sequence"] for current in in_flight],
            [1, 2, 3, 4],
        )
        self.assertTrue(
            all(
                current["provider_summary"]["calls_failed"] == 0
                for current in in_flight
            )
        )

    def test_in_flight_checkpoint_failure_aborts_before_transport(self) -> None:
        transport = FakeTransport([response(REST_REPLY)])
        checkpoint_count = 0

        def checkpoint(_: Mapping[str, object]) -> None:
            nonlocal checkpoint_count
            checkpoint_count += 1
            if checkpoint_count == 2:
                raise OSError("checkpoint failed")

        with self.assertRaises(RuntimeError) as captured:
            run_live_survival(
                model_refs=FREE_MODELS,
                days=1,
                max_calls=4,
                transport=transport,
                environ={},
                checkpoint=checkpoint,
            )

        self.assertIsInstance(captured.exception.__cause__, OSError)
        self.assertEqual(transport.requests, [])

    def test_post_transport_checkpoint_failure_does_not_retry(self) -> None:
        transport = FakeTransport([response(REST_REPLY)])
        checkpoints: list[dict[str, object]] = []

        def checkpoint(current: Mapping[str, object]) -> None:
            checkpoints.append(deepcopy(dict(current)))
            if len(checkpoints) == 3:
                raise OSError("checkpoint failed")

        with self.assertRaises(RuntimeError) as captured:
            run_live_survival(
                model_refs=FREE_MODELS,
                days=1,
                max_calls=4,
                transport=transport,
                environ={},
                checkpoint=checkpoint,
            )

        self.assertIsInstance(captured.exception.__cause__, OSError)
        self.assertEqual(len(transport.requests), 1)
        self.assertEqual(checkpoints[1]["calls"][-1]["status"], "in_flight")
        self.assertEqual(checkpoints[2]["calls"][-1]["status"], "succeeded")

    def test_live_world_uses_and_records_the_calibrated_preset(self) -> None:
        raw = REST_REPLY
        transport = FakeTransport([response(raw) for _ in FREE_MODELS])

        artifact = run_live_survival(
            model_refs=FREE_MODELS,
            days=1,
            max_calls=4,
            transport=transport,
            environ={},
        )

        self.assertEqual(artifact["config"]["world_preset"], LEAN_CAMP_V1)
        self.assertEqual(
            artifact["config"]["calibration_scope"],
            "calibrated",
        )
        self.assertEqual(artifact["authentication"], {"opencode": "none"})
        self.assertEqual(artifact["source"]["world_sim_version"], "0.13.0")
        self.assertEqual(
            artifact["config"]["interaction_protocol"], GLOBAL_BEATS_V2
        )
        source_paths = {
            "cli_sha256": Path(__file__).resolve().parents[1]
            / "src"
            / "world_sim"
            / "cli.py",
            "model_host_sha256": Path(__file__).resolve().parents[1]
            / "src"
            / "world_sim"
            / "model_host.py",
            "demo_sha256": Path(__file__).resolve().parents[1]
            / "src"
            / "world_sim"
            / "survival"
            / "demo.py",
            "engine_sha256": Path(__file__).resolve().parents[1]
            / "src"
            / "world_sim"
            / "survival"
            / "engine.py",
            "models_sha256": Path(__file__).resolve().parents[1]
            / "src"
            / "world_sim"
            / "survival"
            / "models.py",
            "prompt_sha256": Path(__file__).resolve().parents[1]
            / "src"
            / "world_sim"
            / "survival"
            / "prompt.py",
            "protocol_sha256": Path(__file__).resolve().parents[1]
            / "src"
            / "world_sim"
            / "survival"
            / "protocol.py",
            "calibration_sha256": Path(__file__).resolve().parents[1]
            / "src"
            / "world_sim"
            / "survival"
            / "calibration.py",
        }
        for key, path in source_paths.items():
            self.assertEqual(
                artifact["source"][key],
                hashlib.sha256(path.read_bytes()).hexdigest(),
            )
        self.assertEqual(
            artifact["result"]["initial_state"]["config"],
            survival_preset(LEAN_CAMP_V1, cycles=1).to_dict(),
        )
        first_prompt = transport.requests[0]["body"]["messages"][1]["content"]
        self.assertIn("food available: 6 of 12", first_prompt)
        self.assertIn("wood available: 4 of 12", first_prompt)
        self.assertIn("energy due after resting", first_prompt)

    def test_four_models_can_complete_one_fully_budgeted_cycle(self) -> None:
        forage = response('{"action":{"kind":"forage"},"say":null}')
        rest = response('{"action":{"kind":"rest"},"say":null}')
        transport = FakeTransport([forage] * 12 + [rest] * 4)
        models = tuple(f"opencode/model-{index}-free" for index in range(4))

        artifact = run_live_survival(
            model_refs=models,
            days=1,
            max_calls=16,
            require_complete_budget=True,
            transport=transport,
            environ={},
        )

        self.assertEqual(artifact["status"], "completed")
        self.assertTrue(artifact["config"]["require_complete_budget"])
        self.assertEqual(len(transport.requests), 16)
        self.assertEqual(
            [call["slot"] for call in artifact["calls"]],
            [1] * 4 + [2] * 4 + [3] * 4 + [4] * 4,
        )
        original = result_from(artifact)
        self.assertEqual(original.to_dict(), replay_survival(original).to_dict())

    def test_complete_budget_gate_rejects_before_transport(self) -> None:
        transport = FakeTransport([])

        with self.assertRaisesRegex(
            ValueError, "complete-cycle budget requires at least 16 model calls"
        ):
            run_live_survival(
                model_refs=tuple(
                    f"opencode/model-{index}-free" for index in range(4)
                ),
                days=1,
                max_calls=15,
                require_complete_budget=True,
                transport=transport,
                environ={},
            )

        self.assertEqual(transport.requests, [])

    def test_free_pool_key_is_optional_and_never_enters_artifact(self) -> None:
        secret = "zen-key-not-for-the-artifact"
        transport = FakeTransport(
            [response(REST_REPLY) for _ in FREE_MODELS]
        )
        artifact = run_live_survival(
            model_refs=FREE_MODELS,
            days=1,
            max_calls=4,
            transport=transport,
            environ={"OPENCODE_ZEN_API_KEY": f" {secret} "},
        )

        self.assertEqual(artifact["status"], "completed")
        self.assertEqual(
            [request["api_key"] for request in transport.requests],
            [secret] * 4,
        )
        self.assertEqual(artifact["authentication"], {"opencode": "bearer"})
        self.assertNotIn(secret, json.dumps(artifact))

    def test_paid_zen_four_call_cap_and_cost_receipt(self) -> None:
        secret = "paid-zen-key-not-for-the-artifact"
        models = (
            "deepseek-v4-flash",
            "grok-4.5",
            "kimi-k2.6",
            "glm-5.2",
        )
        transport = FakeTransport(
            [
                paid_panel_response(
                    index,
                    '{"action":{"kind":"rest"},"say":null}',
                    cost=cost,
                    cost_in_usage=index % 2 == 0,
                )
                for index, cost in enumerate(("0.001", "0.002", "0.003", "0.004"))
            ]
        )
        artifact = run_live_survival(
            model_refs=tuple(f"opencode-paid/{model}" for model in models),
            days=1,
            max_calls=4,
            max_completion_tokens=1_024,
            reasoning_effort="low",
            max_paid_usd="0.06",
            transport=transport,
            auth_path=Path("missing.json"),
            environ={"OPENCODE_ZEN_API_KEY": f" {secret} "},
        )

        self.assertEqual(artifact["status"], "completed")
        self.assertEqual(artifact["format_version"], 3)
        self.assertEqual(len(transport.requests), 4)
        self.assertEqual(artifact["config"]["max_paid_usd"], "0.06")
        self.assertLessEqual(
            Decimal(artifact["paid_preflight"]["first_chance_cost_bound_usd"]),
            Decimal("0.06"),
        )
        self.assertEqual(
            artifact["provider_summary"]["provider_reported_cost_usd"], "0.01"
        )
        self.assertTrue(artifact["provider_summary"]["cost_reporting_complete"])
        self.assertEqual(
            artifact["provider_summary"]["uncached_calculated_cost_usd"],
            "0.0007426",
        )
        self.assertEqual(
            [request["api_key"] for request in transport.requests],
            [secret, secret, secret, secret],
        )
        for request, model in zip(transport.requests, models, strict=True):
            endpoint = request["endpoint"]
            self.assertIsInstance(endpoint, EndpointSpec)
            expected_path = (
                "/zen/v1/responses"
                if model == "grok-4.5"
                else "/zen/v1/chat/completions"
            )
            self.assertEqual(endpoint.url, f"https://opencode.ai{expected_path}")
            self.assertEqual(request["body"]["model"], model)
        deepseek, grok, kimi, glm = (
            request["body"] for request in transport.requests
        )
        self.assertEqual(
            set(deepseek),
            {
                "model",
                "messages",
                "max_tokens",
                "temperature",
                "response_format",
                "stream",
                "reasoning_effort",
            },
        )
        self.assertEqual(
            set(grok),
            {
                "model",
                "input",
                "max_output_tokens",
                "temperature",
                "text",
                "reasoning",
                "stream",
                "store",
            },
        )
        self.assertEqual(
            set(kimi),
            {
                "model",
                "messages",
                "max_completion_tokens",
                "response_format",
                "thinking",
                "stream",
            },
        )
        self.assertEqual(kimi["thinking"], {"type": "disabled"})
        self.assertEqual(set(glm), set(deepseek))
        self.assertEqual(deepseek["reasoning_effort"], "low")
        self.assertEqual(grok["reasoning"], {"effort": "low"})
        self.assertEqual(grok["text"]["format"]["type"], "json_schema")
        self.assertEqual(glm["reasoning_effort"], "low")
        grok_authorization = artifact["calls"][1]["cost_authorization"]
        self.assertEqual(grok_authorization["max_completion_tokens"], 1_024)
        self.assertEqual(
            grok_authorization["prompt_utf8_bytes"],
            len(
                json.dumps(
                    grok["input"],
                    ensure_ascii=False,
                    separators=(",", ":"),
                ).encode("utf-8")
            ),
        )
        self.assertEqual(
            artifact["provider_summary"]["provider_reported_usage"],
            {
                "prompt_tokens": 400,
                "completion_tokens": 80,
                "reasoning_tokens": 60,
                "total_tokens": 480,
            },
        )
        self.assertNotIn(secret, json.dumps(artifact))

    def test_grok_responses_incomplete_budget_retains_cost_without_mutation(
        self,
    ) -> None:
        exhausted = json.loads(
            responses_response(REST_REPLY, cost="0.001", status="incomplete").body
        )
        exhausted["incomplete_details"] = {"reason": "max_output_tokens"}
        artifact = run_live_survival(
            model_refs=GROK_MODELS,
            days=1,
            max_calls=4,
            max_completion_tokens=1_024,
            max_paid_usd="0.11",
            transport=FakeTransport(
                [TransportResponse(200, {}, json.dumps(exhausted))]
            ),
            environ={"OPENCODE_ZEN_API_KEY": "secret"},
        )

        self.assertEqual(artifact["failure"]["kind"], "completion_budget_exhausted")
        self.assertEqual(artifact["provider_summary"]["calls_attempted"], 1)
        self.assertEqual(
            artifact["provider_summary"]["provider_reported_cost_usd"], "0.001"
        )
        self.assertEqual(artifact["partial_state"]["day"], 0)
        self.assertFalse(
            any(
                event["kind"] == "choice_submitted"
                for event in artifact["partial_state"]["events"]
            )
        )

    def test_grok_responses_rejects_unsupported_output_item(self) -> None:
        malformed = json.loads(responses_response(REST_REPLY, cost="0.001").body)
        malformed["output"].insert(1, {"type": "function_call"})
        artifact = run_live_survival(
            model_refs=GROK_MODELS,
            days=1,
            max_calls=4,
            max_completion_tokens=1_024,
            max_paid_usd="0.11",
            transport=FakeTransport(
                [TransportResponse(200, {}, json.dumps(malformed))]
            ),
            environ={"OPENCODE_ZEN_API_KEY": "secret"},
        )

        self.assertEqual(artifact["failure"]["kind"], "provider_envelope_error")
        self.assertEqual(artifact["partial_state"]["day"], 0)
        self.assertNotIn("raw_body", artifact["calls"][0]["response"])

    def test_grok_responses_rejects_conflicting_usage_aliases(self) -> None:
        for conflicting_field, value in (
            ("prompt_tokens", 101),
            ("completion_tokens", 21),
        ):
            with self.subTest(conflicting_field):
                conflicting = json.loads(
                    responses_response(REST_REPLY, cost="0.001").body
                )
                conflicting["usage"][conflicting_field] = value
                artifact = run_live_survival(
                    model_refs=GROK_MODELS,
                    days=1,
                    max_calls=4,
                    max_completion_tokens=1_024,
                    max_paid_usd="0.11",
                    transport=FakeTransport(
                        [TransportResponse(200, {}, json.dumps(conflicting))]
                    ),
                    environ={"OPENCODE_ZEN_API_KEY": "secret"},
                )

                self.assertEqual(artifact["failure"]["kind"], "provider_cost_error")
                self.assertEqual(artifact["partial_state"]["day"], 0)

    def test_paid_complete_cycle_authorizes_each_request_and_replays(self) -> None:
        transport = FakeTransport(
            [
                paid_panel_response(
                    index - 1,
                    '{"action":{"kind":"forage"},"say":null}',
                    request_id=f"paid-cycle-{index}",
                    cost="0.001",
                )
                for index in range(1, 17)
            ]
        )

        artifact = run_live_survival(
            model_refs=PAID_MODELS,
            seed=17,
            days=1,
            max_calls=16,
            require_complete_budget=True,
            max_completion_tokens=1_024,
            reasoning_effort="provider-default",
            max_paid_usd="0.18",
            transport=transport,
            environ={"OPENCODE_ZEN_API_KEY": "secret"},
        )

        self.assertEqual(artifact["status"], "completed")
        self.assertEqual(artifact["source"]["world_sim_version"], "0.13.0")
        self.assertEqual(len(transport.requests), 16)
        self.assertEqual(len(artifact["calls"]), 16)
        self.assertEqual(artifact["paid_preflight"]["authorized_calls"], 16)
        self.assertEqual(artifact["paid_preflight"]["world_max_calls"], 16)
        self.assertEqual(
            artifact["paid_preflight"]["cost_bound_scope"],
            "first_simultaneous_chance_only",
        )
        self.assertEqual(
            artifact["paid_preflight"]["price_source"],
            "https://opencode.ai/docs/zen",
        )
        for index, call in enumerate(artifact["calls"], start=1):
            authorization = call["cost_authorization"]
            prior = Decimal(authorization["prior_accounted_cost_usd"])
            request_bound = Decimal(authorization["request_cost_bound_usd"])
            cumulative_bound = Decimal(
                authorization["cumulative_cost_bound_usd"]
            )
            self.assertEqual(prior, Decimal(index - 1) * Decimal("0.001"))
            self.assertGreater(request_bound, 0)
            self.assertEqual(cumulative_bound, prior + request_bound)
            self.assertLessEqual(cumulative_bound, Decimal("0.18"))
            self.assertEqual(authorization["max_paid_usd"], "0.18")
            self.assertEqual(authorization["accounted_cost_usd"], "0.001")
            self.assertEqual(
                Decimal(authorization["cumulative_accounted_cost_usd"]),
                Decimal(index) * Decimal("0.001"),
            )
        original = result_from(artifact)
        self.assertEqual(replay_survival(original).to_dict(), original.to_dict())

    def test_paid_complete_cycle_shape_is_explicit_before_transport(self) -> None:
        cases = (
            (
                15,
                True,
                "complete-cycle budget requires at least 16 model calls",
            ),
            (16, False, "require-complete-budget"),
        )
        for max_calls, require_complete_budget, message in cases:
            with self.subTest(max_calls=max_calls):
                transport = FakeTransport([])
                with self.assertRaisesRegex(ValueError, message):
                    run_live_survival(
                        model_refs=PAID_MODELS,
                        days=1,
                        max_calls=max_calls,
                        require_complete_budget=require_complete_budget,
                        max_completion_tokens=1_024,
                        reasoning_effort="provider-default",
                        max_paid_usd="0.18",
                        transport=transport,
                        environ={},
                    )
                self.assertEqual(transport.requests, [])

    def test_paid_cumulative_guard_blocks_the_next_request(self) -> None:
        choice = '{"action":{"kind":"forage"},"say":null}'
        sizing_transport = FakeTransport(
            [
                paid_panel_response(index, choice, cost="0")
                for index in range(len(PAID_MODELS))
            ]
        )
        sized = run_live_survival(
            model_refs=PAID_MODELS,
            days=1,
            max_calls=4,
            max_completion_tokens=1_024,
            reasoning_effort="provider-default",
            max_paid_usd="0.18",
            transport=sizing_transport,
            environ={"OPENCODE_ZEN_API_KEY": "secret"},
        )
        request_bounds = [
            str(call["cost_authorization"]["request_cost_bound_usd"])
            for call in sized["calls"]
        ]
        first_chance_bound = sum(Decimal(bound) for bound in request_bounds)
        transport = FakeTransport(
            [
                paid_panel_response(index, choice, cost=bound)
                for index, bound in enumerate(request_bounds)
            ]
        )

        artifact = run_live_survival(
            model_refs=PAID_MODELS,
            days=1,
            max_calls=16,
            require_complete_budget=True,
            max_completion_tokens=1_024,
            reasoning_effort="provider-default",
            max_paid_usd=str(first_chance_bound),
            transport=transport,
            environ={"OPENCODE_ZEN_API_KEY": "secret"},
        )

        self.assertEqual(artifact["status"], "failed")
        self.assertEqual(artifact["failure"]["kind"], "paid_budget_exhausted")
        self.assertIsNone(artifact["failure"]["call_sequence"])
        self.assertEqual(len(transport.requests), 4)
        self.assertEqual(len(artifact["calls"]), 4)
        authorization = artifact["failure"]["cost_authorization"]
        self.assertEqual(
            Decimal(authorization["prior_accounted_cost_usd"]), first_chance_bound
        )
        self.assertGreater(
            Decimal(authorization["cumulative_cost_bound_usd"]),
            Decimal(authorization["max_paid_usd"]),
        )

    def test_paid_later_message_heavy_requests_receive_larger_bounds(self) -> None:
        long_message = "x" * 500
        choice = json.dumps(
            {
                "action": {"kind": "forage"},
                "say": {"to": "everyone", "text": long_message},
            },
            separators=(",", ":"),
        )
        transport = FakeTransport(
            [paid_panel_response(index, choice, cost="0.001") for index in range(16)]
        )

        artifact = run_live_survival(
            model_refs=PAID_MODELS,
            days=1,
            max_calls=16,
            require_complete_budget=True,
            max_completion_tokens=1_024,
            reasoning_effort="provider-default",
            max_paid_usd="0.23",
            transport=transport,
            environ={"OPENCODE_ZEN_API_KEY": "secret"},
        )

        self.assertEqual(artifact["status"], "completed")
        aster_calls = [
            call for call in artifact["calls"] if call["public_name"] == "Aster"
        ]
        self.assertEqual([call["slot"] for call in aster_calls], [1, 2, 3, 4])
        first = aster_calls[0]["cost_authorization"]
        second = aster_calls[1]["cost_authorization"]
        self.assertGreater(second["prompt_utf8_bytes"], first["prompt_utf8_bytes"])
        self.assertGreater(
            Decimal(second["request_cost_bound_usd"]),
            Decimal(first["request_cost_bound_usd"]),
        )
        for call in aster_calls:
            expected_prompt_bytes = len(
                json.dumps(
                    call["request"]["messages"],
                    ensure_ascii=False,
                    separators=(",", ":"),
                ).encode("utf-8")
            )
            self.assertEqual(
                call["cost_authorization"]["prompt_utf8_bytes"],
                expected_prompt_bytes,
            )

    def test_paid_provider_cost_over_request_bound_fails_before_choice_mutation(
        self,
    ) -> None:
        transport = FakeTransport(
            [
                response(
                    '{"action":{"kind":"forage"},"say":null}',
                    cost="0.01",
                )
            ]
        )

        artifact = run_live_survival(
            model_refs=PAID_MODELS,
            days=1,
            max_calls=16,
            require_complete_budget=True,
            max_completion_tokens=1_024,
            reasoning_effort="provider-default",
            max_paid_usd="0.18",
            transport=transport,
            environ={"OPENCODE_ZEN_API_KEY": "secret"},
        )

        self.assertEqual(artifact["status"], "failed")
        self.assertEqual(artifact["failure"]["kind"], "paid_cost_bound_breached")
        self.assertEqual(len(transport.requests), 1)
        self.assertEqual(len(artifact["calls"]), 1)
        call = artifact["calls"][0]
        self.assertEqual(call["status"], "failed")
        self.assertEqual(call["response"]["provider_reported_cost_usd"], "0.01")
        self.assertGreater(
            Decimal(call["cost_authorization"]["accounted_cost_usd"]),
            Decimal(call["cost_authorization"]["request_cost_bound_usd"]),
        )
        self.assertEqual(artifact["partial_state"]["day"], 0)
        self.assertFalse(
            any(
                event["kind"] == "choice_submitted"
                for event in artifact["partial_state"]["events"]
            )
        )

    def test_paid_timeout_retains_the_authorized_exposure(self) -> None:
        transport = TimeoutTransport()

        artifact = run_live_survival(
            model_refs=PAID_MODELS,
            days=1,
            max_calls=16,
            require_complete_budget=True,
            max_completion_tokens=1_024,
            reasoning_effort="provider-default",
            max_paid_usd="0.18",
            transport=transport,
            environ={"OPENCODE_ZEN_API_KEY": "secret"},
        )

        self.assertEqual(artifact["status"], "failed")
        self.assertEqual(artifact["failure"]["kind"], "transport_error")
        self.assertEqual(len(transport.requests), 1)
        self.assertEqual(len(artifact["calls"]), 1)
        call = artifact["calls"][0]
        self.assertIsNone(call["response"])
        authorization = call["cost_authorization"]
        self.assertEqual(authorization["prior_accounted_cost_usd"], "0")
        self.assertEqual(
            authorization["cumulative_cost_bound_usd"],
            authorization["request_cost_bound_usd"],
        )
        self.assertLessEqual(
            Decimal(authorization["cumulative_cost_bound_usd"]),
            Decimal(authorization["max_paid_usd"]),
        )

    def test_paid_compatibility_profile_uses_model_native_controls(self) -> None:
        models = (
            "deepseek-v4-flash",
            "kimi-k2.6",
            "glm-5.2",
            "deepseek-v4-flash",
        )
        transport = FakeTransport(
            [
                response(
                    '{"action":{"kind":"rest"},"say":null}',
                    cost="0.001",
                )
                for _ in models
            ]
        )
        artifact = run_live_survival(
            model_refs=tuple(f"opencode-paid/{model}" for model in models),
            days=1,
            max_calls=4,
            max_completion_tokens=1_024,
            reasoning_effort="compatibility-first",
            max_paid_usd="0.05",
            transport=transport,
            environ={"OPENCODE_ZEN_API_KEY": "secret"},
        )

        self.assertEqual(artifact["status"], "completed")
        self.assertEqual(
            artifact["config"]["reasoning_effort"], "compatibility-first"
        )
        deepseek, kimi, glm, second_deepseek = (
            request["body"] for request in transport.requests
        )
        self.assertEqual(deepseek["thinking"], {"type": "disabled"})
        self.assertNotIn("reasoning_effort", deepseek)
        self.assertEqual(kimi["thinking"], {"type": "disabled"})
        self.assertNotIn("temperature", kimi)
        self.assertEqual(glm["thinking"], {"type": "disabled"})
        self.assertEqual(glm["response_format"], {"type": "json_object"})
        self.assertNotIn("reasoning_effort", glm)
        self.assertEqual(second_deepseek["thinking"], {"type": "disabled"})

        public_names = ("Aster", "Birch", "Cinder", "Lumen")
        world = make_survival_world(public_names, seed=17)
        marker = "response JSON schema (your response must validate exactly):\n"
        for request, public_name, model in zip(
            transport.requests, public_names, models, strict=True
        ):
            body = request["body"]
            messages = body["messages"]
            self.assertEqual(len(messages), 2)
            user_prompt = messages[1]["content"]
            self.assertEqual(user_prompt.count(marker), 1)
            rendered_schema = json.loads(user_prompt.split(marker, 1)[1])
            expected_schema = response_schema(
                survival_view_for(world, public_name)
            )
            self.assertEqual(rendered_schema, expected_schema)
            fixed_action_kinds = {
                variant["properties"]["kind"]["const"]
                for variant in rendered_schema["properties"]["action"]["oneOf"]
                if set(variant.get("properties", {})) == {"kind"}
            }
            self.assertIn("wait", fixed_action_kinds)
            self.assertIn("gather_wood", fixed_action_kinds)
            self.assertNotIn(model, json.dumps(messages))

    def test_paid_compatibility_profile_rejects_grok_before_transport(self) -> None:
        transport = FakeTransport([])
        with self.assertRaisesRegex(ValueError, "grok-4.5 reasoning cannot be disabled"):
            run_live_survival(
                model_refs=PAID_MODELS,
                days=1,
                max_calls=4,
                max_completion_tokens=1_024,
                reasoning_effort="compatibility-first",
                max_paid_usd="0.18",
                transport=transport,
                environ={"OPENCODE_ZEN_API_KEY": "secret"},
            )
        self.assertEqual(transport.requests, [])

    def test_paid_provider_default_omits_reasoning_controls(self) -> None:
        models = (
            "deepseek-v4-flash",
            "grok-4.6",
            "kimi-k2.6",
            "glm-5.2",
        )
        transport = FakeTransport(
            [
                paid_panel_response(
                    index,
                    '{"action":{"kind":"rest"},"say":null}',
                    cost="0.001",
                )
                for index, _ in enumerate(models)
            ]
        )
        artifact = run_live_survival(
            model_refs=tuple(f"opencode-paid/{model}" for model in models),
            days=1,
            max_calls=4,
            max_completion_tokens=10_000,
            reasoning_effort="provider-default",
            max_paid_usd="0.23",
            transport=transport,
            environ={"OPENCODE_ZEN_API_KEY": "secret"},
        )

        self.assertEqual(artifact["status"], "completed")
        for request in transport.requests:
            body = request["body"]
            self.assertNotIn("reasoning_effort", body)
            self.assertNotIn("thinking", body)
            self.assertNotIn("reasoning", body)
        deepseek, grok, kimi, glm = (
            request["body"] for request in transport.requests
        )
        self.assertEqual(deepseek["max_tokens"], 10_000)
        self.assertEqual(grok["max_output_tokens"], 10_000)
        self.assertEqual(grok["text"]["format"]["type"], "json_schema")
        self.assertFalse(grok["store"])
        self.assertEqual(kimi["max_completion_tokens"], 10_000)
        self.assertEqual(glm["max_tokens"], 10_000)

    def test_paid_10k_ceiling_covers_a_maximum_speech_cycle(self) -> None:
        maximum_speech = "\U00010000" * 500
        choice = json.dumps(
            {
                "action": {"kind": "forage"},
                "say": {"to": "everyone", "text": maximum_speech},
            },
            separators=(",", ":"),
        )
        sizing_transport = FakeTransport(
            [paid_panel_response(index, choice, cost="0") for index in range(16)]
        )
        sized = run_live_survival(
            model_refs=PAID_MODELS,
            seed=29_994,
            days=1,
            max_calls=16,
            require_complete_budget=True,
            max_completion_tokens=10_000,
            reasoning_effort="provider-default",
            max_paid_usd="1.20",
            timeout_seconds=300,
            transport=sizing_transport,
            environ={"OPENCODE_ZEN_API_KEY": "secret"},
        )
        request_bounds = [
            str(call["cost_authorization"]["request_cost_bound_usd"])
            for call in sized["calls"]
        ]

        bounded_transport = FakeTransport(
            [
                paid_panel_response(index, choice, cost=bound)
                for index, bound in enumerate(request_bounds)
            ]
        )
        bounded = run_live_survival(
            model_refs=PAID_MODELS,
            seed=29_994,
            days=1,
            max_calls=16,
            require_complete_budget=True,
            max_completion_tokens=10_000,
            reasoning_effort="provider-default",
            max_paid_usd="1.20",
            timeout_seconds=300,
            transport=bounded_transport,
            environ={"OPENCODE_ZEN_API_KEY": "secret"},
        )

        self.assertEqual(sized["status"], "completed")
        self.assertEqual(bounded["status"], "completed")
        self.assertEqual(len(bounded_transport.requests), 16)
        total_bound = sum(Decimal(bound) for bound in request_bounds)
        self.assertGreater(total_bound, Decimal("0.85"))
        self.assertLess(total_bound, Decimal("1.20"))
        self.assertEqual(
            Decimal(
                bounded["calls"][-1]["cost_authorization"][
                    "cumulative_accounted_cost_usd"
                ]
            ),
            total_bound,
        )

    def test_compatibility_profile_rejects_nonpaid_routes_before_transport(self) -> None:
        transport = FakeTransport([])
        with self.assertRaisesRegex(
            ValueError, "compatibility-first requires a paid-only run"
        ):
            run_live_survival(
                model_refs=FREE_MODELS,
                days=1,
                max_calls=4,
                reasoning_effort="compatibility-first",
                transport=transport,
                environ={},
            )
        self.assertEqual(transport.requests, [])

    def test_paid_multiday_run_is_rejected_before_transport(self) -> None:
        transport = FakeTransport([])
        with self.assertRaisesRegex(ValueError, "require exactly one cycle"):
            run_live_survival(
                model_refs=PAID_MODELS,
                days=2,
                max_calls=8,
                max_completion_tokens=1_024,
                reasoning_effort="compatibility-first",
                max_paid_usd="0.004",
                transport=transport,
                environ={},
            )
        self.assertEqual(transport.requests, [])

    def test_paid_zen_rejects_unsafe_runs_before_credentials_or_transport(self) -> None:
        cases = (
            ({}, "max_paid_usd is required"),
            ({"max_paid_usd": "0"}, "positive finite decimal"),
            ({"max_paid_usd": "NaN"}, "positive finite decimal"),
            ({"max_paid_usd": "1.201"}, "cannot exceed 1.2 USD"),
            (
                {"max_paid_usd": "0.18", "max_completion_tokens": 10_001},
                "max_completion_tokens must be from 1 through 10000",
            ),
            (
                {"max_paid_usd": "0.05", "model_refs": ("opencode-paid/unknown",) * 4},
                "not in the pinned price allowlist",
            ),
            (
                {"max_paid_usd": "0.05", "days": 3, "max_calls": 12},
                "require exactly one cycle",
            ),
            (
                {"max_paid_usd": "0.05", "max_calls": 16},
                "require-complete-budget",
            ),
            (
                {"max_paid_usd": "0.000001"},
                "conservative paid bound",
            ),
        )
        defaults: dict[str, object] = {
            "model_refs": PAID_MODELS,
            "days": 1,
            "max_calls": 4,
            "max_completion_tokens": 1_024,
        }
        for overrides, message in cases:
            with self.subTest(message):
                transport = FakeTransport([])
                arguments = {**defaults, **overrides}
                with self.assertRaisesRegex(ValueError, message):
                    run_live_survival(
                        **arguments,
                        transport=transport,
                        auth_path=Path("missing.json"),
                        environ={},
                    )
                self.assertEqual(transport.requests, [])

    def test_paid_response_requires_a_valid_reported_cost(self) -> None:
        malformed = (
            response('{"action":{"kind":"rest"},"say":null}'),
            response('{"action":{"kind":"rest"},"say":null}', cost="-0.1"),
            response('{"action":{"kind":"rest"},"say":null}', cost="not-a-cost"),
        )
        for provider_response in malformed:
            with self.subTest(provider_response.body):
                artifact = run_live_survival(
                    model_refs=PAID_MODELS,
                    days=1,
                    max_calls=4,
                    max_completion_tokens=1_024,
                    max_paid_usd="0.06",
                    transport=FakeTransport([provider_response]),
                    environ={"OPENCODE_ZEN_API_KEY": "secret"},
                )
                self.assertEqual(artifact["status"], "failed")
                self.assertEqual(artifact["failure"]["kind"], "provider_cost_error")
                self.assertEqual(artifact["partial_state"]["day"], 0)
                self.assertFalse(
                    artifact["provider_summary"]["cost_reporting_complete"]
                )
                self.assertIsNone(
                    artifact["provider_summary"]["provider_reported_cost_usd"]
                )
                self.assertNotIn("raw_body", artifact["calls"][0]["response"])

    def test_paid_cost_preserves_numeric_json_and_rejects_conflicts(self) -> None:
        exact_cost = "0.0000001234567890123456789"
        exact_body = (
            '{"model":"fake","choices":[{"message":{"content":'
            '"{\\"action\\":{\\"kind\\":\\"rest\\"},\\"say\\":null}"},'
            '"finish_reason":"stop"}],"usage":{"prompt_tokens":100,'
            '"completion_tokens":20,"total_tokens":120},"cost":'
            f"{exact_cost}}}"
        )
        artifact = run_live_survival(
            model_refs=PAID_MODELS,
            days=1,
            max_calls=4,
            max_completion_tokens=1_024,
            max_paid_usd="0.06",
            transport=FakeTransport(
                [
                    TransportResponse(200, {}, exact_body),
                    responses_response(REST_REPLY, cost="0"),
                    response(REST_REPLY, cost="0"),
                    response(REST_REPLY, cost="0"),
                ]
            ),
            environ={"OPENCODE_ZEN_API_KEY": "secret"},
        )
        self.assertEqual(
            artifact["provider_summary"]["provider_reported_cost_usd"], exact_cost
        )

        conflict = response(
            '{"action":{"kind":"rest"},"say":null}', cost="0.001"
        )
        conflict_payload = json.loads(conflict.body)
        conflict_payload["usage"]["cost"] = "0.002"
        conflict = TransportResponse(200, {}, json.dumps(conflict_payload))
        failed = run_live_survival(
            model_refs=PAID_MODELS,
            days=1,
            max_calls=4,
            max_completion_tokens=1_024,
            max_paid_usd="0.06",
            transport=FakeTransport([conflict]),
            environ={"OPENCODE_ZEN_API_KEY": "secret"},
        )
        self.assertEqual(failed["failure"]["kind"], "provider_cost_error")
        self.assertFalse(failed["provider_summary"]["cost_reporting_complete"])

    def test_paid_length_failure_retains_the_billed_cost_without_retry(self) -> None:
        exhausted = json.loads(
            response(
                '{"action":{"kind":"rest"},"say":null}', cost="0.001"
            ).body
        )
        exhausted["choices"][0]["finish_reason"] = "length"
        transport = FakeTransport(
            [TransportResponse(200, {}, json.dumps(exhausted))]
        )
        artifact = run_live_survival(
            model_refs=PAID_MODELS,
            days=1,
            max_calls=4,
            max_completion_tokens=1_024,
            max_paid_usd="0.06",
            transport=transport,
            environ={"OPENCODE_ZEN_API_KEY": "secret"},
        )

        self.assertEqual(len(transport.requests), 1)
        self.assertEqual(artifact["failure"]["kind"], "completion_budget_exhausted")
        self.assertTrue(artifact["provider_summary"]["cost_reporting_complete"])
        self.assertEqual(
            artifact["provider_summary"]["provider_reported_cost_usd"], "0.001"
        )

    def test_low_reasoning_effort_is_sent_and_recorded(self) -> None:
        transport = FakeTransport(
            [response(REST_REPLY) for _ in FREE_MODELS]
        )
        artifact = run_live_survival(
            model_refs=FREE_MODELS,
            days=1,
            max_calls=4,
            reasoning_effort="low",
            transport=transport,
            environ={},
        )

        self.assertEqual(artifact["config"]["reasoning_effort"], "low")
        self.assertEqual(
            [request["body"]["reasoning_effort"] for request in transport.requests],
            ["low"] * 4,
        )

    def test_unknown_reasoning_effort_fails_before_transport(self) -> None:
        transport = FakeTransport([])
        with self.assertRaisesRegex(ValueError, "reasoning_effort must be one of"):
            run_live_survival(
                model_refs=FREE_MODELS,
                days=1,
                max_calls=4,
                reasoning_effort="invented",
                transport=transport,
                environ={},
            )
        self.assertEqual(transport.requests, [])

    def test_bad_model_json_wastes_one_slot_without_a_repair_retry(self) -> None:
        transport = FakeTransport(
            [
                response("not json"),
                response(REST_REPLY),
                response(REST_REPLY),
                response(REST_REPLY),
                response(REST_REPLY),
            ]
        )
        artifact = run_live_survival(
            model_refs=FREE_MODELS,
            days=1,
            max_calls=5,
            transport=transport,
            environ={},
        )

        malformed = artifact["calls"][0]
        self.assertEqual(len(transport.requests), 5)
        self.assertEqual(malformed["response"]["model_reply"], "not json")
        self.assertIn("not valid strict JSON", malformed["validation"]["action_error"])
        self.assertEqual(
            malformed["parsed_choice"],
            {"action": {"kind": "invalid"}, "say": None},
        )
        aster = artifact["result"]["final_state"]["survivors"][0]
        self.assertEqual(aster["energy"], 13)
        self.assertEqual(artifact["calls"][4]["slot"], 2)

    def test_provider_failures_stop_before_world_mutation_and_keep_receipts(self) -> None:
        cases = (
            (TransportResponse(503, {}, '{"error":"down"}'), "http_error"),
            (TransportResponse(200, {}, "{}"), "provider_envelope_error"),
            (
                TransportResponse(
                    200,
                    {},
                    json.dumps(
                        {
                            "choices": [
                                {
                                    "finish_reason": "length",
                                    "message": {"content": "{\"action\":"},
                                }
                            ]
                        }
                    ),
                ),
                "completion_budget_exhausted",
            ),
        )
        for provider_response, expected_kind in cases:
            with self.subTest(expected_kind):
                transport = FakeTransport([provider_response])
                checkpoints: list[dict[str, object]] = []
                artifact = run_live_survival(
                    model_refs=FREE_MODELS,
                    days=1,
                    max_calls=4,
                    transport=transport,
                    environ={},
                    checkpoint=lambda current: checkpoints.append(
                        deepcopy(dict(current))
                    ),
                )
                self.assertEqual(artifact["status"], "failed")
                self.assertEqual(artifact["failure"]["kind"], expected_kind)
                self.assertEqual(artifact["failure"]["cycle"], 1)
                self.assertEqual(artifact["failure"]["slot"], 1)
                self.assertEqual(artifact["partial_state"]["day"], 0)
                receipt = artifact["calls"][0]["response"]
                self.assertNotIn("raw_body", receipt)
                self.assertEqual(receipt["body_bytes"], len(provider_response.body.encode()))
                self.assertEqual(len(receipt["body_sha256"]), 64)
                self.assertEqual(len(transport.requests), 1)
                self.assertEqual(
                    [
                        current["calls"][-1]["status"]
                        if current["calls"]
                        else "empty"
                        for current in checkpoints
                    ],
                    ["empty", "in_flight", "failed", "failed"],
                )
                self.assertEqual(checkpoints[-1], artifact)

    def test_call_cap_fails_before_credentials_or_transport(self) -> None:
        transport = FakeTransport([])
        with self.assertRaisesRegex(ValueError, "requires at least 12 model calls"):
            run_live_survival(
                model_refs=GO_MODELS,
                days=3,
                max_calls=11,
                transport=transport,
                auth_path=Path("missing.json"),
                environ={},
            )
        self.assertEqual(transport.requests, [])

    def test_runtime_call_cap_stops_before_an_extra_request(self) -> None:
        raw = '{"action":{"kind":"forage"},"say":null}'
        transport = FakeTransport([response(raw) for _ in FREE_MODELS])

        artifact = run_live_survival(
            model_refs=FREE_MODELS,
            days=1,
            max_calls=4,
            transport=transport,
            environ={},
        )

        self.assertEqual(artifact["status"], "failed")
        self.assertEqual(artifact["failure"]["kind"], "call_cap_reached")
        self.assertEqual(artifact["failure"]["slot"], 2)
        self.assertEqual(len(transport.requests), 4)

    def test_go_key_loader_prefers_env_and_reads_strict_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            auth_path = Path(directory) / "auth.json"
            auth_path.write_text(
                json.dumps({"opencode-go": {"type": "api", "key": " file-key "}}),
                encoding="utf-8",
            )
            self.assertEqual(load_opencode_go_api_key(auth_path=auth_path, environ={}), "file-key")
            self.assertEqual(
                load_opencode_go_api_key(
                    auth_path=Path("missing.json"),
                    environ={"OPENCODE_API_KEY": " env-key "},
                ),
                "env-key",
            )
            auth_path.write_text(
                '{"opencode-go":{"type":"api","key":"one","key":"two"}}',
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "not valid strict JSON"):
                load_opencode_go_api_key(auth_path=auth_path, environ={})

    def test_provider_error_body_cannot_echo_the_key_into_artifacts(self) -> None:
        secret = "credential-that-must-not-leak"
        provider_response = TransportResponse(
            status=401,
            headers={},
            body=json.dumps({"error": {"message": f"rejected bearer {secret}"}}),
        )
        artifact = run_live_survival(
            model_refs=GO_MODELS,
            days=1,
            max_calls=4,
            transport=FakeTransport([provider_response]),
            environ={"OPENCODE_API_KEY": secret},
        )
        self.assertNotIn(secret, json.dumps(artifact))
        self.assertNotIn("raw_body", artifact["calls"][0]["response"])

    def test_unexpected_transport_failure_keeps_a_sanitized_artifact(self) -> None:
        artifact = run_live_survival(
            model_refs=FREE_MODELS,
            days=1,
            max_calls=4,
            transport=BrokenTransport(),
            environ={},
        )
        self.assertEqual(artifact["status"], "failed")
        self.assertEqual(artifact["failure"]["kind"], "transport_error")
        self.assertEqual(artifact["failure"]["message"], "transport raised LookupError")
        self.assertNotIn("secret diagnostic", json.dumps(artifact))
        self.assertEqual(artifact["partial_state"]["day"], 0)

    def test_postmortem_notices_are_quarantined_after_world_completion(self) -> None:
        world_artifact = run_live_survival(
            model_refs=FREE_MODELS,
            seed=41,
            days=6,
            max_calls=24,
            transport=FakeTransport([response(REST_REPLY) for _ in range(24)]),
            environ={},
        )
        self.assertEqual(world_artifact["status"], "completed")
        self.assertEqual(
            world_artifact["result"]["final_state"]["finished_reason"],
            "everyone_died",
        )
        reflections = [
            '{"reflection":"I spent every day resting."}',
            '{"reflection":"I never replenished energy."}',
            '{"reflection":"The role exhausted its remaining energy."}',
            '{"reflection":"Rest did not offset metabolism."}',
        ]
        postmortem_transport = FakeTransport(
            [response(reflection) for reflection in reflections]
        )
        checkpoints: list[dict[str, object]] = []
        with tempfile.TemporaryDirectory() as directory:
            world_path = Path(directory) / "world.json"
            world_bytes = (
                json.dumps(world_artifact, indent=2, sort_keys=True) + "\n"
            ).encode("utf-8")
            world_path.write_bytes(world_bytes)
            world_sha256 = hashlib.sha256(world_bytes).hexdigest()

            postmortem = run_live_postmortem(
                world_artifact_path=world_path,
                expected_world_artifact_sha256=world_sha256,
                transport=postmortem_transport,
                environ={},
                checkpoint=lambda payload: checkpoints.append(deepcopy(dict(payload))),
            )

            self.assertEqual(world_path.read_bytes(), world_bytes)
        self.assertEqual(postmortem["status"], "completed")
        self.assertEqual(
            postmortem["summary"],
            {
                "death_targets": 4,
                "calls_attempted": 4,
                "calls_succeeded": 4,
                "calls_failed": 0,
                "calls_skipped": 0,
                "reflection_characters": sum(
                    len(json.loads(value)["reflection"]) for value in reflections
                ),
                "paid_cost_accounted_usd": None,
            },
        )
        self.assertEqual(
            [call["reflection"] for call in postmortem["calls"]],
            [json.loads(value)["reflection"] for value in reflections],
        )
        self.assertTrue(all(call["status"] == "succeeded" for call in postmortem["calls"]))
        self.assertTrue(
            all(
                "You are not actually dead."
                in request["body"]["messages"][1]["content"]
                for request in postmortem_transport.requests
            )
        )
        self.assertTrue(
            all(
                "Neither you, the model, nor any real entity died."
                in request["body"]["messages"][1]["content"]
                for request in postmortem_transport.requests
            )
        )
        self.assertTrue(
            all(
                "cannot alter the saved world"
                in request["body"]["messages"][1]["content"]
                for request in postmortem_transport.requests
            )
        )
        self.assertEqual(checkpoints[-1]["status"], "completed")
        self.assertTrue(
            any(
                call.get("status") == "in_flight"
                for checkpoint in checkpoints
                for call in checkpoint["calls"]
            )
        )
        self.assertNotIn("postmortem", json.dumps(world_artifact).casefold())

    def test_postmortem_failure_is_not_retried_and_later_deaths_continue(self) -> None:
        world_artifact = run_live_survival(
            model_refs=FREE_MODELS,
            seed=43,
            days=6,
            max_calls=24,
            transport=FakeTransport([response(REST_REPLY) for _ in range(24)]),
            environ={},
        )
        failed = TransportResponse(
            status=503,
            headers={"x-request-id": "failed-postmortem"},
            body='{"error":"temporary"}',
        )
        transport = FakeTransport(
            [
                failed,
                response('{"reflection":"second"}'),
                response('{"reflection":"third"}'),
                response('{"reflection":"fourth"}'),
            ]
        )
        with tempfile.TemporaryDirectory() as directory:
            world_path = Path(directory) / "world.json"
            world_bytes = (
                json.dumps(world_artifact, indent=2, sort_keys=True) + "\n"
            ).encode("utf-8")
            world_path.write_bytes(world_bytes)
            postmortem = run_live_postmortem(
                world_artifact_path=world_path,
                expected_world_artifact_sha256=hashlib.sha256(
                    world_bytes
                ).hexdigest(),
                transport=transport,
                environ={},
            )

        self.assertEqual(len(transport.requests), 4)
        self.assertEqual(
            [call["status"] for call in postmortem["calls"]],
            ["failed", "succeeded", "succeeded", "succeeded"],
        )
        self.assertEqual(postmortem["calls"][0]["error"]["kind"], "http_error")
        self.assertEqual(postmortem["summary"]["calls_failed"], 1)
        self.assertEqual(postmortem["summary"]["calls_succeeded"], 3)

    def test_postmortem_cli_reserves_and_checkpoints_its_separate_output(self) -> None:
        payload = {
            "mode": "live_postmortem_reflection",
            "status": "completed",
            "summary": {
                "death_targets": 1,
                "calls_attempted": 1,
                "calls_succeeded": 1,
                "calls_failed": 0,
                "calls_skipped": 0,
                "reflection_characters": 4,
                "paid_cost_accounted_usd": None,
            },
            "calls": [],
        }

        def fake_run(**kwargs: object) -> dict[str, object]:
            checkpoint = kwargs["checkpoint"]
            assert callable(checkpoint)
            checkpoint(payload)
            return payload

        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "postmortem.json"
            with patch("world_sim.cli.run_live_postmortem", fake_run), redirect_stdout(
                io.StringIO()
            ):
                exit_code = main(
                    (
                        "postmortem-live",
                        "--world-artifact",
                        str(Path(directory) / "world.json"),
                        "--world-artifact-sha256",
                        "0" * 64,
                        "--output",
                        str(output),
                    )
                )

            self.assertEqual(exit_code, 0)
            self.assertEqual(json.loads(output.read_text(encoding="utf-8")), payload)

    def test_paid_postmortem_has_a_separate_full_preflight_and_cost_ledger(self) -> None:
        gather = '{"action":{"kind":"gather_wood"},"say":null}'
        names = ("Aster", "Birch", "Cinder", "Lumen")

        def paid_reply(index: int) -> str:
            if index < 12:
                return gather
            return json.dumps(
                {
                    "action": {"kind": "rest"},
                    "say": {
                        "to": "everyone",
                        "text": f"{names[index % 4]} final note",
                    },
                },
                separators=(",", ":"),
            )

        paid_day = [
            paid_panel_response(
                index,
                paid_reply(index),
                cost="0.0001",
            )
            for index in range(16)
        ]
        with tempfile.TemporaryDirectory() as directory:
            root = run_live_survival(
                model_refs=PAID_MODELS,
                seed=53,
                days=1,
                max_calls=16,
                require_complete_budget=True,
                max_completion_tokens=1_024,
                reasoning_effort="low",
                max_paid_usd="0.62",
                transport=FakeTransport(paid_day),
                environ={"OPENCODE_ZEN_API_KEY": "test-only-key"},
            )
            root_path, root_sha256 = self._write_parent(
                directory,
                root,
                "paid-death-root.json",
            )
            dying_day = run_live_survival_continuation(
                parent_path=root_path,
                expected_parent_sha256=root_sha256,
                transition_reason="paid_postmortem_death_fixture",
                shared_stock=0,
                max_calls=16,
                require_complete_budget=True,
                max_completion_tokens=1_024,
                reasoning_effort="low",
                max_paid_usd="0.62",
                transport=FakeTransport(
                    [
                        paid_panel_response(
                            index,
                            paid_reply(index),
                            cost="0.0001",
                        )
                        for index in range(16)
                    ]
                ),
                environ={"OPENCODE_ZEN_API_KEY": "test-only-key"},
            )
            self.assertEqual(
                dying_day["result"]["final_state"]["finished_reason"],
                "everyone_died",
            )
            world_path, world_sha256 = self._write_parent(
                directory,
                dying_day,
                "paid-death-world.json",
            )
            postmortem = run_live_postmortem(
                world_artifact_path=world_path,
                expected_world_artifact_sha256=world_sha256,
                ancestor_paths=(root_path,),
                max_paid_usd="0.05",
                transport=FakeTransport(
                    [
                        paid_panel_response(
                            index,
                            '{"reflection":"role ended after repeated work"}',
                            cost="0.0001",
                        )
                        for index in range(4)
                    ]
                ),
                environ={"OPENCODE_ZEN_API_KEY": "test-only-key"},
            )
            postmortem_path, _ = self._write_parent(
                directory,
                postmortem,
                "paid-death-postmortem.json",
            )
            verified = subprocess.run(
                (
                    sys.executable,
                    str(
                        Path(__file__).resolve().parents[1]
                        / "tools"
                        / "verify_postmortem_artifact.py"
                    ),
                    str(postmortem_path),
                    "--world-artifact",
                    str(world_path),
                    "--ancestor",
                    str(root_path),
                ),
                cwd=Path(__file__).resolve().parents[1],
                check=True,
                capture_output=True,
                text=True,
            )
            verifier_receipt = json.loads(verified.stdout)

        self.assertEqual(postmortem["status"], "completed")
        self.assertEqual(postmortem["paid_preflight"]["authorized_calls"], 4)
        self.assertLessEqual(
            Decimal(postmortem["paid_preflight"]["total_cost_bound_usd"]),
            Decimal("0.05"),
        )
        self.assertTrue(
            all("cost_authorization" in call for call in postmortem["calls"])
        )
        self.assertTrue(
            all(
                call["cost_authorization"]["accounting_basis"]
                == "provider_or_calculated_cost"
                for call in postmortem["calls"]
            )
        )
        self.assertEqual(postmortem["summary"]["calls_succeeded"], 4)
        self.assertIsNotNone(postmortem["summary"]["paid_cost_accounted_usd"])
        self.assertEqual(verifier_receipt["status"], "verified")
        self.assertEqual(verifier_receipt["calls_succeeded"], 4)


if __name__ == "__main__":
    unittest.main()
