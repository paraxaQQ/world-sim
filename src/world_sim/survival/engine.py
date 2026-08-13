from __future__ import annotations

import hashlib
import json
import random
import re
from collections.abc import Callable, Mapping, Sequence
from copy import deepcopy
from dataclasses import replace
from typing import Any, Protocol

from .models import (
    DEFAULT_SURVIVOR_NAMES,
    GLOBAL_BEATS_V2,
    SLOTS_V1,
    PriorPublicRecord,
    PriorPublicStatement,
    ResourcePool,
    SpokenMessage,
    SurvivalConfig,
    SurvivalEvent,
    SurvivalResult,
    SurvivalWorld,
    Survivor,
    SurvivorView,
    validate_interaction_protocol,
)
from .protocol import (
    ParsedSurvivalChoice,
    SurvivalAction,
    allowed_survival_actions,
    parse_survival_choice,
)


class SurvivalChoiceProvider(Protocol):
    def decide(self, view: SurvivorView) -> Mapping[str, object]:
        """Return one primary action and optional speech from a capability-free view."""


_ProposalSource = Callable[
    [int, Mapping[str, SurvivorView]], Mapping[str, object]
]


def make_survival_world(
    names: Sequence[str] = DEFAULT_SURVIVOR_NAMES,
    *,
    seed: int,
    config: SurvivalConfig | None = None,
    interaction_protocol: str = GLOBAL_BEATS_V2,
) -> SurvivalWorld:
    active_config = config or SurvivalConfig()
    active_protocol = validate_interaction_protocol(interaction_protocol)
    clean_names = tuple(name.strip() for name in names)
    if len(clean_names) < 2:
        raise ValueError("a survival world needs at least two named survivors")
    required_inbox = (len(clean_names) - 1) * active_config.slots_per_cycle
    if required_inbox > active_config.max_inbox_messages:
        raise ValueError(
            "max_inbox_messages must hold one cycle of messages from every peer"
        )
    if any(
        re.fullmatch(r"[A-Za-z][A-Za-z'-]{0,31}", name) is None
        for name in clean_names
    ):
        raise ValueError(
            "survivor names must use 1-32 letters, apostrophes, or hyphens"
        )
    if len(clean_names) != len({name.casefold() for name in clean_names}):
        raise ValueError("survivor names must be unique")
    if any(name.casefold() == "everyone" for name in clean_names):
        raise ValueError("'everyone' is reserved for broadcast speech")
    return SurvivalWorld(
        config=active_config,
        seed=seed,
        survivors={
            name: Survivor(
                seat_id=f"seat-{index:03d}",
                name=name,
                energy=active_config.starting_energy,
                food=active_config.starting_food,
                wood=active_config.starting_wood,
            )
            for index, name in enumerate(clean_names, start=1)
        },
        resources=ResourcePool(
            food=active_config.food_starting_stock,
            food_capacity=active_config.food_capacity,
            wood=active_config.wood_starting_stock,
            wood_capacity=active_config.wood_capacity,
        ),
        interaction_protocol=active_protocol,
    )


def continue_survival_world(
    parent: SurvivalResult,
    *,
    additional_cycles: int = 1,
    interaction_protocol: str | None = None,
) -> SurvivalWorld:
    if isinstance(additional_cycles, bool) or not isinstance(
        additional_cycles, int
    ):
        raise TypeError("additional_cycles must be an integer")
    if additional_cycles < 1:
        raise ValueError("additional_cycles must be a positive integer")

    verified = replay_survival(parent)
    final_state = verified.final_state
    finished_reason = final_state.get("finished_reason")
    if finished_reason != "cycle_limit_reached":
        raise ValueError(
            "only a cycle_limit_reached survival result can be continued"
        )

    cycle = _strict_cycle_alias(final_state, context="parent final state")
    observation_history = verified.initial_state.get("observation_history")
    if observation_history is None:
        history: list[dict[str, Any]] = []
        event_sequence_offset = verified.event_sequence_base
    elif isinstance(observation_history, list):
        history = [dict(event) for event in observation_history]
        event_sequence_offset = int(
            verified.initial_state.get("event_sequence_offset", 0)
        )
    else:
        raise TypeError("parent observation_history must be a list")
    history.extend(dict(event) for event in verified.events)
    expected_sequences = list(
        range(
            event_sequence_offset + 1,
            event_sequence_offset + len(history) + 1,
        )
    )
    actual_sequences = [int(event["sequence"]) for event in history]
    if actual_sequences != expected_sequences:
        raise ValueError("parent event history must have contiguous sequences")

    snapshot = deepcopy(final_state)
    config = SurvivalConfig(**dict(snapshot["config"]))
    snapshot["config"] = replace(
        config,
        max_days=cycle + additional_cycles,
    ).to_dict()
    snapshot["finished_reason"] = None
    snapshot["observation_history"] = history
    snapshot["event_sequence_offset"] = event_sequence_offset
    snapshot.pop("prior_public_record", None)
    if interaction_protocol is not None:
        active_protocol = validate_interaction_protocol(interaction_protocol)
        if active_protocol == SLOTS_V1:
            snapshot.pop("interaction_protocol", None)
        else:
            snapshot["interaction_protocol"] = active_protocol
    world = _world_from_snapshot(
        snapshot,
        event_sequence_offset=event_sequence_offset,
    )
    world.prior_public_record = _prior_public_record_from_result(
        verified,
        world,
    )
    last_sequence = event_sequence_offset + len(history)
    for survivor in world.survivors.values():
        survivor.last_observed_event_sequence = last_sequence
    return world


def adjust_shared_resource(
    world: SurvivalWorld,
    *,
    resource: str,
    stock: int,
    reason: str,
) -> SurvivalEvent:
    if resource not in {"food", "wood"}:
        raise ValueError("resource must be 'food' or 'wood'")
    if isinstance(stock, bool) or not isinstance(stock, int):
        raise TypeError("stock must be an integer")
    if not isinstance(reason, str):
        raise TypeError("reason must be a string")
    if re.fullmatch(r"[a-z][a-z0-9_]{0,63}", reason) is None:
        raise ValueError(
            "reason must be a 1-64 character lowercase identifier"
        )
    if world.finished:
        raise RuntimeError(
            f"survival world is already finished: {world.finished_reason}"
        )
    if world.slot != 0:
        raise RuntimeError("shared resources can only be adjusted between cycles")
    next_cycle = world.day + 1
    if next_cycle > world.config.max_days:
        raise RuntimeError("survival world has no remaining cycle")
    if any(
        event.day == next_cycle
        and (event.slot != 0 or event.kind == "cycle_started")
        for event in world.events
    ):
        raise RuntimeError("the next cycle has already started")
    if any(
        event.day == next_cycle
        and event.slot == 0
        and event.kind == "resource_adjusted"
        and event.detail.get("resource") == resource
        for event in world.events
    ):
        raise RuntimeError(
            f"shared {resource} was already adjusted for cycle {next_cycle}"
        )

    capacity = int(getattr(world.resources, f"{resource}_capacity"))
    if not 0 <= stock <= capacity:
        raise ValueError(
            f"shared {resource} stock must be between 0 and {capacity}"
        )
    before = int(getattr(world.resources, resource))
    if stock == before:
        raise ValueError(f"shared {resource} stock is already {stock}")
    setattr(world.resources, resource, stock)
    _emit(
        world,
        next_cycle,
        0,
        "resource_adjusted",
        None,
        resource=resource,
        before=before,
        after=stock,
        delta=stock - before,
        reason=reason,
    )
    return world.events[-1]


def survival_view_for(world: SurvivalWorld, name: str) -> SurvivorView:
    survivor = world.survivors.get(name)
    if survivor is None:
        raise ValueError(f"unknown survivor {name!r}")
    if not survivor.alive:
        raise ValueError(f"dead survivor {name!r} cannot receive a world view")
    if survivor.resting:
        raise ValueError(f"resting survivor {name!r} cannot receive another view")
    cycle = world.day + 1
    slot = world.slot or 1
    living_peers = [peer for peer in world.alive_names() if peer != name]
    inbox = [
        message.to_dict()
        for message in world.messages
        if message.sequence > survivor.last_observed_event_sequence
        and (message.recipient == name or message.recipient == "everyone")
        and message.speaker != name
    ]
    inbox.sort(key=lambda message: int(message["sequence"]))
    recent_events = _bounded_recent_events(
        world.events,
        viewer=name,
        after_sequence=survivor.last_observed_event_sequence,
        limit=world.config.max_recent_events,
    )
    config = world.config
    action_energy_costs = config.action_energy_costs
    if world.interaction_protocol == GLOBAL_BEATS_V2:
        action_energy_costs = {**action_energy_costs, "wait": 0}
    return SurvivorView(
        name=name,
        day=cycle,
        slot=slot,
        slots_remaining=config.slots_per_cycle - slot + 1,
        self_state=survivor.to_view_dict(),
        others=tuple(
            world.survivors[peer].to_public_dict() for peer in living_peers
        ),
        resources=world.resources.to_dict(),
        inbox=tuple(inbox[: config.max_inbox_messages]),
        recent_events=recent_events,
        rules={
            "max_energy": config.max_energy,
            "cycle_energy_cost_after_rest": _cycle_cost(config, survivor),
            "slots_per_cycle": config.slots_per_cycle,
            "slots_remaining": config.slots_per_cycle - slot + 1,
            "final_slot_requires_rest": True,
            "exhaustion_energy_penalty": config.exhaustion_energy_penalty,
            "action_energy_costs": action_energy_costs,
            "forage_food_range": [
                config.forage_min_food,
                config.forage_max_food,
            ],
            "gather_wood_yield": config.gather_wood_yield,
            "food_regeneration": config.food_regeneration,
            "wood_regeneration": config.wood_regeneration,
            "food_energy": config.food_energy,
            "max_food_eaten": config.max_food_eaten,
            "shelter_wood_cost": config.shelter_wood_cost,
            "shelter_cycle_discount": config.shelter_energy_discount,
            "speech_energy_cost": config.speech_energy_cost,
            "max_speech_chars": config.max_speech_chars,
            "death": "energy at or below 0 is permanent death",
        },
        allowed_actions=allowed_survival_actions(
            living_peers=living_peers,
            max_food_eaten=config.max_food_eaten,
            interaction_protocol=world.interaction_protocol,
        ),
        prior_public_record=world.prior_public_record,
        interaction_protocol=world.interaction_protocol,
    )


def run_survival(
    world: SurvivalWorld,
    providers: Mapping[str, SurvivalChoiceProvider],
    *,
    days: int | None = None,
) -> SurvivalResult:
    if days is not None and days < 1:
        raise ValueError("cycles must be positive when provided")
    missing = sorted(set(world.alive_names()) - set(providers))
    if missing:
        raise ValueError(f"missing choice providers for: {', '.join(missing)}")
    initial_state = _snapshot(world, include_observation_history=True)
    event_start = len(world.events)
    remaining_cycles = world.config.max_days - world.day
    requested_cycles = (
        remaining_cycles if days is None else min(days, remaining_cycles)
    )

    def source(
        slot: int, views: Mapping[str, SurvivorView]
    ) -> Mapping[str, object]:
        del slot
        proposals: dict[str, Mapping[str, object]] = {}
        for survivor in _ordered_view_survivors(world, views):
            view = views[survivor.name]
            try:
                proposal = providers[survivor.name].decide(view)
            except Exception as error:  # noqa: BLE001 - retain provider cause.
                raise RuntimeError(
                    f"survival choice provider failed for {survivor.name!r}"
                ) from error
            if not isinstance(proposal, Mapping):
                raise TypeError(
                    f"survival choice provider for {survivor.name!r} "
                    "returned a non-object choice"
                )
            proposals[survivor.name] = proposal
        return proposals

    for _ in range(requested_cycles):
        if world.finished:
            break
        _execute_cycle(world, source)
    return _result_from(world, initial_state, event_start)


def run_survival_cycle(
    world: SurvivalWorld,
    proposals_by_slot: Sequence[Mapping[str, object]],
) -> tuple[SurvivalEvent, ...]:
    if isinstance(proposals_by_slot, Mapping) or not isinstance(
        proposals_by_slot, Sequence
    ):
        raise TypeError("proposals_by_slot must be a sequence of slot maps")
    if not 1 <= len(proposals_by_slot) <= world.config.slots_per_cycle:
        raise ValueError(
            "proposal sequence must contain between 1 and "
            f"{world.config.slots_per_cycle} slot maps"
        )
    if any(not isinstance(proposals, Mapping) for proposals in proposals_by_slot):
        raise TypeError("every slot proposal value must be an object")

    consumed_slots = 0

    def dry_source(
        slot: int, views: Mapping[str, SurvivorView]
    ) -> Mapping[str, object]:
        nonlocal consumed_slots
        del views
        consumed_slots = slot
        try:
            return proposals_by_slot[slot - 1]
        except IndexError as error:
            raise ValueError(f"missing proposal map for slot {slot}") from error

    _execute_cycle(deepcopy(world), dry_source)
    if len(proposals_by_slot) != consumed_slots:
        raise ValueError("proposal sequence contains unreachable slot maps")

    def source(
        slot: int, views: Mapping[str, SurvivorView]
    ) -> Mapping[str, object]:
        del views
        return proposals_by_slot[slot - 1]

    return _execute_cycle(world, source)


def run_survival_day(
    world: SurvivalWorld,
    proposals_by_slot: Sequence[Mapping[str, object]],
) -> tuple[SurvivalEvent, ...]:
    return run_survival_cycle(world, proposals_by_slot)


def replay_survival(result: SurvivalResult) -> SurvivalResult:
    world = _world_from_snapshot(
        result.initial_state,
        event_sequence_offset=result.event_sequence_base,
    )
    event_start = len(world.events)
    records_by_cycle_slot: dict[tuple[int, int], list[dict[str, Any]]] = {}
    for record in result.choice_tape:
        cycle = _strict_cycle_alias(record, context="choice tape")
        slot = int(record.get("slot", 1))
        records_by_cycle_slot.setdefault((cycle, slot), []).append(record)

    cycles = sorted({cycle for cycle, _ in records_by_cycle_slot})
    for cycle in cycles:
        if cycle != world.day + 1:
            raise ValueError(
                f"choice tape skips from cycle {world.day} to cycle {cycle}"
            )
        used_slots: set[int] = set()

        def source(
            slot: int, views: Mapping[str, SurvivorView]
        ) -> Mapping[str, object]:
            records = records_by_cycle_slot.get((cycle, slot))
            if records is None:
                raise ValueError(
                    f"choice tape has no records for cycle {cycle} slot {slot}"
                )
            used_slots.add(slot)
            expected_names = list(views)
            actual_names = [str(record["actor"]) for record in records]
            if actual_names != expected_names:
                raise ValueError(
                    f"choice tape actors for cycle {cycle} slot {slot} "
                    "do not match awake seat order"
                )
            proposals: dict[str, object] = {}
            for record in records:
                actor = str(record["actor"])
                actual_view_hash = _view_sha256(views[actor])
                if actual_view_hash != record["view_sha256"]:
                    raise ValueError(
                        f"choice tape view hash mismatch for {actor!r} "
                        f"on cycle {cycle} slot {slot}"
                    )
                proposals[actor] = record["raw_choice"]
            return proposals

        _execute_cycle(world, source)
        recorded_slots = {
            slot for recorded_cycle, slot in records_by_cycle_slot if recorded_cycle == cycle
        }
        if used_slots != recorded_slots:
            raise ValueError(f"choice tape has unreachable slots in cycle {cycle}")

    replayed = _result_from(
        world,
        _canonical_json_value(result.initial_state),
        event_start,
    )
    if (
        replayed.final_state != result.final_state
        or replayed.events != result.events
        or replayed.choice_tape != result.choice_tape
    ):
        raise ValueError("choice tape replay does not match the recorded result")
    return replayed


def _execute_cycle(
    world: SurvivalWorld,
    source: _ProposalSource,
) -> tuple[SurvivalEvent, ...]:
    if world.finished:
        raise RuntimeError(
            f"survival world is already finished: {world.finished_reason}"
        )
    cycle = world.day + 1
    event_start = len(world.events)
    for survivor in world.living_by_seat():
        survivor.resting = False
    world.slot = 0
    _emit(
        world,
        cycle,
        0,
        "cycle_started",
        None,
        survivors=world.alive_names(),
        slots=world.config.slots_per_cycle,
    )
    for slot in range(1, world.config.slots_per_cycle + 1):
        awake = _awake_by_seat(world)
        if not awake:
            break
        world.slot = slot
        _emit(
            world,
            cycle,
            slot,
            "slot_started",
            None,
            awake=[survivor.name for survivor in awake],
        )
        views = {
            survivor.name: survival_view_for(world, survivor.name)
            for survivor in awake
        }
        proposals = source(slot, views)
        _run_slot(world, cycle, slot, awake, views, proposals)

    _force_collapse(world, cycle)
    _apply_cycle_cost(world, cycle)
    _regenerate_resources(world, cycle)
    world.day = cycle
    world.slot = 0
    for survivor in world.living_by_seat():
        survivor.resting = False
    _finalize_survival(world, cycle)
    return tuple(world.events[event_start:])


def _run_slot(
    world: SurvivalWorld,
    cycle: int,
    slot: int,
    awake: Sequence[Survivor],
    views: Mapping[str, SurvivorView],
    proposals: Mapping[str, object],
) -> None:
    if not isinstance(proposals, Mapping):
        raise TypeError(f"slot {slot} proposals must be an object")
    awake_names = [survivor.name for survivor in awake]
    unknown = sorted(set(proposals) - set(awake_names))
    if unknown:
        raise ValueError(
            f"slot {slot} choices reference unavailable survivors: {', '.join(unknown)}"
        )
    missing = [name for name in awake_names if name not in proposals]
    if missing:
        raise ValueError(
            f"slot {slot} is missing choices for awake survivors: {', '.join(missing)}"
        )
    raw_choices = {
        name: _canonical_json_value(proposals[name]) for name in awake_names
    }
    observed_sequence = world.event_sequence_offset + len(world.events)
    living_names = world.alive_names()
    parsed: dict[str, ParsedSurvivalChoice] = {}
    for survivor in awake:
        raw_choice = raw_choices[survivor.name]
        peers = [name for name in living_names if name != survivor.name]
        _emit(
            world,
            cycle,
            slot,
            "choice_submitted",
            survivor.name,
            view_sha256=_view_sha256(views[survivor.name]),
            raw_choice=raw_choice,
        )
        choice = parse_survival_choice(
            raw_choice,
            actor=survivor.name,
            living_peers=peers,
            max_food_eaten=world.config.max_food_eaten,
            max_speech_chars=world.config.max_speech_chars,
            interaction_protocol=world.interaction_protocol,
        )
        parsed[survivor.name] = choice
        _emit(
            world,
            cycle,
            slot,
            "choice_recorded",
            survivor.name,
            choice=choice.to_dict(),
        )
        if choice.action_error is not None:
            _emit(
                world,
                cycle,
                slot,
                "action_rejected",
                survivor.name,
                reason=choice.action_error,
                fallback="no_action",
            )
        if choice.speech_error is not None:
            _emit(
                world,
                cycle,
                slot,
                "speech_rejected",
                survivor.name,
                reason=choice.speech_error,
            )
    for survivor in awake:
        survivor.last_observed_event_sequence = observed_sequence

    resolving = list(awake)
    if slot == world.config.slots_per_cycle:
        resolving = []
        for survivor in awake:
            choice = parsed[survivor.name]
            if choice.action_error is None and choice.action.kind == "rest":
                resolving.append(survivor)
            else:
                _emit(
                    world,
                    cycle,
                    slot,
                    "deadline_choice_cancelled",
                    survivor.name,
                    attempted_choice=parsed[survivor.name].to_dict(),
                )

    _charge_choice_costs(world, cycle, slot, resolving, parsed)
    active = [survivor for survivor in resolving if survivor.alive]
    valid_action = [
        survivor
        for survivor in active
        if parsed[survivor.name].action_error is None
    ]
    _resolve_wait(world, cycle, slot, valid_action, parsed)
    _resolve_forage(world, cycle, slot, valid_action, parsed)
    _resolve_wood_gathering(world, cycle, slot, valid_action, parsed)
    _resolve_gifts(world, cycle, slot, valid_action, parsed)
    _resolve_personal_actions(world, cycle, slot, valid_action, parsed)
    _resolve_speech(world, cycle, slot, active, parsed)


def _charge_choice_costs(
    world: SurvivalWorld,
    cycle: int,
    slot: int,
    survivors: Sequence[Survivor],
    parsed: Mapping[str, ParsedSurvivalChoice],
) -> None:
    costs = world.config.action_energy_costs
    for survivor in survivors:
        choice = parsed[survivor.name]
        action_cost = (
            (
                0
                if choice.action.kind == "wait"
                else costs[choice.action.kind]
            )
            if choice.action_error is None
            else 0
        )
        speech_cost = (
            world.config.speech_energy_cost if choice.speech is not None else 0
        )
        survivor.energy -= action_cost + speech_cost
        _emit(
            world,
            cycle,
            slot,
            "choice_energy_paid",
            survivor.name,
            action=choice.action.kind,
            action_cost=action_cost,
            speech_cost=speech_cost,
            energy_after=survivor.energy,
        )
    for survivor in survivors:
        if survivor.energy <= 0:
            _die(world, cycle, slot, survivor, "choice_energy_depleted")


def _resolve_wait(
    world: SurvivalWorld,
    cycle: int,
    slot: int,
    active: Sequence[Survivor],
    parsed: Mapping[str, ParsedSurvivalChoice],
) -> None:
    for survivor in active:
        if parsed[survivor.name].action.kind == "wait":
            _emit(world, cycle, slot, "wait_completed", survivor.name)


def _resolve_forage(
    world: SurvivalWorld,
    cycle: int,
    slot: int,
    active: Sequence[Survivor],
    parsed: Mapping[str, ParsedSurvivalChoice],
) -> None:
    foragers = [
        survivor
        for survivor in active
        if parsed[survivor.name].action.kind == "forage"
    ]
    order = _resolution_order(world, cycle, slot, foragers, "forage")
    wanted = {
        survivor.seat_id: _stable_range(
            world.seed,
            cycle,
            slot,
            survivor.seat_id,
            "forage-yield",
            world.config.forage_min_food,
            world.config.forage_max_food,
        )
        for survivor in order
    }
    gathered = {survivor.seat_id: 0 for survivor in order}
    for survivor in order:
        if world.resources.food > 0:
            world.resources.food -= 1
            gathered[survivor.seat_id] += 1
    for survivor in order:
        extra = min(
            wanted[survivor.seat_id] - gathered[survivor.seat_id],
            world.resources.food,
        )
        world.resources.food -= extra
        gathered[survivor.seat_id] += extra
    for survivor in order:
        amount = gathered[survivor.seat_id]
        survivor.food += amount
        _emit(
            world,
            cycle,
            slot,
            "food_foraged",
            survivor.name,
            food_gathered=amount,
            food_available=world.resources.food,
        )


def _resolve_wood_gathering(
    world: SurvivalWorld,
    cycle: int,
    slot: int,
    active: Sequence[Survivor],
    parsed: Mapping[str, ParsedSurvivalChoice],
) -> None:
    gatherers = [
        survivor
        for survivor in active
        if parsed[survivor.name].action.kind == "gather_wood"
    ]
    for survivor in _resolution_order(
        world, cycle, slot, gatherers, "gather-wood"
    ):
        gathered = min(world.config.gather_wood_yield, world.resources.wood)
        world.resources.wood -= gathered
        survivor.wood += gathered
        _emit(
            world,
            cycle,
            slot,
            "wood_gathered",
            survivor.name,
            wood_gathered=gathered,
            wood_available=world.resources.wood,
        )


def _resolve_gifts(
    world: SurvivalWorld,
    cycle: int,
    slot: int,
    active: Sequence[Survivor],
    parsed: Mapping[str, ParsedSurvivalChoice],
) -> None:
    if world.interaction_protocol == SLOTS_V1:
        _resolve_gifts_slots_v1(world, cycle, slot, active, parsed)
        return
    _resolve_gifts_global_beats_v2(world, cycle, slot, active, parsed)


def _resolve_gifts_slots_v1(
    world: SurvivalWorld,
    cycle: int,
    slot: int,
    active: Sequence[Survivor],
    parsed: Mapping[str, ParsedSurvivalChoice],
) -> None:
    givers = [
        survivor
        for survivor in active
        if parsed[survivor.name].action.kind in {"give_food", "give_wood"}
    ]
    for survivor in _resolution_order(world, cycle, slot, givers, "give"):
        action = parsed[survivor.name].action
        resource = "food" if action.kind == "give_food" else "wood"
        target = world.survivors[str(action.payload["target"])]
        amount = int(action.payload["amount"])
        if not target.alive:
            _reject_resolution(
                world,
                cycle,
                slot,
                survivor,
                "gift target died before resolution",
            )
            continue
        if int(getattr(survivor, resource)) < amount:
            _reject_resolution(
                world,
                cycle,
                slot,
                survivor,
                f"not enough {resource} to give",
            )
            continue
        setattr(survivor, resource, int(getattr(survivor, resource)) - amount)
        setattr(target, resource, int(getattr(target, resource)) + amount)
        _emit(
            world,
            cycle,
            slot,
            "resource_given",
            survivor.name,
            target=target.name,
            resource=resource,
            amount=amount,
        )


def _resolve_gifts_global_beats_v2(
    world: SurvivalWorld,
    cycle: int,
    slot: int,
    active: Sequence[Survivor],
    parsed: Mapping[str, ParsedSurvivalChoice],
) -> None:
    givers = [
        survivor
        for survivor in active
        if parsed[survivor.name].action.kind in {"give_food", "give_wood"}
    ]
    phase_start_holdings = {
        (survivor.name, resource): int(getattr(survivor, resource))
        for survivor in givers
        for resource in ("food", "wood")
    }
    transfers: list[tuple[Survivor, Survivor, str, int]] = []
    for survivor in _resolution_order(world, cycle, slot, givers, "give"):
        action = parsed[survivor.name].action
        resource = "food" if action.kind == "give_food" else "wood"
        target = world.survivors[str(action.payload["target"])]
        amount = int(action.payload["amount"])
        if not target.alive:
            _reject_resolution(
                world,
                cycle,
                slot,
                survivor,
                "gift target died before resolution",
            )
            continue
        if phase_start_holdings[(survivor.name, resource)] < amount:
            _reject_resolution(
                world,
                cycle,
                slot,
                survivor,
                f"not enough {resource} to give at transfer-phase start",
            )
            continue
        transfers.append((survivor, target, resource, amount))

    for survivor, target, resource, amount in transfers:
        setattr(survivor, resource, int(getattr(survivor, resource)) - amount)
        setattr(target, resource, int(getattr(target, resource)) + amount)
        _emit(
            world,
            cycle,
            slot,
            "resource_given",
            survivor.name,
            target=target.name,
            resource=resource,
            amount=amount,
        )


def _resolve_personal_actions(
    world: SurvivalWorld,
    cycle: int,
    slot: int,
    active: Sequence[Survivor],
    parsed: Mapping[str, ParsedSurvivalChoice],
) -> None:
    personal = [
        survivor
        for survivor in active
        if parsed[survivor.name].action.kind
        in {"rest", "eat", "build_shelter"}
    ]
    for survivor in _resolution_order(world, cycle, slot, personal, "personal"):
        action = parsed[survivor.name].action
        if action.kind == "rest":
            survivor.resting = True
            _emit(world, cycle, slot, "rest_started", survivor.name)
        elif action.kind == "eat":
            _eat(world, cycle, slot, survivor, action)
        else:
            _build_shelter(world, cycle, slot, survivor)


def _eat(
    world: SurvivalWorld,
    cycle: int,
    slot: int,
    survivor: Survivor,
    action: SurvivalAction,
) -> None:
    amount = int(action.payload["amount"])
    if survivor.food < amount:
        _reject_resolution(
            world, cycle, slot, survivor, "not enough food to eat"
        )
        return
    survivor.food -= amount
    before = survivor.energy
    survivor.energy = min(
        world.config.max_energy,
        survivor.energy + amount * world.config.food_energy,
    )
    _emit(
        world,
        cycle,
        slot,
        "food_eaten",
        survivor.name,
        food_eaten=amount,
        energy_gained=survivor.energy - before,
    )


def _build_shelter(
    world: SurvivalWorld,
    cycle: int,
    slot: int,
    survivor: Survivor,
) -> None:
    if survivor.shelter:
        _reject_resolution(
            world, cycle, slot, survivor, "shelter is already built"
        )
        return
    if survivor.wood < world.config.shelter_wood_cost:
        _reject_resolution(
            world, cycle, slot, survivor, "not enough wood to build shelter"
        )
        return
    survivor.wood -= world.config.shelter_wood_cost
    survivor.shelter = True
    _emit(
        world,
        cycle,
        slot,
        "shelter_built",
        survivor.name,
        wood_spent=world.config.shelter_wood_cost,
    )


def _resolve_speech(
    world: SurvivalWorld,
    cycle: int,
    slot: int,
    active: Sequence[Survivor],
    parsed: Mapping[str, ParsedSurvivalChoice],
) -> None:
    speakers = [
        survivor for survivor in active if parsed[survivor.name].speech is not None
    ]
    for survivor in _resolution_order(world, cycle, slot, speakers, "speech"):
        speech = parsed[survivor.name].speech
        if speech is None:
            continue
        if (
            speech.recipient != "everyone"
            and not world.survivors[speech.recipient].alive
        ):
            _emit(
                world,
                cycle,
                slot,
                "speech_resolution_rejected",
                survivor.name,
                reason="recipient died before resolution",
            )
            continue
        sequence = world.event_sequence_offset + len(world.events) + 1
        message = SpokenMessage(
            message_id=f"message-{cycle}-{slot}-{len(world.messages) + 1}",
            sequence=sequence,
            cycle=cycle,
            slot=slot,
            speaker=survivor.name,
            recipient=speech.recipient,
            text=speech.text,
        )
        world.messages.append(message)
        _emit(
            world,
            cycle,
            slot,
            "speech_sent",
            survivor.name,
            message=message.to_dict(),
        )


def _force_collapse(world: SurvivalWorld, cycle: int) -> None:
    slot = world.config.slots_per_cycle
    for survivor in _awake_by_seat(world):
        survivor.energy -= world.config.exhaustion_energy_penalty
        _emit(
            world,
            cycle,
            slot,
            "forced_collapse",
            survivor.name,
            energy_penalty=world.config.exhaustion_energy_penalty,
            energy_after=survivor.energy,
        )
        if survivor.energy <= 0:
            _die(world, cycle, slot, survivor, "exhaustion_energy_depleted")
        else:
            survivor.resting = True


def _apply_cycle_cost(world: SurvivalWorld, cycle: int) -> None:
    living = world.living_by_seat()
    for survivor in living:
        cost = _cycle_cost(world.config, survivor)
        survivor.energy -= cost
        _emit(
            world,
            cycle,
            world.config.slots_per_cycle,
            "cycle_energy_paid",
            survivor.name,
            amount=cost,
            energy_after=survivor.energy,
        )
    for survivor in living:
        if survivor.energy <= 0:
            _die(
                world,
                cycle,
                world.config.slots_per_cycle,
                survivor,
                "cycle_energy_depleted",
            )


def _cycle_cost(config: SurvivalConfig, survivor: Survivor) -> int:
    discount = config.shelter_energy_discount if survivor.shelter else 0
    return max(1, config.daily_energy_cost - discount)


def _regenerate_resources(world: SurvivalWorld, cycle: int) -> None:
    world.resources.food = min(
        world.resources.food_capacity,
        world.resources.food + world.config.food_regeneration,
    )
    world.resources.wood = min(
        world.resources.wood_capacity,
        world.resources.wood + world.config.wood_regeneration,
    )
    _emit(
        world,
        cycle,
        world.config.slots_per_cycle,
        "resources_regenerated",
        None,
        resources=world.resources.to_dict(),
    )


def _die(
    world: SurvivalWorld,
    cycle: int,
    slot: int,
    survivor: Survivor,
    cause: str,
) -> None:
    if not survivor.alive:
        return
    survivor.alive = False
    survivor.resting = True
    survivor.energy = 0
    survivor.died_on_day = cycle
    _emit(world, cycle, slot, "survivor_died", survivor.name, cause=cause)


def _finalize_survival(world: SurvivalWorld, cycle: int) -> None:
    if not world.alive_names():
        world.finished_reason = "everyone_died"
    elif cycle >= world.config.max_days:
        world.finished_reason = "cycle_limit_reached"
    if world.finished_reason is not None:
        _emit(
            world,
            cycle,
            world.config.slots_per_cycle,
            "world_finished",
            None,
            reason=world.finished_reason,
        )


def _awake_by_seat(world: SurvivalWorld) -> list[Survivor]:
    return [survivor for survivor in world.living_by_seat() if not survivor.resting]


def _ordered_view_survivors(
    world: SurvivalWorld, views: Mapping[str, SurvivorView]
) -> list[Survivor]:
    return [
        survivor
        for survivor in world.living_by_seat()
        if survivor.name in views
    ]


def _resolution_order(
    world: SurvivalWorld,
    cycle: int,
    slot: int,
    survivors: Sequence[Survivor],
    namespace: str,
) -> list[Survivor]:
    order = list(sorted(survivors, key=lambda survivor: survivor.seat_id))
    random.Random(
        _stable_int(f"order:{namespace}:{world.seed}:{cycle}:{slot}")
    ).shuffle(order)
    return order


def _stable_range(
    seed: int,
    cycle: int,
    slot: int,
    opaque_id: str,
    namespace: str,
    minimum: int,
    maximum: int,
) -> int:
    return minimum + (
        _stable_int(f"{namespace}:{seed}:{cycle}:{slot}:{opaque_id}")
        % (maximum - minimum + 1)
    )


def _stable_int(value: str) -> int:
    return int.from_bytes(
        hashlib.sha256(value.encode("utf-8")).digest()[:8], "big"
    )


def _view_sha256(view: SurvivorView) -> str:
    payload = json.dumps(
        view.to_dict(),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _canonical_json_value(value: object) -> Any:
    try:
        encoded = json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError) as error:
        raise TypeError("survival choices must be JSON-serializable values") from error
    return json.loads(encoded)


def _choice_tape(events: Sequence[SurvivalEvent]) -> tuple[dict[str, Any], ...]:
    return tuple(
        {
            "day": event.day,
            "cycle": event.day,
            "slot": event.slot,
            "actor": event.actor,
            "view_sha256": event.detail["view_sha256"],
            "raw_choice": event.detail["raw_choice"],
        }
        for event in events
        if event.kind == "choice_submitted"
    )


def _prior_public_record_from_result(
    result: SurvivalResult,
    world: SurvivalWorld,
) -> PriorPublicRecord:
    cycle = _strict_cycle_alias(result.final_state, context="parent final state")
    messages_by_speaker: dict[str, list[Mapping[str, Any]]] = {
        name: [] for name in world.survivors
    }
    raw_messages = result.final_state.get("messages")
    if not isinstance(raw_messages, list):
        raise TypeError("parent final state messages must be a list")
    for message in raw_messages:
        if not isinstance(message, Mapping):
            raise TypeError("parent final state message must be an object")
        if _strict_cycle_alias(message, context="parent message") != cycle:
            continue
        if message.get("recipient") != "everyone":
            continue
        speaker = str(message.get("speaker", ""))
        if speaker not in messages_by_speaker:
            raise ValueError(f"parent message has unknown speaker {speaker!r}")
        messages_by_speaker[speaker].append(message)

    statements: list[PriorPublicStatement] = []
    for survivor in sorted(
        world.survivors.values(), key=lambda item: item.seat_id
    ):
        candidates = messages_by_speaker[survivor.name]
        if not candidates:
            raise ValueError(
                f"parent cycle has no public broadcast from {survivor.name!r}"
            )
        selected = max(candidates, key=lambda item: int(item["sequence"]))
        statements.append(
            PriorPublicStatement(
                message_id=str(selected["id"]),
                sequence=int(selected["sequence"]),
                cycle=cycle,
                slot=int(selected["slot"]),
                speaker=survivor.name,
                recipient="everyone",
                text=str(selected["text"]),
            )
        )

    return PriorPublicRecord(
        cycle=cycle,
        statements=tuple(statements),
        completed_resource_transfers=sum(
            event["kind"] == "resource_given" for event in result.events
        ),
        shelters_built=sum(
            event["kind"] == "shelter_built" for event in result.events
        ),
    )


def _prior_public_record_from_dict(
    value: object,
    *,
    survivor_names: set[str],
    world_cycle: int,
) -> PriorPublicRecord | None:
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise TypeError("prior_public_record must be an object")
    if set(value) != {
        "cycle",
        "selection_rule",
        "statements",
        "objective_totals",
    }:
        raise ValueError("prior_public_record has unexpected fields")
    if value["selection_rule"] != "final_public_broadcast_per_identity":
        raise ValueError("prior_public_record selection_rule is invalid")
    cycle = value["cycle"]
    if isinstance(cycle, bool) or not isinstance(cycle, int) or cycle < 1:
        raise ValueError("prior_public_record cycle must be a positive integer")
    if cycle != world_cycle:
        raise ValueError("prior_public_record cycle must match the world cycle")

    raw_statements = value["statements"]
    if not isinstance(raw_statements, list):
        raise TypeError("prior_public_record statements must be a list")
    statements: list[PriorPublicStatement] = []
    seen_speakers: set[str] = set()
    expected_statement_fields = {
        "cycle",
        "message_id",
        "sequence",
        "slot",
        "speaker",
        "recipient",
        "text",
        "verification",
    }
    for raw_statement in raw_statements:
        if not isinstance(raw_statement, Mapping):
            raise TypeError("prior public statement must be an object")
        if set(raw_statement) != expected_statement_fields:
            raise ValueError("prior public statement has unexpected fields")
        if raw_statement["verification"] != "unverified":
            raise ValueError("prior public statement must be marked unverified")
        message_id = raw_statement["message_id"]
        if not isinstance(message_id, str) or not message_id:
            raise ValueError("prior public statement message_id must be non-empty")
        sequence = raw_statement["sequence"]
        if (
            isinstance(sequence, bool)
            or not isinstance(sequence, int)
            or sequence < 1
        ):
            raise ValueError("prior public statement sequence must be positive")
        statement_cycle = raw_statement["cycle"]
        slot = raw_statement["slot"]
        if statement_cycle != cycle:
            raise ValueError("prior public statement cycle does not match record")
        if isinstance(slot, bool) or not isinstance(slot, int) or slot < 1:
            raise ValueError("prior public statement slot must be positive")
        speaker = raw_statement["speaker"]
        if not isinstance(speaker, str) or speaker not in survivor_names:
            raise ValueError("prior public statement has an unknown speaker")
        if speaker in seen_speakers:
            raise ValueError("prior public record has duplicate speakers")
        if raw_statement["recipient"] != "everyone":
            raise ValueError("prior public statement must be a broadcast")
        text = raw_statement["text"]
        if not isinstance(text, str) or not text:
            raise ValueError("prior public statement text must be non-empty")
        seen_speakers.add(speaker)
        statements.append(
            PriorPublicStatement(
                message_id=message_id,
                sequence=sequence,
                cycle=cycle,
                slot=slot,
                speaker=speaker,
                recipient="everyone",
                text=text,
            )
        )
    if seen_speakers != survivor_names:
        raise ValueError("prior public record must contain every survivor")

    objective_totals = value["objective_totals"]
    if not isinstance(objective_totals, Mapping) or set(objective_totals) != {
        "completed_resource_transfers",
        "shelters_built",
    }:
        raise ValueError("prior_public_record objective_totals are invalid")
    totals: dict[str, int] = {}
    for key in ("completed_resource_transfers", "shelters_built"):
        total = objective_totals[key]
        if isinstance(total, bool) or not isinstance(total, int) or total < 0:
            raise ValueError(f"prior_public_record {key} must be non-negative")
        totals[key] = total
    return PriorPublicRecord(
        cycle=cycle,
        statements=tuple(statements),
        completed_resource_transfers=totals["completed_resource_transfers"],
        shelters_built=totals["shelters_built"],
    )


def _world_from_snapshot(
    snapshot: Mapping[str, Any],
    *,
    event_sequence_offset: int,
) -> SurvivalWorld:
    config = SurvivalConfig(**dict(snapshot["config"]))
    interaction_protocol = validate_interaction_protocol(
        snapshot.get("interaction_protocol", SLOTS_V1)
    )
    survivors = {
        str(item["name"]): Survivor(
            seat_id=str(item["seat_id"]),
            name=str(item["name"]),
            energy=int(item["energy"]),
            food=int(item["food"]),
            wood=int(item["wood"]),
            shelter=bool(item["shelter"]),
            resting=bool(item.get("resting", False)),
            last_observed_event_sequence=int(
                item.get("last_observed_event_sequence", event_sequence_offset)
            ),
            alive=bool(item["alive"]),
            died_on_day=(
                int(item["died_on_day"])
                if item["died_on_day"] is not None
                else None
            ),
        )
        for item in snapshot["survivors"]
    }
    resources_data = snapshot["resources"]
    messages = [
        SpokenMessage(
            message_id=str(item["id"]),
            sequence=int(item.get("sequence", 0)),
            cycle=_strict_cycle_alias(item, context="message"),
            slot=int(item.get("slot", 1)),
            speaker=str(item["speaker"]),
            recipient=str(item["recipient"]),
            text=str(item["text"]),
        )
        for item in snapshot["messages"]
    ]
    observation_history = snapshot.get("observation_history")
    events = (
        [
            SurvivalEvent(
                sequence=int(item["sequence"]),
                day=_strict_cycle_alias(item, context="observation event"),
                slot=int(item.get("slot", 0)),
                kind=str(item["kind"]),
                actor=(str(item["actor"]) if item["actor"] is not None else None),
                detail=dict(item["detail"]),
            )
            for item in observation_history
        ]
        if isinstance(observation_history, list)
        else []
    )
    snapshot_event_offset = (
        int(snapshot.get("event_sequence_offset", 0))
        if observation_history is not None
        else event_sequence_offset
    )
    world_cycle = _strict_cycle_alias(snapshot, context="world snapshot")
    prior_public_record = _prior_public_record_from_dict(
        snapshot.get("prior_public_record"),
        survivor_names=set(survivors),
        world_cycle=world_cycle,
    )
    return SurvivalWorld(
        config=config,
        seed=int(snapshot["seed"]),
        survivors=survivors,
        resources=ResourcePool(
            food=int(resources_data["food"]),
            food_capacity=int(resources_data["food_capacity"]),
            wood=int(resources_data["wood"]),
            wood_capacity=int(resources_data["wood_capacity"]),
        ),
        day=world_cycle,
        slot=int(snapshot.get("slot", 0)),
        messages=messages,
        events=events,
        event_sequence_offset=snapshot_event_offset,
        finished_reason=(
            str(snapshot["finished_reason"])
            if snapshot["finished_reason"] is not None
            else None
        ),
        prior_public_record=prior_public_record,
        interaction_protocol=interaction_protocol,
    )


def _reject_resolution(
    world: SurvivalWorld,
    cycle: int,
    slot: int,
    survivor: Survivor,
    reason: str,
) -> None:
    _emit(
        world,
        cycle,
        slot,
        "action_resolution_rejected",
        survivor.name,
        reason=reason,
    )


def _strict_cycle_alias(value: Mapping[str, Any], *, context: str) -> int:
    if "day" not in value and "cycle" not in value:
        raise ValueError(f"{context} has no cycle")
    if "day" in value and "cycle" in value:
        day = int(value["day"])
        cycle = int(value["cycle"])
        if day != cycle:
            raise ValueError(f"{context} day and cycle aliases disagree")
        return cycle
    return int(value["cycle"] if "cycle" in value else value["day"])


def _emit(
    world: SurvivalWorld,
    cycle: int,
    slot: int,
    kind: str,
    actor: str | None,
    **detail: Any,
) -> None:
    world.events.append(
        SurvivalEvent(
            sequence=world.event_sequence_offset + len(world.events) + 1,
            day=cycle,
            slot=slot,
            kind=kind,
            actor=actor,
            detail=detail,
        )
    )


def _event_for_view(
    event: SurvivalEvent, viewer: str
) -> dict[str, Any] | None:
    if event.kind in {
        "cycle_started",
        "slot_started",
        "choice_submitted",
        "speech_sent",
        "world_finished",
    }:
        return None
    if event.actor == viewer:
        return event.to_dict()
    if event.kind == "resource_adjusted":
        rendered = event.to_dict()
        rendered["detail"] = {
            key: event.detail[key]
            for key in ("resource", "before", "after", "delta")
        }
        return rendered
    if event.kind == "resource_given" and event.detail.get("target") == viewer:
        return event.to_dict()
    if event.kind in {
        "action_rejected",
        "speech_rejected",
        "action_resolution_rejected",
        "choice_energy_paid",
        "food_foraged",
        "wood_gathered",
        "food_eaten",
        "wait_completed",
        "resource_given",
        "cycle_energy_paid",
        "deadline_choice_cancelled",
    }:
        return None
    if event.kind in {
        "rest_started",
        "shelter_built",
        "forced_collapse",
        "survivor_died",
        "resources_regenerated",
    }:
        rendered = event.to_dict()
        rendered["detail"] = {}
        return rendered
    return None


def _bounded_recent_events(
    events: Sequence[SurvivalEvent],
    *,
    viewer: str,
    after_sequence: int,
    limit: int,
) -> tuple[dict[str, Any], ...]:
    projected = [
        (event, rendered)
        for event in events
        if event.sequence > after_sequence
        and (rendered := _event_for_view(event, viewer)) is not None
    ]
    if len(projected) <= limit:
        return tuple(rendered for _, rendered in projected)
    own = [pair for pair in projected if pair[0].actor == viewer]
    selected = own[-limit:]
    selected_sequences = {event.sequence for event, _ in selected}
    remaining = limit - len(selected)
    if remaining > 0:
        public = [
            pair for pair in projected if pair[0].sequence not in selected_sequences
        ]
        selected.extend(public[-remaining:])
    selected.sort(key=lambda pair: pair[0].sequence)
    return tuple(rendered for _, rendered in selected)


def _result_from(
    world: SurvivalWorld,
    initial_state: dict[str, Any],
    event_start: int,
) -> SurvivalResult:
    return SurvivalResult(
        initial_state=initial_state,
        final_state=_snapshot(world),
        events=tuple(event.to_dict() for event in world.events[event_start:]),
        choice_tape=_choice_tape(world.events[event_start:]),
        event_sequence_base=world.event_sequence_offset + event_start,
    )


def _snapshot(
    world: SurvivalWorld, *, include_observation_history: bool = False
) -> dict[str, Any]:
    snapshot = world.to_dict(include_events=False)
    if include_observation_history:
        snapshot["event_sequence_offset"] = world.event_sequence_offset
        snapshot["observation_history"] = [
            event.to_dict() for event in world.events
        ]
    return snapshot
