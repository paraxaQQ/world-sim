from __future__ import annotations

import hashlib
import importlib.util
import json
import tempfile
import unittest
from collections.abc import Mapping
from copy import deepcopy
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
VERIFIER_PATH = REPOSITORY_ROOT / "tools" / "verify_live_artifact.py"
SESSION_1_ARTIFACT = (
    REPOSITORY_ROOT / "outputs" / "v0.8.0-paid-survival-29993.json"
)
SESSION_1_ARTIFACT_SHA256 = (
    "a98ec8216c08a172c4ed29fb1da65b63defd3b4a29f53e95fa26a1e187e38b90"
)
SESSION_1_CANONICAL_SHA256 = (
    "490663b4a743f51c4b0f44ccc57ba91ee2a7b6d6adafbcda072373a7748a54e7"
)
SESSION_2_ARTIFACT = (
    REPOSITORY_ROOT
    / "outputs"
    / "v0.9.0-session-002-shelter-dilemma-29993.json"
)
SESSION_2_ARTIFACT_SHA256 = (
    "fc0b07dfc404a2f485f3b6a2c2f191fec5e495153d6147d428d6cb251cab27fe"
)
SESSION_2_CANONICAL_SHA256 = (
    "ed1f299bbc698951e77256b46291ea4ee142469bc0a7cd0e7b6bf476820392ca"
)
SPEC = importlib.util.spec_from_file_location("verify_live_artifact", VERIFIER_PATH)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError("cannot load live artifact verifier")
VERIFIER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(VERIFIER)

from world_sim.survival.demo import result_sha256
from world_sim.survival.engine import (
    adjust_shared_resource,
    continue_survival_world,
    make_survival_world,
    replay_survival,
    run_survival,
)
from world_sim.survival.models import (
    GLOBAL_BEATS_V2,
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
            "parent_canonical_result_sha256": parent[
                "canonical_result_sha256"
            ],
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


class LiveArtifactVerifierTests(unittest.TestCase):
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

    def test_completed_continuation_verifies_actual_parent_chain(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            parent, child, child_payload, _ = _continuation_artifacts(
                Path(directory)
            )

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

    def test_continuation_rejects_a_tampered_parent_even_with_updated_link(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            parent_path, child_path, child, _ = _continuation_artifacts(root)
            parent = json.loads(parent_path.read_text(encoding="utf-8"))
            parent["result"]["final_state"]["survivors"][0]["energy"] += 1
            tampered_parent_sha256 = _write_artifact(parent_path, parent)
            child["continuation_link"][
                "parent_artifact_sha256"
            ] = tampered_parent_sha256
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

    def test_continuation_rejects_a_tampered_public_record(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            parent, child_path, child, _ = _continuation_artifacts(root)
            child["public_record_receipt"]["record"]["statements"][0][
                "text"
            ] = "tampered"
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
            parent, child_path, child, boundary_state = _continuation_artifacts(
                root
            )
            child.pop("result")
            child.pop("canonical_result_sha256")
            child["status"] = "failed"
            child["initial_state"] = {
                key: value
                for key, value in boundary_state.items()
                if key != "events"
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
            _write_artifact(child_path, child)

            receipt = VERIFIER.verify_live_artifact(
                child_path,
                parent_path=parent,
            )
            self.assertEqual(receipt["status"], "failed")
            self.assertTrue(receipt["failure_call_receipt_consistent"])

            child["failure"]["public_name"] = "Birch"
            _write_artifact(child_path, child)
            with self.assertRaisesRegex(ValueError, "failure identity"):
                VERIFIER.verify_live_artifact(
                    child_path,
                    parent_path=parent,
                )

    def test_failed_10k_episode_receipt_is_consistent(self) -> None:
        artifact = (
            REPOSITORY_ROOT
            / "outputs"
            / "v0.7.0-paid-reasoning-29994.json"
        )

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
        artifact = (
            REPOSITORY_ROOT
            / "outputs"
            / "v0.6.0-paid-observation-29995.json"
        )

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
        source = (
            REPOSITORY_ROOT
            / "outputs"
            / "v0.6.0-paid-observation-29995.json"
        )
        payload = json.loads(source.read_text(encoding="utf-8"))
        payload["failure"]["kind"] = "tampered"
        with tempfile.TemporaryDirectory() as directory:
            artifact = Path(directory) / "tampered.json"
            artifact.write_text(json.dumps(payload), encoding="utf-8")

            with self.assertRaisesRegex(
                ValueError, "failure error does not match"
            ):
                VERIFIER.verify_live_artifact(artifact)

    def test_failed_receipt_rejects_a_mismatched_identity(self) -> None:
        source = (
            REPOSITORY_ROOT
            / "outputs"
            / "v0.6.0-paid-observation-29995.json"
        )
        payload = json.loads(source.read_text(encoding="utf-8"))
        payload["failure"]["public_name"] = "tampered"
        with tempfile.TemporaryDirectory() as directory:
            artifact = Path(directory) / "tampered.json"
            artifact.write_text(json.dumps(payload), encoding="utf-8")

            with self.assertRaisesRegex(
                ValueError, "failure identity does not match"
            ):
                VERIFIER.verify_live_artifact(artifact)


if __name__ == "__main__":
    unittest.main()
