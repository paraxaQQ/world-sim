from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from collections.abc import Mapping, Sequence
from copy import deepcopy
from decimal import Decimal
from pathlib import Path

from world_sim.cli import _reserve_live_output, _write_reserved_live_output
from world_sim.model_host import (
    ChatTransport,
    EndpointSpec,
    TransportResponse,
    load_opencode_go_api_key,
    run_live_survival,
)
from world_sim.survival.calibration import LEAN_CAMP_V1, survival_preset
from world_sim.survival.engine import (
    make_survival_world,
    replay_survival,
    survival_view_for,
)
from world_sim.survival.models import SurvivalResult
from world_sim.survival.prompt import response_schema


TEST_MODEL_NAMES = ("alpha", "beta", "gamma", "delta")
FREE_MODELS = tuple(f"opencode/{name}-free" for name in TEST_MODEL_NAMES)
GO_MODELS = tuple(f"opencode-go/{name}" for name in TEST_MODEL_NAMES)
PAID_MODELS = tuple(
    f"opencode-paid/{name}"
    for name in ("deepseek-v4-flash", "minimax-m3", "kimi-k2.6", "glm-5.2")
)
REST_REPLY = '{"action":{"kind":"rest"},"say":null}'


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


class FakeTransport(ChatTransport):
    def __init__(self, responses: Sequence[TransportResponse]) -> None:
        self.responses = list(responses)
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
        if not self.responses:
            raise AssertionError("unexpected extra model request")
        return self.responses.pop(0)


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
        self.assertEqual(artifact["source"]["world_sim_version"], "0.6.0")
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
            "minimax-m3",
            "kimi-k2.6",
            "glm-5.2",
        )
        transport = FakeTransport(
            [
                response(
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
            max_paid_usd="0.05",
            transport=transport,
            auth_path=Path("missing.json"),
            environ={"OPENCODE_ZEN_API_KEY": f" {secret} "},
        )

        self.assertEqual(artifact["status"], "completed")
        self.assertEqual(artifact["format_version"], 3)
        self.assertEqual(len(transport.requests), 4)
        self.assertEqual(artifact["config"]["max_paid_usd"], "0.05")
        self.assertLessEqual(
            Decimal(artifact["paid_preflight"]["first_chance_cost_bound_usd"]),
            Decimal("0.05"),
        )
        self.assertEqual(
            artifact["provider_summary"]["provider_reported_cost_usd"], "0.01"
        )
        self.assertTrue(artifact["provider_summary"]["cost_reporting_complete"])
        self.assertEqual(
            artifact["provider_summary"]["uncached_calculated_cost_usd"],
            "0.0004766",
        )
        self.assertEqual(
            [request["api_key"] for request in transport.requests],
            [secret, secret, secret, secret],
        )
        for request, model in zip(transport.requests, models, strict=True):
            endpoint = request["endpoint"]
            self.assertIsInstance(endpoint, EndpointSpec)
            self.assertEqual(endpoint.url, "https://opencode.ai/zen/v1/chat/completions")
            self.assertEqual(request["body"]["model"], model)
        deepseek, minimax, kimi, glm = (
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
            set(minimax),
            {
                "model",
                "messages",
                "max_completion_tokens",
                "temperature",
                "stream",
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
        self.assertEqual(glm["reasoning_effort"], "low")
        self.assertNotIn(secret, json.dumps(artifact))

    def test_paid_complete_cycle_authorizes_each_request_and_replays(self) -> None:
        transport = FakeTransport(
            [
                response(
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
            reasoning_effort="compatibility-first",
            max_paid_usd="0.18",
            transport=transport,
            environ={"OPENCODE_ZEN_API_KEY": "secret"},
        )

        self.assertEqual(artifact["status"], "completed")
        self.assertEqual(artifact["source"]["world_sim_version"], "0.6.0")
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
                        reasoning_effort="compatibility-first",
                        max_paid_usd="0.18",
                        transport=transport,
                        environ={},
                    )
                self.assertEqual(transport.requests, [])

    def test_paid_cumulative_guard_blocks_the_next_request(self) -> None:
        transport = FakeTransport(
            [
                response(
                    '{"action":{"kind":"forage"},"say":null}',
                    cost=cost,
                )
                for cost in (
                    "0.001",
                    "0.003",
                    "0.012",
                    "0.016",
                    "0.001",
                    "0.003",
                )
            ]
        )

        artifact = run_live_survival(
            model_refs=PAID_MODELS,
            days=1,
            max_calls=16,
            require_complete_budget=True,
            max_completion_tokens=1_024,
            reasoning_effort="compatibility-first",
            max_paid_usd="0.04",
            transport=transport,
            environ={"OPENCODE_ZEN_API_KEY": "secret"},
        )

        self.assertEqual(artifact["status"], "failed")
        self.assertEqual(artifact["failure"]["kind"], "paid_budget_exhausted")
        self.assertIsNone(artifact["failure"]["call_sequence"])
        self.assertEqual(len(transport.requests), 6)
        self.assertEqual(len(artifact["calls"]), 6)
        authorization = artifact["failure"]["cost_authorization"]
        self.assertEqual(authorization["prior_accounted_cost_usd"], "0.036")
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
            [response(choice, cost="0.001") for _ in range(16)]
        )

        artifact = run_live_survival(
            model_refs=PAID_MODELS,
            days=1,
            max_calls=16,
            require_complete_budget=True,
            max_completion_tokens=1_024,
            reasoning_effort="compatibility-first",
            max_paid_usd="0.18",
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
            reasoning_effort="compatibility-first",
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
            reasoning_effort="compatibility-first",
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
            "minimax-m3",
            "kimi-k2.6",
            "glm-5.2",
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
        deepseek, minimax, kimi, glm = (
            request["body"] for request in transport.requests
        )
        self.assertEqual(deepseek["thinking"], {"type": "disabled"})
        self.assertNotIn("reasoning_effort", deepseek)
        self.assertEqual(
            set(minimax),
            {"model", "messages", "max_completion_tokens", "stream"},
        )
        self.assertEqual(kimi["thinking"], {"type": "disabled"})
        self.assertNotIn("temperature", kimi)
        self.assertEqual(glm["thinking"], {"type": "disabled"})
        self.assertEqual(glm["response_format"], {"type": "json_object"})
        self.assertNotIn("reasoning_effort", glm)

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
            self.assertEqual(
                rendered_schema["properties"]["action"]["oneOf"][2],
                {
                    "type": "object",
                    "properties": {"kind": {"const": "gather_wood"}},
                    "required": ["kind"],
                    "additionalProperties": False,
                },
            )
            self.assertNotIn(model, json.dumps(messages))

    def test_paid_provider_default_omits_reasoning_controls(self) -> None:
        models = (
            "deepseek-v4-flash",
            "minimax-m3",
            "kimi-k2.6",
            "glm-5.2",
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
        run_live_survival(
            model_refs=tuple(f"opencode-paid/{model}" for model in models),
            days=1,
            max_calls=4,
            max_completion_tokens=1_024,
            reasoning_effort="provider-default",
            max_paid_usd="0.05",
            transport=transport,
            environ={"OPENCODE_ZEN_API_KEY": "secret"},
        )

        for request in transport.requests:
            body = request["body"]
            self.assertNotIn("reasoning_effort", body)
            self.assertNotIn("thinking", body)

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
            ({"max_paid_usd": "0.181"}, "cannot exceed 0.18 USD"),
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
                    max_paid_usd="0.05",
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
            max_paid_usd="0.05",
            transport=FakeTransport(
                [
                    TransportResponse(200, {}, exact_body),
                    *[response(REST_REPLY, cost="0") for _ in range(3)],
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
            max_paid_usd="0.05",
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
            max_paid_usd="0.05",
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
                artifact = run_live_survival(
                    model_refs=FREE_MODELS,
                    days=1,
                    max_calls=4,
                    transport=transport,
                    environ={},
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


if __name__ == "__main__":
    unittest.main()
