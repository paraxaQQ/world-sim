from __future__ import annotations

import json
import tempfile
import unittest
from collections.abc import Mapping, Sequence
from copy import deepcopy
from decimal import Decimal
from pathlib import Path

from world_sim.model_host import (
    ChatTransport,
    EndpointSpec,
    TransportResponse,
    load_opencode_go_api_key,
    run_live_survival,
)
from world_sim.survival.engine import replay_survival
from world_sim.survival.models import SurvivalResult


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
    def test_request_is_tool_free_and_credentials_do_not_leak(self) -> None:
        secret = "not-for-the-artifact"
        transport = FakeTransport(
            [
                response('{"action":{"kind":"rest"},"say":null}'),
                response('{"action":{"kind":"rest"},"say":null}'),
            ]
        )
        artifact = run_live_survival(
            model_refs=("opencode-go/alpha", "opencode-go/beta"),
            days=1,
            max_calls=2,
            transport=transport,
            environ={"OPENCODE_API_KEY": secret},
        )

        self.assertEqual(artifact["status"], "completed")
        for request, model in zip(transport.requests, ("alpha", "beta"), strict=True):
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

    def test_two_models_complete_three_days_and_replay_without_calls(self) -> None:
        raw = '{"action":{"kind":"forage"},"say":null}'
        transport = FakeTransport(
            [response(raw, request_id=f"request-{index}") for index in range(6)]
        )
        artifact = run_live_survival(
            model_refs=("opencode/alpha-free", "opencode/beta-free"),
            seed=29,
            days=3,
            max_calls=6,
            transport=transport,
            environ={},
        )

        self.assertEqual(artifact["status"], "completed")
        self.assertEqual(len(transport.requests), 6)
        self.assertEqual(
            [(call["day"], call["public_name"]) for call in artifact["calls"]],
            [(1, "Aster"), (1, "Birch"), (2, "Aster"), (2, "Birch"), (3, "Aster"), (3, "Birch")],
        )
        self.assertEqual(
            artifact["provider_summary"]["provider_reported_usage"]["reasoning_tokens"],
            90,
        )
        original = result_from(artifact)
        self.assertEqual(original.to_dict(), replay_survival(original).to_dict())
        self.assertEqual(len(transport.requests), 6)

    def test_free_pool_key_is_optional_and_never_enters_artifact(self) -> None:
        secret = "zen-key-not-for-the-artifact"
        transport = FakeTransport(
            [
                response('{"action":{"kind":"rest"},"say":null}'),
                response('{"action":{"kind":"rest"},"say":null}'),
            ]
        )
        artifact = run_live_survival(
            model_refs=("opencode/alpha-free", "opencode/beta-free"),
            days=1,
            max_calls=2,
            transport=transport,
            environ={"OPENCODE_ZEN_API_KEY": f" {secret} "},
        )

        self.assertEqual(artifact["status"], "completed")
        self.assertEqual(
            [request["api_key"] for request in transport.requests],
            [secret, secret],
        )
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
        self.assertEqual(len(transport.requests), 4)
        self.assertEqual(artifact["config"]["max_paid_usd"], "0.05")
        self.assertLessEqual(
            Decimal(artifact["paid_preflight"]["total_cost_bound_usd"]),
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
            {"model", "messages", "max_completion_tokens", "thinking", "stream"},
        )
        self.assertEqual(minimax["thinking"], {"type": "disabled"})
        self.assertEqual(kimi["thinking"], {"type": "disabled"})
        self.assertNotIn("temperature", kimi)
        self.assertEqual(glm["thinking"], {"type": "disabled"})
        self.assertNotIn("reasoning_effort", glm)

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
                model_refs=("opencode/alpha-free", "opencode/beta-free"),
                days=1,
                max_calls=2,
                reasoning_effort="compatibility-first",
                transport=transport,
                environ={},
            )
        self.assertEqual(transport.requests, [])

    def test_paid_multiday_run_is_rejected_before_transport(self) -> None:
        transport = FakeTransport([])
        with self.assertRaisesRegex(ValueError, "require exactly one day"):
            run_live_survival(
                model_refs=(
                    "opencode-paid/deepseek-v4-flash",
                    "opencode-paid/minimax-m3",
                ),
                days=2,
                max_calls=4,
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
            ({"max_paid_usd": "0.051"}, "cannot exceed 0.05 USD"),
            (
                {"max_paid_usd": "0.05", "model_refs": ("opencode-paid/unknown",) * 2},
                "not in the pinned price allowlist",
            ),
            (
                {"max_paid_usd": "0.05", "days": 3, "max_calls": 6},
                "require exactly one day",
            ),
            (
                {"max_paid_usd": "0.000001"},
                "conservative paid bound",
            ),
        )
        defaults: dict[str, object] = {
            "model_refs": (
                "opencode-paid/deepseek-v4-flash",
                "opencode-paid/minimax-m3",
            ),
            "days": 1,
            "max_calls": 2,
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
                    model_refs=(
                        "opencode-paid/deepseek-v4-flash",
                        "opencode-paid/minimax-m3",
                    ),
                    days=1,
                    max_calls=2,
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
            model_refs=(
                "opencode-paid/deepseek-v4-flash",
                "opencode-paid/minimax-m3",
            ),
            days=1,
            max_calls=2,
            max_completion_tokens=1_024,
            max_paid_usd="0.05",
            transport=FakeTransport(
                [
                    TransportResponse(200, {}, exact_body),
                    response(
                        '{"action":{"kind":"rest"},"say":null}',
                        cost="0",
                    ),
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
            model_refs=(
                "opencode-paid/deepseek-v4-flash",
                "opencode-paid/minimax-m3",
            ),
            days=1,
            max_calls=2,
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
            model_refs=(
                "opencode-paid/deepseek-v4-flash",
                "opencode-paid/minimax-m3",
            ),
            days=1,
            max_calls=2,
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
            [
                response('{"action":{"kind":"rest"},"say":null}'),
                response('{"action":{"kind":"rest"},"say":null}'),
            ]
        )
        artifact = run_live_survival(
            model_refs=("opencode/alpha-free", "opencode/beta-free"),
            days=1,
            max_calls=2,
            reasoning_effort="low",
            transport=transport,
            environ={},
        )

        self.assertEqual(artifact["config"]["reasoning_effort"], "low")
        self.assertEqual(
            [request["body"]["reasoning_effort"] for request in transport.requests],
            ["low", "low"],
        )

    def test_unknown_reasoning_effort_fails_before_transport(self) -> None:
        transport = FakeTransport([])
        with self.assertRaisesRegex(ValueError, "reasoning_effort must be one of"):
            run_live_survival(
                model_refs=("opencode/alpha-free", "opencode/beta-free"),
                days=1,
                max_calls=2,
                reasoning_effort="invented",
                transport=transport,
                environ={},
            )
        self.assertEqual(transport.requests, [])

    def test_bad_model_json_is_one_paid_rest_without_retry(self) -> None:
        transport = FakeTransport(
            [
                response("not json"),
                response('{"action":{"kind":"rest"},"say":null}'),
            ]
        )
        artifact = run_live_survival(
            model_refs=("opencode/alpha-free", "opencode/beta-free"),
            days=1,
            max_calls=2,
            transport=transport,
            environ={},
        )

        malformed = artifact["calls"][0]
        self.assertEqual(len(transport.requests), 2)
        self.assertEqual(malformed["response"]["model_reply"], "not json")
        self.assertIn("not valid strict JSON", malformed["validation"]["action_error"])
        self.assertEqual(malformed["parsed_choice"], {"action": {"kind": "rest"}, "say": None})
        aster = artifact["result"]["final_state"]["survivors"][0]
        self.assertEqual(aster["energy"], 13)

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
                    model_refs=("opencode/alpha-free", "opencode/beta-free"),
                    days=1,
                    max_calls=2,
                    transport=transport,
                    environ={},
                )
                self.assertEqual(artifact["status"], "failed")
                self.assertEqual(artifact["failure"]["kind"], expected_kind)
                self.assertEqual(artifact["partial_state"]["day"], 0)
                receipt = artifact["calls"][0]["response"]
                self.assertNotIn("raw_body", receipt)
                self.assertEqual(receipt["body_bytes"], len(provider_response.body.encode()))
                self.assertEqual(len(receipt["body_sha256"]), 64)
                self.assertEqual(len(transport.requests), 1)

    def test_call_cap_fails_before_credentials_or_transport(self) -> None:
        transport = FakeTransport([])
        with self.assertRaisesRegex(ValueError, "could require 6 model calls"):
            run_live_survival(
                model_refs=("opencode-go/alpha", "opencode-go/beta"),
                days=3,
                max_calls=5,
                transport=transport,
                auth_path=Path("missing.json"),
                environ={},
            )
        self.assertEqual(transport.requests, [])

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
            model_refs=("opencode-go/alpha", "opencode-go/beta"),
            days=1,
            max_calls=2,
            transport=FakeTransport([provider_response]),
            environ={"OPENCODE_API_KEY": secret},
        )
        self.assertNotIn(secret, json.dumps(artifact))
        self.assertNotIn("raw_body", artifact["calls"][0]["response"])

    def test_unexpected_transport_failure_keeps_a_sanitized_artifact(self) -> None:
        artifact = run_live_survival(
            model_refs=("opencode/alpha-free", "opencode/beta-free"),
            days=1,
            max_calls=2,
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
