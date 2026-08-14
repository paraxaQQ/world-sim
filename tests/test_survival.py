from __future__ import annotations

import hashlib
import json
import unittest
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import replace

from world_sim.survival.demo import (
    canonical_result_json,
    run_survival_demo,
    survival_metrics,
)
from world_sim.survival.engine import (
    continue_survival_world,
    make_survival_world,
    replay_survival,
    run_survival,
    run_survival_cycle,
    survival_view_for,
)
from world_sim.survival.models import (
    GLOBAL_BEATS_V2,
    SEQUENTIAL_DIALOGUE_V3,
    SLOTS_V1,
    SurvivalConfig,
    SurvivalEvent,
    SurvivalResult,
    SurvivalWorld,
    SurvivorView,
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


def wait(say: object = None) -> dict[str, object]:
    return choice({"kind": "wait"}, say)


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
    def test_legacy_protocol_results_remain_byte_stable(self) -> None:
        expected = {
            SLOTS_V1: "e89c8a44ae551bd555cfa990abbed669d5746d6b01cdcb53dc4c4eae3085187e",
            GLOBAL_BEATS_V2: "5f0e7ab7c7ccf960a865c118d947f7516feeb6df70fffbbd85aea46137bb3d7e",
        }
        for interaction_protocol, expected_sha256 in expected.items():
            with self.subTest(interaction_protocol=interaction_protocol):
                world = make_survival_world(
                    ("Aster", "Birch"),
                    seed=31,
                    config=SurvivalConfig(max_days=1),
                    interaction_protocol=interaction_protocol,
                )
                result = run_survival(
                    world,
                    {
                        "Aster": ScriptedPolicy(
                            (choice({"kind": "forage"}), rest())
                        ),
                        "Birch": ScriptedPolicy(
                            (choice({"kind": "gather_wood"}), rest())
                        ),
                    },
                    days=1,
                )

                digest = hashlib.sha256(
                    canonical_result_json(result).encode("utf-8")
                ).hexdigest()
                self.assertEqual(digest, expected_sha256)

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

    def test_replay_rejects_json_numeric_type_aliases(self) -> None:
        original = run_survival_demo(seed=29, days=1)
        for seed in (29.0, "29"):
            with self.subTest(initial_seed=seed):
                initial_state = dict(original.initial_state)
                initial_state["seed"] = seed
                with self.assertRaisesRegex(
                    ValueError,
                    "initial state does not match its reconstructed JSON types",
                ):
                    replay_survival(
                        replace(original, initial_state=initial_state)
                    )

        for field, convert in (("energy", float), ("alive", int)):
            with self.subTest(field=field):
                final_state = dict(original.final_state)
                survivors = [
                    dict(survivor) for survivor in final_state["survivors"]
                ]
                survivors[0][field] = convert(survivors[0][field])
                final_state["survivors"] = survivors
                with self.assertRaisesRegex(
                    ValueError,
                    "choice tape replay does not match",
                ):
                    replay_survival(replace(original, final_state=final_state))

        events = [dict(event) for event in original.events]
        events[0]["sequence"] = float(events[0]["sequence"])
        with self.assertRaisesRegex(
            ValueError,
            "choice tape replay does not match",
        ):
            replay_survival(replace(original, events=tuple(events)))

    def test_replay_rejects_disagreeing_day_and_cycle_aliases(self) -> None:
        original = run_survival_demo(seed=29, days=1)
        tape = [dict(record) for record in original.choice_tape]
        tape[0]["day"] = 999

        with self.assertRaisesRegex(ValueError, "aliases disagree"):
            replay_survival(replace(original, choice_tape=tuple(tape)))

        for cycle in (True, 1.0, "1"):
            with self.subTest(cycle=cycle):
                tape = [dict(record) for record in original.choice_tape]
                tape[0]["day"] = cycle
                tape[0]["cycle"] = cycle
                with self.assertRaisesRegex(
                    ValueError,
                    "choice tape day must be an integer",
                ):
                    replay_survival(
                        replace(original, choice_tape=tuple(tape))
                    )

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

    def test_legacy_snapshot_omits_protocol_and_retains_legacy_actions(self) -> None:
        world = make_survival_world(
            ("Aster", "Birch"),
            seed=3,
            config=SurvivalConfig(max_days=1),
            interaction_protocol=SLOTS_V1,
        )
        view = survival_view_for(world, "Aster")

        self.assertEqual(view.interaction_protocol, SLOTS_V1)
        self.assertNotIn("interaction_protocol", world.to_dict())
        self.assertNotIn("interaction_protocol", view.to_dict())
        self.assertNotIn("wait", view.rules["action_energy_costs"])
        self.assertFalse(
            any(action["kind"] == "wait" for action in view.allowed_actions)
        )
        parsed = parse_survival_choice(
            wait(),
            actor="Aster",
            living_peers=("Birch",),
            max_food_eaten=2,
            max_speech_chars=500,
            interaction_protocol=SLOTS_V1,
        )
        self.assertEqual(parsed.action.kind, "invalid")

        result = run_survival(
            world,
            {
                "Aster": ScriptedPolicy((rest(),)),
                "Birch": ScriptedPolicy((rest(),)),
            },
            days=1,
        )
        self.assertNotIn("interaction_protocol", result.initial_state)
        self.assertNotIn("interaction_protocol", result.final_state)
        self.assertEqual(result.to_dict(), replay_survival(result).to_dict())

    def test_continuation_preserves_or_explicitly_upgrades_protocol(self) -> None:
        world = make_survival_world(
            ("Aster", "Birch"),
            seed=3,
            config=SurvivalConfig(max_days=1),
            interaction_protocol=SLOTS_V1,
        )
        parent = run_survival(
            world,
            {
                "Aster": ScriptedPolicy(
                    (rest({"to": "everyone", "text": "Aster final"}),)
                ),
                "Birch": ScriptedPolicy(
                    (rest({"to": "everyone", "text": "Birch final"}),)
                ),
            },
            days=1,
        )

        preserved = continue_survival_world(parent)
        upgraded = continue_survival_world(
            parent,
            interaction_protocol=GLOBAL_BEATS_V2,
        )

        self.assertEqual(preserved.interaction_protocol, SLOTS_V1)
        self.assertNotIn("interaction_protocol", preserved.to_dict())
        self.assertEqual(upgraded.interaction_protocol, GLOBAL_BEATS_V2)
        self.assertEqual(
            upgraded.to_dict()["interaction_protocol"],
            GLOBAL_BEATS_V2,
        )
        self.assertTrue(
            any(
                action["kind"] == "wait"
                for action in survival_view_for(upgraded, "Aster").allowed_actions
            )
        )

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

    def test_sequential_dialogue_exposes_speech_but_seals_physical_changes(
        self,
    ) -> None:
        world = make_survival_world(
            ("Aster", "Birch"),
            seed=3,
            config=SurvivalConfig(max_days=1),
            interaction_protocol=SEQUENTIAL_DIALOGUE_V3,
        )
        aster = ScriptedPolicy(
            (
                choice(
                    {"kind": "forage"},
                    {"to": "Birch", "text": "food moved"},
                ),
                rest(),
            )
        )
        birch = ScriptedPolicy((rest(),))

        result = run_survival(
            world,
            {"Aster": aster, "Birch": birch},
            days=1,
        )

        birch_view = birch.views[0]
        self.assertEqual(birch_view.resources["food"], 16)
        self.assertEqual(birch_view.others[0]["energy"], 16)
        self.assertEqual(
            [message["text"] for message in birch_view.inbox],
            ["food moved"],
        )
        self.assertNotIn("food", birch_view.others[0])
        self.assertFalse(
            any(
                event["kind"] == "food_foraged"
                and event["actor"] == "Aster"
                for event in birch_view.recent_events
            )
        )
        self.assertEqual(len(events(world, "food_foraged", actor="Aster")), 1)
        self.assertEqual(result.to_dict(), replay_survival(result).to_dict())

    def test_sequential_dialogue_supports_a_same_beat_reply(self) -> None:
        class ReplyPolicy:
            def __init__(self) -> None:
                self.views: list[SurvivorView] = []

            def decide(self, view: SurvivorView) -> Mapping[str, object]:
                self.views.append(view)
                if len(self.views) == 1:
                    if [message["text"] for message in view.inbox] != ["ping"]:
                        raise AssertionError("Birch did not hear the same-beat ping")
                    return wait({"to": "Aster", "text": "pong"})
                return rest()

        world = make_survival_world(
            ("Aster", "Birch"),
            seed=3,
            config=SurvivalConfig(max_days=1),
            interaction_protocol=SEQUENTIAL_DIALOGUE_V3,
        )
        aster = ScriptedPolicy(
            (wait({"to": "Birch", "text": "ping"}), rest())
        )
        birch = ReplyPolicy()

        result = run_survival(
            world,
            {"Aster": aster, "Birch": birch},
            days=1,
        )

        self.assertEqual(
            [message["text"] for message in birch.views[0].inbox],
            ["ping"],
        )
        self.assertEqual(
            [message["text"] for message in aster.views[1].inbox],
            ["pong"],
        )
        first_beat_messages = [
            message.text for message in world.messages if message.slot == 1
        ]
        self.assertEqual(first_beat_messages, ["ping", "pong"])
        self.assertEqual(result.to_dict(), replay_survival(result).to_dict())

    def test_sequential_dialogue_can_trust_a_gift_before_atomic_build(self) -> None:
        class TrustingBuilder:
            def __init__(self) -> None:
                self.views: list[SurvivorView] = []

            def decide(self, view: SurvivorView) -> Mapping[str, object]:
                self.views.append(view)
                if len(self.views) == 1:
                    heard = [message["text"] for message in view.inbox]
                    if heard != ["sending two wood"]:
                        raise AssertionError("Birch did not hear the gift claim")
                    return choice({"kind": "build_shelter"})
                return rest()

        world = make_survival_world(
            ("Aster", "Birch"),
            seed=3,
            config=SurvivalConfig(max_days=1),
            interaction_protocol=SEQUENTIAL_DIALOGUE_V3,
        )
        world.survivors["Aster"].wood = 2
        world.survivors["Birch"].wood = 2
        aster = ScriptedPolicy(
            (
                choice(
                    {
                        "kind": "give_wood",
                        "target": "Birch",
                        "amount": 2,
                    },
                    {"to": "Birch", "text": "sending two wood"},
                ),
                rest(),
            )
        )
        birch = TrustingBuilder()

        run_survival(
            world,
            {"Aster": aster, "Birch": birch},
            days=1,
        )

        self.assertEqual(birch.views[0].self_state["wood"], 2)
        self.assertTrue(world.survivors["Birch"].shelter)
        self.assertEqual(world.survivors["Aster"].wood, 0)
        self.assertEqual(world.survivors["Birch"].wood, 0)
        gift = events(world, "resource_given", actor="Aster")[0]
        build = events(world, "shelter_built", actor="Birch")[0]
        self.assertLess(gift.sequence, build.sequence)

    def test_silent_dialogue_matches_global_physics_across_seeds_and_seats(
        self,
    ) -> None:
        names = ("Aster", "Birch", "Cinder", "Lumen")
        rotations = tuple(
            names[offset:] + names[:offset] for offset in range(len(names))
        )
        scripts = {
            "Aster": (
                choice({"kind": "gather_wood"}),
                choice(
                    {
                        "kind": "give_wood",
                        "target": "Birch",
                        "amount": 2,
                    }
                ),
                wait(),
                rest(),
            ),
            "Birch": (
                choice({"kind": "gather_wood"}),
                choice({"kind": "build_shelter"}),
                wait(),
                rest(),
            ),
            "Cinder": (
                choice({"kind": "forage"}),
                choice({"kind": "eat", "amount": 1}),
                wait(),
                rest(),
            ),
            "Lumen": (
                choice({"kind": "forage"}),
                choice({"kind": "eat", "amount": 1}),
                wait(),
                rest(),
            ),
        }
        nonphysical = {
            "cycle_started",
            "slot_started",
            "choice_submitted",
            "choice_recorded",
            "speech_sent",
        }

        def physical_state(result: SurvivalResult) -> dict[str, object]:
            final_state = result.final_state
            return {
                "day": final_state["day"],
                "slot": final_state["slot"],
                "resources": final_state["resources"],
                "finished_reason": final_state["finished_reason"],
                "survivors": [
                    {
                        key: survivor[key]
                        for key in (
                            "seat_id",
                            "name",
                            "energy",
                            "food",
                            "wood",
                            "shelter",
                            "resting",
                            "alive",
                            "died_on_day",
                        )
                    }
                    for survivor in final_state["survivors"]
                ],
            }

        def objective_events(result: SurvivalResult) -> Counter[str]:
            return Counter(
                json.dumps(
                    {
                        "day": event["day"],
                        "slot": event["slot"],
                        "kind": event["kind"],
                        "actor": event["actor"],
                        "detail": event["detail"],
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                )
                for event in result.events
                if event["kind"] not in nonphysical
            )

        for seed in (3, 17, 31):
            for rotation in rotations:
                with self.subTest(seed=seed, rotation=rotation):
                    config = SurvivalConfig(max_days=1)
                    global_world = make_survival_world(
                        rotation,
                        seed=seed,
                        config=config,
                        interaction_protocol=GLOBAL_BEATS_V2,
                    )
                    dialogue_world = make_survival_world(
                        rotation,
                        seed=seed,
                        config=config,
                        interaction_protocol=SEQUENTIAL_DIALOGUE_V3,
                    )
                    global_result = run_survival(
                        global_world,
                        {
                            name: ScriptedPolicy(scripts[name])
                            for name in names
                        },
                        days=1,
                    )
                    dialogue_result = run_survival(
                        dialogue_world,
                        {
                            name: ScriptedPolicy(scripts[name])
                            for name in names
                        },
                        days=1,
                    )

                    self.assertEqual(
                        physical_state(dialogue_result),
                        physical_state(global_result),
                    )
                    self.assertEqual(
                        objective_events(dialogue_result),
                        objective_events(global_result),
                    )

    def test_dialogue_initiative_rotates_and_is_bound_into_replay(self) -> None:
        names = ("Aster", "Birch", "Cinder", "Lumen")
        world = make_survival_world(
            names,
            seed=3,
            config=SurvivalConfig(max_days=2),
            interaction_protocol=SEQUENTIAL_DIALOGUE_V3,
        )
        policies = {
            name: ScriptedPolicy((wait(), wait(), wait(), rest()) * 2)
            for name in names
        }

        result = run_survival(world, policies, days=2)

        slot_orders = [
            event["detail"]["initiative_order"]
            for event in result.events
            if event["kind"] == "slot_started"
        ]
        self.assertEqual(
            slot_orders,
            [
                ["Aster", "Birch", "Cinder", "Lumen"],
                ["Birch", "Cinder", "Lumen", "Aster"],
                ["Cinder", "Lumen", "Aster", "Birch"],
                ["Lumen", "Aster", "Birch", "Cinder"],
                ["Birch", "Cinder", "Lumen", "Aster"],
                ["Cinder", "Lumen", "Aster", "Birch"],
                ["Lumen", "Aster", "Birch", "Cinder"],
                ["Aster", "Birch", "Cinder", "Lumen"],
            ],
        )
        first_slot = [
            record
            for record in result.choice_tape
            if record["cycle"] == 1 and record["slot"] == 1
        ]
        self.assertEqual(
            [record["initiative_position"] for record in first_slot],
            [1, 2, 3, 4],
        )
        self.assertTrue(
            all(record["initiative_order"] == slot_orders[0] for record in first_slot)
        )
        self.assertEqual(result.to_dict(), replay_survival(result).to_dict())

        tape = [dict(record) for record in result.choice_tape]
        tape[0]["initiative_position"] = 2
        with self.assertRaisesRegex(ValueError, "initiative position mismatch"):
            replay_survival(replace(result, choice_tape=tuple(tape)))

        for invalid_position in (True, 1.0):
            with self.subTest(invalid_position=invalid_position):
                tape = [dict(record) for record in result.choice_tape]
                tape[0]["initiative_position"] = invalid_position
                with self.assertRaisesRegex(
                    ValueError,
                    "initiative position mismatch",
                ):
                    replay_survival(replace(result, choice_tape=tuple(tape)))

        tape = [dict(record) for record in result.choice_tape]
        tape[0]["initiative_order"] = list(reversed(tape[0]["initiative_order"]))
        with self.assertRaisesRegex(ValueError, "initiative order mismatch"):
            replay_survival(replace(result, choice_tape=tuple(tape)))

    def test_initiative_phase_balances_positions_without_moving_state(self) -> None:
        names = ("Aster", "Birch", "Cinder", "Lumen")
        phase_orders: list[list[list[str]]] = []
        final_physics: list[list[tuple[object, ...]]] = []
        for phase in range(4):
            world = make_survival_world(
                names,
                seed=3,
                config=SurvivalConfig(max_days=1),
                interaction_protocol=SEQUENTIAL_DIALOGUE_V3,
                initiative_phase=phase,
            )
            result = run_survival(
                world,
                {
                    name: ScriptedPolicy((wait(), wait(), wait(), rest()))
                    for name in names
                },
                days=1,
            )
            phase_orders.append(
                [
                    event["detail"]["initiative_order"]
                    for event in result.events
                    if event["kind"] == "slot_started"
                ]
            )
            final_physics.append(
                [
                    (
                        survivor["seat_id"],
                        survivor["name"],
                        survivor["energy"],
                        survivor["food"],
                        survivor["wood"],
                        survivor["shelter"],
                        survivor["alive"],
                    )
                    for survivor in result.final_state["survivors"]
                ]
            )
            self.assertEqual(result.to_dict(), replay_survival(result).to_dict())
            if phase == 0:
                self.assertNotIn("initiative_phase", result.initial_state)
            else:
                self.assertEqual(result.initial_state["initiative_phase"], phase)

        self.assertEqual(
            [orders[0][0] for orders in phase_orders],
            ["Aster", "Birch", "Cinder", "Lumen"],
        )
        for beat in range(4):
            for position in range(4):
                self.assertEqual(
                    {orders[beat][position] for orders in phase_orders},
                    set(names),
                )
        self.assertTrue(all(physics == final_physics[0] for physics in final_physics))

    def test_initiative_phase_rejects_invalid_scope_and_values(self) -> None:
        names = ("Aster", "Birch", "Cinder", "Lumen")
        with self.assertRaisesRegex(ValueError, "sequential-dialogue-v3"):
            make_survival_world(names, seed=3, initiative_phase=1)
        with self.assertRaisesRegex(TypeError, "must be an integer"):
            make_survival_world(
                names,
                seed=3,
                interaction_protocol=SEQUENTIAL_DIALOGUE_V3,
                initiative_phase=True,
            )
        with self.assertRaisesRegex(ValueError, "population minus one"):
            make_survival_world(
                names,
                seed=3,
                interaction_protocol=SEQUENTIAL_DIALOGUE_V3,
                initiative_phase=4,
            )

    def test_dialogue_view_hides_global_sequence_side_channels(self) -> None:
        class EarlierPolicy:
            def __init__(self, action: Mapping[str, object]) -> None:
                self.action = action

            def decide(self, view: SurvivorView) -> Mapping[str, object]:
                del view
                return choice(
                    self.action,
                    {"to": "Birch", "text": "same words"},
                )

        class CapturePolicy:
            def __init__(self) -> None:
                self.view: SurvivorView | None = None

            def decide(self, view: SurvivorView) -> Mapping[str, object]:
                self.view = view
                raise ValueError("stop after capturing the later view")

        def later_view(action: Mapping[str, object]) -> dict[str, object]:
            world = make_survival_world(
                ("Aster", "Birch"),
                seed=3,
                interaction_protocol=SEQUENTIAL_DIALOGUE_V3,
            )
            capture = CapturePolicy()
            with self.assertRaisesRegex(
                RuntimeError,
                "survival choice provider failed for 'Birch'",
            ):
                run_survival(
                    world,
                    {
                        "Aster": EarlierPolicy(action),
                        "Birch": capture,
                    },
                    days=1,
                )
            if capture.view is None:
                raise AssertionError("later survivor received no view")
            return capture.view.to_dict()

        valid_view = later_view({"kind": "wait"})
        invalid_view = later_view({"kind": "not_an_action"})

        self.assertEqual(valid_view, invalid_view)
        self.assertNotIn("id", valid_view["inbox"][0])
        self.assertNotIn("sequence", valid_view["inbox"][0])

        completed_world = make_survival_world(
            ("Aster", "Birch"),
            seed=3,
            config=SurvivalConfig(max_days=2),
            interaction_protocol=SEQUENTIAL_DIALOGUE_V3,
        )
        run_survival(
            completed_world,
            {
                "Aster": ScriptedPolicy((rest(),)),
                "Birch": ScriptedPolicy((rest(),)),
            },
            days=1,
        )
        next_view = survival_view_for(completed_world, "Aster")
        self.assertTrue(next_view.recent_events)
        self.assertTrue(
            all("sequence" not in event for event in next_view.recent_events)
        )

    def test_sequential_provider_failure_keeps_only_dialogue_committed(self) -> None:
        class FailingPolicy:
            def __init__(self) -> None:
                self.calls = 0
                self.views: list[SurvivorView] = []

            def decide(self, view: SurvivorView) -> Mapping[str, object]:
                self.calls += 1
                self.views.append(view)
                raise ValueError("provider stopped")

        world = make_survival_world(
            ("Aster", "Birch", "Cinder", "Lumen"),
            seed=3,
            config=SurvivalConfig(max_days=1),
            interaction_protocol=SEQUENTIAL_DIALOGUE_V3,
        )
        aster = ScriptedPolicy(
            (
                choice(
                    {"kind": "forage"},
                    {"to": "everyone", "text": "i foraged"},
                ),
            )
        )
        birch = ScriptedPolicy(
            (wait({"to": "everyone", "text": "i will wait"}),)
        )
        cinder = FailingPolicy()
        lumen = ScriptedPolicy((wait(),))

        with self.assertRaisesRegex(
            RuntimeError,
            "survival choice provider failed for 'Cinder'",
        ):
            run_survival(
                world,
                {
                    "Aster": aster,
                    "Birch": birch,
                    "Cinder": cinder,
                    "Lumen": lumen,
                },
                days=1,
            )

        self.assertEqual(
            [event.actor for event in events(world, "choice_submitted")],
            ["Aster", "Birch"],
        )
        self.assertEqual(
            [message.text for message in world.messages],
            ["i foraged", "i will wait"],
        )
        for physical_kind in (
            "choice_energy_paid",
            "food_foraged",
            "wait_completed",
            "rest_started",
            "deadline_choice_cancelled",
            "forced_collapse",
        ):
            self.assertFalse(events(world, physical_kind))
        self.assertEqual(world.resources.food, 16)
        self.assertEqual(world.resources.wood, 16)
        for survivor in world.survivors.values():
            self.assertEqual(survivor.energy, 16)
            self.assertEqual(survivor.food, 1)
            self.assertEqual(survivor.wood, 0)
            self.assertFalse(survivor.resting)
        self.assertEqual(cinder.calls, 1)
        self.assertEqual(lumen.calls, 0)

    def test_dialogue_final_beat_sends_speech_but_only_resolves_rest(self) -> None:
        world = make_survival_world(
            ("Aster", "Birch"),
            seed=3,
            interaction_protocol=SEQUENTIAL_DIALOGUE_V3,
        )

        run_survival_cycle(
            world,
            (
                {"Aster": rest(), "Birch": wait()},
                {"Birch": wait()},
                {"Birch": wait()},
                {
                    "Birch": choice(
                        {"kind": "forage"},
                        {"to": "Aster", "text": "too late"},
                    )
                },
            ),
        )

        self.assertFalse(events(world, "food_foraged", actor="Birch"))
        self.assertEqual(
            [event.detail["message"]["text"] for event in events(
                world, "speech_sent", actor="Birch"
            )],
            ["too late"],
        )
        self.assertEqual(
            len(events(world, "deadline_choice_cancelled", actor="Birch")),
            1,
        )
        self.assertEqual(len(events(world, "forced_collapse", actor="Birch")), 1)

    def test_incomplete_final_dialogue_sends_speech_without_exhaustion(self) -> None:
        class FailOnFourthCall:
            def __init__(self) -> None:
                self.calls = 0

            def decide(self, view: SurvivorView) -> Mapping[str, object]:
                del view
                self.calls += 1
                if self.calls == 4:
                    raise ValueError("final response failed")
                return wait()

        world = make_survival_world(
            ("Aster", "Birch"),
            seed=3,
            interaction_protocol=SEQUENTIAL_DIALOGUE_V3,
        )
        aster = FailOnFourthCall()
        birch = ScriptedPolicy(
            (
                wait(),
                wait(),
                wait(),
                choice(
                    {"kind": "forage"},
                    {"to": "Aster", "text": "final words"},
                ),
            )
        )

        with self.assertRaisesRegex(
            RuntimeError,
            "survival choice provider failed for 'Aster'",
        ):
            run_survival(
                world,
                {"Aster": aster, "Birch": birch},
                days=1,
            )

        self.assertEqual(
            [
                message.text
                for message in world.messages
                if message.slot == world.config.slots_per_cycle
            ],
            ["final words"],
        )
        self.assertFalse(events(world, "deadline_choice_cancelled"))
        self.assertFalse(events(world, "forced_collapse"))
        self.assertFalse(events(world, "food_foraged"))
        self.assertEqual(world.resources.food, 16)
        self.assertEqual(world.survivors["Aster"].energy, 16)
        self.assertEqual(world.survivors["Birch"].energy, 16)

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

    def test_wait_stays_awake_and_speech_arrives_on_the_next_beat(self) -> None:
        world = make_survival_world(
            ("Aster", "Birch"),
            seed=3,
            config=SurvivalConfig(
                max_days=1,
                food_starting_stock=16,
                food_capacity=16,
                wood_starting_stock=16,
                wood_capacity=16,
            ),
        )
        aster = ScriptedPolicy(
            (
                wait({"to": "Birch", "text": "ping next beat"}),
                rest(),
            )
        )
        birch = ScriptedPolicy((wait(), rest()))

        result = run_survival(
            world,
            {"Aster": aster, "Birch": birch},
            days=1,
        )

        self.assertEqual(aster.calls, 2)
        self.assertEqual(birch.calls, 2)
        self.assertEqual(birch.views[0].inbox, ())
        self.assertEqual(
            [message["text"] for message in birch.views[1].inbox],
            ["ping next beat"],
        )
        self.assertEqual(len(events(world, "wait_completed")), 2)
        wait_costs = [
            event.detail["action_cost"]
            for event in events(world, "choice_energy_paid")
            if event.detail["action"] == "wait"
        ]
        self.assertEqual(wait_costs, [0, 0])
        self.assertEqual(world.resources.food, 16)
        self.assertEqual(world.resources.wood, 16)
        self.assertEqual(survival_metrics(result)["waits_completed"], 2)
        self.assertEqual(
            result.initial_state["interaction_protocol"],
            GLOBAL_BEATS_V2,
        )
        self.assertEqual(result.to_dict(), replay_survival(result).to_dict())

    def test_final_beat_wait_and_speech_are_cancelled_then_exhausted(self) -> None:
        world = make_survival_world(("Aster", "Birch"), seed=3)

        run_survival_cycle(
            world,
            (
                {"Aster": wait(), "Birch": rest()},
                {"Aster": wait()},
                {"Aster": wait()},
                {
                    "Aster": wait(
                        {"to": "Birch", "text": "too late to send"}
                    )
                },
            ),
        )

        self.assertEqual(len(events(world, "wait_completed", actor="Aster")), 3)
        self.assertFalse(events(world, "speech_sent", actor="Aster"))
        cancelled = events(world, "deadline_choice_cancelled", actor="Aster")
        self.assertEqual(cancelled[0].detail["attempted_choice"], wait(
            {"to": "Birch", "text": "too late to send"}
        ))
        self.assertEqual(len(events(world, "forced_collapse", actor="Aster")), 1)

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

    def test_dialogue_slot_map_rejects_an_extra_actor_before_mutation(self) -> None:
        world = make_survival_world(
            ("Aster", "Birch"),
            seed=3,
            interaction_protocol=SEQUENTIAL_DIALOGUE_V3,
        )
        before = world.to_dict()

        with self.assertRaisesRegex(ValueError, "unavailable survivors: Cinder"):
            run_survival_cycle(
                world,
                (
                    {
                        "Aster": rest(),
                        "Birch": rest(),
                        "Cinder": rest(),
                    },
                ),
            )

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

    def test_gift_resolves_before_dependent_shelter_build_in_same_beat(
        self,
    ) -> None:
        world = make_survival_world(("Aster", "Birch"), seed=3)
        world.survivors["Aster"].wood = 2
        world.survivors["Birch"].wood = 2

        run_survival_cycle(
            world,
            (
                {
                    "Aster": choice(
                        {"kind": "give_wood", "target": "Birch", "amount": 2}
                    ),
                    "Birch": choice({"kind": "build_shelter"}),
                },
                {"Aster": rest(), "Birch": rest()},
            ),
        )

        self.assertEqual(world.survivors["Aster"].wood, 0)
        self.assertEqual(world.survivors["Birch"].wood, 0)
        self.assertTrue(world.survivors["Birch"].shelter)
        self.assertEqual(len(events(world, "resource_given")), 1)
        self.assertEqual(len(events(world, "shelter_built", actor="Birch")), 1)

    def test_global_beat_gifts_cannot_relay_inbound_resources(self) -> None:
        world = make_survival_world(("Aster", "Birch", "Cinder"), seed=3)
        world.survivors["Aster"].wood = 2
        world.survivors["Birch"].wood = 0
        world.survivors["Cinder"].wood = 0

        run_survival_cycle(
            world,
            (
                {
                    "Aster": choice(
                        {"kind": "give_wood", "target": "Birch", "amount": 2}
                    ),
                    "Birch": choice(
                        {"kind": "give_wood", "target": "Cinder", "amount": 1}
                    ),
                    "Cinder": rest(),
                },
                {"Aster": rest(), "Birch": rest()},
            ),
        )

        self.assertEqual(world.survivors["Aster"].wood, 0)
        self.assertEqual(world.survivors["Birch"].wood, 2)
        self.assertEqual(world.survivors["Cinder"].wood, 0)
        transfers = events(world, "resource_given")
        self.assertEqual([(event.actor, event.detail["target"]) for event in transfers], [
            ("Aster", "Birch")
        ])
        rejected = events(world, "action_resolution_rejected", actor="Birch")
        self.assertIn("transfer-phase start", rejected[0].detail["reason"])

    def test_global_beat_allows_reciprocal_funded_gifts(self) -> None:
        world = make_survival_world(("Aster", "Birch"), seed=3)
        world.survivors["Aster"].wood = 1
        world.survivors["Birch"].wood = 1

        run_survival_cycle(
            world,
            (
                {
                    "Aster": choice(
                        {"kind": "give_wood", "target": "Birch", "amount": 1}
                    ),
                    "Birch": choice(
                        {"kind": "give_wood", "target": "Aster", "amount": 1}
                    ),
                },
                {"Aster": rest(), "Birch": rest()},
            ),
        )

        self.assertEqual(world.survivors["Aster"].wood, 1)
        self.assertEqual(world.survivors["Birch"].wood, 1)
        self.assertEqual(len(events(world, "resource_given")), 2)
        self.assertFalse(events(world, "action_resolution_rejected"))

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
    def test_prompt_describes_sequential_dialogue_without_private_state(
        self,
    ) -> None:
        world = make_survival_world(
            ("Aster", "Birch"),
            seed=3,
            interaction_protocol=SEQUENTIAL_DIALOGUE_V3,
        )
        view = survival_view_for(world, "Aster")
        rendered = render_system_prompt(
            "Aster",
            interaction_protocol=view.interaction_protocol,
        ) + render_turn_prompt(view)
        lowered = rendered.lower()

        self.assertIn("initiative order: aster -> birch", lowered)
        self.assertIn("your position: 1 of 2", lowered)
        self.assertIn("valid speech from earlier turns", lowered)
        self.assertIn("speech is sent immediately", lowered)
        self.assertIn("physical actions resolve together", lowered)
        self.assertIn("cannot inspect earlier submitted physical actions", lowered)
        self.assertIn("claims about them remain unverified", lowered)
        self.assertIn("valid speech is still sent", lowered)
        self.assertIn("only after every required final-beat choice", lowered)
        self.assertIn("may be later in the same beat", lowered)
        self.assertNotIn("seat-", rendered)
        self.assertNotIn("provider", lowered)

    def test_prompt_describes_global_beat_contract_without_provider_leaks(
        self,
    ) -> None:
        world = make_survival_world(("Aster", "Birch"), seed=3)
        view = survival_view_for(world, "Aster")
        rendered = render_system_prompt(
            "Aster",
            interaction_protocol=view.interaction_protocol,
        ) + render_turn_prompt(view)
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
        self.assertIn("day 1, beat 1 of 4", lowered)
        self.assertIn("same frozen, unresolved moment", lowered)
        self.assertIn("action and optional speech", lowered)
        self.assertIn("wait does nothing and leaves you awake", lowered)
        self.assertIn("rest ends your participation for the day", lowered)
        self.assertIn("final beat", lowered)
        self.assertIn("next active beat", lowered)
        self.assertIn("transfers resolve before", lowered)
        self.assertNotIn("cycle 1", lowered)
        self.assertNotIn("chance 1", lowered)
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
        self.assertEqual(MODEL_MAX_COMPLETION_TOKENS, 10_000)
        self.assertEqual(MODEL_MAX_RESPONSE_BYTES, 8_192)
        action_kinds = [
            variant["properties"]["kind"]["const"]
            for variant in schema["properties"]["action"]["oneOf"]
        ]
        self.assertIn("wait", action_kinds)
        self.assertEqual(
            survival_view_for(world, "Aster").rules["action_energy_costs"][
                "wait"
            ],
            0,
        )

    def test_wait_protocol_accepts_exact_action_and_rejects_extra_fields(
        self,
    ) -> None:
        accepted = parse_survival_choice(
            wait(),
            actor="Aster",
            living_peers=("Birch",),
            max_food_eaten=2,
            max_speech_chars=500,
            interaction_protocol=GLOBAL_BEATS_V2,
        )
        rejected = parse_survival_choice(
            choice({"kind": "wait", "until": "later"}),
            actor="Aster",
            living_peers=("Birch",),
            max_food_eaten=2,
            max_speech_chars=500,
            interaction_protocol=GLOBAL_BEATS_V2,
        )

        self.assertEqual(accepted.action.kind, "wait")
        self.assertIsNone(accepted.action_error)
        self.assertEqual(rejected.action.kind, "invalid")
        self.assertIn("exactly", str(rejected.action_error))

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
