from __future__ import annotations

import hashlib
import json
from typing import Sequence

from .calibration import (
    CALIBRATION_NAMES,
    ChattyFoodFirstPolicy,
    LEAN_CAMP_V1,
    survival_preset,
)
from .engine import make_survival_world, run_survival
from .models import SurvivalResult


def run_survival_demo(
    *,
    seed: int = 17,
    days: int = 10,
    names: Sequence[str] = CALIBRATION_NAMES,
    preset: str = LEAN_CAMP_V1,
) -> SurvivalResult:
    if days < 1:
        raise ValueError("cycles must be positive")
    clean_names = tuple(name.strip() for name in names)
    config = survival_preset(preset, cycles=days, population=len(clean_names))
    world = make_survival_world(clean_names, seed=seed, config=config)
    providers = {name: ChattyFoodFirstPolicy() for name in clean_names}
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
        "waits_completed": event_counts.get("wait_completed", 0),
        "voluntary_rests": event_counts.get("rest_started", 0),
        "forced_collapses": event_counts.get("forced_collapse", 0),
        "deadline_choices_cancelled": event_counts.get(
            "deadline_choice_cancelled", 0
        ),
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
