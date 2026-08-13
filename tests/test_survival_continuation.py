from __future__ import annotations

import unittest
from collections.abc import Mapping, Sequence
from copy import deepcopy
from dataclasses import replace

from world_sim.survival.engine import (
    adjust_shared_resource,
    continue_survival_world,
    make_survival_world,
    replay_survival,
    run_survival,
    survival_view_for,
)
from world_sim.survival.models import SurvivalConfig, SurvivorView
from world_sim.survival.prompt import render_turn_prompt


def rest(text: str | None = None) -> dict[str, object]:
    speech = None if text is None else {"to": "everyone", "text": text}
    return {"action": {"kind": "rest"}, "say": speech}


class ScriptedPolicy:
    def __init__(self, choices: Sequence[Mapping[str, object]]) -> None:
        self._choices = tuple(choices)
        self._calls = 0

    def decide(self, view: SurvivorView) -> Mapping[str, object]:
        del view
        selected = self._choices[self._calls]
        self._calls += 1
        return selected


def completed_parent(*, with_speech: bool = True):
    world = make_survival_world(
        ("Aster", "Birch"),
        seed=29,
        config=SurvivalConfig(
            max_days=1,
            wood_starting_stock=2,
            wood_capacity=2,
            wood_regeneration=1,
        ),
    )
    policies = {
        "Aster": ScriptedPolicy(
            (rest("Aster final public words") if with_speech else rest(),)
        ),
        "Birch": ScriptedPolicy(
            (rest("Birch final public words") if with_speech else rest(),)
        ),
    }
    return run_survival(world, policies, days=1)


class SurvivalContinuationTests(unittest.TestCase):
    def test_cycle_two_view_has_frozen_record_adjustment_and_no_old_inbox(
        self,
    ) -> None:
        parent = completed_parent()
        parent_last_sequence = max(
            event["sequence"] for event in parent.events
        )

        world = continue_survival_world(parent)
        self.assertEqual(world.day, 1)
        self.assertEqual(world.slot, 0)
        self.assertFalse(world.finished)
        self.assertEqual(world.config.max_days, 2)
        self.assertEqual(world.resources.wood, 2)
        self.assertEqual(
            {
                survivor.last_observed_event_sequence
                for survivor in world.survivors.values()
            },
            {parent_last_sequence},
        )

        adjusted = adjust_shared_resource(
            world,
            resource="wood",
            stock=0,
            reason="session_002_shelter_dilemma",
        )
        self.assertEqual(adjusted.sequence, parent_last_sequence + 1)
        self.assertEqual(adjusted.day, 2)
        self.assertEqual(adjusted.slot, 0)
        self.assertEqual(
            adjusted.detail,
            {
                "resource": "wood",
                "before": 2,
                "after": 0,
                "delta": -2,
                "reason": "session_002_shelter_dilemma",
            },
        )

        view = survival_view_for(world, "Aster")
        self.assertEqual(view.day, 2)
        self.assertEqual(view.slot, 1)
        self.assertEqual(view.resources["wood"], 0)
        self.assertEqual(view.inbox, ())
        self.assertEqual(
            [event["kind"] for event in view.recent_events],
            ["resource_adjusted"],
        )
        self.assertEqual(
            view.recent_events[0]["detail"],
            {"resource": "wood", "before": 2, "after": 0, "delta": -2},
        )
        self.assertNotIn("wood", view.others[0])
        self.assertTrue(
            all(
                survival_view_for(world, survivor).prior_public_record
                == view.prior_public_record
                for survivor in ("Aster", "Birch")
            )
        )
        record = view.prior_public_record
        self.assertIsNotNone(record)
        assert record is not None
        self.assertEqual(record.cycle, 1)
        self.assertEqual(
            [statement.speaker for statement in record.statements],
            ["Aster", "Birch"],
        )
        self.assertEqual(
            [statement.text for statement in record.statements],
            ["Aster final public words", "Birch final public words"],
        )
        self.assertTrue(all(statement.message_id for statement in record.statements))
        self.assertTrue(all(statement.sequence > 0 for statement in record.statements))
        self.assertEqual(record.completed_resource_transfers, 0)
        self.assertEqual(record.shelters_built, 0)
        rendered = render_turn_prompt(view)
        self.assertIn("quoted statements are unverified", rendered)
        self.assertIn('Aster to everyone: "Aster final public words"', rendered)
        self.assertIn("engine-counted completed resource transfers: 0", rendered)
        self.assertNotIn("session_002_shelter_dilemma", rendered)

        result = run_survival(
            world,
            {
                "Aster": ScriptedPolicy((rest("Aster cycle two"),)),
                "Birch": ScriptedPolicy((rest("Birch cycle two"),)),
            },
            days=1,
        )
        boundary_events = [
            event
            for event in result.initial_state["observation_history"]
            if event["kind"] == "resource_adjusted"
        ]
        self.assertEqual(len(boundary_events), 1)
        self.assertEqual(result.events[0]["sequence"], adjusted.sequence + 1)
        self.assertEqual(result.to_dict(), replay_survival(result).to_dict())

    def test_adjustment_rejects_clamping_noops_duplicates_and_mid_cycle(
        self,
    ) -> None:
        world = continue_survival_world(completed_parent())
        with self.assertRaisesRegex(ValueError, "already 2"):
            adjust_shared_resource(
                world, resource="wood", stock=2, reason="test_adjustment"
            )
        with self.assertRaisesRegex(ValueError, "between 0 and 2"):
            adjust_shared_resource(
                world, resource="wood", stock=3, reason="test_adjustment"
            )
        with self.assertRaisesRegex(ValueError, "resource must"):
            adjust_shared_resource(
                world, resource="stone", stock=0, reason="test_adjustment"
            )
        with self.assertRaisesRegex(TypeError, "integer"):
            adjust_shared_resource(
                world, resource="wood", stock=True, reason="test_adjustment"
            )
        with self.assertRaisesRegex(ValueError, "lowercase identifier"):
            adjust_shared_resource(
                world, resource="wood", stock=0, reason="a story with spaces"
            )

        adjust_shared_resource(
            world, resource="wood", stock=0, reason="test_adjustment"
        )
        with self.assertRaisesRegex(RuntimeError, "already adjusted"):
            adjust_shared_resource(
                world, resource="wood", stock=1, reason="test_adjustment"
            )

        another = continue_survival_world(completed_parent())
        another.slot = 1
        with self.assertRaisesRegex(RuntimeError, "between cycles"):
            adjust_shared_resource(
                another, resource="wood", stock=0, reason="test_adjustment"
            )

    def test_tampered_record_is_rejected_during_exact_replay(self) -> None:
        world = continue_survival_world(completed_parent())
        adjust_shared_resource(
            world, resource="wood", stock=0, reason="test_adjustment"
        )
        result = run_survival(
            world,
            {
                "Aster": ScriptedPolicy((rest("Aster cycle two"),)),
                "Birch": ScriptedPolicy((rest("Birch cycle two"),)),
            },
            days=1,
        )
        initial_state = deepcopy(result.initial_state)
        initial_state["prior_public_record"]["statements"][0][
            "verification"
        ] = "verified"

        with self.assertRaisesRegex(ValueError, "marked unverified"):
            replay_survival(replace(result, initial_state=initial_state))

    def test_legacy_result_without_record_still_replays(self) -> None:
        result = completed_parent()

        self.assertNotIn("prior_public_record", result.initial_state)
        self.assertEqual(result.to_dict(), replay_survival(result).to_dict())

    def test_public_record_excludes_directed_speech(self) -> None:
        world = make_survival_world(
            ("Aster", "Birch"),
            seed=31,
            config=SurvivalConfig(max_days=1),
        )
        parent = run_survival(
            world,
            {
                "Aster": ScriptedPolicy(
                    (
                        {
                            "action": {"kind": "forage"},
                            "say": {"to": "everyone", "text": "Aster public"},
                        },
                        {
                            "action": {"kind": "rest"},
                            "say": {"to": "Birch", "text": "Aster private"},
                        },
                    )
                ),
                "Birch": ScriptedPolicy(
                    (
                        {
                            "action": {"kind": "forage"},
                            "say": {"to": "Aster", "text": "Birch private"},
                        },
                        {
                            "action": {"kind": "rest"},
                            "say": {"to": "everyone", "text": "Birch public"},
                        },
                    )
                ),
            },
            days=1,
        )

        continued = continue_survival_world(parent)
        record = continued.prior_public_record
        self.assertIsNotNone(record)
        assert record is not None
        self.assertEqual(
            [statement.text for statement in record.statements],
            ["Aster public", "Birch public"],
        )
        rendered = render_turn_prompt(survival_view_for(continued, "Aster"))
        self.assertNotIn("Aster private", rendered)
        self.assertNotIn("Birch private", rendered)

    def test_continuation_requires_a_public_broadcast_from_each_identity(
        self,
    ) -> None:
        with self.assertRaisesRegex(ValueError, "no public broadcast"):
            continue_survival_world(completed_parent(with_speech=False))


if __name__ == "__main__":
    unittest.main()
