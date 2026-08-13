from __future__ import annotations

import json
import tempfile
import unittest
from collections.abc import Mapping, Sequence
from copy import deepcopy
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


def response(content: str, *, request_id: str = "request-1") -> TransportResponse:
    return TransportResponse(
        status=200,
        headers={"x-request-id": request_id},
        body=json.dumps(
            {
                "model": "fake-model",
                "choices": [
                    {
                        "message": {"content": content},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": 100,
                    "completion_tokens": 20,
                    "total_tokens": 120,
                    "completion_tokens_details": {"reasoning_tokens": 15},
                },
            }
        ),
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
