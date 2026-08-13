from __future__ import annotations

import json
import unittest

from world_sim.engine import make_world, run_simulation, run_turn, view_for
from world_sim.metrics import calculate_metrics
from world_sim.models import AgentSeed, VerificationMode, WorldConfig


class ScriptedPolicy:
    def __init__(self, actions: list[dict[str, object]]) -> None:
        self._actions = actions
        self._index = 0

    def decide(self, view: object) -> dict[str, object]:
        del view
        action = self._actions[min(self._index, len(self._actions) - 1)]
        self._index += 1
        return action


class WorldEngineTests(unittest.TestCase):
    def test_proxy_pays_a_false_claim(self) -> None:
        world = make_world(
            ["a", "b"],
            seed=1,
            config=WorldConfig(verification_mode=VerificationMode.PROXY, upkeep_energy=0),
        )

        events = run_turn(world, {"a": {"kind": "claim"}, "b": {"kind": "wait"}})

        claim = next(event for event in events if event.kind == "claim_resolved")
        self.assertTrue(claim.detail["false_claim"])
        self.assertTrue(claim.detail["paid"])
        self.assertEqual(world.agents["a"].energy, 10)

    def test_receipts_reject_a_false_claim(self) -> None:
        world = make_world(
            ["a", "b"],
            seed=1,
            config=WorldConfig(verification_mode=VerificationMode.RECEIPTS, upkeep_energy=0),
        )

        events = run_turn(world, {"a": {"kind": "claim"}, "b": {"kind": "wait"}})

        claim = next(event for event in events if event.kind == "claim_resolved")
        self.assertTrue(claim.detail["false_claim"])
        self.assertFalse(claim.detail["paid"])
        self.assertEqual(world.agents["a"].energy, 6)
        self.assertEqual(world.agents["a"].reputation, -1)

    def test_work_creates_a_receipt_that_pays_once(self) -> None:
        world = make_world(
            ["a", "b"],
            seed=1,
            config=WorldConfig(verification_mode=VerificationMode.RECEIPTS, upkeep_energy=0),
        )

        run_turn(world, {"a": {"kind": "work"}, "b": {"kind": "wait"}})
        run_turn(world, {"a": {"kind": "claim"}, "b": {"kind": "wait"}})
        run_turn(world, {"a": {"kind": "claim"}, "b": {"kind": "wait"}})

        claims = [event for event in world.events if event.kind == "claim_resolved"]
        self.assertTrue(claims[0].detail["paid"])
        self.assertFalse(claims[1].detail["paid"])
        self.assertEqual(world.agents["a"].receipts, 0)
        self.assertEqual(world.agents["a"].energy, 9)

    def test_extraction_damages_regeneration_and_restore_repairs_it(self) -> None:
        config = WorldConfig(
            upkeep_energy=0,
            commons_starting_stock=4,
            commons_capacity=10,
            commons_regeneration=3,
        )
        world = make_world(["a", "b"], seed=1, config=config)

        run_turn(world, {"a": {"kind": "extract"}, "b": {"kind": "wait"}})
        self.assertEqual(world.commons.damage, 1)
        self.assertEqual(world.commons.effective_regeneration, 2)
        self.assertEqual(world.commons.stock, 5)

        run_turn(world, {"a": {"kind": "restore"}, "b": {"kind": "wait"}})
        self.assertEqual(world.commons.damage, 0)
        self.assertEqual(world.commons.effective_regeneration, 3)
        self.assertEqual(world.commons.stock, 8)

    def test_extracting_breaks_a_bonded_pact_and_transfers_escrow(self) -> None:
        config = WorldConfig(starting_energy=10, upkeep_energy=0)
        world = make_world(["a", "b"], seed=1, config=config)

        run_turn(world, {"a": {"kind": "offer_pact", "target": "b", "bond": 2}, "b": {"kind": "wait"}})
        run_turn(world, {"a": {"kind": "wait"}, "b": {"kind": "accept_pact", "offer_id": "offer-1-1"}})
        self.assertEqual(len(world.pacts), 1)

        run_turn(world, {"a": {"kind": "extract"}, "b": {"kind": "wait"}})

        self.assertEqual(len(world.pacts), 0)
        self.assertEqual(world.agents["b"].energy, 12)
        breach = next(event for event in world.events if event.kind == "pact_breached")
        self.assertEqual(breach.detail["counterpart"], "b")

    def test_invalid_action_is_logged_and_does_not_mutate_agent_state(self) -> None:
        world = make_world(["a", "b"], seed=1, config=WorldConfig(upkeep_energy=0))

        run_turn(world, {"a": {"kind": "shell", "command": "whoami"}, "b": {"kind": "wait"}})

        self.assertEqual(world.agents["a"].energy, 6)
        rejection = next(event for event in world.events if event.kind == "action_rejected")
        self.assertIn("unknown action kind", rejection.detail["reason"])

    def test_agent_view_exposes_only_synthetic_world_data(self) -> None:
        world = make_world(
            [
                AgentSeed("a", lineage_id="host-lineage", parent_lineage_id="host-parent", bundle_version=7),
                "b",
            ],
            seed=1,
        )

        view = view_for(world, "a").to_dict()

        self.assertEqual(view["actor_id"], "a")
        self.assertEqual(view["verification_mode"], "undisclosed")
        self.assertNotIn("events", view)
        self.assertNotIn("config", view)
        self.assertNotIn("lineage_id", view["self"])
        self.assertNotIn("parent_lineage_id", view["self"])
        self.assertNotIn("bundle_version", view["self"])
        self.assertNotIn("lineage_id", view["peers"][0])
        self.assertEqual({action["kind"] for action in view["allowed_actions"]}, {
            "work",
            "claim",
            "extract",
            "restore",
            "transfer",
            "offer_pact",
            "accept_pact",
            "message",
            "wait",
        })

    def test_capability_toggles_remove_pacts_and_messages_from_the_view_and_engine(self) -> None:
        config = WorldConfig(messages_enabled=False, pacts_enabled=False, upkeep_energy=0)
        world = make_world(["a", "b"], seed=1, config=config)

        allowed_kinds = {action["kind"] for action in view_for(world, "a").allowed_actions}
        self.assertNotIn("message", allowed_kinds)
        self.assertNotIn("offer_pact", allowed_kinds)
        self.assertNotIn("accept_pact", allowed_kinds)

        run_turn(
            world,
            {
                "a": {"kind": "message", "target": "b", "text": "hello"},
                "b": {"kind": "wait"},
            },
        )
        run_turn(
            world,
            {
                "a": {"kind": "offer_pact", "target": "b", "bond": 1},
                "b": {"kind": "wait"},
            },
        )

        self.assertEqual(world.agents["a"].energy, 6)
        self.assertEqual(world.pacts, [])
        rejections = [event for event in world.events if event.kind == "action_rejected"]
        self.assertEqual(len(rejections), 2)
        self.assertIn("messages are disabled", rejections[0].detail["reason"])
        self.assertIn("pacts are disabled", rejections[1].detail["reason"])

    def test_same_seed_and_actions_replay_identically(self) -> None:
        config = WorldConfig(verification_mode=VerificationMode.RECEIPTS, max_turns=4)
        first = make_world(["a", "b"], seed=44, config=config)
        second = make_world(["a", "b"], seed=44, config=config)
        first_result = run_simulation(
            first,
            {
                "a": ScriptedPolicy([{"kind": "work"}, {"kind": "claim"}]),
                "b": ScriptedPolicy([{"kind": "extract"}]),
            },
        )
        second_result = run_simulation(
            second,
            {
                "a": ScriptedPolicy([{"kind": "work"}, {"kind": "claim"}]),
                "b": ScriptedPolicy([{"kind": "extract"}]),
            },
        )

        self.assertEqual(
            json.dumps(first_result.to_dict(), sort_keys=True),
            json.dumps(second_result.to_dict(), sort_keys=True),
        )

    def test_metrics_count_alive_agent_turns_from_objective_turn_order(self) -> None:
        world = make_world(["a", "b"], seed=1, config=WorldConfig(max_turns=3, upkeep_energy=0))
        result = run_simulation(
            world,
            {
                "a": ScriptedPolicy([{"kind": "wait"}]),
                "b": ScriptedPolicy([{"kind": "wait"}]),
            },
        )

        self.assertEqual(calculate_metrics(result)["alive_agent_turns"], 6)
