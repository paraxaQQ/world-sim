from __future__ import annotations

import hashlib
import importlib.util
import json
import re
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
    run_live_postmortem,
    run_live_survival,
    run_live_survival_continuation,
)


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
VERIFIER_PATH = REPOSITORY_ROOT / "tools" / "verify_postmortem_artifact.py"
SPEC = importlib.util.spec_from_file_location(
    "verify_postmortem_artifact",
    VERIFIER_PATH,
)
assert SPEC is not None and SPEC.loader is not None
VERIFIER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(VERIFIER)

FREE_MODELS = tuple(
    f"opencode/{name}-free" for name in ("alpha", "beta", "gamma", "delta")
)
PAID_MODELS = tuple(
    f"opencode-paid/{name}"
    for name in ("deepseek-v4-flash", "grok-4.5", "kimi-k2.6", "glm-5.2")
)
REST_REPLY = '{"action":{"kind":"rest"},"say":null}'


def response(content: str) -> TransportResponse:
    return TransportResponse(
        status=200,
        headers={"x-request-id": "test-request"},
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
                },
            }
        ),
    )


def paid_response(content: str, *, cost: str = "0.0001") -> TransportResponse:
    plain = response(content)
    payload = json.loads(plain.body)
    payload["cost"] = cost
    return TransportResponse(
        status=plain.status,
        headers=plain.headers,
        body=json.dumps(payload),
    )


def paid_responses_response(
    content: str, *, cost: str = "0.0001"
) -> TransportResponse:
    return TransportResponse(
        status=200,
        headers={"x-request-id": "test-request"},
        body=json.dumps(
            {
                "object": "response",
                "model": "fake-model",
                "status": "completed",
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
                "usage": {
                    "input_tokens": 100,
                    "output_tokens": 20,
                    "total_tokens": 120,
                },
                "incomplete_details": None,
                "cost": cost,
            }
        ),
    )


def paid_panel_response(index: int, content: str) -> TransportResponse:
    if index % len(PAID_MODELS) == 1:
        return paid_responses_response(content)
    return paid_response(content)


class FakeTransport(ChatTransport):
    def __init__(self, responses: Sequence[TransportResponse]) -> None:
        self.responses = list(responses)

    def post(
        self,
        endpoint: EndpointSpec,
        request_body: Mapping[str, object],
        *,
        api_key: str | None,
        timeout_seconds: float,
    ) -> TransportResponse:
        del endpoint, api_key, timeout_seconds
        selected = self.responses.pop(0)
        model_text = json.dumps(str(request_body["model"]))
        body = re.sub(
            r'("model"\s*:\s*)"(?:\\.|[^"\\])*"',
            lambda match: match.group(1) + model_text,
            selected.body,
            count=1,
        )
        return TransportResponse(selected.status, selected.headers, body)


class PostmortemVerifierTests(unittest.TestCase):
    def test_verifier_binds_world_deaths_requests_and_reflections(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            world_path, postmortem_path, postmortem = _write_fixture(
                Path(directory)
            )
            receipt = VERIFIER.verify_postmortem_artifact(
                postmortem_path,
                world_artifact_path=world_path,
                expected_artifact_sha256=_sha256(postmortem_path),
            )

        self.assertEqual(receipt["status"], "verified")
        self.assertEqual(receipt["death_targets"], 4)
        self.assertEqual(receipt["calls_succeeded"], 4)
        self.assertTrue(receipt["world_replay_verified"])
        self.assertTrue(receipt["postmortem_is_causally_separate"])
        self.assertEqual(postmortem["summary"]["calls_succeeded"], 4)

    def test_verifier_rejects_reflection_request_and_world_field_tampering(self) -> None:
        mutations = (
            lambda artifact: artifact["calls"][0].__setitem__(
                "reflection", "changed after the call"
            ),
            lambda artifact: artifact["calls"][0]["request"]["messages"][1].__setitem__(
                "content", "different terminal notice"
            ),
            lambda artifact: artifact.__setitem__("events", []),
        )
        patterns = (
            "does not match raw reply",
            "request does not reconstruct",
            "unexpected fields",
        )
        for mutate, pattern in zip(mutations, patterns, strict=True):
            with self.subTest(pattern=pattern), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                world_path, _, artifact = _write_fixture(root)
                mutate(artifact)
                tampered_path = root / "tampered.json"
                _write_json(tampered_path, artifact)
                with self.assertRaisesRegex(ValueError, pattern):
                    VERIFIER.verify_postmortem_artifact(
                        tampered_path,
                        world_artifact_path=world_path,
                    )

    def test_verifier_rejects_a_fabricated_failure_kind_without_a_response(
        self,
    ) -> None:
        failed = TransportResponse(
            status=503,
            headers={"x-request-id": "failed-postmortem"},
            body='{"error":"temporary"}',
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            world_path, original_path, artifact = _write_fixture(
                root,
                postmortem_responses=(
                    failed,
                    response('{"reflection":"two"}'),
                    response('{"reflection":"three"}'),
                    response('{"reflection":"four"}'),
                ),
            )
            original = VERIFIER.verify_postmortem_artifact(
                original_path,
                world_artifact_path=world_path,
            )
            self.assertEqual(original["calls_failed"], 1)

            failed_call = artifact["calls"][0]
            failed_call["response"] = None
            failed_call["error"] = {
                "kind": "response_validation_error",
                "message": "fabricated validation failure",
                "http_status": 200,
            }
            tampered_path = root / "fabricated-failure.json"
            _write_json(tampered_path, artifact)

            with self.assertRaisesRegex(
                ValueError,
                "failure kind is incompatible with a missing response",
            ):
                VERIFIER.verify_postmortem_artifact(
                    tampered_path,
                    world_artifact_path=world_path,
                )

    def test_verifier_rejects_validation_failure_from_the_wrong_model(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            world_path, _, artifact = _write_fixture(root)
            call = artifact["calls"][0]
            call["status"] = "failed"
            call["response"]["provider_model"] = "wrong-model"
            call["response"]["model_reply"] = '{"wrong":"shape"}'
            call.pop("reflection")
            call.pop("validation")
            call["error"] = {
                "kind": "response_validation_error",
                "message": "postmortem response must contain only reflection",
                "http_status": 200,
            }
            artifact["summary"] = VERIFIER._postmortem_summary(
                artifact["calls"],
                4,
                None,
            )
            tampered_path = root / "wrong-model-validation.json"
            _write_json(tampered_path, artifact)

            with self.assertRaisesRegex(ValueError, "wrong provider model"):
                VERIFIER.verify_postmortem_artifact(
                    tampered_path,
                    world_artifact_path=world_path,
                )

    def test_verifier_enforces_paid_terminal_accounting_states(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ancestor_path, world_path, original_path, original = (
                _write_paid_fixture(root)
            )
            verified = VERIFIER.verify_postmortem_artifact(
                original_path,
                world_artifact_path=world_path,
                ancestor_paths=(ancestor_path,),
            )
            self.assertEqual(verified["calls_succeeded"], 4)

            reserved_success = deepcopy(original)
            reserved_call = reserved_success["calls"][-1]
            authorization = reserved_call["cost_authorization"]
            prior = Decimal(authorization["prior_accounted_cost_usd"])
            bound = Decimal(authorization["request_cost_bound_usd"])
            authorization["accounted_cost_usd"] = _decimal_text(bound)
            authorization["cumulative_accounted_cost_usd"] = _decimal_text(
                prior + bound
            )
            authorization["accounting_basis"] = "authorized_bound_after_failure"
            reserved_success["summary"]["paid_cost_accounted_usd"] = (
                authorization["cumulative_accounted_cost_usd"]
            )
            reserved_path = root / "reserved-success.json"
            _write_json(reserved_path, reserved_success)
            with self.assertRaisesRegex(ValueError, "cost state is invalid"):
                VERIFIER.verify_postmortem_artifact(
                    reserved_path,
                    world_artifact_path=world_path,
                    ancestor_paths=(ancestor_path,),
                )

            early_skip = deepcopy(original)
            skipped_call = early_skip["calls"][-1]
            skip_authorization = skipped_call["cost_authorization"]
            for key in (
                "accounted_cost_usd",
                "cumulative_accounted_cost_usd",
                "accounting_basis",
            ):
                skip_authorization.pop(key)
            skipped_call["status"] = "skipped"
            skipped_call["response"] = None
            skipped_call.pop("reflection")
            skipped_call.pop("validation")
            skipped_call["error"] = {
                "kind": "paid_budget_exhausted",
                "message": "postmortem paid authorization exhausted before request",
                "http_status": None,
            }
            early_skip["summary"] = {
                "death_targets": 4,
                "calls_attempted": 3,
                "calls_succeeded": 3,
                "calls_failed": 0,
                "calls_skipped": 1,
                "reflection_characters": sum(
                    len(str(call["reflection"]))
                    for call in early_skip["calls"][:3]
                ),
                "paid_cost_accounted_usd": skip_authorization[
                    "prior_accounted_cost_usd"
                ],
            }
            skipped_path = root / "fabricated-skip.json"
            _write_json(skipped_path, early_skip)
            with self.assertRaisesRegex(ValueError, "did not exceed the paid budget"):
                VERIFIER.verify_postmortem_artifact(
                    skipped_path,
                    world_artifact_path=world_path,
                    ancestor_paths=(ancestor_path,),
                )

            world_artifact, world_result, _ = (
                VERIFIER._load_verified_parent_artifact(
                    world_path,
                    expected_sha256=_sha256(world_path),
                    ancestor_paths=(ancestor_path,),
                )
            )
            _, assignment = VERIFIER._verified_targets(
                world_artifact,
                world_result,
            )[-1]
            request = original["calls"][-1]["request"]
            authorization_budget = VERIFIER._PaidBudget(
                Decimal("0.05"),
                accounted=Decimal("0.05"),
            )
            over_limit_authorization = authorization_budget.quote(
                assignment,
                request,
            )
            authorization_budget.reserve_failed_request(
                over_limit_authorization
            )
            attempted = {
                "status": "failed",
                "response": None,
                "cost_authorization": over_limit_authorization,
            }
            with self.assertRaisesRegex(ValueError, "attempted call exceeded"):
                VERIFIER._verify_postmortem_cost(
                    attempted,
                    assignment=assignment,
                    request=request,
                    budget=VERIFIER._PaidBudget(
                        Decimal("0.05"),
                        accounted=Decimal("0.05"),
                    ),
                )

            calculated_only = {
                "http_status": 503,
                "request_id": None,
                "body_bytes": 2,
                "body_sha256": "0" * 64,
                "uncached_calculated_cost_usd": "0",
            }
            with self.assertRaisesRegex(ValueError, "calculated cost only"):
                VERIFIER._verify_postmortem_error_response(
                    calculated_only,
                    assignment=assignment,
                )
            negative_cost = {
                **calculated_only,
                "provider_reported_cost_usd": "-1",
            }
            with self.assertRaisesRegex(ValueError, "non-negative decimal"):
                VERIFIER._verify_postmortem_error_response(
                    negative_cost,
                    assignment=assignment,
                )


def _write_fixture(
    root: Path,
    *,
    postmortem_responses: Sequence[TransportResponse] | None = None,
) -> tuple[Path, Path, dict[str, object]]:
    world = run_live_survival(
        model_refs=FREE_MODELS,
        seed=47,
        days=6,
        max_calls=24,
        transport=FakeTransport([response(REST_REPLY) for _ in range(24)]),
        environ={},
    )
    world_path = root / "world.json"
    _write_json(world_path, world)
    responses = postmortem_responses or (
        response('{"reflection":"one"}'),
        response('{"reflection":"two"}'),
        response('{"reflection":"three"}'),
        response('{"reflection":"four"}'),
    )
    postmortem = run_live_postmortem(
        world_artifact_path=world_path,
        expected_world_artifact_sha256=_sha256(world_path),
        transport=FakeTransport(responses),
        environ={},
    )
    postmortem_path = root / "postmortem.json"
    _write_json(postmortem_path, postmortem)
    return world_path, postmortem_path, postmortem


def _write_paid_fixture(
    root: Path,
) -> tuple[Path, Path, Path, dict[str, object]]:
    names = ("Aster", "Birch", "Cinder", "Lumen")

    def paid_reply(index: int) -> str:
        if index < 12:
            return '{"action":{"kind":"gather_wood"},"say":null}'
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

    root_world = run_live_survival(
        model_refs=PAID_MODELS,
        seed=61,
        days=1,
        max_calls=16,
        require_complete_budget=True,
        max_completion_tokens=1_024,
        reasoning_effort="low",
        max_paid_usd="0.62",
        transport=FakeTransport(
            [paid_panel_response(index, paid_reply(index)) for index in range(16)]
        ),
        environ={"OPENCODE_ZEN_API_KEY": "test-only-key"},
    )
    if root_world["status"] != "completed":
        raise RuntimeError(f"paid root fixture failed: {root_world.get('failure')}")
    ancestor_path = root / "paid-root.json"
    _write_json(ancestor_path, root_world)
    world = run_live_survival_continuation(
        parent_path=ancestor_path,
        expected_parent_sha256=_sha256(ancestor_path),
        transition_reason="postmortem_verifier_paid_death_fixture",
        shared_stock=0,
        max_calls=16,
        require_complete_budget=True,
        max_completion_tokens=1_024,
        reasoning_effort="low",
        max_paid_usd="0.62",
        transport=FakeTransport(
            [paid_panel_response(index, paid_reply(index)) for index in range(16)]
        ),
        environ={"OPENCODE_ZEN_API_KEY": "test-only-key"},
    )
    world_path = root / "paid-world.json"
    _write_json(world_path, world)
    postmortem = run_live_postmortem(
        world_artifact_path=world_path,
        expected_world_artifact_sha256=_sha256(world_path),
        ancestor_paths=(ancestor_path,),
        max_completion_tokens=256,
        reasoning_effort="low",
        max_paid_usd="0.05",
        transport=FakeTransport(
            [
                paid_panel_response(0, '{"reflection":"one"}'),
                paid_panel_response(1, '{"reflection":"two"}'),
                paid_panel_response(2, '{"reflection":"three"}'),
                paid_panel_response(3, '{"reflection":"four"}'),
            ]
        ),
        environ={"OPENCODE_ZEN_API_KEY": "test-only-key"},
    )
    postmortem_path = root / "paid-postmortem.json"
    _write_json(postmortem_path, postmortem)
    return ancestor_path, world_path, postmortem_path, postmortem


def _write_json(path: Path, payload: Mapping[str, object]) -> None:
    path.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _decimal_text(value: Decimal) -> str:
    text = format(value.normalize(), "f")
    return "0" if text == "-0" else text


if __name__ == "__main__":
    unittest.main()
