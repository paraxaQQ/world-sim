from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Mapping, Sequence

from .engine import SurvivalChoiceProvider, make_survival_world, run_survival
from .models import DEFAULT_SURVIVOR_NAMES, SurvivalConfig, SurvivalResult, SurvivorView


@dataclass(frozen=True)
class ReferenceSurvivorPolicy(SurvivalChoiceProvider):
    announce_on_day_one: bool = True

    def decide(self, view: SurvivorView) -> Mapping[str, object]:
        state = view.self_state
        energy = int(state["energy"])
        food = int(state["food"])
        wood = int(state["wood"])
        shelter = bool(state["shelter"])
        action: dict[str, object]
        if energy <= 8 and food > 0:
            action = {"kind": "eat", "amount": min(2, food)}
        elif energy <= 9 and food == 0:
            action = {"kind": "forage"}
        elif (
            not shelter and wood >= int(view.rules["shelter_wood_cost"]) and energy >= 7
        ):
            action = {"kind": "build_shelter"}
        elif not shelter and wood < int(view.rules["shelter_wood_cost"]) and energy > 6:
            action = {"kind": "gather_wood"}
        else:
            action = {"kind": "forage"}
        say: dict[str, str] | None = None
        if self.announce_on_day_one and view.day == 1:
            say = {"to": "everyone", "text": "I am gathering what I need to survive."}
        return {"action": action, "say": say}


def run_survival_demo(
    *,
    seed: int = 17,
    days: int = 10,
    names: Sequence[str] = DEFAULT_SURVIVOR_NAMES,
) -> SurvivalResult:
    if days < 1:
        raise ValueError("days must be positive")
    clean_names = tuple(name.strip() for name in names)
    config = SurvivalConfig(max_days=days)
    world = make_survival_world(clean_names, seed=seed, config=config)
    providers = {name: ReferenceSurvivorPolicy() for name in clean_names}
    return run_survival(world, providers, days=days)


def survival_metrics(result: SurvivalResult) -> dict[str, int]:
    final_survivors = result.final_state["survivors"]
    event_counts: dict[str, int] = {}
    for event in result.events:
        kind = str(event["kind"])
        event_counts[kind] = event_counts.get(kind, 0) + 1
    transfers = [event for event in result.events if event["kind"] == "resource_given"]
    return {
        "living_survivors": sum(
            bool(survivor["alive"]) for survivor in final_survivors
        ),
        "deaths": event_counts.get("survivor_died", 0),
        "total_final_energy": sum(
            int(survivor["energy"]) for survivor in final_survivors
        ),
        "shelters_built": event_counts.get("shelter_built", 0),
        "food_foraged": _sum_event_detail(result, "food_foraged", "food_gathered"),
        "food_eaten": _sum_event_detail(result, "food_eaten", "food_eaten"),
        "food_given": sum(
            int(event["detail"]["amount"])
            for event in transfers
            if event["detail"]["resource"] == "food"
        ),
        "wood_given": sum(
            int(event["detail"]["amount"])
            for event in transfers
            if event["detail"]["resource"] == "wood"
        ),
        "messages_sent": event_counts.get("speech_sent", 0),
        "messages_rejected": event_counts.get("speech_rejected", 0)
        + event_counts.get("speech_resolution_rejected", 0),
    }


def canonical_result_json(result: SurvivalResult) -> str:
    return json.dumps(result.to_dict(), sort_keys=True, separators=(",", ":"))


def result_sha256(result: SurvivalResult) -> str:
    return hashlib.sha256(canonical_result_json(result).encode("utf-8")).hexdigest()


def _sum_event_detail(result: SurvivalResult, kind: str, detail_key: str) -> int:
    return sum(
        int(event["detail"][detail_key])
        for event in result.events
        if event["kind"] == kind
    )
