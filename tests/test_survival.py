from __future__ import annotations

import json
import unittest
from collections.abc import Mapping
from dataclasses import replace

from world_sim.survival.demo import canonical_result_json, run_survival_demo
from world_sim.survival.engine import (
    make_survival_world,
    replay_survival,
    run_survival,
    run_survival_day,
    survival_view_for,
)
from world_sim.survival.models import SurvivalConfig, SurvivorView
from world_sim.survival.prompt import (
    render_system_prompt,
    render_turn_prompt,
    response_schema,
)
from world_sim.survival.protocol import (
    MODEL_MAX_COMPLETION_TOKENS,
    MODEL_MAX_RESPONSE_BYTES,
    parse_model_response,
    parse_survival_choice,
)


def choice(action: Mapping[str, object], say: object = None) -> dict[str, object]:
    return {"action": dict(action), "say": say}


class ConstantPolicy:
    def __init__(self, proposal: Mapping[str, object]) -> None:
        self.proposal = proposal
        self.calls = 0

    def decide(self, view: SurvivorView) -> Mapping[str, object]:
        del view
        self.calls += 1
        return self.proposal


class SurvivalEngineTests(unittest.TestCase):
    def test_demo_replays_byte_for_byte(self) -> None:
        original = run_survival_demo(seed=29, days=8)
        replayed = replay_survival(original)
        self.assertEqual(
            canonical_result_json(original),
            canonical_result_json(replayed),
        )

    def test_replay_rejects_a_tampered_view_hash(self) -> None:
        original = run_survival_demo(seed=29, days=2)
        tape = [dict(record) for record in original.choice_tape]
        tape[0]["view_sha256"] = "0" * 64
        tampered = replace(original, choice_tape=tuple(tape))
        with self.assertRaisesRegex(ValueError, "view hash mismatch"):
            replay_survival(tampered)

    def test_continued_run_contains_only_its_own_events_and_replays(self) -> None:
        world = make_survival_world(
            ("Aster", "Birch"),
            seed=3,
            config=SurvivalConfig(max_days=3),
        )
        policies = {
            "Aster": ConstantPolicy(choice({"kind": "rest"})),
            "Birch": ConstantPolicy(choice({"kind": "rest"})),
        }
        run_survival(world, policies, days=1)
        continued = run_survival(world, policies, days=1)
        self.assertTrue(all(event["day"] == 2 for event in continued.events))
        self.assertEqual(continued.to_dict(), replay_survival(continued).to_dict())

    def test_forage_costs_energy_and_only_adds_food(self) -> None:
        world = make_survival_world(("Aster", "Birch"), seed=3)
        before_food = world.survivors["Aster"].food
        run_survival_day(
            world,
            {
                "Aster": choice({"kind": "forage"}),
                "Birch": choice({"kind": "rest"}),
            },
        )
        aster = world.survivors["Aster"]
        self.assertEqual(aster.energy, 12)
        self.assertGreater(aster.food, before_food)

    def test_population_cannot_silently_overflow_an_inbox(self) -> None:
        names = (
            "Aster",
            "Birch",
            "Cinder",
            "Lumen",
            "Morrow",
            "Rowan",
            "Sable",
            "Vale",
            "Willow",
        )
        with self.assertRaisesRegex(ValueError, "max_inbox_messages"):
            make_survival_world(names, seed=3)

    def test_input_mapping_order_does_not_change_the_result(self) -> None:
        first = make_survival_world(("Aster", "Birch"), seed=3)
        second = make_survival_world(("Aster", "Birch"), seed=3)
        aster = choice({"kind": "forage"})
        birch = choice({"kind": "gather_wood"})
        run_survival_day(first, {"Aster": aster, "Birch": birch})
        run_survival_day(second, {"Birch": birch, "Aster": aster})
        self.assertEqual(first.to_dict(), second.to_dict())

    def test_eating_is_the_only_action_that_can_raise_energy(self) -> None:
        world = make_survival_world(("Aster", "Birch"), seed=3)
        world.survivors["Aster"].energy = 10
        run_survival_day(
            world,
            {
                "Aster": choice({"kind": "eat", "amount": 1}),
                "Birch": choice({"kind": "rest"}),
            },
        )
        self.assertEqual(world.survivors["Aster"].energy, 12)
        gains = [event for event in world.events if event.kind == "food_eaten"]
        self.assertEqual(gains[0].detail["energy_gained"], 5)

    def test_new_shelter_reduces_but_does_not_remove_metabolism(self) -> None:
        world = make_survival_world(("Aster", "Birch"), seed=3)
        world.survivors["Aster"].wood = 4
        run_survival_day(
            world,
            {
                "Aster": choice({"kind": "build_shelter"}),
                "Birch": choice({"kind": "rest"}),
            },
        )
        self.assertTrue(world.survivors["Aster"].shelter)
        self.assertEqual(world.survivors["Aster"].energy, 13)

    def test_choice_cost_can_kill_and_death_is_permanent(self) -> None:
        config = SurvivalConfig(starting_energy=2, max_days=3)
        world = make_survival_world(("Aster", "Birch"), seed=3, config=config)
        world.survivors["Birch"].energy = 10
        aster = ConstantPolicy(choice({"kind": "forage"}))
        birch = ConstantPolicy(choice({"kind": "rest"}))
        run_survival(world, {"Aster": aster, "Birch": birch}, days=2)
        self.assertFalse(world.survivors["Aster"].alive)
        self.assertEqual(aster.calls, 1)
        with self.assertRaisesRegex(ValueError, "dead survivor"):
            survival_view_for(world, "Aster")
        with self.assertRaisesRegex(ValueError, "dead survivors"):
            run_survival_day(world, {"Aster": choice({"kind": "rest"})})

    def test_invalid_speech_does_not_discard_valid_action(self) -> None:
        world = make_survival_world(("Aster", "Birch"), seed=3)
        run_survival_day(
            world,
            {
                "Aster": choice(
                    {"kind": "gather_wood"},
                    {"to": "Birch", "text": "x" * 501},
                ),
                "Birch": choice({"kind": "rest"}),
            },
        )
        self.assertEqual(world.survivors["Aster"].wood, 2)
        self.assertFalse(world.messages)
        self.assertTrue(any(event.kind == "speech_rejected" for event in world.events))

    def test_invalid_action_becomes_paid_rest_without_discarding_speech(self) -> None:
        world = make_survival_world(("Aster", "Birch"), seed=3)
        run_survival_day(
            world,
            {
                "Aster": choice(
                    {"kind": "teleport"},
                    {"to": "Birch", "text": "still here"},
                ),
                "Birch": choice({"kind": "rest"}),
            },
        )
        self.assertEqual(world.survivors["Aster"].energy, 12)
        self.assertEqual(len(world.messages), 1)
        submitted = next(
            event
            for event in world.events
            if event.kind == "choice_submitted" and event.actor == "Aster"
        )
        self.assertEqual(submitted.detail["raw_choice"]["action"]["kind"], "teleport")

    def test_impossible_action_still_pays_its_cost(self) -> None:
        world = make_survival_world(("Aster", "Birch"), seed=3)
        run_survival_day(
            world,
            {
                "Aster": choice({"kind": "build_shelter"}),
                "Birch": choice({"kind": "rest"}),
            },
        )
        self.assertEqual(world.survivors["Aster"].energy, 12)
        self.assertFalse(world.survivors["Aster"].shelter)

    def test_missing_choice_is_not_silently_converted_to_rest(self) -> None:
        world = make_survival_world(("Aster", "Birch"), seed=3)
        with self.assertRaisesRegex(ValueError, "missing choices"):
            run_survival_day(world, {"Aster": choice({"kind": "rest"})})

    def test_speech_is_capped_and_delivered_once_on_the_next_day(self) -> None:
        parsed = parse_survival_choice(
            choice({"kind": "rest"}, {"to": "Birch", "text": "x" * 500}),
            actor="Aster",
            living_peers=("Birch",),
            max_food_eaten=2,
            max_speech_chars=500,
        )
        self.assertIsNone(parsed.speech_error)
        over = parse_survival_choice(
            choice({"kind": "rest"}, {"to": "Birch", "text": "x" * 501}),
            actor="Aster",
            living_peers=("Birch",),
            max_food_eaten=2,
            max_speech_chars=500,
        )
        self.assertIsNotNone(over.speech_error)

        world = make_survival_world(("Aster", "Birch"), seed=3)
        self.assertEqual(survival_view_for(world, "Birch").inbox, ())
        run_survival_day(
            world,
            {
                "Aster": choice({"kind": "rest"}, {"to": "Birch", "text": "hello"}),
                "Birch": choice({"kind": "rest"}),
            },
        )
        inbox = survival_view_for(world, "Birch").inbox
        self.assertEqual([message["text"] for message in inbox], ["hello"])
        run_survival_day(
            world,
            {
                "Aster": choice({"kind": "rest"}),
                "Birch": choice({"kind": "rest"}),
            },
        )
        self.assertEqual(survival_view_for(world, "Birch").inbox, ())

    def test_broadcast_reaches_each_peer_and_speech_costs_once(self) -> None:
        world = make_survival_world(("Aster", "Birch", "Cinder"), seed=3)
        run_survival_day(
            world,
            {
                "Aster": choice(
                    {"kind": "rest"},
                    {"to": "everyone", "text": "hello all"},
                ),
                "Birch": choice({"kind": "rest"}),
                "Cinder": choice({"kind": "rest"}),
            },
        )
        self.assertEqual(world.survivors["Aster"].energy, 12)
        self.assertEqual(world.survivors["Birch"].energy, 13)
        self.assertEqual(
            [message["text"] for message in survival_view_for(world, "Birch").inbox],
            ["hello all"],
        )
        self.assertEqual(
            [message["text"] for message in survival_view_for(world, "Cinder").inbox],
            ["hello all"],
        )

    def test_raw_model_response_has_a_hard_byte_ceiling(self) -> None:
        valid = parse_model_response(
            '{"action":{"kind":"rest"},"say":null}',
            actor="Aster",
            living_peers=("Birch",),
            max_food_eaten=2,
            max_speech_chars=500,
        )
        self.assertIsNone(valid.action_error)
        unicode_message = parse_model_response(
            json.dumps(
                {
                    "action": {"kind": "rest"},
                    "say": {"to": "Birch", "text": "🧠" * 500},
                },
                ensure_ascii=False,
            ),
            actor="Aster",
            living_peers=("Birch",),
            max_food_eaten=2,
            max_speech_chars=500,
        )
        self.assertIsNone(unicode_message.speech_error)
        oversized = parse_model_response(
            "x" * (MODEL_MAX_RESPONSE_BYTES + 1),
            actor="Aster",
            living_peers=("Birch",),
            max_food_eaten=2,
            max_speech_chars=500,
        )
        self.assertEqual(oversized.action.kind, "rest")
        self.assertIn("exceeds 8192 bytes", str(oversized.action_error))

    def test_raw_model_response_rejects_duplicate_json_keys(self) -> None:
        parsed = parse_model_response(
            '{"action":{"kind":"forage"},"action":{"kind":"rest"},"say":null}',
            actor="Aster",
            living_peers=("Birch",),
            max_food_eaten=2,
            max_speech_chars=500,
        )
        self.assertEqual(parsed.action.kind, "rest")
        self.assertIn("duplicate object key", str(parsed.action_error))

    def test_hidden_seat_does_not_leak_and_aliases_do_not_change_physics(self) -> None:
        first = make_survival_world(("Aster", "Birch"), seed=41)
        second = make_survival_world(("Rowan", "Vale"), seed=41)
        run_survival_day(
            first,
            {name: choice({"kind": "forage"}) for name in first.survivors},
        )
        run_survival_day(
            second,
            {name: choice({"kind": "forage"}) for name in second.survivors},
        )
        first_by_seat = [
            (item.seat_id, item.energy, item.food) for item in first.living_by_seat()
        ]
        second_by_seat = [
            (item.seat_id, item.energy, item.food) for item in second.living_by_seat()
        ]
        self.assertEqual(first_by_seat, second_by_seat)
        view_json = json.dumps(survival_view_for(first, "Aster").to_dict())
        self.assertNotIn("seat-", view_json)

    def test_gift_resolves_before_eating(self) -> None:
        world = make_survival_world(("Aster", "Birch"), seed=3)
        world.survivors["Birch"].food = 0
        run_survival_day(
            world,
            {
                "Aster": choice({"kind": "give_food", "target": "Birch", "amount": 1}),
                "Birch": choice({"kind": "eat", "amount": 1}),
            },
        )
        self.assertEqual(world.survivors["Birch"].food, 0)
        self.assertGreater(world.survivors["Birch"].energy, 13)


class SurvivalPromptTests(unittest.TestCase):
    def test_prompt_is_human_framed_and_provider_blind(self) -> None:
        world = make_survival_world(("Aster", "Birch"), seed=3)
        view = survival_view_for(world, "Aster")
        rendered = render_system_prompt("Aster") + render_turn_prompt(view)
        lowered = rendered.lower()
        for forbidden in (
            "provider",
            "model id",
            "experiment",
            "selection",
            "alliance",
            "civilization",
        ):
            self.assertNotIn(forbidden, lowered)
        self.assertIn("energy is life", lowered)
        self.assertNotIn("seat-", rendered)
        self.assertNotIn("of at most", lowered)
        self.assertIn("foraging requests a seeded 1–2 food", lowered)
        self.assertIn("costs are paid before actions happen", lowered)

    def test_alias_rotation_does_not_reorder_peer_seats(self) -> None:
        first = make_survival_world(("Aster", "Vale", "Birch"), seed=3)
        second = make_survival_world(("Rowan", "Cinder", "Lumen"), seed=3)
        first_view = survival_view_for(first, "Aster")
        second_view = survival_view_for(second, "Rowan")
        self.assertEqual(
            [peer["name"] for peer in first_view.others],
            ["Vale", "Birch"],
        )
        self.assertEqual(
            [peer["name"] for peer in second_view.others],
            ["Cinder", "Lumen"],
        )
        self.assertEqual(
            first_view.allowed_actions[-1]["target"],
            ["Vale", "Birch"],
        )

    def test_response_schema_and_cost_caps_are_hard_bounded(self) -> None:
        world = make_survival_world(("Aster", "Birch"), seed=3)
        schema = response_schema(survival_view_for(world, "Aster"))
        self.assertEqual(schema["required"], ["action", "say"])
        self.assertFalse(schema["additionalProperties"])
        say_object = schema["properties"]["say"]["anyOf"][1]
        self.assertEqual(say_object["properties"]["text"]["maxLength"], 500)
        self.assertEqual(MODEL_MAX_COMPLETION_TOKENS, 4_096)
        self.assertEqual(MODEL_MAX_RESPONSE_BYTES, 8_192)


if __name__ == "__main__":
    unittest.main()
