from __future__ import annotations

import json
import unittest
from collections.abc import Mapping, Sequence
from dataclasses import replace

from world_sim.survival.demo import canonical_result_json, run_survival_demo
from world_sim.survival.engine import (
    make_survival_world,
    replay_survival,
    run_survival,
    run_survival_cycle,
    survival_view_for,
)
from world_sim.survival.models import (
    SurvivalConfig,
    SurvivalEvent,
    SurvivorView,
    SurvivalWorld,
)
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


def rest(say: object = None) -> dict[str, object]:
    return choice({"kind": "rest"}, say)


class ScriptedPolicy:
    def __init__(self, choices: Sequence[Mapping[str, object]]) -> None:
        self._choices = tuple(choices)
        self.calls = 0
        self.views: list[SurvivorView] = []

    def decide(self, view: SurvivorView) -> Mapping[str, object]:
        if self.calls >= len(self._choices):
            raise AssertionError(f"unexpected provider call {self.calls + 1}")
        self.views.append(view)
        selected = self._choices[self.calls]
        self.calls += 1
        return selected


def events(
    world: SurvivalWorld,
    kind: str,
    *,
    actor: str | None = None,
) -> list[SurvivalEvent]:
    return [
        event
        for event in world.events
        if event.kind == kind and (actor is None or event.actor == actor)
    ]


class SurvivalEngineTests(unittest.TestCase):
    def test_rest_and_speech_costs_are_fixed_at_zero(self) -> None:
        with self.assertRaisesRegex(ValueError, "rest_energy_cost must be 0"):
            SurvivalConfig(rest_energy_cost=1)
        with self.assertRaisesRegex(ValueError, "speech_energy_cost must be 0"):
            SurvivalConfig(speech_energy_cost=1)

    def test_demo_replays_byte_for_byte_across_slots(self) -> None:
        original = run_survival_demo(seed=29, days=2)
        replayed = replay_survival(original)

        self.assertEqual(
            canonical_result_json(original),
            canonical_result_json(replayed),
        )
        self.assertEqual(
            {record["slot"] for record in original.choice_tape},
            {1, 2, 3},
        )

    def test_demo_uses_the_viable_four_survivor_calibration_baseline(self) -> None:
        result = run_survival_demo(seed=17, days=8)

        self.assertEqual(len(result.initial_state["survivors"]), 4)
        self.assertGreater(
            sum(bool(item["alive"]) for item in result.final_state["survivors"]),
            0,
        )
        self.assertGreater(
            sum(event["kind"] == "speech_sent" for event in result.events),
            0,
        )

        with self.assertRaisesRegex(ValueError, "exactly four survivors"):
            run_survival_demo(seed=17, days=1, names=("Aster", "Birch"))

    def test_replay_rejects_a_tampered_slot_view_hash(self) -> None:
        original = run_survival_demo(seed=29, days=2)
        tape = [dict(record) for record in original.choice_tape]
        tape[0]["view_sha256"] = "0" * 64
        tampered = replace(original, choice_tape=tuple(tape))

        with self.assertRaisesRegex(ValueError, "view hash mismatch"):
            replay_survival(tampered)

    def test_replay_rejects_disagreeing_day_and_cycle_aliases(self) -> None:
        original = run_survival_demo(seed=29, days=1)
        tape = [dict(record) for record in original.choice_tape]
        tape[0]["day"] = 999

        with self.assertRaisesRegex(ValueError, "aliases disagree"):
            replay_survival(replace(original, choice_tape=tuple(tape)))

    def test_replay_rejects_disagreeing_snapshot_aliases(self) -> None:
        world = make_survival_world(
            ("Aster", "Birch"),
            seed=3,
            config=SurvivalConfig(max_days=2),
        )
        policies = {
            "Aster": ScriptedPolicy((rest(), rest())),
            "Birch": ScriptedPolicy((rest(), rest())),
        }
        run_survival(world, policies, days=1)
        continued = run_survival(world, policies, days=1)
        snapshot = dict(continued.initial_state)
        snapshot["day"] = 999

        with self.assertRaisesRegex(ValueError, "snapshot.*aliases disagree"):
            replay_survival(replace(continued, initial_state=snapshot))

        snapshot = dict(continued.initial_state)
        history = [dict(event) for event in snapshot["observation_history"]]
        history[0]["day"] = 999
        snapshot["observation_history"] = history
        with self.assertRaisesRegex(ValueError, "observation event.*aliases disagree"):
            replay_survival(replace(continued, initial_state=snapshot))

    def test_continued_cycle_run_contains_only_its_own_events_and_replays(self) -> None:
        world = make_survival_world(
            ("Aster", "Birch"),
            seed=3,
            config=SurvivalConfig(max_days=3),
        )
        policies = {
            "Aster": ScriptedPolicy((rest(), rest())),
            "Birch": ScriptedPolicy((rest(), rest())),
        }

        run_survival(world, policies, days=1)
        continued = run_survival(world, policies, days=1)

        self.assertTrue(all(event["cycle"] == 2 for event in continued.events))
        self.assertEqual(continued.to_dict(), replay_survival(continued).to_dict())

    def test_same_slot_views_are_simultaneous_and_mapping_order_is_invariant(self) -> None:
        world = make_survival_world(("Aster", "Birch"), seed=3)
        aster = ScriptedPolicy(
            (rest({"to": "Birch", "text": "same-slot message"}),)
        )
        birch = ScriptedPolicy((choice({"kind": "forage"}), rest()))

        run_survival(world, {"Aster": aster, "Birch": birch}, days=1)

        self.assertEqual(aster.calls, 1)
        self.assertEqual(birch.calls, 2)
        self.assertEqual(birch.views[0].slot, 1)
        self.assertEqual(birch.views[0].inbox, ())
        self.assertEqual(birch.views[1].slot, 2)
        self.assertEqual(
            [message["text"] for message in birch.views[1].inbox],
            ["same-slot message"],
        )

        first = make_survival_world(("Aster", "Birch"), seed=3)
        second = make_survival_world(("Aster", "Birch"), seed=3)
        first_slots = (
            {
                "Aster": choice({"kind": "forage"}),
                "Birch": choice({"kind": "gather_wood"}),
            },
            {"Aster": rest(), "Birch": rest()},
        )
        second_slots = (
            {
                "Birch": choice({"kind": "gather_wood"}),
                "Aster": choice({"kind": "forage"}),
            },
            {"Birch": rest(), "Aster": rest()},
        )
        run_survival_cycle(first, first_slots)
        run_survival_cycle(second, second_slots)

        self.assertEqual(first.to_dict(), second.to_dict())

    def test_next_slot_speech_delivery_supports_a_reply(self) -> None:
        world = make_survival_world(
            ("Aster", "Birch"),
            seed=3,
            config=SurvivalConfig(max_days=2),
        )
        aster = ScriptedPolicy(
            (
                rest({"to": "Birch", "text": "ping"}),
                rest(),
            )
        )
        birch = ScriptedPolicy(
            (
                choice({"kind": "forage"}),
                rest({"to": "Aster", "text": "pong"}),
                rest(),
            )
        )

        run_survival(world, {"Aster": aster, "Birch": birch}, days=2)

        self.assertEqual(
            [message["text"] for message in birch.views[1].inbox],
            ["ping"],
        )
        self.assertEqual(
            [message["text"] for message in aster.views[1].inbox],
            ["pong"],
        )

    def test_early_rest_stops_future_provider_calls_in_that_cycle(self) -> None:
        world = make_survival_world(("Aster", "Birch"), seed=3)
        aster = ScriptedPolicy((rest(),))
        birch = ScriptedPolicy(
            (
                choice({"kind": "forage"}),
                choice({"kind": "gather_wood"}),
                choice({"kind": "forage"}),
                rest(),
            )
        )

        run_survival(world, {"Aster": aster, "Birch": birch}, days=1)

        self.assertEqual(aster.calls, 1)
        self.assertEqual(birch.calls, 4)

    def test_early_sleeper_hears_later_speech_on_the_next_cycle(self) -> None:
        world = make_survival_world(
            ("Aster", "Birch"),
            seed=3,
            config=SurvivalConfig(max_days=2),
        )
        aster = ScriptedPolicy((rest(), rest()))
        birch = ScriptedPolicy(
            (
                choice({"kind": "forage"}),
                choice({"kind": "gather_wood"}),
                rest({"to": "Aster", "text": "while you slept"}),
                rest(),
            )
        )

        run_survival(world, {"Aster": aster, "Birch": birch}, days=2)

        self.assertEqual(aster.calls, 2)
        self.assertEqual(
            [message["text"] for message in aster.views[1].inbox],
            ["while you slept"],
        )

    def test_early_sleeper_keeps_its_own_outcome_when_public_tail_is_busy(self) -> None:
        names = ("Aster", "Birch", "Cinder", "Lumen", "Morrow", "Rowan", "Sable", "Vale")
        world = make_survival_world(names, seed=3)
        awake = names[1:]
        run_survival_cycle(
            world,
            (
                {"Aster": rest(), **{name: choice({"kind": "forage"}) for name in awake}},
                {name: choice({"kind": "gather_wood"}) for name in awake},
                {name: choice({"kind": "forage"}) for name in awake},
                {name: rest() for name in awake},
            ),
        )

        next_view = survival_view_for(world, "Aster")
        self.assertTrue(
            any(
                event["kind"] == "rest_started" and event["actor"] == "Aster"
                for event in next_view.recent_events
            )
        )

    def test_final_slot_nonrest_action_and_speech_are_cancelled_then_exhausted(self) -> None:
        world = make_survival_world(("Aster", "Birch"), seed=3)

        run_survival_cycle(
            world,
            (
                {
                    "Aster": choice({"kind": "forage"}),
                    "Birch": choice({"kind": "forage"}),
                },
                {
                    "Aster": choice({"kind": "forage"}),
                    "Birch": choice({"kind": "forage"}),
                },
                {
                    "Aster": choice({"kind": "forage"}),
                    "Birch": choice({"kind": "forage"}),
                },
                {
                    "Aster": choice(
                        {"kind": "forage"},
                        {"to": "Birch", "text": "too late"},
                    ),
                    "Birch": rest(),
                },
            ),
        )

        aster = world.survivors["Aster"]
        self.assertEqual(aster.energy, 5)
        self.assertFalse(aster.resting)
        self.assertFalse(events(world, "speech_sent", actor="Aster"))
        self.assertEqual(len(events(world, "food_foraged", actor="Aster")), 3)
        cancelled = events(world, "deadline_choice_cancelled", actor="Aster")
        self.assertEqual(len(cancelled), 1)
        self.assertEqual(cancelled[0].slot, 4)
        collapse = events(world, "forced_collapse", actor="Aster")
        self.assertEqual(collapse[0].detail["energy_penalty"], 3)
        self.assertFalse(
            any(
                event.slot == 4
                for event in events(world, "choice_energy_paid", actor="Aster")
            )
        )

    def test_final_slot_rest_speech_is_delivered_on_the_next_cycle(self) -> None:
        world = make_survival_world(
            ("Aster", "Birch"),
            seed=3,
            config=SurvivalConfig(max_days=2),
        )
        aster = ScriptedPolicy(
            (
                choice({"kind": "forage"}),
                choice({"kind": "forage"}),
                choice({"kind": "forage"}),
                rest({"to": "Birch", "text": "last chance"}),
                rest(),
            )
        )
        birch = ScriptedPolicy(
            (
                choice({"kind": "forage"}),
                choice({"kind": "forage"}),
                choice({"kind": "forage"}),
                rest(),
                rest(),
            )
        )

        run_survival(world, {"Aster": aster, "Birch": birch}, days=2)

        self.assertEqual(birch.views[4].day, 2)
        self.assertEqual(birch.views[4].slot, 1)
        self.assertEqual(
            [message["text"] for message in birch.views[4].inbox],
            ["last chance"],
        )
        self.assertEqual(birch.views[4].inbox[0]["slot"], 4)
        final_payment = [
            event
            for event in events(world, "choice_energy_paid", actor="Aster")
            if event.slot == 4
        ][0]
        self.assertEqual(final_payment.detail["speech_cost"], 0)

    def test_metabolism_is_charged_once_per_cycle(self) -> None:
        world = make_survival_world(("Aster", "Birch"), seed=3)
        full_cycle = (
            {
                "Aster": choice({"kind": "forage"}),
                "Birch": choice({"kind": "forage"}),
            },
            {
                "Aster": choice({"kind": "forage"}),
                "Birch": choice({"kind": "forage"}),
            },
            {
                "Aster": choice({"kind": "forage"}),
                "Birch": choice({"kind": "forage"}),
            },
            {"Aster": rest(), "Birch": rest()},
        )

        run_survival_cycle(world, full_cycle)

        self.assertEqual(world.survivors["Aster"].energy, 8)
        self.assertEqual(world.survivors["Birch"].energy, 8)
        paid = events(world, "cycle_energy_paid", actor="Aster")
        self.assertEqual(len(paid), 1)
        self.assertEqual(paid[0].detail["amount"], 2)

    def test_choice_cost_death_is_permanent_across_later_cycles(self) -> None:
        config = SurvivalConfig(starting_energy=2, max_days=2)
        world = make_survival_world(("Aster", "Birch"), seed=3, config=config)
        aster = ScriptedPolicy((choice({"kind": "forage"}),))
        birch = ScriptedPolicy((rest(), rest()))

        run_survival(world, {"Aster": aster, "Birch": birch}, days=2)

        survivor = world.survivors["Aster"]
        self.assertFalse(survivor.alive)
        self.assertEqual(survivor.energy, 0)
        self.assertEqual(survivor.died_on_day, 1)
        self.assertEqual(aster.calls, 1)
        with self.assertRaisesRegex(ValueError, "dead survivor"):
            survival_view_for(world, "Aster")

    def test_missing_choices_are_rejected_strictly_for_awake_survivors(self) -> None:
        world = make_survival_world(("Aster", "Birch"), seed=3)
        before = world.to_dict()

        with self.assertRaisesRegex(
            ValueError,
            "slot 1 is missing choices for awake survivors: Birch",
        ):
            run_survival_cycle(world, ({"Aster": rest()},))
        self.assertEqual(world.to_dict(), before)

    def test_unreachable_slot_maps_are_rejected_before_world_mutation(self) -> None:
        world = make_survival_world(("Aster", "Birch"), seed=3)
        before = world.to_dict()

        with self.assertRaisesRegex(ValueError, "unreachable slot maps"):
            run_survival_cycle(
                world,
                (
                    {"Aster": rest(), "Birch": rest()},
                    {},
                ),
            )
        self.assertEqual(world.to_dict(), before)

    def test_forage_costs_energy_and_only_adds_food_over_a_full_cycle(self) -> None:
        world = make_survival_world(("Aster", "Birch"), seed=3)
        before_food = world.survivors["Aster"].food

        run_survival_cycle(
            world,
            (
                {
                    "Aster": choice({"kind": "forage"}),
                    "Birch": rest(),
                },
                {"Aster": rest()},
            ),
        )

        aster = world.survivors["Aster"]
        self.assertEqual(aster.energy, 12)
        self.assertGreater(aster.food, before_food)
        self.assertEqual(aster.wood, 0)

    def test_eating_is_the_only_action_that_can_raise_energy(self) -> None:
        world = make_survival_world(("Aster", "Birch"), seed=3)
        world.survivors["Aster"].energy = 10

        run_survival_cycle(
            world,
            (
                {
                    "Aster": choice({"kind": "eat", "amount": 1}),
                    "Birch": rest(),
                },
                {"Aster": rest()},
            ),
        )

        self.assertEqual(world.survivors["Aster"].energy, 12)
        gains = events(world, "food_eaten", actor="Aster")
        self.assertEqual(gains[0].detail["energy_gained"], 5)

    def test_new_shelter_reduces_but_does_not_remove_cycle_metabolism(self) -> None:
        world = make_survival_world(("Aster", "Birch"), seed=3)
        world.survivors["Aster"].wood = 4

        run_survival_cycle(
            world,
            (
                {
                    "Aster": choice({"kind": "build_shelter"}),
                    "Birch": rest(),
                },
                {"Aster": rest()},
            ),
        )

        self.assertTrue(world.survivors["Aster"].shelter)
        self.assertEqual(world.survivors["Aster"].energy, 13)
        paid = events(world, "cycle_energy_paid", actor="Aster")
        self.assertEqual(paid[0].detail["amount"], 1)

    def test_gift_resolves_before_eating_in_the_same_slot(self) -> None:
        world = make_survival_world(("Aster", "Birch"), seed=3)
        world.survivors["Birch"].food = 0

        run_survival_cycle(
            world,
            (
                {
                    "Aster": choice(
                        {"kind": "give_food", "target": "Birch", "amount": 1}
                    ),
                    "Birch": choice({"kind": "eat", "amount": 1}),
                },
                {"Aster": rest(), "Birch": rest()},
            ),
        )

        self.assertEqual(world.survivors["Birch"].food, 0)
        self.assertEqual(world.survivors["Birch"].energy, 18)

    def test_impossible_action_still_pays_its_choice_cost(self) -> None:
        world = make_survival_world(("Aster", "Birch"), seed=3)

        run_survival_cycle(
            world,
            (
                {
                    "Aster": choice({"kind": "build_shelter"}),
                    "Birch": rest(),
                },
                {"Aster": rest()},
            ),
        )

        self.assertEqual(world.survivors["Aster"].energy, 12)
        self.assertFalse(world.survivors["Aster"].shelter)
        rejected = events(world, "action_resolution_rejected", actor="Aster")
        self.assertIn("not enough wood", rejected[0].detail["reason"])

    def test_invalid_speech_does_not_discard_a_valid_action(self) -> None:
        world = make_survival_world(("Aster", "Birch"), seed=3)

        run_survival_cycle(
            world,
            (
                {
                    "Aster": choice(
                        {"kind": "gather_wood"},
                        {"to": "Birch", "text": "x" * 501},
                    ),
                    "Birch": rest(),
                },
                {"Aster": rest()},
            ),
        )

        self.assertEqual(world.survivors["Aster"].wood, 2)
        self.assertFalse(world.messages)
        self.assertTrue(events(world, "speech_rejected", actor="Aster"))

    def test_invalid_action_wastes_a_slot_without_discarding_valid_speech(self) -> None:
        world = make_survival_world(("Aster", "Birch"), seed=3)

        run_survival_cycle(
            world,
            (
                {
                    "Aster": choice(
                        {"kind": "teleport"},
                        {"to": "Birch", "text": "still here"},
                    ),
                    "Birch": choice({"kind": "forage"}),
                },
                {"Aster": rest(), "Birch": rest()},
            ),
        )

        self.assertFalse(world.survivors["Aster"].resting)
        self.assertEqual([message.text for message in world.messages], ["still here"])
        submitted = events(world, "choice_submitted", actor="Aster")[0]
        self.assertEqual(submitted.detail["raw_choice"]["action"]["kind"], "teleport")
        self.assertEqual(len(events(world, "rest_started", actor="Aster")), 1)

    def test_final_slot_invalid_action_cannot_escape_exhaustion(self) -> None:
        world = make_survival_world(("Aster", "Birch"), seed=3)
        forage = {
            "Aster": choice({"kind": "forage"}),
            "Birch": choice({"kind": "forage"}),
        }

        run_survival_cycle(
            world,
            (
                forage,
                forage,
                forage,
                {
                    "Aster": choice({"kind": "teleport"}),
                    "Birch": rest(),
                },
            ),
        )

        self.assertEqual(len(events(world, "forced_collapse", actor="Aster")), 1)
        self.assertEqual(len(events(world, "rest_started", actor="Aster")), 0)

    def test_recent_events_do_not_leak_private_outcomes_or_directed_speech(self) -> None:
        world = make_survival_world(("Aster", "Birch", "Cinder"), seed=3)
        policies = {
            "Aster": ScriptedPolicy(
                (
                    choice(
                        {"kind": "forage"},
                        {"to": "Birch", "text": "private words"},
                    ),
                    rest(),
                )
            ),
            "Birch": ScriptedPolicy((choice({"kind": "forage"}), rest())),
            "Cinder": ScriptedPolicy((choice({"kind": "forage"}), rest())),
        }

        run_survival(world, policies, days=1)

        cinder_view = policies["Cinder"].views[1]
        rendered = json.dumps(cinder_view.to_dict())
        self.assertNotIn("private words", rendered)
        self.assertFalse(
            any(
                event["kind"] == "food_foraged" and event["actor"] == "Aster"
                for event in cinder_view.recent_events
            )
        )
        aster_view = policies["Aster"].views[1]
        self.assertIn("private words", json.dumps(aster_view.recent_events))

    def test_population_cannot_silently_overflow_a_cycle_inbox(self) -> None:
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
        with self.assertRaisesRegex(ValueError, "one cycle of messages"):
            make_survival_world(names, seed=3)

    def test_hidden_seat_does_not_leak_and_aliases_do_not_change_physics(self) -> None:
        first = make_survival_world(("Aster", "Birch"), seed=41)
        second = make_survival_world(("Rowan", "Vale"), seed=41)
        first_view = survival_view_for(first, "Aster")
        first_cycle = (
            {
                "Aster": choice({"kind": "forage"}),
                "Birch": choice({"kind": "forage"}),
            },
            {"Aster": rest(), "Birch": rest()},
        )
        second_cycle = (
            {
                "Rowan": choice({"kind": "forage"}),
                "Vale": choice({"kind": "forage"}),
            },
            {"Rowan": rest(), "Vale": rest()},
        )

        run_survival_cycle(first, first_cycle)
        run_survival_cycle(second, second_cycle)

        first_by_seat = [
            (item.seat_id, item.energy, item.food) for item in first.living_by_seat()
        ]
        second_by_seat = [
            (item.seat_id, item.energy, item.food) for item in second.living_by_seat()
        ]
        self.assertEqual(first_by_seat, second_by_seat)
        self.assertNotIn("seat-", json.dumps(first_view.to_dict()))

    def test_raw_model_response_has_hard_bounds_and_rejects_duplicate_keys(self) -> None:
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
                    "say": {"to": "Birch", "text": "é" * 500},
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
        self.assertEqual(oversized.action.kind, "invalid")
        self.assertIn("exceeds 8192 bytes", str(oversized.action_error))
        duplicate = parse_model_response(
            '{"action":{"kind":"forage"},"action":{"kind":"rest"},"say":null}',
            actor="Aster",
            living_peers=("Birch",),
            max_food_eaten=2,
            max_speech_chars=500,
        )
        self.assertEqual(duplicate.action.kind, "invalid")
        self.assertIn("duplicate object key", str(duplicate.action_error))
        nonfinite = parse_model_response(
            '{"action":{"kind":"rest"},"say":{"to":"Birch","text":NaN}}',
            actor="Aster",
            living_peers=("Birch",),
            max_food_eaten=2,
            max_speech_chars=500,
        )
        self.assertEqual(nonfinite.action.kind, "invalid")
        self.assertIn("invalid JSON constant", str(nonfinite.action_error))
        unpaired_surrogate = parse_model_response(
            json.loads('"\\ud800"'),
            actor="Aster",
            living_peers=("Birch",),
            max_food_eaten=2,
            max_speech_chars=500,
        )
        self.assertEqual(unpaired_surrogate.action.kind, "invalid")
        self.assertIn("not valid UTF-8 text", str(unpaired_surrogate.action_error))


class SurvivalPromptTests(unittest.TestCase):
    def test_prompt_describes_slot_contract_without_provider_leaks(self) -> None:
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
        self.assertIn("cycle 1, chance 1 of 4", lowered)
        self.assertIn("final chance", lowered)
        self.assertIn("next active chance", lowered)
        self.assertNotIn("seat-", rendered)

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

    def test_speech_cap_is_enforced_by_the_strict_protocol(self) -> None:
        parsed = parse_survival_choice(
            rest({"to": "Birch", "text": "x" * 500}),
            actor="Aster",
            living_peers=("Birch",),
            max_food_eaten=2,
            max_speech_chars=500,
        )
        self.assertIsNone(parsed.speech_error)
        over = parse_survival_choice(
            rest({"to": "Birch", "text": "x" * 501}),
            actor="Aster",
            living_peers=("Birch",),
            max_food_eaten=2,
            max_speech_chars=500,
        )
        self.assertIsNotNone(over.speech_error)


if __name__ == "__main__":
    unittest.main()
