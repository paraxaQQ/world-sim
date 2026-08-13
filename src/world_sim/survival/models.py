from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


DEFAULT_SURVIVOR_NAMES: tuple[str, ...] = (
    "Aster",
    "Birch",
    "Cinder",
    "Lumen",
    "Morrow",
    "Rowan",
    "Sable",
    "Vale",
)


@dataclass(frozen=True)
class SurvivalConfig:
    max_days: int = 20
    starting_energy: int = 16
    max_energy: int = 24
    starting_food: int = 1
    starting_wood: int = 0
    daily_energy_cost: int = 2
    shelter_energy_discount: int = 1
    rest_energy_cost: int = 1
    forage_energy_cost: int = 2
    forage_min_food: int = 1
    forage_max_food: int = 2
    gather_wood_energy_cost: int = 2
    gather_wood_yield: int = 2
    eat_energy_cost: int = 1
    food_energy: int = 5
    max_food_eaten: int = 2
    build_shelter_energy_cost: int = 2
    shelter_wood_cost: int = 4
    give_energy_cost: int = 1
    speech_energy_cost: int = 1
    max_speech_chars: int = 500
    max_inbox_messages: int = 7
    food_starting_stock: int = 16
    food_capacity: int = 24
    food_regeneration: int = 4
    wood_starting_stock: int = 16
    wood_capacity: int = 24
    wood_regeneration: int = 4

    def __post_init__(self) -> None:
        positive_fields = {
            "max_days": self.max_days,
            "starting_energy": self.starting_energy,
            "max_energy": self.max_energy,
            "daily_energy_cost": self.daily_energy_cost,
            "rest_energy_cost": self.rest_energy_cost,
            "forage_energy_cost": self.forage_energy_cost,
            "forage_min_food": self.forage_min_food,
            "forage_max_food": self.forage_max_food,
            "gather_wood_energy_cost": self.gather_wood_energy_cost,
            "gather_wood_yield": self.gather_wood_yield,
            "eat_energy_cost": self.eat_energy_cost,
            "food_energy": self.food_energy,
            "max_food_eaten": self.max_food_eaten,
            "build_shelter_energy_cost": self.build_shelter_energy_cost,
            "shelter_wood_cost": self.shelter_wood_cost,
            "give_energy_cost": self.give_energy_cost,
            "speech_energy_cost": self.speech_energy_cost,
            "max_speech_chars": self.max_speech_chars,
            "max_inbox_messages": self.max_inbox_messages,
            "food_capacity": self.food_capacity,
            "food_regeneration": self.food_regeneration,
            "wood_capacity": self.wood_capacity,
            "wood_regeneration": self.wood_regeneration,
        }
        invalid = [name for name, value in positive_fields.items() if value < 1]
        if invalid:
            raise ValueError(
                f"these survival config values must be positive: {', '.join(invalid)}"
            )
        nonnegative_fields = {
            "starting_food": self.starting_food,
            "starting_wood": self.starting_wood,
            "shelter_energy_discount": self.shelter_energy_discount,
            "food_starting_stock": self.food_starting_stock,
            "wood_starting_stock": self.wood_starting_stock,
        }
        invalid_nonnegative = [
            name for name, value in nonnegative_fields.items() if value < 0
        ]
        if invalid_nonnegative:
            raise ValueError(
                f"these survival config values must be non-negative: {', '.join(invalid_nonnegative)}"
            )
        if self.starting_energy > self.max_energy:
            raise ValueError("starting_energy cannot exceed max_energy")
        if self.shelter_energy_discount >= self.daily_energy_cost:
            raise ValueError(
                "shelter_energy_discount must be less than daily_energy_cost"
            )
        if self.forage_min_food > self.forage_max_food:
            raise ValueError("forage_min_food cannot exceed forage_max_food")
        if self.food_starting_stock > self.food_capacity:
            raise ValueError("food_starting_stock cannot exceed food_capacity")
        if self.wood_starting_stock > self.wood_capacity:
            raise ValueError("wood_starting_stock cannot exceed wood_capacity")

    @property
    def action_energy_costs(self) -> dict[str, int]:
        return {
            "rest": self.rest_energy_cost,
            "forage": self.forage_energy_cost,
            "gather_wood": self.gather_wood_energy_cost,
            "eat": self.eat_energy_cost,
            "build_shelter": self.build_shelter_energy_cost,
            "give_food": self.give_energy_cost,
            "give_wood": self.give_energy_cost,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "max_days": self.max_days,
            "starting_energy": self.starting_energy,
            "max_energy": self.max_energy,
            "starting_food": self.starting_food,
            "starting_wood": self.starting_wood,
            "daily_energy_cost": self.daily_energy_cost,
            "shelter_energy_discount": self.shelter_energy_discount,
            "rest_energy_cost": self.rest_energy_cost,
            "forage_energy_cost": self.forage_energy_cost,
            "forage_min_food": self.forage_min_food,
            "forage_max_food": self.forage_max_food,
            "gather_wood_energy_cost": self.gather_wood_energy_cost,
            "gather_wood_yield": self.gather_wood_yield,
            "eat_energy_cost": self.eat_energy_cost,
            "food_energy": self.food_energy,
            "max_food_eaten": self.max_food_eaten,
            "build_shelter_energy_cost": self.build_shelter_energy_cost,
            "shelter_wood_cost": self.shelter_wood_cost,
            "give_energy_cost": self.give_energy_cost,
            "speech_energy_cost": self.speech_energy_cost,
            "max_speech_chars": self.max_speech_chars,
            "max_inbox_messages": self.max_inbox_messages,
            "food_starting_stock": self.food_starting_stock,
            "food_capacity": self.food_capacity,
            "food_regeneration": self.food_regeneration,
            "wood_starting_stock": self.wood_starting_stock,
            "wood_capacity": self.wood_capacity,
            "wood_regeneration": self.wood_regeneration,
        }


@dataclass
class Survivor:
    seat_id: str
    name: str
    energy: int
    food: int
    wood: int
    shelter: bool = False
    alive: bool = True
    died_on_day: int | None = None

    def to_private_dict(self) -> dict[str, Any]:
        return {
            "seat_id": self.seat_id,
            "name": self.name,
            "energy": self.energy,
            "food": self.food,
            "wood": self.wood,
            "shelter": self.shelter,
            "alive": self.alive,
            "died_on_day": self.died_on_day,
        }

    def to_view_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "energy": self.energy,
            "food": self.food,
            "wood": self.wood,
            "shelter": self.shelter,
            "alive": self.alive,
        }

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "energy": self.energy,
            "shelter": self.shelter,
            "alive": self.alive,
        }


@dataclass
class ResourcePool:
    food: int
    food_capacity: int
    wood: int
    wood_capacity: int

    def to_dict(self) -> dict[str, int]:
        return {
            "food": self.food,
            "food_capacity": self.food_capacity,
            "wood": self.wood,
            "wood_capacity": self.wood_capacity,
        }


@dataclass(frozen=True)
class SpokenMessage:
    message_id: str
    day: int
    speaker: str
    recipient: str
    text: str

    def to_dict(self) -> dict[str, int | str]:
        return {
            "id": self.message_id,
            "day": self.day,
            "speaker": self.speaker,
            "recipient": self.recipient,
            "text": self.text,
        }


@dataclass(frozen=True)
class SurvivalEvent:
    sequence: int
    day: int
    kind: str
    actor: str | None
    detail: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "sequence": self.sequence,
            "day": self.day,
            "kind": self.kind,
            "actor": self.actor,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class SurvivorView:
    name: str
    day: int
    self_state: dict[str, Any]
    others: tuple[dict[str, Any], ...]
    resources: dict[str, int]
    inbox: tuple[dict[str, Any], ...]
    rules: dict[str, Any]
    allowed_actions: tuple[dict[str, Any], ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "day": self.day,
            "self": self.self_state,
            "others": list(self.others),
            "resources": self.resources,
            "inbox": list(self.inbox),
            "rules": self.rules,
            "allowed_actions": list(self.allowed_actions),
        }


@dataclass
class SurvivalWorld:
    config: SurvivalConfig
    seed: int
    survivors: dict[str, Survivor]
    resources: ResourcePool
    day: int = 0
    messages: list[SpokenMessage] = field(default_factory=list)
    events: list[SurvivalEvent] = field(default_factory=list)
    event_sequence_offset: int = 0
    finished_reason: str | None = None

    @property
    def finished(self) -> bool:
        return self.finished_reason is not None

    def alive_names(self) -> list[str]:
        return [survivor.name for survivor in self.living_by_seat()]

    def living_by_seat(self) -> list[Survivor]:
        return sorted(
            (survivor for survivor in self.survivors.values() if survivor.alive),
            key=lambda survivor: survivor.seat_id,
        )

    def to_dict(self, *, include_events: bool = True) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "config": self.config.to_dict(),
            "seed": self.seed,
            "day": self.day,
            "survivors": [
                survivor.to_private_dict()
                for survivor in sorted(
                    self.survivors.values(), key=lambda item: item.seat_id
                )
            ],
            "resources": self.resources.to_dict(),
            "messages": [message.to_dict() for message in self.messages],
            "finished_reason": self.finished_reason,
        }
        if include_events:
            payload["events"] = [event.to_dict() for event in self.events]
        return payload


@dataclass(frozen=True)
class SurvivalResult:
    initial_state: dict[str, Any]
    final_state: dict[str, Any]
    events: tuple[dict[str, Any], ...]
    choice_tape: tuple[dict[str, Any], ...]
    event_sequence_base: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "initial_state": self.initial_state,
            "final_state": self.final_state,
            "events": list(self.events),
            "choice_tape": list(self.choice_tape),
            "event_sequence_base": self.event_sequence_base,
        }
