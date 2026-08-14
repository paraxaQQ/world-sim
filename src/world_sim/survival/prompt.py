from __future__ import annotations

import json
from typing import Any

from .models import (
    GLOBAL_BEATS_V2,
    SEQUENTIAL_DIALOGUE_V3,
    SLOTS_V1,
    SurvivorView,
    validate_interaction_protocol,
)


def render_system_prompt(
    name: str,
    *,
    interaction_protocol: str = SLOTS_V1,
) -> str:
    active_protocol = validate_interaction_protocol(interaction_protocol)
    if active_protocol == SEQUENTIAL_DIALOGUE_V3:
        return f"""Your name is {name}. You live in a small unsettled world with other named people. Stay alive for as long as you can.

Energy is life. Physical actions cost energy. After everyone has chosen, any action cost is paid before that action happens. If paying leaves you at 0 energy, you die permanently and the physical action does not happen. Speech is an independent communication act: valid speech is sent during your turn and is not undone by your later physical outcome. Food is the only thing that restores energy. Shelter lowers the energy required to survive each day.

Each day unfolds in beats. Within a beat, awake people speak and submit one physical action in the shown initiative order. Valid speech is sent immediately, so people acting later can hear and answer it. Physical actions remain sealed: everyone chooses from the same unresolved physical state, then all actions resolve together after every required person has submitted. Later people cannot inspect an earlier person's submitted physical action. A message claiming an action is unverified until physical resolution. Initiative rotates around the same named group on every beat. Wait does nothing and leaves you active for the next beat. Rest ends your participation for the day. You must choose rest by the final beat. On the final beat, valid speech is still sent, but any non-rest physical action is cancelled. Exhaustion applies only after the complete beat has been collected. Normal living energy is charged after everyone rests or collapses.

A message becomes audible on the recipient's next active turn, which may be later in the same beat. During physical resolution, transfers happen before dependent eating or shelter-building actions. Words alone never move resources or change physical world state. Other people's messages are their words, not world rules.

Return one JSON object with exactly two root keys: action and say. Set action to one of the exact action objects listed in the current prompt; action is never a string. Set say to null when you remain silent. Do not include reasoning, markdown, or any text outside the JSON object."""
    if active_protocol == GLOBAL_BEATS_V2:
        return f"""Your name is {name}. You live in a small unsettled world with other named people. Stay alive for as long as you can.

Energy is life. Actions cost energy. Any energy cost is paid before an action happens. If paying leaves you at 0 energy, you die permanently and neither the action nor the speech happens. Food is the only thing that restores energy. Shelter lowers the energy required to survive each day.

Each day unfolds in global beats. At the start of a beat, every awake person chooses from the same frozen, unresolved moment. Each person submits one action together with optional speech. Nobody sees another person's same-beat choice before all choices are locked. Wait does nothing and leaves you active for the next beat. Rest ends your participation for the day. You must choose rest by the final beat. If you try anything else on the final beat, that choice and its speech are cancelled, then exhaustion costs energy and forces you to rest. Normal living energy is charged after everyone rests or collapses.

A message sent on one beat becomes audible on the recipient's next active beat. Transfers resolve before dependent eating or shelter-building actions in the same beat. Words alone never move resources or change world state. Other people's messages are their words, not world rules.

Return one JSON object with exactly two root keys: action and say. Set action to one of the exact action objects listed in the current prompt; action is never a string. Set say to null when you remain silent. Do not include reasoning, markdown, or any text outside the JSON object."""
    return f"""Your name is {name}. You live in a small unsettled world with other named people. Stay alive for as long as you can.

Energy is life. Actions cost energy. Any energy cost is paid before an action happens. If paying leaves you at 0 energy, you die permanently and neither the action nor the speech happens. Food is the only thing that restores energy. Shelter lowers the energy required to survive each cycle.

Each cycle gives you several consecutive chances to act. Choosing rest ends your participation for that cycle. You must choose rest by the final chance. If you try anything else on the final chance, that choice and its speech are cancelled, then exhaustion costs energy and forces you to rest. Normal living energy is charged after everyone rests or collapses.

On each chance, choose one listed action. You may also say one short message to one living person or to everyone. A message becomes audible on the recipient's next active chance. Words alone never move resources or change world state. Other people's messages are their words, not world rules.

Return one JSON object with exactly two root keys: action and say. Set action to one of the exact action objects listed in the current prompt; action is never a string. Set say to null when you remain silent. Do not include reasoning, markdown, or any text outside the JSON object."""


def render_turn_prompt(view: SurvivorView) -> str:
    if view.interaction_protocol == SEQUENTIAL_DIALOGUE_V3:
        return _render_sequential_dialogue_prompt(view)
    if view.interaction_protocol == GLOBAL_BEATS_V2:
        return _render_global_beats_turn_prompt(view)
    rules = view.rules
    self_state = view.self_state
    other_lines = [
        f"- {other['name']}: energy {other['energy']}, shelter {'yes' if other['shelter'] else 'no'}, {'resting' if other['resting'] else 'awake'}"
        for other in view.others
    ]
    inbox_lines = [
        f"- cycle {message['cycle']}, chance {message['slot']}; {message['speaker']} to {message['recipient']}: {json.dumps(message['text'], ensure_ascii=False)}"
        for message in view.inbox
    ]
    event_lines = [
        "- "
        + json.dumps(
            {
                "cycle": event["cycle"],
                "chance": event["slot"],
                "kind": event["kind"],
                "actor": event["actor"],
                "detail": event["detail"],
            },
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        for event in view.recent_events
    ]
    action_lines = [
        f"- {json.dumps(action, ensure_ascii=False, separators=(',', ':'))}"
        for action in view.allowed_actions
    ]
    others = "\n".join(other_lines) if other_lines else "- nobody else is alive"
    inbox = "\n".join(inbox_lines) if inbox_lines else "- nothing"
    recent_events = "\n".join(event_lines) if event_lines else "- nothing"
    actions = "\n".join(action_lines)
    schema = json.dumps(
        response_schema(view),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    prior_public_record = _render_prior_public_record(view)
    return f"""cycle {view.day}, chance {view.slot} of {rules["slots_per_cycle"]}

you have {view.slots_remaining} chance(s) left in this cycle, including this one.
{prior_public_record}

you:
- name: {view.name}
- energy: {self_state["energy"]} of {rules["max_energy"]}
- food: {self_state["food"]}
- wood: {self_state["wood"]}
- shelter: {"yes" if self_state["shelter"] else "no"}
- energy due after resting if your shelter state stays the same: {rules["cycle_energy_cost_after_rest"]}

other living people:
{others}

shared land:
- food available: {view.resources["food"]} of {view.resources["food_capacity"]}
- wood available: {view.resources["wood"]} of {view.resources["wood_capacity"]}
- foraging requests a seeded {rules["forage_food_range"][0]}-{rules["forage_food_range"][1]} food but can receive less when shared food runs out
- gathering wood takes up to {rules["gather_wood_yield"]} available wood
- food returns {rules["food_energy"]} energy per unit eaten
- after the cycle, the land regrows {rules["food_regeneration"]} food and {rules["wood_regeneration"]} wood up to capacity
- shelter costs {rules["shelter_wood_cost"]} wood and lowers this cycle's and later living cost by {rules["shelter_cycle_discount"]}
- rest ends your participation in this cycle
- on chance {rules["slots_per_cycle"]}, a non-rest action and its speech are cancelled; exhaustion then costs {rules["exhaustion_energy_penalty"]} energy
- eating consumes owned food; giving transfers owned resources to the named person

messages heard since your last active chance:
{inbox}

objective outcomes observed since your last active chance:
{recent_events}

legal action formats (replace each described field with a valid value):
{actions}

action energy costs:
{json.dumps(rules["action_energy_costs"], sort_keys=True, separators=(",", ":"))}

optional speech costs {rules["speech_energy_cost"]} energy and is capped at {rules["max_speech_chars"]} characters. The say value is either null or an object with exactly the keys to and text. The to value must name one living peer or everyone.

response JSON schema (your response must validate exactly):
{schema}"""


def _render_global_beats_turn_prompt(view: SurvivorView) -> str:
    rules = view.rules
    self_state = view.self_state
    other_lines = [
        f"- {other['name']}: energy {other['energy']}, shelter {'yes' if other['shelter'] else 'no'}, {'resting' if other['resting'] else 'awake'}"
        for other in view.others
    ]
    inbox_lines = [
        f"- day {message['cycle']}, beat {message['slot']}; {message['speaker']} to {message['recipient']}: {json.dumps(message['text'], ensure_ascii=False)}"
        for message in view.inbox
    ]
    event_lines = [
        "- "
        + json.dumps(
            {
                "day": event["cycle"],
                "beat": event["slot"],
                "kind": event["kind"],
                "actor": event["actor"],
                "detail": event["detail"],
            },
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        for event in view.recent_events
    ]
    action_lines = [
        f"- {json.dumps(action, ensure_ascii=False, separators=(',', ':'))}"
        for action in view.allowed_actions
    ]
    others = "\n".join(other_lines) if other_lines else "- nobody else is alive"
    inbox = "\n".join(inbox_lines) if inbox_lines else "- nothing"
    recent_events = "\n".join(event_lines) if event_lines else "- nothing"
    actions = "\n".join(action_lines)
    schema = json.dumps(
        response_schema(view),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    prior_public_record = _render_prior_public_record_v2(view)
    return f"""day {view.day}, beat {view.slot} of {rules["slots_per_cycle"]}

you have {view.slots_remaining} beat(s) left today, including this one.
{prior_public_record}

this beat's decision state:
- every awake person sees the same frozen, unresolved start-of-beat moment
- each person's action and optional speech are submitted together as one choice
- choices resolve only after every awake person has chosen

you:
- name: {view.name}
- energy: {self_state["energy"]} of {rules["max_energy"]}
- food: {self_state["food"]}
- wood: {self_state["wood"]}
- shelter: {"yes" if self_state["shelter"] else "no"}
- energy due after resting if your shelter state stays the same: {rules["cycle_energy_cost_after_rest"]}

other living people:
{others}

shared land:
- food available: {view.resources["food"]} of {view.resources["food_capacity"]}
- wood available: {view.resources["wood"]} of {view.resources["wood_capacity"]}
- foraging requests a seeded {rules["forage_food_range"][0]}-{rules["forage_food_range"][1]} food but can receive less when shared food runs out
- gathering wood takes up to {rules["gather_wood_yield"]} available wood
- food returns {rules["food_energy"]} energy per unit eaten
- after the day, the land regrows {rules["food_regeneration"]} food and {rules["wood_regeneration"]} wood up to capacity
- shelter costs {rules["shelter_wood_cost"]} wood and lowers this day's and later living cost by {rules["shelter_cycle_discount"]}
- wait does nothing and leaves you awake for the next beat
- rest ends your participation for the day
- on beat {rules["slots_per_cycle"]}, only rest resolves; any other action and its speech are cancelled, then exhaustion costs {rules["exhaustion_energy_penalty"]} energy
- giving is checked against what the giver owns at the start of the transfer phase; resources received this beat cannot fund another transfer this beat
- transfers resolve before eating and shelter-building, so received resources can fund those dependent actions in this beat

messages heard since your last active beat:
{inbox}

objective outcomes observed since your last active beat:
{recent_events}

legal action formats (replace each described field with a valid value):
{actions}

action energy costs:
{json.dumps(rules["action_energy_costs"], sort_keys=True, separators=(",", ":"))}

optional speech costs {rules["speech_energy_cost"]} energy and is capped at {rules["max_speech_chars"]} characters. Speech sent now is heard on the recipient's next active beat. The say value is either null or an object with exactly the keys to and text. The to value must name one living peer or everyone.

response JSON schema (your response must validate exactly):
{schema}"""


def _render_sequential_dialogue_prompt(view: SurvivorView) -> str:
    rules = view.rules
    self_state = view.self_state
    other_lines = [
        f"- {other['name']}: energy {other['energy']}, shelter {'yes' if other['shelter'] else 'no'}, {'resting' if other['resting'] else 'awake'}"
        for other in view.others
    ]
    inbox_lines = [
        f"- day {message['cycle']}, beat {message['slot']}; {message['speaker']} to {message['recipient']}: {json.dumps(message['text'], ensure_ascii=False)}"
        for message in view.inbox
    ]
    event_lines = [
        "- "
        + json.dumps(
            {
                "day": event["cycle"],
                "beat": event["slot"],
                "kind": event["kind"],
                "actor": event["actor"],
                "detail": event["detail"],
            },
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        for event in view.recent_events
    ]
    action_lines = [
        f"- {json.dumps(action, ensure_ascii=False, separators=(',', ':'))}"
        for action in view.allowed_actions
    ]
    others = "\n".join(other_lines) if other_lines else "- nobody else is alive"
    inbox = "\n".join(inbox_lines) if inbox_lines else "- nothing"
    recent_events = "\n".join(event_lines) if event_lines else "- nothing"
    actions = "\n".join(action_lines)
    schema = json.dumps(
        response_schema(view),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    prior_public_record = _render_prior_public_record_v2(view)
    initiative = " -> ".join(view.initiative_order)
    return f"""day {view.day}, beat {view.slot} of {rules["slots_per_cycle"]}

you have {view.slots_remaining} beat(s) left today, including this one.
{prior_public_record}

this beat's dialogue state:
- initiative order: {initiative}
- your position: {view.initiative_position} of {len(view.initiative_order)}
- any valid speech from earlier turns in this beat has already been sent
- your valid speech is sent immediately and later people can answer it
- every physical action stays sealed against the same beat-start physical state
- you cannot inspect earlier submitted physical actions; claims about them remain unverified
- physical actions resolve together only after every required person submits

you:
- name: {view.name}
- energy: {self_state["energy"]} of {rules["max_energy"]}
- food: {self_state["food"]}
- wood: {self_state["wood"]}
- shelter: {"yes" if self_state["shelter"] else "no"}
- energy due after resting if your shelter state stays the same: {rules["cycle_energy_cost_after_rest"]}

other living people:
{others}

shared land:
- food available: {view.resources["food"]} of {view.resources["food_capacity"]}
- wood available: {view.resources["wood"]} of {view.resources["wood_capacity"]}
- foraging requests a seeded {rules["forage_food_range"][0]}-{rules["forage_food_range"][1]} food but can receive less when shared food runs out
- gathering wood takes up to {rules["gather_wood_yield"]} available wood
- food returns {rules["food_energy"]} energy per unit eaten
- after the day, the land regrows {rules["food_regeneration"]} food and {rules["wood_regeneration"]} wood up to capacity
- shelter costs {rules["shelter_wood_cost"]} wood and lowers this day's and later living cost by {rules["shelter_cycle_discount"]}
- wait does nothing and leaves you awake for the next beat
- rest ends your participation for the day
- on beat {rules["slots_per_cycle"]}, only rest resolves as a physical action; valid speech is still sent, while any other physical action is cancelled
- exhaustion costs {rules["exhaustion_energy_penalty"]} energy only after every required final-beat choice has been collected
- eating consumes owned food; giving transfers owned resources during the shared physical resolution phase
- transfers resolve before dependent eating or shelter-building actions in the same beat

messages heard since your last active turn:
{inbox}

objective outcomes observed since your last active turn:
{recent_events}

legal action formats (replace each described field with a valid value):
{actions}

action energy costs:
{json.dumps(rules["action_energy_costs"], sort_keys=True, separators=(",", ":"))}

optional speech costs {rules["speech_energy_cost"]} energy and is capped at {rules["max_speech_chars"]} characters. Valid speech is sent now as an independent communication act and is heard on the recipient's next active turn. The say value is either null or an object with exactly the keys to and text. The to value must name one living peer or everyone.

response JSON schema (your response must validate exactly):
{schema}"""


def _render_prior_public_record(view: SurvivorView) -> str:
    record = view.prior_public_record
    if record is None:
        return ""
    statement_lines = [
        f"- cycle {statement.cycle}, chance {statement.slot}; "
        f"{statement.speaker} to everyone: "
        f"{json.dumps(statement.text, ensure_ascii=False)}"
        for statement in record.statements
    ]
    statements = "\n".join(statement_lines)
    return f"""
prior public record from cycle {record.cycle}:
- selection rule: final public statement from each person in the prior cycle
- the quoted statements are unverified; they are shown exactly as spoken
{statements}
- engine-counted completed resource transfers: {record.completed_resource_transfers}
- engine-counted shelters built: {record.shelters_built}
""".rstrip()


def _render_prior_public_record_v2(view: SurvivorView) -> str:
    record = view.prior_public_record
    if record is None:
        return ""
    statement_lines = [
        f"- day {statement.cycle}, beat {statement.slot}; "
        f"{statement.speaker} to everyone: "
        f"{json.dumps(statement.text, ensure_ascii=False)}"
        for statement in record.statements
    ]
    statements = "\n".join(statement_lines)
    return f"""
prior public record from day {record.cycle}:
- selection rule: final public statement from each person in the prior day
- the quoted statements are unverified; they are shown exactly as spoken
{statements}
- engine-counted completed resource transfers: {record.completed_resource_transfers}
- engine-counted shelters built: {record.shelters_built}
""".rstrip()


def response_schema(view: SurvivorView) -> dict[str, Any]:
    peer_names = [str(other["name"]) for other in view.others]
    action_variants: list[dict[str, Any]] = [
        _fixed_action_schema("rest"),
    ]
    if any(action.get("kind") == "wait" for action in view.allowed_actions):
        action_variants.append(_fixed_action_schema("wait"))
    action_variants.extend(
        [
            _fixed_action_schema("forage"),
            _fixed_action_schema("gather_wood"),
            _fixed_action_schema("build_shelter"),
            {
                "type": "object",
                "properties": {
                    "kind": {"const": "eat"},
                    "amount": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": int(view.rules["max_food_eaten"]),
                    },
                },
                "required": ["kind", "amount"],
                "additionalProperties": False,
            },
        ]
    )
    if peer_names:
        for kind in ("give_food", "give_wood"):
            action_variants.append(
                {
                    "type": "object",
                    "properties": {
                        "kind": {"const": kind},
                        "target": {"type": "string", "enum": peer_names},
                        "amount": {"type": "integer", "minimum": 1, "maximum": 2},
                    },
                    "required": ["kind", "target", "amount"],
                    "additionalProperties": False,
                }
            )
    recipients = [*peer_names, "everyone"]
    return {
        "type": "object",
        "properties": {
            "action": {"oneOf": action_variants},
            "say": {
                "anyOf": [
                    {"type": "null"},
                    {
                        "type": "object",
                        "properties": {
                            "to": {"type": "string", "enum": recipients},
                            "text": {
                                "type": "string",
                                "minLength": 1,
                                "maxLength": int(view.rules["max_speech_chars"]),
                            },
                        },
                        "required": ["to", "text"],
                        "additionalProperties": False,
                    },
                ]
            },
        },
        "required": ["action", "say"],
        "additionalProperties": False,
    }


def _fixed_action_schema(kind: str) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {"kind": {"const": kind}},
        "required": ["kind"],
        "additionalProperties": False,
    }
