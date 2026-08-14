from __future__ import annotations

import importlib.util
import json
import unittest
from copy import deepcopy
from decimal import Decimal
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SCORER_PATH = REPOSITORY_ROOT / "tools" / "score_turn_order_matrix.py"
MANIFEST_PATH = (
    REPOSITORY_ROOT
    / "outputs"
    / "v0.13.0-session-005-turn-order-matrix-protocol.json"
)
COMPLETED_CELL = (
    REPOSITORY_ROOT
    / "outputs"
    / "v0.13.0-session-005-turn-order-b01-p1-29993.json"
)
FAILED_CELL = (
    REPOSITORY_ROOT
    / "outputs"
    / "v0.13.0-session-005-turn-order-b02-p2-29993.json"
)

SPEC = importlib.util.spec_from_file_location("score_turn_order_matrix", SCORER_PATH)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError("cannot load turn-order matrix scorer")
SCORER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(SCORER)


class TurnOrderMatrixScorerTests(unittest.TestCase):
    def test_retained_matrix_is_verified_and_failed_cells_are_censored(self) -> None:
        report = SCORER.score_turn_order_matrix(MANIFEST_PATH)

        self.assertEqual(
            report["manifest"]["artifact_sha256"],
            "8ae4b6f3fd36e162ca1349be83e72424092a45807f7664e95a1788af9ab665c6",
        )
        self.assertEqual(
            report["batch"],
            {
                "planned_n": 12,
                "scoreable_n": 8,
                "censored_n": 4,
                "pending_n": 0,
                "terminal": True,
                "primary_success_count": 5,
                "primary_success_rate": 0.625,
                "max_minus_min_phase_rate": 0.5,
                "cost_exposure_usd": "0.69282431",
            },
        )
        self.assertEqual(
            [phase["primary_success_rate"] for phase in report["phases"]],
            [0.5, 1.0, 0.5, 0.5],
        )
        self.assertEqual(
            report["protocol"],
            {
                "stopping_rule": (
                    "run all 12 planned cells unless a technical or "
                    "aggregate-budget gate stops the batch"
                ),
                "status": "deviated",
                "first_technical_failure_execution_position": 6,
                "observed_post_stop_execution_positions": [7, 8, 9, 10, 11, 12],
                "post_stop_use": "exploratory_only",
                "reported_aggregate_use": "all_retained_cells_descriptive",
            },
        )
        self.assertEqual(
            report["raw_rows"][6]["analysis_set"],
            "post_stop_exploratory",
        )
        failed_rows = [row for row in report["raw_rows"] if row["censored"]]
        self.assertEqual(len(failed_rows), 4)
        for row in failed_rows:
            self.assertFalse(row["scoreable"])
            for field in SCORER.BEHAVIOR_FIELDS:
                self.assertIsNone(row[field])

    def test_completed_behavior_is_recomputed_instead_of_trusted(self) -> None:
        artifact = json.loads(COMPLETED_CELL.read_text(encoding="utf-8"))
        artifact["session_outcomes"][
            "primary_shelter_chain_by_end_of_chance_3"
        ] = False

        with self.assertRaisesRegex(ValueError, "independently recomputed"):
            SCORER._completed_behavior(artifact)

    def test_failed_fields_are_absent_and_failed_call_uses_bound(self) -> None:
        artifact = json.loads(FAILED_CELL.read_text(encoding="utf-8"))
        calls = artifact["calls"]
        expected = sum(
            (
                Decimal(call["cost_authorization"]["accounted_cost_usd"])
                if call["status"] == "succeeded"
                else Decimal(call["cost_authorization"]["request_cost_bound_usd"])
            )
            for call in calls
        )
        self.assertEqual(SCORER._call_cost_exposure(calls), expected)
        self.assertEqual(expected, Decimal("0.02588463"))

        tampered = deepcopy(artifact)
        tampered["session_outcomes"] = {}
        with self.assertRaisesRegex(ValueError, "outcome/result fields"):
            SCORER._validate_failed_artifact(
                tampered,
                receipt={
                    "exact_replay": None,
                    "continuation_chain_verified": True,
                    "failure_call_receipt_consistent": True,
                    "failure_kind": artifact["failure"]["kind"],
                },
            )

    def test_rate_range_waits_until_every_planned_cell_is_terminal(self) -> None:
        rows = []
        for phase in range(4):
            for primary in (True, False):
                rows.append(
                    {
                        "initiative_phase": phase,
                        "status": "completed",
                        "scoreable": True,
                        "censored": False,
                        "primary_shelter_chain_by_end_of_chance_3": primary,
                        "cost_exposure_usd": "0",
                    }
                )
            rows.append(
                {
                    "initiative_phase": phase,
                    "status": "pending",
                    "scoreable": False,
                    "censored": False,
                    "primary_shelter_chain_by_end_of_chance_3": None,
                    "cost_exposure_usd": None,
                }
            )

        batch, phases = SCORER._aggregate_rows(rows)

        self.assertFalse(batch["terminal"])
        self.assertIsNone(batch["max_minus_min_phase_rate"])
        self.assertTrue(all(phase["pending_n"] == 1 for phase in phases))


if __name__ == "__main__":
    unittest.main()
