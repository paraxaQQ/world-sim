from __future__ import annotations

import json
import tempfile
import unittest
from collections.abc import Mapping, Sequence
from contextlib import redirect_stdout
from copy import deepcopy
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from world_sim.cli import _replace_reserved_live_output, _reserve_live_output, main
from world_sim.model_host import (
    ChatTransport,
    EndpointSpec,
    PAID_QUALIFICATION_MODELS,
    TransportResponse,
    run_paid_adapter_qualification,
)


QUALIFICATION_REPLY = '{"protocol":"world-sim-adapter-v1","ok":true}'


def _chat_response(
    model: str,
    content: str = QUALIFICATION_REPLY,
    *,
    cost: str = "0.001",
) -> TransportResponse:
    return TransportResponse(
        status=200,
        headers={"x-request-id": f"request-{model}"},
        body=json.dumps(
            {
                "model": model,
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
                },
                "cost": cost,
            }
        ),
    )


def _responses_response(
    model: str,
    content: str = QUALIFICATION_REPLY,
    *,
    cost_ticks: int = 10_000_000,
) -> TransportResponse:
    return TransportResponse(
        status=200,
        headers={"x-request-id": f"request-{model}"},
        body=json.dumps(
            {
                "object": "response",
                "model": model,
                "status": "completed",
                "error": None,
                "incomplete_details": None,
                "output": [
                    {"type": "reasoning", "status": "completed"},
                    {
                        "type": "message",
                        "role": "assistant",
                        "status": "completed",
                        "content": [{"type": "output_text", "text": content}],
                    },
                ],
                "usage": {
                    "input_tokens": 100,
                    "output_tokens": 20,
                    "total_tokens": 120,
                    "cost_in_usd_ticks": cost_ticks,
                },
            }
        ),
    )


def _passing_panel() -> list[TransportResponse]:
    return [
        _chat_response("deepseek-v4-flash"),
        _responses_response("grok-4.6"),
        _chat_response("kimi-k2.6"),
        _chat_response("glm-5.2"),
    ]


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
            raise AssertionError("unexpected qualification retry")
        return self.responses.pop(0)


class PaidQualificationTests(unittest.TestCase):
    def test_cli_closes_the_reservation_before_atomic_checkpoints(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "qualification.json"
            final = {
                "mode": "paid_adapter_qualification",
                "status": "passed",
                "qualification_id": "paid-panel-qualification-001",
                "calls": [],
                "summary": {"models_passed": 4},
            }

            def fake_run(**kwargs: object) -> dict[str, object]:
                checkpoint = kwargs["checkpoint"]
                checkpoint({**final, "status": "running"})
                checkpoint(final)
                return final

            arguments = ["qualify-live"]
            for model in PAID_QUALIFICATION_MODELS:
                arguments.extend(("--model", model))
            arguments.extend(("--max-paid-usd", "0.30", "--output", str(output)))
            with patch("world_sim.cli.run_paid_adapter_qualification", fake_run):
                with redirect_stdout(StringIO()):
                    exit_code = main(arguments)

            self.assertEqual(exit_code, 0)
            self.assertEqual(json.loads(output.read_text(encoding="utf-8")), final)

    def test_reserved_artifact_checkpoints_replace_the_file_atomically(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "qualification.json"
            handle = _reserve_live_output(output)
            handle.close()

            _replace_reserved_live_output(output, {"status": "running", "calls": []})
            self.assertEqual(
                json.loads(output.read_text(encoding="utf-8")),
                {"status": "running", "calls": []},
            )
            _replace_reserved_live_output(output, {"status": "passed", "calls": [1]})
            self.assertEqual(
                json.loads(output.read_text(encoding="utf-8")),
                {"status": "passed", "calls": [1]},
            )
            self.assertEqual(list(output.parent.glob(".*.tmp")), [])

    def test_exact_panel_passes_without_receiving_world_context(self) -> None:
        secret = "qualification-key-not-for-artifacts"
        transport = FakeTransport(_passing_panel())
        checkpoints: list[dict[str, object]] = []

        artifact = run_paid_adapter_qualification(
            model_refs=PAID_QUALIFICATION_MODELS,
            max_completion_tokens=10_000,
            temperature=0.2,
            max_paid_usd="0.30",
            timeout_seconds=300,
            transport=transport,
            environ={"OPENCODE_ZEN_API_KEY": secret},
            checkpoint=lambda current: checkpoints.append(deepcopy(dict(current))),
        )

        self.assertEqual(artifact["status"], "passed")
        self.assertEqual(artifact["summary"]["models_passed"], 4)
        self.assertEqual(artifact["summary"]["models_failed"], 0)
        self.assertEqual(
            artifact["paid_preflight"]["panel_envelope_cost_bound_usd"],
            "0.29575",
        )
        self.assertEqual(artifact["summary"]["provider_reported_cost_usd"], "0.004")
        self.assertEqual(len(transport.requests), 4)
        self.assertEqual(checkpoints[-1]["status"], "passed")
        self.assertTrue(
            any(
                call.get("status") == "in_flight"
                for checkpoint in checkpoints
                for call in checkpoint["calls"]
            )
        )
        self.assertNotIn(secret, json.dumps(artifact))
        request_text = json.dumps(
            [request["body"] for request in transport.requests]
        ).casefold()
        for forbidden in (
            "aster",
            "birch",
            "cinder",
            "lumen",
            "energy",
            "survival",
            "forage",
            "give_food",
        ):
            self.assertNotIn(forbidden, request_text)
        grok = transport.requests[1]
        self.assertEqual(
            grok["endpoint"].url, "https://opencode.ai/zen/v1/responses"
        )
        self.assertEqual(grok["body"]["max_output_tokens"], 10_000)
        self.assertEqual(
            grok["body"]["text"]["format"]["schema"],
            {
                "type": "object",
                "properties": {
                    "protocol": {"const": "world-sim-adapter-v1"},
                    "ok": {"const": True},
                },
                "required": ["protocol", "ok"],
                "additionalProperties": False,
            },
        )

    def test_schema_failure_does_not_hide_later_models_or_trigger_a_retry(self) -> None:
        responses = _passing_panel()
        responses[0] = _chat_response(
            "deepseek-v4-flash",
            '{"protocol":"world-sim-adapter-v1","ok":false}',
        )
        transport = FakeTransport(responses)

        artifact = run_paid_adapter_qualification(
            model_refs=PAID_QUALIFICATION_MODELS,
            max_paid_usd="0.30",
            timeout_seconds=300,
            transport=transport,
            environ={"OPENCODE_ZEN_API_KEY": "secret"},
        )

        self.assertEqual(artifact["status"], "failed")
        self.assertEqual(artifact["summary"]["models_attempted"], 4)
        self.assertEqual(artifact["summary"]["models_passed"], 3)
        self.assertEqual(len(transport.requests), 4)
        self.assertFalse(artifact["calls"][0]["validation"]["exact_schema"])
        self.assertEqual(
            [call["status"] for call in artifact["calls"]],
            ["failed", "passed", "passed", "passed"],
        )

    def test_grok_cost_ticks_must_agree_with_any_dollar_cost(self) -> None:
        responses = _passing_panel()
        grok_payload = json.loads(responses[1].body)
        grok_payload["cost"] = "0.002"
        responses[1] = TransportResponse(200, {}, json.dumps(grok_payload))
        transport = FakeTransport(responses)

        artifact = run_paid_adapter_qualification(
            model_refs=PAID_QUALIFICATION_MODELS,
            max_paid_usd="0.30",
            timeout_seconds=300,
            transport=transport,
            environ={"OPENCODE_ZEN_API_KEY": "secret"},
        )

        self.assertEqual(artifact["status"], "failed")
        self.assertEqual(len(transport.requests), 4)
        self.assertEqual(
            artifact["calls"][1]["error"]["kind"], "provider_cost_error"
        )

    def test_http_failure_reserves_its_bound_and_continues_once_per_model(self) -> None:
        responses = _passing_panel()
        responses[0] = TransportResponse(400, {"x-request-id": "bad-request"}, "{}")
        transport = FakeTransport(responses)

        artifact = run_paid_adapter_qualification(
            model_refs=PAID_QUALIFICATION_MODELS,
            max_paid_usd="0.30",
            timeout_seconds=300,
            transport=transport,
            environ={"OPENCODE_ZEN_API_KEY": "secret"},
        )

        self.assertEqual(artifact["status"], "failed")
        self.assertEqual(len(transport.requests), 4)
        failed = artifact["calls"][0]
        self.assertEqual(failed["error"]["kind"], "http_error")
        self.assertEqual(
            failed["cost_authorization"]["accounting_basis"],
            "authorized_bound_after_failure",
        )
        self.assertFalse(artifact["summary"]["cost_reporting_complete"])
        self.assertGreater(
            float(artifact["summary"]["accounted_exposure_usd"]), 0.003
        )

    def test_cost_bound_breach_skips_later_transport_and_retains_a_result(self) -> None:
        transport = FakeTransport(
            [
                _chat_response("deepseek-v4-flash", cost="0.299"),
                *_passing_panel()[1:],
            ]
        )

        artifact = run_paid_adapter_qualification(
            model_refs=PAID_QUALIFICATION_MODELS,
            max_paid_usd="0.30",
            timeout_seconds=300,
            transport=transport,
            environ={"OPENCODE_ZEN_API_KEY": "secret"},
        )

        self.assertEqual(artifact["status"], "failed")
        self.assertEqual(len(transport.requests), 1)
        self.assertEqual(artifact["summary"]["models_recorded"], 4)
        self.assertEqual(artifact["summary"]["models_attempted"], 1)
        self.assertEqual(artifact["summary"]["models_skipped"], 3)
        self.assertEqual(
            [call["error"]["kind"] for call in artifact["calls"][1:]],
            ["paid_budget_exhausted"] * 3,
        )


if __name__ == "__main__":
    unittest.main()
