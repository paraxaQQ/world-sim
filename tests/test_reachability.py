from __future__ import annotations

import hashlib
import unittest
from pathlib import Path

from _retained_outputs import retained_outputs_root
from world_sim.reachability import run_shelter_reachability_control


RETAINED_REPOSITORY_ROOT = retained_outputs_root()
ROOT = RETAINED_REPOSITORY_ROOT / "outputs" / "v0.8.0-paid-survival-29993.json"
PARENT = (
    RETAINED_REPOSITORY_ROOT
    / "outputs"
    / "v0.9.0-session-002-shelter-dilemma-29993.json"
)


class ShelterReachabilityTests(unittest.TestCase):
    def test_exact_session_parent_is_reachable_in_every_initiative_phase(self) -> None:
        receipt = run_shelter_reachability_control(
            parent_path=PARENT,
            expected_parent_sha256=_sha256(PARENT),
            ancestor_paths=(ROOT,),
            transition_reason="test_shelter_reachability",
        )

        self.assertEqual(receipt["generic_mutual_aid_baseline"]["shelters_built"], 0)
        self.assertTrue(
            receipt["conclusion"]["reachable_in_every_initiative_phase"]
        )
        self.assertEqual(
            [row["beat_1_opener"] for row in receipt["phase_controls"]],
            ["Cinder", "Lumen", "Aster", "Birch"],
        )
        for row in receipt["phase_controls"]:
            self.assertTrue(row["exact_replay"])
            self.assertLess(
                row["gift_event"]["sequence"],
                row["shelter_event"]["sequence"],
            )

    def test_insufficient_gift_is_a_real_negative_control(self) -> None:
        receipt = run_shelter_reachability_control(
            parent_path=PARENT,
            expected_parent_sha256=_sha256(PARENT),
            ancestor_paths=(ROOT,),
            transition_reason="test_shelter_reachability",
            gift_amount=1,
        )

        self.assertFalse(
            receipt["conclusion"]["reachable_in_every_initiative_phase"]
        )
        self.assertTrue(
            all(not row["reachable"] for row in receipt["phase_controls"])
        )
        self.assertTrue(
            all(row["gift_event"] is not None for row in receipt["phase_controls"])
        )
        self.assertTrue(
            all(row["shelter_event"] is None for row in receipt["phase_controls"])
        )


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


if __name__ == "__main__":
    unittest.main()
