from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from world_sim.survival.calibration import (
    CALIBRATION_NAMES,
    CALIBRATION_POLICIES,
    ChattyFoodFirstPolicy,
    LEAN_CAMP_V1,
    FoodFirstPolicy,
    MutualAidPolicy,
    RestOnlyPolicy,
    ShelterFirstPolicy,
    canonical_calibration_json,
    run_calibration,
    seat_rotations,
    survival_preset,
)
from world_sim.survival.engine import make_survival_world, survival_view_for


class SurvivalCalibrationTests(unittest.TestCase):
    def test_lean_camp_v1_has_the_preregistered_physics(self) -> None:
        config = survival_preset(LEAN_CAMP_V1)

        self.assertEqual(config.max_days, 8)
        self.assertEqual(config.slots_per_cycle, 4)
        self.assertEqual(config.exhaustion_energy_penalty, 3)
        self.assertEqual((config.starting_energy, config.max_energy), (16, 24))
        self.assertEqual(config.daily_energy_cost, 3)
        self.assertEqual(config.shelter_energy_discount, 2)
        self.assertEqual(config.rest_energy_cost, 0)
        self.assertEqual(config.speech_energy_cost, 0)
        self.assertEqual(
            (
                config.food_starting_stock,
                config.food_capacity,
                config.food_regeneration,
            ),
            (6, 12, 3),
        )
        self.assertEqual(
            (
                config.wood_starting_stock,
                config.wood_capacity,
                config.wood_regeneration,
            ),
            (4, 12, 2),
        )
        self.assertEqual(config.shelter_wood_cost, 4)

    def test_rotations_move_every_name_through_every_seat(self) -> None:
        rotations = seat_rotations()

        self.assertEqual(len(rotations), 4)
        for seat_index in range(4):
            self.assertEqual(
                {rotation[seat_index] for rotation in rotations},
                set(CALIBRATION_NAMES),
            )

    def test_unknown_preset_and_duplicate_seeds_fail_before_simulation(self) -> None:
        with self.assertRaisesRegex(ValueError, "unknown survival calibration preset"):
            survival_preset("not-a-preset")
        with self.assertRaisesRegex(ValueError, "seeds must be unique"):
            run_calibration(seeds=(3, 3), bootstrap_samples=1)

    def test_scripted_policies_take_distinct_initial_paths_and_rest_at_deadline(
        self,
    ) -> None:
        world = make_survival_world(
            CALIBRATION_NAMES,
            seed=3,
            config=survival_preset(LEAN_CAMP_V1, cycles=2),
        )
        initial = survival_view_for(world, "Aster")

        self.assertEqual(
            RestOnlyPolicy().decide(initial)["action"], {"kind": "rest"}
        )
        self.assertEqual(
            FoodFirstPolicy().decide(initial)["action"], {"kind": "forage"}
        )
        self.assertEqual(
            ShelterFirstPolicy().decide(initial)["action"],
            {"kind": "gather_wood"},
        )
        self.assertEqual(
            MutualAidPolicy().decide(initial)["action"], {"kind": "forage"}
        )

        final_slot = replace(initial, slot=4, slots_remaining=1)
        for policy in (
            FoodFirstPolicy(),
            ChattyFoodFirstPolicy(),
            ShelterFirstPolicy(),
            MutualAidPolicy(),
        ):
            self.assertEqual(policy.decide(final_slot)["action"], {"kind": "rest"})

    def test_mutual_aid_has_one_visible_costly_gift_rule(self) -> None:
        world = make_survival_world(
            CALIBRATION_NAMES,
            seed=3,
            config=survival_preset(LEAN_CAMP_V1, cycles=2),
        )
        world.survivors["Aster"].food = 2
        world.survivors["Aster"].energy = 16
        world.survivors["Birch"].energy = 10
        world.survivors["Cinder"].energy = 12
        world.survivors["Lumen"].energy = 11

        choice = MutualAidPolicy().decide(survival_view_for(world, "Aster"))

        self.assertEqual(
            choice,
            {
                "action": {"kind": "give_food", "target": "Birch", "amount": 1},
                "say": None,
            },
        )

    def test_mutual_aid_uses_food_first_when_no_peer_remains(self) -> None:
        world = make_survival_world(
            CALIBRATION_NAMES,
            seed=3,
            config=survival_preset(LEAN_CAMP_V1, cycles=2),
        )
        view = replace(survival_view_for(world, "Aster"), others=())

        self.assertEqual(
            MutualAidPolicy().decide(view),
            FoodFirstPolicy().decide(view),
        )

    def test_small_calibration_is_paired_replayed_and_canonical(self) -> None:
        first = run_calibration(seeds=(7,), cycles=2, bootstrap_samples=32)
        second = run_calibration(seeds=(7,), cycles=2, bootstrap_samples=32)

        self.assertEqual(
            canonical_calibration_json(first), canonical_calibration_json(second)
        )
        self.assertEqual(first["design"]["run_count"], 20)
        self.assertEqual(first["schema_version"], 2)
        self.assertEqual(
            len(first["source"]["calibration_module_sha256"]),
            64,
        )
        self.assertEqual(len(first["per_seed_food_first_vs_mutual_aid"]), 1)
        self.assertEqual(first["design"]["seat_rotation_count"], 4)
        self.assertEqual(
            first["paired_food_first_vs_mutual_aid"]["pair_count"], 4
        )
        self.assertEqual(
            first["paired_food_first_vs_mutual_aid"]["independent_seed_count"],
            1,
        )
        self.assertEqual(first["paired_food_first_vs_chatty"]["pair_count"], 4)
        self.assertEqual(
            first["paired_food_first_vs_chatty"]["mean_survivor_gain"], 0.0
        )
        self.assertEqual(set(first["policy_metrics"]), set(CALIBRATION_POLICIES))
        for metrics in first["policy_metrics"].values():
            self.assertEqual(metrics["replay_exact_rate"], 1.0)
            self.assertEqual(metrics["forced_collapse_count"], 0)
        self.assertGreater(
            first["policy_metrics"]["food_first_chatty"]["messages_sent_per_run"],
            0,
        )
        self.assertEqual(
            json.loads(canonical_calibration_json(first)),
            first,
        )

    def test_cli_stdout_and_output_are_the_same_canonical_json(self) -> None:
        repository_root = Path(__file__).resolve().parents[1]
        script = repository_root / "tools" / "calibrate_survival.py"
        with tempfile.TemporaryDirectory() as temporary_directory:
            output = Path(temporary_directory) / "calibration.json"
            completed = subprocess.run(
                (
                    sys.executable,
                    str(script),
                    "--seed-count",
                    "1",
                    "--cycles",
                    "2",
                    "--bootstrap-samples",
                    "16",
                    "--output",
                    str(output),
                ),
                cwd=repository_root,
                check=True,
                capture_output=True,
                text=True,
            )

            stdout = completed.stdout.strip()
            self.assertEqual(output.read_text(encoding="utf-8").strip(), stdout)
            parsed = json.loads(stdout)
            self.assertEqual(
                stdout,
                json.dumps(
                    parsed,
                    allow_nan=False,
                    ensure_ascii=False,
                    separators=(",", ":"),
                    sort_keys=True,
                ),
            )


if __name__ == "__main__":
    unittest.main()
