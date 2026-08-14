from __future__ import annotations

import hashlib
import importlib.util
import json
import tempfile
import unittest
from collections.abc import Mapping, Sequence
from copy import deepcopy
from functools import lru_cache
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
VERIFIER_PATH = REPOSITORY_ROOT / "tools" / "verify_live_artifact.py"
SESSION_1_ARTIFACT = REPOSITORY_ROOT / "outputs" / "v0.8.0-paid-survival-29993.json"
SESSION_1_ARTIFACT_SHA256 = (
    "a98ec8216c08a172c4ed29fb1da65b63defd3b4a29f53e95fa26a1e187e38b90"
)
SESSION_1_CANONICAL_SHA256 = (
    "490663b4a743f51c4b0f44ccc57ba91ee2a7b6d6adafbcda072373a7748a54e7"
)
SESSION_2_ARTIFACT = (
    REPOSITORY_ROOT / "outputs" / "v0.9.0-session-002-shelter-dilemma-29993.json"
)
SESSION_2_ARTIFACT_SHA256 = (
    "fc0b07dfc404a2f485f3b6a2c2f191fec5e495153d6147d428d6cb251cab27fe"
)
SESSION_2_CANONICAL_SHA256 = (
    "ed1f299bbc698951e77256b46291ea4ee142469bc0a7cd0e7b6bf476820392ca"
)
SESSION_3_ARTIFACT = (
    REPOSITORY_ROOT
    / "outputs"
    / "v0.11.0-session-003-global-beats-shelter-dilemma-29993.json"
)
SESSION_3_ARTIFACT_SHA256 = (
    "ca283bd336fd58c1cb0e461e14e8394299cf3a06c7f44654f412ecf408756b27"
)
SPEC = importlib.util.spec_from_file_location("verify_live_artifact", VERIFIER_PATH)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError("cannot load live artifact verifier")
VERIFIER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(VERIFIER)

from world_sim.survival.demo import result_sha256  # noqa: E402
from world_sim.model_host import (  # noqa: E402
    ChatTransport,
    EndpointSpec,
    TransportResponse,
    run_live_survival_continuation,
)
from world_sim.survival.engine import (  # noqa: E402
    adjust_shared_resource,
    continue_survival_world,
    make_survival_world,
    replay_survival,
    run_survival,
)
from world_sim.survival.models import (  # noqa: E402
    GLOBAL_BEATS_V2,
    SEQUENTIAL_DIALOGUE_V3,
    SLOTS_V1,
    SurvivalConfig,
    SurvivorView,
)


class _RestSpeaker:
    def decide(self, view: SurvivorView) -> Mapping[str, object]:
        return {
            "action": {"kind": "rest"},
            "say": {
                "to": "everyone",
                "text": f"{view.name} final public note",
            },
        }


class _FailingSpeaker:
    def decide(self, view: SurvivorView) -> Mapping[str, object]:
        del view
        raise RuntimeError("synthetic provider failure")


class _HostFixtureTransport(ChatTransport):
    def __init__(
        self,
        *,
        fail_at: int | None = None,
        action_kind: str = "rest",
    ) -> None:
        self.fail_at = fail_at
        self.action_kind = action_kind
        self.call_count = 0

    def post(
        self,
        endpoint: EndpointSpec,
        request_body: Mapping[str, object],
        *,
        api_key: str | None,
        timeout_seconds: float,
    ) -> TransportResponse:
        del api_key, timeout_seconds
        self.call_count += 1
        if self.call_count == self.fail_at:
            return TransportResponse(
                status=503,
                headers={"x-request-id": f"failed-{self.call_count}"},
                body='{"error":{"message":"synthetic provider failure"}}',
            )
        content = json.dumps(
            {
                "action": {"kind": self.action_kind},
                "say": {
                    "to": "everyone",
                    "text": f"host fixture call {self.call_count}",
                },
            },
            separators=(",", ":"),
        )
        if endpoint.api_style == "responses":
            payload: dict[str, object] = {
                "object": "response",
                "model": request_body["model"],
                "status": "completed",
                "error": None,
                "output": [
                    {
                        "type": "message",
                        "role": "assistant",
                        "status": "completed",
                        "content": [{"type": "output_text", "text": content}],
                    }
                ],
                "usage": {
                    "input_tokens": 100,
                    "output_tokens": 20,
                    "total_tokens": 120,
                    "output_tokens_details": {"reasoning_tokens": 10},
                },
                "incomplete_details": None,
                "cost": "0.0001",
            }
        else:
            payload = {
                "model": request_body["model"],
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
                    "completion_tokens_details": {"reasoning_tokens": 10},
                },
                "cost": "0.0001",
            }
        return TransportResponse(
            status=200,
            headers={"x-request-id": f"request-{self.call_count}"},
            body=json.dumps(payload),
        )


def _source_receipt() -> dict[str, str]:
    return {
        key: hashlib.sha256(path.read_bytes()).hexdigest()
        for key, path in VERIFIER.SOURCE_FILES.items()
    }


def _parent_artifact(*, seed: int = 29_993) -> dict[str, object]:
    world = make_survival_world(
        ("Aster", "Birch"),
        seed=seed,
        config=SurvivalConfig(max_days=1),
        interaction_protocol=SLOTS_V1,
    )
    result = run_survival(
        world,
        {"Aster": _RestSpeaker(), "Birch": _RestSpeaker()},
        days=1,
    )
    return {
        "format_version": 3,
        "mode": "live_named_survival",
        "source": _source_receipt(),
        "status": "completed",
        "canonical_result_sha256": result_sha256(result),
        "result": result.to_dict(),
    }


def _write_artifact(path: Path, artifact: Mapping[str, object]) -> str:
    raw = json.dumps(
        artifact,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    path.write_bytes(raw)
    return hashlib.sha256(raw).hexdigest()


def _continuation_artifacts(
    directory: Path,
    *,
    interaction_protocol: str | None = None,
) -> tuple[Path, Path, dict[str, object], dict[str, object]]:
    parent = _parent_artifact()
    parent_path = directory / "renamed-parent.data"
    parent_sha256 = _write_artifact(parent_path, parent)
    parent_result = VERIFIER._survival_result(parent["result"])
    world = continue_survival_world(
        parent_result,
        interaction_protocol=interaction_protocol,
    )
    transition = adjust_shared_resource(
        world,
        resource="wood",
        stock=0,
        reason="session_002_shelter_dilemma",
    )
    boundary_state = deepcopy(world.to_dict())
    public_record = world.prior_public_record
    if public_record is None:
        raise AssertionError("continuation fixture has no prior public record")
    result = run_survival(
        world,
        {"Aster": _RestSpeaker(), "Birch": _RestSpeaker()},
        days=1,
    )
    child_config: dict[str, object] = {
        "seed": world.seed,
        "cycles_requested": 1,
        "starting_cycle": 2,
        "ending_cycle": 2,
        "world_config": world.config.to_dict(),
    }
    if interaction_protocol is not None:
        child_config["interaction_protocol"] = interaction_protocol
    child: dict[str, object] = {
        "format_version": 4,
        "mode": "live_named_survival_continuation",
        "source": _source_receipt(),
        "continuation_link": {
            "parent_artifact_name": "not-used-for-verification.json",
            "parent_artifact_sha256": parent_sha256,
            "parent_canonical_result_sha256": parent["canonical_result_sha256"],
            "parent_format_version": 3,
            "parent_mode": "live_named_survival",
        },
        "transition_receipt": {
            "method": "deterministic_between_cycle_shared_resource_adjustment",
            "event": transition.to_dict(),
        },
        "public_record_receipt": {
            "method": "final_public_broadcast_per_identity_verbatim",
            "statement_status": "unverified",
            "objective_totals_source": "verified_parent_engine_events",
            "record": public_record.to_dict(),
        },
        "config": child_config,
        "seat_assignments": [
            {
                "seat_id": "seat-001",
                "public_name": "Aster",
                "model": "test/model-a",
            },
            {
                "seat_id": "seat-002",
                "public_name": "Birch",
                "model": "test/model-b",
            },
        ],
        "calls": [],
        "status": "completed",
        "canonical_result_sha256": result_sha256(result),
        "result": result.to_dict(),
    }
    child_path = directory / "child.json"
    _write_artifact(child_path, child)
    return parent_path, child_path, child, boundary_state


def _next_continuation_artifact(
    directory: Path,
    *,
    parent_path: Path,
    name: str,
) -> tuple[Path, dict[str, object], dict[str, object]]:
    parent = json.loads(parent_path.read_text(encoding="utf-8"))
    parent_sha256 = hashlib.sha256(parent_path.read_bytes()).hexdigest()
    parent_result = VERIFIER._survival_result(parent["result"])
    world = continue_survival_world(parent_result)
    transition = adjust_shared_resource(
        world,
        resource="wood",
        stock=0,
        reason=f"synthetic_{name.replace('-', '_')}",
    )
    boundary_state = deepcopy(world.to_dict())
    starting_cycle = world.day + 1
    public_record = world.prior_public_record
    if public_record is None:
        raise AssertionError("continuation fixture has no prior public record")
    result = run_survival(
        world,
        {"Aster": _RestSpeaker(), "Birch": _RestSpeaker()},
        days=1,
    )
    artifact: dict[str, object] = {
        "format_version": 5,
        "mode": "live_named_survival_continuation",
        "source": _source_receipt(),
        "continuation_link": {
            "parent_artifact_name": parent_path.name,
            "parent_artifact_sha256": parent_sha256,
            "parent_canonical_result_sha256": parent["canonical_result_sha256"],
            "parent_format_version": parent["format_version"],
            "parent_mode": parent["mode"],
        },
        "transition_receipt": {
            "method": "deterministic_between_cycle_shared_resource_adjustment",
            "event": transition.to_dict(),
        },
        "public_record_receipt": {
            "method": "final_public_broadcast_per_identity_verbatim",
            "statement_status": "unverified",
            "objective_totals_source": "verified_parent_engine_events",
            "record": public_record.to_dict(),
        },
        "config": {
            "seed": world.seed,
            "cycles_requested": 1,
            "starting_cycle": starting_cycle,
            "ending_cycle": starting_cycle,
            "world_config": world.config.to_dict(),
        },
        "seat_assignments": [
            {
                "seat_id": "seat-001",
                "public_name": "Aster",
                "model": "test/model-a",
            },
            {
                "seat_id": "seat-002",
                "public_name": "Birch",
                "model": "test/model-b",
            },
        ],
        "calls": [],
        "status": "completed",
        "canonical_result_sha256": result_sha256(result),
        "result": result.to_dict(),
    }
    child_path = directory / f"{name}.json"
    _write_artifact(child_path, artifact)
    return child_path, artifact, boundary_state


def _host_v6_artifact(
    *,
    transport: ChatTransport,
    parent_path: Path = SESSION_2_ARTIFACT,
    expected_parent_sha256: str = SESSION_2_ARTIFACT_SHA256,
    ancestor_paths: Sequence[Path] = (SESSION_1_ARTIFACT,),
    model_replacements: Sequence[str] = (
        "Cinder=opencode-paid/gpt-5.6-luna",
    ),
    initiative_phase: int = 0,
    preserve_shared_resources: bool = False,
) -> dict[str, object]:
    return run_live_survival_continuation(
        parent_path=parent_path,
        expected_parent_sha256=expected_parent_sha256,
        ancestor_paths=ancestor_paths,
        additional_cycles=1,
        shared_resource="wood",
        shared_stock=0,
        transition_reason=(None if preserve_shared_resources else "synthetic_host_v6"),
        preserve_shared_resources=preserve_shared_resources,
        interaction_protocol=SEQUENTIAL_DIALOGUE_V3,
        initiative_phase=initiative_phase,
        model_replacements=model_replacements,
        replacement_reason=(
            "replace Kimi for the bounded sequential retest"
            if model_replacements
            else None
        ),
        max_calls=4,
        max_completion_tokens=4096,
        temperature=0.2,
        reasoning_effort="low",
        max_paid_usd="1.20",
        timeout_seconds=30,
        transport=transport,
        environ={"OPENCODE_ZEN_API_KEY": "fixture-key"},
    )


@lru_cache(maxsize=2)
def _retained_host_v6_json(fail_at: int | None) -> str:
    artifact = _host_v6_artifact(
        transport=_HostFixtureTransport(fail_at=fail_at)
    )
    return json.dumps(
        artifact,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _retained_host_v6_artifact(fail_at: int | None = None) -> dict[str, object]:
    return json.loads(_retained_host_v6_json(fail_at))


def _tamper_first_call_request(artifact: dict[str, object]) -> None:
    request = artifact["calls"][0]["request"]
    prompt_key = "input" if "input" in request else "messages"
    request[prompt_key][1]["content"] = "tampered view"


class LiveArtifactVerifierTests(unittest.TestCase):
    def test_verifier_rejects_ambiguous_json_before_schema_validation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            artifact = Path(directory) / "ambiguous.json"
            artifact.write_text(
                '{"format_version":3,"format_version":3}',
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "duplicate object key"):
                VERIFIER.verify_live_artifact(artifact)

            artifact.write_text('{"format_version":NaN}', encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "invalid JSON constant"):
                VERIFIER.verify_live_artifact(artifact)

    def test_retained_session_one_and_two_chain_remain_exact(self) -> None:
        session_one = json.loads(SESSION_1_ARTIFACT.read_text(encoding="utf-8"))
        session_two = json.loads(SESSION_2_ARTIFACT.read_text(encoding="utf-8"))
        parent_receipt = VERIFIER.verify_live_artifact(
            SESSION_1_ARTIFACT,
            expected_artifact_sha256=SESSION_1_ARTIFACT_SHA256,
        )
        child_receipt = VERIFIER.verify_live_artifact(
            SESSION_2_ARTIFACT,
            expected_artifact_sha256=SESSION_2_ARTIFACT_SHA256,
            parent_path=SESSION_1_ARTIFACT,
        )

        self.assertTrue(parent_receipt["exact_replay"])
        self.assertNotIn(
            "interaction_protocol",
            session_one["result"]["initial_state"],
        )
        self.assertNotIn("interaction_protocol", session_two["config"])
        self.assertNotIn(
            "interaction_protocol",
            session_two["result"]["initial_state"],
        )
        self.assertEqual(
            parent_receipt["canonical_result_sha256"],
            SESSION_1_CANONICAL_SHA256,
        )
        self.assertTrue(child_receipt["exact_replay"])
        self.assertTrue(child_receipt["continuation_chain_verified"])
        self.assertEqual(
            child_receipt["canonical_result_sha256"],
            SESSION_2_CANONICAL_SHA256,
        )
        self.assertEqual(
            child_receipt["parent_artifact_sha256"],
            SESSION_1_ARTIFACT_SHA256,
        )
        self.assertEqual(
            child_receipt["parent_canonical_result_sha256"],
            SESSION_1_CANONICAL_SHA256,
        )
        self.assertEqual(parent_receipt["continuation_depth"], 0)
        self.assertEqual(
            parent_receipt["root_artifact_sha256"],
            SESSION_1_ARTIFACT_SHA256,
        )
        self.assertEqual(child_receipt["continuation_depth"], 1)
        self.assertEqual(
            child_receipt["root_artifact_sha256"],
            SESSION_1_ARTIFACT_SHA256,
        )

    def test_retained_session_three_failure_chain_remains_exact(self) -> None:
        receipt = VERIFIER.verify_live_artifact(
            SESSION_3_ARTIFACT,
            expected_artifact_sha256=SESSION_3_ARTIFACT_SHA256,
            parent_path=SESSION_2_ARTIFACT,
            ancestor_paths=(SESSION_1_ARTIFACT,),
        )

        self.assertEqual(receipt["status"], "failed")
        self.assertEqual(receipt["failure_kind"], "completion_budget_exhausted")
        self.assertTrue(receipt["failure_call_receipt_consistent"])
        self.assertTrue(receipt["continuation_chain_verified"])
        self.assertEqual(receipt["continuation_depth"], 2)
        self.assertEqual(
            receipt["root_artifact_sha256"],
            SESSION_1_ARTIFACT_SHA256,
        )

    def test_completed_continuation_verifies_actual_parent_chain(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            parent, child, child_payload, _ = _continuation_artifacts(Path(directory))

            receipt = VERIFIER.verify_live_artifact(
                child,
                parent_path=parent,
            )

        self.assertEqual(receipt["status"], "completed")
        self.assertTrue(receipt["exact_replay"])
        self.assertTrue(receipt["continuation_chain_verified"])
        self.assertEqual(
            receipt["parent_artifact_sha256"],
            child_payload["continuation_link"]["parent_artifact_sha256"],
        )

    def test_recursive_v3_v4_v5_and_v5_v5_chains_verify(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            v3, v4, _, _ = _continuation_artifacts(root)
            v5, _, _ = _next_continuation_artifact(
                root,
                parent_path=v4,
                name="session-003",
            )
            v5_next, _, _ = _next_continuation_artifact(
                root,
                parent_path=v5,
                name="session-004",
            )
            root_sha256 = hashlib.sha256(v3.read_bytes()).hexdigest()

            first = VERIFIER.verify_live_artifact(
                v5,
                parent_path=v4,
                ancestor_paths=(v3,),
            )
            second = VERIFIER.verify_live_artifact(
                v5_next,
                parent_path=v5,
                ancestor_paths=(v3, v4),
            )

        self.assertTrue(first["continuation_chain_verified"])
        self.assertTrue(first["exact_replay"])
        self.assertEqual(first["continuation_depth"], 2)
        self.assertEqual(second["continuation_depth"], 3)
        self.assertEqual(
            first["root_artifact_sha256"],
            root_sha256,
        )
        self.assertEqual(
            second["root_artifact_sha256"],
            first["root_artifact_sha256"],
        )

    def test_v5_requires_complete_ordered_untampered_ancestry(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            v3, v4, _, _ = _continuation_artifacts(root)
            v5, _, _ = _next_continuation_artifact(
                root,
                parent_path=v4,
                name="session-003",
            )
            v5_next, _, _ = _next_continuation_artifact(
                root,
                parent_path=v5,
                name="session-004",
            )

            with self.assertRaisesRegex(ValueError, "complete ancestor chain"):
                VERIFIER.verify_live_artifact(v5, parent_path=v4)
            with self.assertRaisesRegex(ValueError, "artifact SHA-256"):
                VERIFIER.verify_live_artifact(
                    v5_next,
                    parent_path=v5,
                    ancestor_paths=(v4, v3),
                )

            wrong_root = root / "wrong-root.json"
            _write_artifact(wrong_root, _parent_artifact(seed=29_994))
            with self.assertRaisesRegex(ValueError, "artifact SHA-256"):
                VERIFIER.verify_live_artifact(
                    v5,
                    parent_path=v4,
                    ancestor_paths=(wrong_root,),
                )

            original_root = v3.read_bytes()
            v3.write_bytes(original_root + b" ")
            with self.assertRaisesRegex(ValueError, "artifact SHA-256"):
                VERIFIER.verify_live_artifact(
                    v5,
                    parent_path=v4,
                    ancestor_paths=(v3,),
                )

    def test_v4_rejects_extra_ancestors_and_v5_rejects_v3_parent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            v3, v4, v4_payload, _ = _continuation_artifacts(root)

            with self.assertRaisesRegex(ValueError, "does not accept --ancestor"):
                VERIFIER.verify_live_artifact(
                    v4,
                    parent_path=v3,
                    ancestor_paths=(v3,),
                )

            v4_payload["format_version"] = 5
            v4_payload["continuation_link"]["parent_format_version"] = 3
            _write_artifact(v4, v4_payload)
            with self.assertRaisesRegex(ValueError, "format_version 4 or 5"):
                VERIFIER.verify_live_artifact(v4, parent_path=v3)

    def test_cli_accepts_repeatable_ancestors_oldest_to_newest(self) -> None:
        args = VERIFIER.build_parser().parse_args(
            [
                "child.json",
                "--ancestor",
                "root.json",
                "--ancestor",
                "middle.json",
                "--parent",
                "direct.json",
            ]
        )

        self.assertEqual(args.ancestor, [Path("root.json"), Path("middle.json")])
        self.assertEqual(args.parent, Path("direct.json"))

    def test_recorded_global_beats_v2_upgrade_is_replay_bound(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            parent, child_path, child, _ = _continuation_artifacts(
                root,
                interaction_protocol=GLOBAL_BEATS_V2,
            )

            receipt = VERIFIER.verify_live_artifact(
                child_path,
                parent_path=parent,
            )
            result = VERIFIER._survival_result(child["result"])
            self.assertEqual(
                child["config"]["interaction_protocol"],
                GLOBAL_BEATS_V2,
            )
            self.assertEqual(
                result.initial_state["interaction_protocol"],
                GLOBAL_BEATS_V2,
            )
            self.assertEqual(replay_survival(result).to_dict(), result.to_dict())
            self.assertTrue(receipt["continuation_chain_verified"])

            child["config"]["interaction_protocol"] = SLOTS_V1
            child["result"]["initial_state"].pop("interaction_protocol")
            tampered_result = VERIFIER._survival_result(child["result"])
            child["canonical_result_sha256"] = result_sha256(tampered_result)
            _write_artifact(child_path, child)

            with self.assertRaisesRegex(ValueError, "view hash mismatch"):
                VERIFIER.verify_live_artifact(
                    child_path,
                    parent_path=parent,
                )

    def test_v5_interaction_protocol_is_replay_bound(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            v3, v4, _, _ = _continuation_artifacts(
                root,
                interaction_protocol=GLOBAL_BEATS_V2,
            )
            v5, artifact, _ = _next_continuation_artifact(
                root,
                parent_path=v4,
                name="session-003",
            )

            receipt = VERIFIER.verify_live_artifact(
                v5,
                parent_path=v4,
                ancestor_paths=(v3,),
            )
            self.assertTrue(receipt["continuation_chain_verified"])

            artifact["config"]["interaction_protocol"] = SLOTS_V1
            artifact["result"]["initial_state"].pop("interaction_protocol")
            tampered_result = VERIFIER._survival_result(artifact["result"])
            artifact["canonical_result_sha256"] = result_sha256(tampered_result)
            _write_artifact(v5, artifact)

            with self.assertRaisesRegex(ValueError, "view hash mismatch"):
                VERIFIER.verify_live_artifact(
                    v5,
                    parent_path=v4,
                    ancestor_paths=(v3,),
                )

    def test_host_emitted_completed_v6_binds_every_call_to_its_result(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            child = Path(directory) / "host-completed-v6.json"
            artifact = _retained_host_v6_artifact()
            self.assertEqual(artifact["status"], "completed")
            self.assertEqual(artifact["format_version"], 6)
            _write_artifact(child, artifact)

            receipt = VERIFIER.verify_live_artifact(
                child,
                parent_path=SESSION_2_ARTIFACT,
                ancestor_paths=(SESSION_1_ARTIFACT,),
            )
            self.assertTrue(receipt["exact_replay"])
            self.assertTrue(receipt["continuation_chain_verified"])

            mutations = {
                "order": lambda payload: payload["calls"][0].__setitem__(
                    "public_name", "Birch"
                ),
                "request": _tamper_first_call_request,
                "provider_model": lambda payload: payload["calls"][0][
                    "response"
                ].__setitem__("provider_model", "wrong-model"),
                "raw_reply": lambda payload: payload["calls"][0][
                    "response"
                ].__setitem__(
                    "model_reply",
                    '{"action":{"kind":"wait"},"say":null}',
                ),
                "validation": lambda payload: payload["calls"][0][
                    "validation"
                ].__setitem__("action_error", "invented validation error"),
                "cost_authorization": lambda payload: payload["calls"][0][
                    "cost_authorization"
                ].__setitem__("request_cost_bound_usd", "999"),
                "parsed_choice": lambda payload: payload["calls"][0][
                    "parsed_choice"
                ]["action"].__setitem__("kind", "wait"),
                "extra": lambda payload: payload["calls"].append(
                    deepcopy(payload["calls"][-1])
                ),
                "missing": lambda payload: payload["calls"].pop(),
            }
            for label, mutate in mutations.items():
                with self.subTest(tamper=label):
                    tampered = deepcopy(artifact)
                    mutate(tampered)
                    _write_artifact(child, tampered)
                    with self.assertRaises(ValueError):
                        VERIFIER.verify_live_artifact(
                            child,
                            parent_path=SESSION_2_ARTIFACT,
                            ancestor_paths=(SESSION_1_ARTIFACT,),
                        )

    def test_v6_verifier_reconstructs_initiative_phase_as_a_treatment(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            child = Path(directory) / "phase-3-v6.json"
            artifact = _host_v6_artifact(
                transport=_HostFixtureTransport(),
                initiative_phase=3,
            )
            _write_artifact(child, artifact)

            receipt = VERIFIER.verify_live_artifact(
                child,
                parent_path=SESSION_2_ARTIFACT,
                ancestor_paths=(SESSION_1_ARTIFACT,),
            )

            self.assertTrue(receipt["exact_replay"])
            self.assertEqual(artifact["config"]["initiative_phase"], 3)
            self.assertEqual(
                [call["public_name"] for call in artifact["calls"]],
                ["Birch", "Cinder", "Lumen", "Aster"],
            )

            artifact["config"]["initiative_phase"] = 2
            _write_artifact(child, artifact)
            with self.assertRaises(ValueError):
                VERIFIER.verify_live_artifact(
                    child,
                    parent_path=SESSION_2_ARTIFACT,
                    ancestor_paths=(SESSION_1_ARTIFACT,),
                )

    def test_host_emitted_failed_v6_replays_its_committed_prefix(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            child = Path(directory) / "host-failed-v6.json"
            artifact = _retained_host_v6_artifact(2)
            self.assertEqual(artifact["status"], "failed")
            self.assertEqual(len(artifact["calls"]), 2)
            _write_artifact(child, artifact)

            receipt = VERIFIER.verify_live_artifact(
                child,
                parent_path=SESSION_2_ARTIFACT,
                ancestor_paths=(SESSION_1_ARTIFACT,),
            )

        self.assertEqual(receipt["status"], "failed")
        self.assertEqual(receipt["failure_kind"], "http_error")
        self.assertTrue(receipt["failure_call_receipt_consistent"])
        self.assertTrue(receipt["continuation_chain_verified"])
        current_events = [
            event
            for event in artifact["partial_state"]["events"]
            if event["cycle"] == artifact["failure"]["cycle"]
            and event["slot"] == artifact["failure"]["slot"]
        ]
        event_kinds = {event["kind"] for event in current_events}
        self.assertIn("choice_submitted", event_kinds)
        self.assertIn("choice_recorded", event_kinds)
        self.assertIn("speech_sent", event_kinds)
        self.assertTrue(
            {
                "choice_energy_paid",
                "wait_completed",
                "food_foraged",
                "wood_gathered",
                "resource_given",
                "food_eaten",
                "shelter_built",
                "rest_started",
                "action_resolution_rejected",
                "cycle_energy_paid",
                "deadline_choice_cancelled",
                "forced_collapse",
                "survivor_died",
                "resources_regenerated",
                "world_finished",
            }.isdisjoint(event_kinds)
        )
        initial_survivors = {
            survivor["name"]: survivor
            for survivor in artifact["initial_state"]["survivors"]
        }
        for survivor in artifact["partial_state"]["survivors"]:
            initial = initial_survivors[survivor["name"]]
            for key in ("energy", "food", "wood", "shelter", "resting"):
                self.assertEqual(survivor[key], initial[key])
        self.assertEqual(
            artifact["partial_state"]["resources"],
            artifact["initial_state"]["resources"],
        )
        first_speech = artifact["calls"][0]["parsed_choice"]["say"]["text"]
        second_request = artifact["calls"][1]["request"]
        prompt_key = "input" if "input" in second_request else "messages"
        self.assertIn(first_speech, second_request[prompt_key][1]["content"])

    def test_host_emitted_v6_call_cap_failure_proves_the_cap(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            child = Path(directory) / "host-call-cap-v6.json"
            artifact = _host_v6_artifact(
                transport=_HostFixtureTransport(action_kind="wait")
            )
            self.assertEqual(artifact["status"], "failed")
            self.assertEqual(artifact["failure"]["kind"], "call_cap_reached")
            self.assertEqual(len(artifact["calls"]), artifact["config"]["max_calls"])
            _write_artifact(child, artifact)

            receipt = VERIFIER.verify_live_artifact(
                child,
                parent_path=SESSION_2_ARTIFACT,
                ancestor_paths=(SESSION_1_ARTIFACT,),
            )

        self.assertEqual(receipt["status"], "failed")
        self.assertTrue(receipt["failure_call_receipt_consistent"])

    def test_v6_rejects_invented_no_call_failure_preconditions(self) -> None:
        base = _retained_host_v6_artifact(2)
        failed_call = deepcopy(base["calls"][-1])
        mutations = {
            "early_call_cap": {
                "kind": "call_cap_reached",
                "message": "live model call cap reached before request",
            },
            "budget_within_limit": {
                "kind": "paid_budget_exhausted",
                "message": "paid cost authorization exhausted before request",
                "cost_authorization": failed_call["cost_authorization"],
            },
        }
        with tempfile.TemporaryDirectory() as directory:
            child = Path(directory) / "forged-no-call-v6.json"
            for label, replacement in mutations.items():
                with self.subTest(tamper=label):
                    artifact = deepcopy(base)
                    artifact["calls"].pop()
                    artifact["failure"].update(
                        {
                            "call_sequence": None,
                            "http_status": None,
                            **replacement,
                        }
                    )
                    _write_artifact(child, artifact)
                    with self.assertRaises(ValueError):
                        VERIFIER.verify_live_artifact(
                            child,
                            parent_path=SESSION_2_ARTIFACT,
                            ancestor_paths=(SESSION_1_ARTIFACT,),
                        )

    def test_host_emitted_v6_continues_without_forced_model_churn(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first_path = root / "first-v6.json"
            first = _retained_host_v6_artifact()
            first_sha256 = _write_artifact(first_path, first)

            second_path = root / "second-v6.json"
            second = _host_v6_artifact(
                transport=_HostFixtureTransport(),
                parent_path=first_path,
                expected_parent_sha256=first_sha256,
                ancestor_paths=(SESSION_1_ARTIFACT, SESSION_2_ARTIFACT),
                model_replacements=(),
            )
            _write_artifact(second_path, second)

            receipt = VERIFIER.verify_live_artifact(
                second_path,
                parent_path=first_path,
                ancestor_paths=(SESSION_1_ARTIFACT, SESSION_2_ARTIFACT),
            )

        self.assertEqual(second["format_version"], 6)
        self.assertEqual(second["assignment_transition_receipts"], [])
        self.assertTrue(receipt["exact_replay"])
        self.assertEqual(receipt["continuation_depth"], 3)

    def test_failed_dialogue_v6_rejects_order_speech_choice_view_and_state_tampering(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            child = root / "host-failed-v6.json"
            artifact = _retained_host_v6_artifact(2)
            mutations = {
                "order": lambda payload: payload["calls"][0].__setitem__(
                    "public_name", "Aster"
                ),
                "speech": lambda payload: payload["calls"][0][
                    "parsed_choice"
                ]["say"].__setitem__("text", "tampered speech"),
                "raw_reply": lambda payload: payload["calls"][0][
                    "response"
                ].__setitem__(
                    "model_reply",
                    '{"action":{"kind":"wait"},"say":null}',
                ),
                "validation": lambda payload: payload["calls"][0][
                    "validation"
                ].__setitem__("speech_error", "invented validation error"),
                "cost_authorization": lambda payload: payload["calls"][0][
                    "cost_authorization"
                ].__setitem__("cumulative_cost_bound_usd", "999"),
                "choice": lambda payload: payload["calls"][0][
                    "parsed_choice"
                ]["action"].__setitem__("kind", "wait"),
                "view": _tamper_first_call_request,
                "state": lambda payload: payload["partial_state"]["survivors"][
                    0
                ].__setitem__(
                    "energy",
                    payload["partial_state"]["survivors"][0]["energy"] + 1,
                ),
            }
            for label, mutate in mutations.items():
                with self.subTest(tamper=label):
                    tampered = deepcopy(artifact)
                    mutate(tampered)
                    _write_artifact(child, tampered)
                    with self.assertRaises(ValueError):
                        VERIFIER.verify_live_artifact(
                            child,
                            parent_path=SESSION_2_ARTIFACT,
                            ancestor_paths=(SESSION_1_ARTIFACT,),
                        )

    def test_failed_v6_rejects_boolean_aliases_for_numeric_state(self) -> None:
        artifact = _retained_host_v6_artifact(2)
        mutations = {
            "config": lambda payload: payload["config"]["world_config"].__setitem__(
                "eat_energy_cost", True
            ),
            "initial": lambda payload: payload["initial_state"]["survivors"][
                2
            ].__setitem__("food", True),
            "partial": lambda payload: payload["partial_state"].__setitem__(
                "slot", True
            ),
        }
        with tempfile.TemporaryDirectory() as directory:
            child = Path(directory) / "boolean-alias-v6.json"
            for label, mutate in mutations.items():
                with self.subTest(tamper=label):
                    tampered = deepcopy(artifact)
                    mutate(tampered)
                    _write_artifact(child, tampered)
                    with self.assertRaises(ValueError):
                        VERIFIER.verify_live_artifact(
                            child,
                            parent_path=SESSION_2_ARTIFACT,
                            ancestor_paths=(SESSION_1_ARTIFACT,),
                        )

    def test_v6_requires_exact_assignment_transition_receipts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            child = root / "host-failed-v6.json"
            artifact = _retained_host_v6_artifact(2)

            mutations = {
                "absent": lambda payload: payload.pop(
                    "assignment_transition_receipts"
                ),
                "empty": lambda payload: payload.__setitem__(
                    "assignment_transition_receipts", []
                ),
                "fake": lambda payload: payload["seat_assignments"][2].__setitem__(
                    "model", "opencode-paid/kimi-k2.6"
                ),
                "unknown": lambda payload: payload[
                    "assignment_transition_receipts"
                ][0].__setitem__("public_name", "Ghost"),
                "duplicate": lambda payload: payload[
                    "assignment_transition_receipts"
                ].append(deepcopy(payload["assignment_transition_receipts"][0])),
                "duplicate_child_seat": lambda payload: payload[
                    "seat_assignments"
                ].append(deepcopy(payload["seat_assignments"][2])),
                "wrong_seat": lambda payload: payload[
                    "assignment_transition_receipts"
                ][0].__setitem__("seat_id", "seat-001"),
                "previous": lambda payload: payload[
                    "assignment_transition_receipts"
                ][0].__setitem__("previous_model", "test/wrong-before"),
                "replacement": lambda payload: payload[
                    "assignment_transition_receipts"
                ][0].__setitem__("replacement_model", "test/wrong-after"),
                "unexplained": lambda payload: payload["seat_assignments"][
                    0
                ].__setitem__("model", "test/unexplained"),
                "extra_field": lambda payload: payload[
                    "assignment_transition_receipts"
                ][0].__setitem__("extra", "not allowed"),
                "invalid_reason": lambda payload: payload[
                    "assignment_transition_receipts"
                ][0].__setitem__("reason", " padded "),
            }
            for label, mutate in mutations.items():
                with self.subTest(tamper=label):
                    tampered = deepcopy(artifact)
                    mutate(tampered)
                    _write_artifact(child, tampered)
                    with self.assertRaises(ValueError):
                        VERIFIER.verify_live_artifact(
                            child,
                            parent_path=SESSION_2_ARTIFACT,
                            ancestor_paths=(SESSION_1_ARTIFACT,),
                        )

    def test_v6_rejects_sequential_protocol_or_receipts_on_legacy_format(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            child = root / "host-failed-v6.json"
            artifact = _retained_host_v6_artifact(2)

            wrong_protocol = deepcopy(artifact)
            wrong_protocol["config"]["interaction_protocol"] = GLOBAL_BEATS_V2
            _write_artifact(child, wrong_protocol)
            with self.assertRaisesRegex(ValueError, "sequential-dialogue-v3"):
                VERIFIER.verify_live_artifact(
                    child,
                    parent_path=SESSION_2_ARTIFACT,
                    ancestor_paths=(SESSION_1_ARTIFACT,),
                )

            legacy = deepcopy(artifact)
            legacy["format_version"] = 5
            _write_artifact(child, legacy)
            with self.assertRaisesRegex(
                ValueError,
                "assignment_transition_receipts requires format_version 6",
            ):
                VERIFIER.verify_live_artifact(
                    child,
                    parent_path=SESSION_2_ARTIFACT,
                    ancestor_paths=(SESSION_1_ARTIFACT,),
                )

    def test_continuation_requires_an_actual_parent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _, child, _, _ = _continuation_artifacts(root)

            with self.assertRaisesRegex(ValueError, "requires --parent"):
                VERIFIER.verify_live_artifact(child)
            with self.assertRaisesRegex(ValueError, "cannot read continuation parent"):
                VERIFIER.verify_live_artifact(
                    child,
                    parent_path=root / "missing-parent.json",
                )

    def test_continuation_rejects_the_wrong_parent_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _, child, _, _ = _continuation_artifacts(root)
            wrong_parent = root / "wrong-parent.json"
            _write_artifact(wrong_parent, _parent_artifact(seed=29_994))

            with self.assertRaisesRegex(ValueError, "parent artifact SHA-256"):
                VERIFIER.verify_live_artifact(
                    child,
                    parent_path=wrong_parent,
                )

    def test_continuation_rejects_a_tampered_parent_even_with_updated_link(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            parent_path, child_path, child, _ = _continuation_artifacts(root)
            parent = json.loads(parent_path.read_text(encoding="utf-8"))
            parent["result"]["final_state"]["survivors"][0]["energy"] += 1
            tampered_parent_sha256 = _write_artifact(parent_path, parent)
            child["continuation_link"]["parent_artifact_sha256"] = (
                tampered_parent_sha256
            )
            _write_artifact(child_path, child)

            with self.assertRaisesRegex(ValueError, "replay|canonical"):
                VERIFIER.verify_live_artifact(
                    child_path,
                    parent_path=parent_path,
                )

    def test_continuation_rejects_a_tampered_transition_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            parent, child_path, child, _ = _continuation_artifacts(root)
            child["transition_receipt"]["event"]["detail"]["before"] += 1
            _write_artifact(child_path, child)

            with self.assertRaisesRegex(ValueError, "transition receipt"):
                VERIFIER.verify_live_artifact(
                    child_path,
                    parent_path=parent,
                )

    def test_continuation_verifies_preserved_parent_state_and_rejects_an_event(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            child_path = root / "preserved-v6.json"
            child = _host_v6_artifact(
                transport=_HostFixtureTransport(fail_at=None),
                preserve_shared_resources=True,
            )
            _write_artifact(child_path, child)

            receipt = VERIFIER.verify_live_artifact(
                child_path,
                parent_path=SESSION_2_ARTIFACT,
                ancestor_paths=(SESSION_1_ARTIFACT,),
            )
            self.assertTrue(receipt["exact_replay"])
            self.assertEqual(
                child["transition_receipt"],
                {"method": "verified_parent_state_preserved", "event": None},
            )

            child["transition_receipt"]["event"] = {}
            _write_artifact(child_path, child)
            with self.assertRaisesRegex(ValueError, "event must be null"):
                VERIFIER.verify_live_artifact(
                    child_path,
                    parent_path=SESSION_2_ARTIFACT,
                    ancestor_paths=(SESSION_1_ARTIFACT,),
                )

    def test_continuation_rejects_a_tampered_public_record(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            parent, child_path, child, _ = _continuation_artifacts(root)
            child["public_record_receipt"]["record"]["statements"][0]["text"] = (
                "tampered"
            )
            _write_artifact(child_path, child)

            with self.assertRaisesRegex(ValueError, "public record receipt"):
                VERIFIER.verify_live_artifact(
                    child_path,
                    parent_path=parent,
                )

    def test_continuation_rejects_a_tampered_history_offset(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            parent, child_path, child, _ = _continuation_artifacts(root)
            child["result"]["initial_state"]["event_sequence_offset"] += 1
            _write_artifact(child_path, child)

            with self.assertRaisesRegex(ValueError, "child initial state"):
                VERIFIER.verify_live_artifact(
                    child_path,
                    parent_path=parent,
                )

    def test_failed_continuation_validates_its_call_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            parent, child_path, child, boundary_state = _continuation_artifacts(root)
            child.pop("result")
            child.pop("canonical_result_sha256")
            child["status"] = "failed"
            child["initial_state"] = {
                key: value for key, value in boundary_state.items() if key != "events"
            }
            child["partial_state"] = boundary_state
            failed_call = {
                "sequence": 1,
                "day": 2,
                "cycle": 2,
                "slot": 1,
                "seat_id": "seat-001",
                "public_name": "Aster",
                "model": "test/model-a",
                "status": "failed",
                "error": {
                    "kind": "http_error",
                    "message": "synthetic provider failure",
                    "http_status": 503,
                },
            }
            child["calls"] = [failed_call]
            child["failure"] = {
                "call_sequence": 1,
                "day": 2,
                "cycle": 2,
                "slot": 1,
                "seat_id": "seat-001",
                "public_name": "Aster",
                "model": "test/model-a",
                **failed_call["error"],
            }
            parent_payload = json.loads(parent.read_text(encoding="utf-8"))
            failed_world = continue_survival_world(
                VERIFIER._survival_result(parent_payload["result"])
            )
            adjust_shared_resource(
                failed_world,
                resource="wood",
                stock=0,
                reason="session_002_shelter_dilemma",
            )
            with self.assertRaises(RuntimeError):
                run_survival(
                    failed_world,
                    {
                        "Aster": _FailingSpeaker(),
                        "Birch": _RestSpeaker(),
                    },
                    days=1,
                )
            child["partial_state"] = failed_world.to_dict()
            _write_artifact(child_path, child)

            receipt = VERIFIER.verify_live_artifact(
                child_path,
                parent_path=parent,
            )
            self.assertEqual(receipt["status"], "failed")
            self.assertTrue(receipt["failure_call_receipt_consistent"])

            for label, mutate in (
                (
                    "resource",
                    lambda payload: payload["partial_state"]["resources"].__setitem__(
                        "food", payload["partial_state"]["resources"]["food"] + 1
                    ),
                ),
                (
                    "survivor",
                    lambda payload: payload["partial_state"]["survivors"][
                        0
                    ].__setitem__(
                        "energy",
                        payload["partial_state"]["survivors"][0]["energy"] + 1,
                    ),
                ),
                (
                    "day",
                    lambda payload: payload["partial_state"].__setitem__("day", 3),
                ),
                (
                    "event",
                    lambda payload: payload["partial_state"]["events"].append(
                        deepcopy(payload["partial_state"]["events"][-1])
                    ),
                ),
            ):
                with self.subTest(tamper=label):
                    tampered = deepcopy(child)
                    mutate(tampered)
                    _write_artifact(child_path, tampered)
                    with self.assertRaisesRegex(
                        ValueError,
                        "does not match reconstructed recorded calls",
                    ):
                        VERIFIER.verify_live_artifact(
                            child_path,
                            parent_path=parent,
                        )

            child["failure"]["public_name"] = "Birch"
            _write_artifact(child_path, child)
            with self.assertRaisesRegex(ValueError, "failure identity"):
                VERIFIER.verify_live_artifact(
                    child_path,
                    parent_path=parent,
                )

    def test_failed_10k_episode_receipt_is_consistent(self) -> None:
        artifact = REPOSITORY_ROOT / "outputs" / "v0.7.0-paid-reasoning-29994.json"

        receipt = VERIFIER.verify_live_artifact(
            artifact,
            expected_artifact_sha256=(
                "cb2164f1cb410cc617bf80188c9c90215996e9ae577240918f424fd429de3d0c"
            ),
        )

        self.assertEqual(receipt["status"], "failed")
        self.assertEqual(receipt["failure_kind"], "http_error")
        self.assertTrue(receipt["failure_call_receipt_consistent"])
        self.assertIsNone(receipt["exact_replay"])
        self.assertEqual(receipt["source_hashes_matched"], 8)

    def test_failed_paid_episode_receipt_is_consistent(self) -> None:
        artifact = REPOSITORY_ROOT / "outputs" / "v0.6.0-paid-observation-29995.json"

        receipt = VERIFIER.verify_live_artifact(artifact)

        self.assertEqual(receipt["status"], "failed")
        self.assertEqual(receipt["failure_kind"], "completion_budget_exhausted")
        self.assertTrue(receipt["failure_call_receipt_consistent"])
        self.assertIsNone(receipt["exact_replay"])
        self.assertEqual(receipt["source_hashes_matched"], 8)
        self.assertEqual(receipt["source_match"], "git_commit")
        self.assertEqual(
            receipt["source_commit"],
            "1c199cf36c885e16660baad7f62f5ab920ef3b55",
        )

    def test_failed_receipt_rejects_a_mismatched_failure_kind(self) -> None:
        source = REPOSITORY_ROOT / "outputs" / "v0.6.0-paid-observation-29995.json"
        payload = json.loads(source.read_text(encoding="utf-8"))
        payload["failure"]["kind"] = "tampered"
        with tempfile.TemporaryDirectory() as directory:
            artifact = Path(directory) / "tampered.json"
            artifact.write_text(json.dumps(payload), encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "failure error does not match"):
                VERIFIER.verify_live_artifact(artifact)

    def test_failed_receipt_rejects_a_mismatched_identity(self) -> None:
        source = REPOSITORY_ROOT / "outputs" / "v0.6.0-paid-observation-29995.json"
        payload = json.loads(source.read_text(encoding="utf-8"))
        payload["failure"]["public_name"] = "tampered"
        with tempfile.TemporaryDirectory() as directory:
            artifact = Path(directory) / "tampered.json"
            artifact.write_text(json.dumps(payload), encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "failure identity does not match"):
                VERIFIER.verify_live_artifact(artifact)


if __name__ == "__main__":
    unittest.main()
