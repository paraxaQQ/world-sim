from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
VERIFIER_PATH = REPOSITORY_ROOT / "tools" / "verify_live_artifact.py"
SPEC = importlib.util.spec_from_file_location("verify_live_artifact", VERIFIER_PATH)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError("cannot load live artifact verifier")
VERIFIER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(VERIFIER)


class LiveArtifactVerifierTests(unittest.TestCase):
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
