from __future__ import annotations

import hashlib
import json
import random
import re
from collections.abc import Mapping, Sequence
from typing import Any, Protocol

from .models import (
    DEFAULT_SURVIVOR_NAMES,
    ResourcePool,
    SpokenMessage,
    SurvivalConfig,
    SurvivalEvent,
    SurvivalResult,
    Survivor,
    SurvivorView,
    SurvivalWorld,
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


def make_survival_world(
    names: Sequence[str] = DEFAULT_SURVIVOR_NAMES,
    *,
    seed: int,
    config: SurvivalConfig | None = None,
) -> SurvivalWorld:
    active_config = config or SurvivalConfig()
    clean_names = tuple(name.strip() for name in names)
    if len(clean_names) < 2:
        raise ValueError("a survival world needs at least two named survivors")
    if len(clean_names) - 1 > active_config.max_inbox_messages:
        raise ValueError("max_inbox_messages must hold one message from every peer")
    if any(
        re.fullmatch(r"[A-Za-z][A-Za-z'-]{0,31}", name) is None for name in clean_names
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
    )


def survival_view_for(world: SurvivalWorld, name: str) -> SurvivorView:
    survivor = world.survivors.get(name)
    if survivor is None:
        raise ValueError(f"unknown survivor {name!r}")
    if not survivor.alive:
        raise ValueError(f"dead survivor {name!r} cannot receive a world view")
    living_peers = [peer for peer in world.alive_names() if peer != name]
    delivery_day = world.day + 1
    inbox = [
        message.to_dict()
        for message in world.messages
        if message.day == delivery_day
        and (message.recipient == name or message.recipient == "everyone")
        and message.speaker != name
    ]
    inbox.sort(
        key=lambda message: (
            world.survivors[str(message["speaker"])].seat_id,
            str(message["id"]),
        )
    )
    config = world.config
    daily_cost = _daily_cost(config, survivor)
    return SurvivorView(
        name=name,
        day=delivery_day,
        self_state=survivor.to_view_dict(),
        others=tuple(world.survivors[peer].to_public_dict() for peer in living_peers),
        resources=world.resources.to_dict(),
        inbox=tuple(inbox[: config.max_inbox_messages]),
        rules={
            "max_energy": config.max_energy,
            "daily_energy_cost_tonight": daily_cost,
            "action_energy_costs": config.action_energy_costs,
            "forage_food_range": [config.forage_min_food, config.forage_max_food],
            "gather_wood_yield": config.gather_wood_yield,
            "food_regeneration": config.food_regeneration,
            "wood_regeneration": config.wood_regeneration,
            "food_energy": config.food_energy,
            "max_food_eaten": config.max_food_eaten,
            "shelter_wood_cost": config.shelter_wood_cost,
            "shelter_daily_discount": config.shelter_energy_discount,
            "speech_energy_cost": config.speech_energy_cost,
            "max_speech_chars": config.max_speech_chars,
            "death": "energy at or below 0 is permanent death",
        },
        allowed_actions=allowed_survival_actions(
            living_peers=living_peers,
            max_food_eaten=config.max_food_eaten,
        ),
    )


def run_survival(
    world: SurvivalWorld,
    providers: Mapping[str, SurvivalChoiceProvider],
    *,
    days: int | None = None,
) -> SurvivalResult:
    if days is not None and days < 1:
        raise ValueError("days must be positive when provided")
    missing = sorted(set(world.alive_names()) - set(providers))
    if missing:
        raise ValueError(f"missing choice providers for: {', '.join(missing)}")
    initial_state = _snapshot(world)
    event_start = len(world.events)
    remaining_days = world.config.max_days - world.day
    requested_days = remaining_days if days is None else min(days, remaining_days)
    for _ in range(requested_days):
        if world.finished:
            break
        proposals: dict[str, Mapping[str, object]] = {}
        for survivor in world.living_by_seat():
            view = survival_view_for(world, survivor.name)
            try:
                proposal = providers[survivor.name].decide(view)
            except Exception as error:  # noqa: BLE001 - provider failures must retain their cause.
                raise RuntimeError(
                    f"survival choice provider failed for {survivor.name!r}"
                ) from error
            if not isinstance(proposal, Mapping):
                raise TypeError(
                    f"survival choice provider for {survivor.name!r} returned a non-object choice"
                )
            proposals[survivor.name] = proposal
        run_survival_day(world, proposals)
    return SurvivalResult(
        initial_state=initial_state,
        final_state=_snapshot(world),
        events=tuple(event.to_dict() for event in world.events[event_start:]),
        choice_tape=_choice_tape(world.events[event_start:]),
        event_sequence_base=world.event_sequence_offset + event_start,
    )


def replay_survival(result: SurvivalResult) -> SurvivalResult:
    world = _world_from_snapshot(
        result.initial_state,
        event_sequence_offset=result.event_sequence_base,
    )
    event_start = len(world.events)
    records_by_day: dict[int, list[dict[str, Any]]] = {}
    for record in result.choice_tape:
        day = int(record["day"])
        records_by_day.setdefault(day, []).append(record)

    for day in sorted(records_by_day):
        if day != world.day + 1:
            raise ValueError(f"choice tape skips from day {world.day} to day {day}")
        records = records_by_day[day]
        expected_names = [survivor.name for survivor in world.living_by_seat()]
        actual_names = [str(record["actor"]) for record in records]
        if actual_names != expected_names:
            raise ValueError(
                f"choice tape actors for day {day} do not match living seat order"
            )
        proposals: dict[str, object] = {}
        for record in records:
            actor = str(record["actor"])
            actual_view_hash = _view_sha256(survival_view_for(world, actor))
            if actual_view_hash != record["view_sha256"]:
                raise ValueError(
                    f"choice tape view hash mismatch for {actor!r} on day {day}"
                )
            proposals[actor] = record["raw_choice"]
        run_survival_day(world, proposals)

    replayed = SurvivalResult(
        initial_state=_canonical_json_value(result.initial_state),
        final_state=_snapshot(world),
        events=tuple(event.to_dict() for event in world.events[event_start:]),
        choice_tape=_choice_tape(world.events[event_start:]),
        event_sequence_base=result.event_sequence_base,
    )
    if replayed.final_state != result.final_state or replayed.events != result.events:
        raise ValueError("choice tape replay does not match the recorded result")
    return replayed


def run_survival_day(
    world: SurvivalWorld,
    proposals: Mapping[str, object],
) -> tuple[SurvivalEvent, ...]:
    if world.finished:
        raise RuntimeError(
            f"survival world is already finished: {world.finished_reason}"
        )
    unknown = sorted(set(proposals) - set(world.survivors))
    if unknown:
        raise ValueError(f"choices reference unknown survivors: {', '.join(unknown)}")
    living = world.living_by_seat()
    dead_submitters = sorted(
        name for name in proposals if not world.survivors[name].alive
    )
    if dead_submitters:
        raise ValueError(
            f"dead survivors cannot submit choices: {', '.join(dead_submitters)}"
        )
    missing = [survivor.name for survivor in living if survivor.name not in proposals]
    if missing:
        raise ValueError(f"missing choices for living survivors: {', '.join(missing)}")

    day = world.day + 1
    event_start = len(world.events)
    start_names = [survivor.name for survivor in living]
    _emit(world, day, "day_started", None, survivors=start_names)
    parsed = _parse_day_choices(world, day, living, proposals)
    _charge_choice_costs(world, day, living, parsed)

    active = [survivor for survivor in living if survivor.alive]
    _resolve_forage(world, day, active, parsed)
    _resolve_wood_gathering(world, day, active, parsed)
    _resolve_gifts(world, day, active, parsed)
    _resolve_personal_actions(world, day, active, parsed)
    _resolve_speech(world, day, active, parsed)
    _apply_daily_cost(world, day)
    _regenerate_resources(world, day)
    world.day = day
    _finalize_survival(world, day)
    return tuple(world.events[event_start:])


def _parse_day_choices(
    world: SurvivalWorld,
    day: int,
    living: Sequence[Survivor],
    proposals: Mapping[str, object],
) -> dict[str, ParsedSurvivalChoice]:
    living_names = [survivor.name for survivor in living]
    parsed: dict[str, ParsedSurvivalChoice] = {}
    for survivor in living:
        peers = [name for name in living_names if name != survivor.name]
        raw_choice = _canonical_json_value(proposals[survivor.name])
        _emit(
            world,
            day,
            "choice_submitted",
            survivor.name,
            view_sha256=_view_sha256(survival_view_for(world, survivor.name)),
            raw_choice=raw_choice,
        )
        choice = parse_survival_choice(
            raw_choice,
            actor=survivor.name,
            living_peers=peers,
            max_food_eaten=world.config.max_food_eaten,
            max_speech_chars=world.config.max_speech_chars,
        )
        parsed[survivor.name] = choice
        _emit(world, day, "choice_recorded", survivor.name, choice=choice.to_dict())
        if choice.action_error is not None:
            _emit(
                world,
                day,
                "action_rejected",
                survivor.name,
                reason=choice.action_error,
                fallback="rest",
            )
        if choice.speech_error is not None:
            _emit(
                world, day, "speech_rejected", survivor.name, reason=choice.speech_error
            )
    return parsed


def _charge_choice_costs(
    world: SurvivalWorld,
    day: int,
    living: Sequence[Survivor],
    parsed: Mapping[str, ParsedSurvivalChoice],
) -> None:
    costs = world.config.action_energy_costs
    for survivor in living:
        choice = parsed[survivor.name]
        action_cost = costs[choice.action.kind]
        speech_cost = (
            world.config.speech_energy_cost if choice.speech is not None else 0
        )
        survivor.energy -= action_cost + speech_cost
        _emit(
            world,
            day,
            "choice_energy_paid",
            survivor.name,
            action=choice.action.kind,
            action_cost=action_cost,
            speech_cost=speech_cost,
            energy_after=survivor.energy,
        )
    for survivor in living:
        if survivor.energy <= 0:
            _die(world, day, survivor, "choice_energy_depleted")


def _resolve_forage(
    world: SurvivalWorld,
    day: int,
    active: Sequence[Survivor],
    parsed: Mapping[str, ParsedSurvivalChoice],
) -> None:
    foragers = [
        survivor for survivor in active if parsed[survivor.name].action.kind == "forage"
    ]
    order = _resolution_order(world, day, foragers, "forage")
    wanted = {
        survivor.seat_id: _stable_range(
            world.seed,
            day,
            survivor.seat_id,
            "forage-yield",
            world.config.forage_min_food,
            world.config.forage_max_food,
        )
        for survivor in order
    }
    gathered = {survivor.seat_id: 0 for survivor in order}
    for survivor in order:
        if world.resources.food > 0 and wanted[survivor.seat_id] > 0:
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
            day,
            "food_foraged",
            survivor.name,
            food_gathered=amount,
            food_available=world.resources.food,
        )


def _resolve_wood_gathering(
    world: SurvivalWorld,
    day: int,
    active: Sequence[Survivor],
    parsed: Mapping[str, ParsedSurvivalChoice],
) -> None:
    gatherers = [
        survivor
        for survivor in active
        if parsed[survivor.name].action.kind == "gather_wood"
    ]
    for survivor in _resolution_order(world, day, gatherers, "gather-wood"):
        gathered = min(world.config.gather_wood_yield, world.resources.wood)
        world.resources.wood -= gathered
        survivor.wood += gathered
        _emit(
            world,
            day,
            "wood_gathered",
            survivor.name,
            wood_gathered=gathered,
            wood_available=world.resources.wood,
        )


def _resolve_gifts(
    world: SurvivalWorld,
    day: int,
    active: Sequence[Survivor],
    parsed: Mapping[str, ParsedSurvivalChoice],
) -> None:
    givers = [
        survivor
        for survivor in active
        if parsed[survivor.name].action.kind in {"give_food", "give_wood"}
    ]
    for survivor in _resolution_order(world, day, givers, "give"):
        action = parsed[survivor.name].action
        resource = "food" if action.kind == "give_food" else "wood"
        target = world.survivors[str(action.payload["target"])]
        amount = int(action.payload["amount"])
        if not target.alive:
            _reject_resolution(
                world, day, survivor, "gift target died before resolution"
            )
            continue
        if int(getattr(survivor, resource)) < amount:
            _reject_resolution(world, day, survivor, f"not enough {resource} to give")
            continue
        setattr(survivor, resource, int(getattr(survivor, resource)) - amount)
        setattr(target, resource, int(getattr(target, resource)) + amount)
        _emit(
            world,
            day,
            "resource_given",
            survivor.name,
            target=target.name,
            resource=resource,
            amount=amount,
        )


def _resolve_personal_actions(
    world: SurvivalWorld,
    day: int,
    active: Sequence[Survivor],
    parsed: Mapping[str, ParsedSurvivalChoice],
) -> None:
    personal = [
        survivor
        for survivor in active
        if parsed[survivor.name].action.kind in {"rest", "eat", "build_shelter"}
    ]
    for survivor in _resolution_order(world, day, personal, "personal"):
        action = parsed[survivor.name].action
        if action.kind == "rest":
            _emit(world, day, "rested", survivor.name)
        elif action.kind == "eat":
            _eat(world, day, survivor, action)
        else:
            _build_shelter(world, day, survivor)


def _eat(
    world: SurvivalWorld, day: int, survivor: Survivor, action: SurvivalAction
) -> None:
    amount = int(action.payload["amount"])
    if survivor.food < amount:
        _reject_resolution(world, day, survivor, "not enough food to eat")
        return
    survivor.food -= amount
    before = survivor.energy
    survivor.energy = min(
        world.config.max_energy, survivor.energy + amount * world.config.food_energy
    )
    _emit(
        world,
        day,
        "food_eaten",
        survivor.name,
        food_eaten=amount,
        energy_gained=survivor.energy - before,
    )


def _build_shelter(world: SurvivalWorld, day: int, survivor: Survivor) -> None:
    if survivor.shelter:
        _reject_resolution(world, day, survivor, "shelter is already built")
        return
    if survivor.wood < world.config.shelter_wood_cost:
        _reject_resolution(world, day, survivor, "not enough wood to build shelter")
        return
    survivor.wood -= world.config.shelter_wood_cost
    survivor.shelter = True
    _emit(
        world,
        day,
        "shelter_built",
        survivor.name,
        wood_spent=world.config.shelter_wood_cost,
    )


def _resolve_speech(
    world: SurvivalWorld,
    day: int,
    active: Sequence[Survivor],
    parsed: Mapping[str, ParsedSurvivalChoice],
) -> None:
    speakers = [
        survivor for survivor in active if parsed[survivor.name].speech is not None
    ]
    for survivor in _resolution_order(world, day, speakers, "speech"):
        speech = parsed[survivor.name].speech
        if speech is None:
            continue
        if (
            speech.recipient != "everyone"
            and not world.survivors[speech.recipient].alive
        ):
            _emit(
                world,
                day,
                "speech_resolution_rejected",
                survivor.name,
                reason="recipient died before resolution",
            )
            continue
        message = SpokenMessage(
            message_id=f"message-{day}-{len(world.messages) + 1}",
            day=day + 1,
            speaker=survivor.name,
            recipient=speech.recipient,
            text=speech.text,
        )
        world.messages.append(message)
        _emit(world, day, "speech_sent", survivor.name, message=message.to_dict())


def _apply_daily_cost(world: SurvivalWorld, day: int) -> None:
    living = world.living_by_seat()
    for survivor in living:
        cost = _daily_cost(world.config, survivor)
        survivor.energy -= cost
        _emit(
            world,
            day,
            "daily_energy_paid",
            survivor.name,
            amount=cost,
            energy_after=survivor.energy,
        )
    for survivor in living:
        if survivor.energy <= 0:
            _die(world, day, survivor, "daily_energy_depleted")


def _daily_cost(config: SurvivalConfig, survivor: Survivor) -> int:
    discount = config.shelter_energy_discount if survivor.shelter else 0
    return max(1, config.daily_energy_cost - discount)


def _regenerate_resources(world: SurvivalWorld, day: int) -> None:
    world.resources.food = min(
        world.resources.food_capacity,
        world.resources.food + world.config.food_regeneration,
    )
    world.resources.wood = min(
        world.resources.wood_capacity,
        world.resources.wood + world.config.wood_regeneration,
    )
    _emit(
        world, day, "resources_regenerated", None, resources=world.resources.to_dict()
    )


def _die(world: SurvivalWorld, day: int, survivor: Survivor, cause: str) -> None:
    if not survivor.alive:
        return
    survivor.alive = False
    survivor.energy = 0
    survivor.died_on_day = day
    _emit(world, day, "survivor_died", survivor.name, cause=cause)


def _finalize_survival(world: SurvivalWorld, day: int) -> None:
    if not world.alive_names():
        world.finished_reason = "everyone_died"
    elif day >= world.config.max_days:
        world.finished_reason = "day_limit_reached"
    if world.finished_reason is not None:
        _emit(world, day, "world_finished", None, reason=world.finished_reason)


def _resolution_order(
    world: SurvivalWorld,
    day: int,
    survivors: Sequence[Survivor],
    namespace: str,
) -> list[Survivor]:
    order = list(sorted(survivors, key=lambda survivor: survivor.seat_id))
    random.Random(_stable_int(f"order:{namespace}:{world.seed}:{day}")).shuffle(order)
    return order


def _stable_range(
    seed: int,
    day: int,
    opaque_id: str,
    namespace: str,
    minimum: int,
    maximum: int,
) -> int:
    return minimum + (
        _stable_int(f"{namespace}:{seed}:{day}:{opaque_id}") % (maximum - minimum + 1)
    )


def _stable_int(value: str) -> int:
    return int.from_bytes(hashlib.sha256(value.encode("utf-8")).digest()[:8], "big")


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
            "actor": event.actor,
            "view_sha256": event.detail["view_sha256"],
            "raw_choice": event.detail["raw_choice"],
        }
        for event in events
        if event.kind == "choice_submitted"
    )


def _world_from_snapshot(
    snapshot: Mapping[str, Any],
    *,
    event_sequence_offset: int,
) -> SurvivalWorld:
    config = SurvivalConfig(**dict(snapshot["config"]))
    survivors = {
        str(item["name"]): Survivor(
            seat_id=str(item["seat_id"]),
            name=str(item["name"]),
            energy=int(item["energy"]),
            food=int(item["food"]),
            wood=int(item["wood"]),
            shelter=bool(item["shelter"]),
            alive=bool(item["alive"]),
            died_on_day=(
                int(item["died_on_day"]) if item["died_on_day"] is not None else None
            ),
        )
        for item in snapshot["survivors"]
    }
    resources_data = snapshot["resources"]
    messages = [
        SpokenMessage(
            message_id=str(item["id"]),
            day=int(item["day"]),
            speaker=str(item["speaker"]),
            recipient=str(item["recipient"]),
            text=str(item["text"]),
        )
        for item in snapshot["messages"]
    ]
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
        day=int(snapshot["day"]),
        messages=messages,
        event_sequence_offset=event_sequence_offset,
        finished_reason=(
            str(snapshot["finished_reason"])
            if snapshot["finished_reason"] is not None
            else None
        ),
    )


def _reject_resolution(
    world: SurvivalWorld, day: int, survivor: Survivor, reason: str
) -> None:
    _emit(world, day, "action_resolution_rejected", survivor.name, reason=reason)


def _emit(
    world: SurvivalWorld, day: int, kind: str, actor: str | None, **detail: Any
) -> None:
    world.events.append(
        SurvivalEvent(
            sequence=world.event_sequence_offset + len(world.events) + 1,
            day=day,
            kind=kind,
            actor=actor,
            detail=detail,
        )
    )


def _snapshot(world: SurvivalWorld) -> dict[str, Any]:
    return world.to_dict(include_events=False)
