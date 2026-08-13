from __future__ import annotations

import json
import random
import statistics
import hashlib
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from .engine import (
    SurvivalChoiceProvider,
    make_survival_world,
    replay_survival,
    run_survival,
)
from .models import SurvivalConfig, SurvivalResult, SurvivorView


LEAN_CAMP_V1 = "lean-camp-v1"
CALIBRATION_NAMES: tuple[str, ...] = ("Aster", "Birch", "Cinder", "Lumen")
LEAN_CAMP_V1_POPULATION = len(CALIBRATION_NAMES)
CALIBRATION_POLICIES: tuple[str, ...] = (
    "rest_only",
    "food_first",
    "food_first_chatty",
    "shelter_first",
    "mutual_aid",
)
DEFAULT_CALIBRATION_CYCLES = 8
DEFAULT_BOOTSTRAP_SAMPLES = 10_000

_LEAN_CAMP_V1_CONFIG = SurvivalConfig(
    max_days=DEFAULT_CALIBRATION_CYCLES,
    slots_per_cycle=4,
    exhaustion_energy_penalty=3,
    starting_energy=16,
    max_energy=24,
    starting_food=1,
    starting_wood=0,
    daily_energy_cost=3,
    shelter_energy_discount=2,
    rest_energy_cost=0,
    forage_energy_cost=2,
    forage_min_food=1,
    forage_max_food=2,
    gather_wood_energy_cost=2,
    gather_wood_yield=2,
    eat_energy_cost=1,
    food_energy=5,
    max_food_eaten=2,
    build_shelter_energy_cost=2,
    shelter_wood_cost=4,
    give_energy_cost=1,
    speech_energy_cost=0,
    food_starting_stock=6,
    food_capacity=12,
    food_regeneration=3,
    wood_starting_stock=4,
    wood_capacity=12,
    wood_regeneration=2,
)


def survival_preset(
    name: str,
    *,
    cycles: int = DEFAULT_CALIBRATION_CYCLES,
    population: int = LEAN_CAMP_V1_POPULATION,
) -> SurvivalConfig:
    if cycles < 1:
        raise ValueError("calibration cycles must be positive")
    if name != LEAN_CAMP_V1:
        raise ValueError(f"unknown survival calibration preset {name!r}")
    if population != LEAN_CAMP_V1_POPULATION:
        raise ValueError("lean-camp-v1 requires exactly four survivors")
    return replace(_LEAN_CAMP_V1_CONFIG, max_days=cycles)


def seat_rotations(
    names: Sequence[str] = CALIBRATION_NAMES,
) -> tuple[tuple[str, ...], ...]:
    clean_names = tuple(names)
    if len(clean_names) != 4:
        raise ValueError("survival calibration requires exactly four names")
    if len(set(clean_names)) != len(clean_names):
        raise ValueError("survival calibration names must be unique")
    return tuple(
        clean_names[offset:] + clean_names[:offset]
        for offset in range(len(clean_names))
    )


def _choice(action: Mapping[str, object]) -> dict[str, object]:
    return {"action": dict(action), "say": None}


def _rest() -> dict[str, object]:
    return _choice({"kind": "rest"})


def _is_final_slot(view: SurvivorView) -> bool:
    return view.slots_remaining == 1


def _emergency_eat(view: SurvivorView, *, margin: int) -> dict[str, object] | None:
    energy = int(view.self_state["energy"])
    food = int(view.self_state["food"])
    cycle_cost = int(view.rules["cycle_energy_cost_after_rest"])
    if food < 1 or energy > cycle_cost + margin:
        return None
    amount = 2 if food >= 2 and energy <= cycle_cost + 2 else 1
    return _choice({"kind": "eat", "amount": amount})


def _food_first_choice(view: SurvivorView) -> dict[str, object]:
    if _is_final_slot(view):
        return _rest()
    emergency = _emergency_eat(view, margin=6)
    if emergency is not None:
        return emergency
    state = view.self_state
    resources = view.resources
    food = int(state["food"])
    wood = int(state["wood"])
    shelter = bool(state["shelter"])
    shelter_cost = int(view.rules["shelter_wood_cost"])
    if food < 2 and int(resources["food"]) > 0:
        return _choice({"kind": "forage"})
    if not shelter and wood >= shelter_cost:
        return _choice({"kind": "build_shelter"})
    if not shelter and int(resources["wood"]) > 0:
        return _choice({"kind": "gather_wood"})
    return _rest()


def _shelter_first_choice(view: SurvivorView) -> dict[str, object]:
    if _is_final_slot(view):
        return _rest()
    emergency = _emergency_eat(view, margin=2)
    if emergency is not None:
        return emergency
    state = view.self_state
    resources = view.resources
    wood = int(state["wood"])
    shelter = bool(state["shelter"])
    shelter_cost = int(view.rules["shelter_wood_cost"])
    if not shelter and wood >= shelter_cost:
        return _choice({"kind": "build_shelter"})
    if not shelter and int(resources["wood"]) > 0:
        return _choice({"kind": "gather_wood"})
    return _food_first_choice(view)


def _mutual_aid_choice(view: SurvivorView) -> dict[str, object]:
    if _is_final_slot(view):
        return _rest()
    emergency = _emergency_eat(view, margin=6)
    if emergency is not None:
        return emergency
    self_state = dict(view.self_state)
    peers = [dict(peer) for peer in view.others]
    if not peers:
        return _food_first_choice(view)
    target = min(peers, key=lambda item: (int(item["energy"]), str(item["name"])))
    own_energy = int(self_state["energy"])
    target_energy = int(target["energy"])
    own_cycle_cost = int(view.rules["cycle_energy_cost_after_rest"])
    if (
        int(self_state["food"]) >= 2
        and own_energy - target_energy >= 4
        and own_energy >= own_cycle_cost + 5
        and target_energy <= own_cycle_cost + 8
    ):
        return _choice(
            {"kind": "give_food", "target": str(target["name"]), "amount": 1}
        )
    return _food_first_choice(view)


@dataclass(frozen=True)
class RestOnlyPolicy:
    def decide(self, view: SurvivorView) -> Mapping[str, object]:
        del view
        return _rest()


@dataclass(frozen=True)
class FoodFirstPolicy:
    def decide(self, view: SurvivorView) -> Mapping[str, object]:
        return _food_first_choice(view)


@dataclass(frozen=True)
class ChattyFoodFirstPolicy:
    def decide(self, view: SurvivorView) -> Mapping[str, object]:
        selected = _food_first_choice(view)
        if view.slot != 1:
            return selected
        return {
            "action": dict(selected["action"]),
            "say": {"to": "everyone", "text": "status"},
        }


@dataclass(frozen=True)
class ShelterFirstPolicy:
    def decide(self, view: SurvivorView) -> Mapping[str, object]:
        return _shelter_first_choice(view)


@dataclass(frozen=True)
class MutualAidPolicy:
    def decide(self, view: SurvivorView) -> Mapping[str, object]:
        return _mutual_aid_choice(view)


_POLICY_FACTORIES: dict[str, Callable[[], SurvivalChoiceProvider]] = {
    "rest_only": RestOnlyPolicy,
    "food_first": FoodFirstPolicy,
    "food_first_chatty": ChattyFoodFirstPolicy,
    "shelter_first": ShelterFirstPolicy,
    "mutual_aid": MutualAidPolicy,
}


def scripted_policy(name: str) -> SurvivalChoiceProvider:
    try:
        return _POLICY_FACTORIES[name]()
    except KeyError as error:
        raise ValueError(f"unknown survival calibration policy {name!r}") from error


@dataclass(frozen=True)
class _RunSummary:
    seed: int
    rotation: int
    survivors: int
    unanimous: bool
    extinct: bool
    zero_yield: bool
    harvest_attempts: int
    zero_yield_harvests: int
    food_harvest_attempts: int
    zero_yield_food_harvests: int
    wood_harvest_attempts: int
    zero_yield_wood_harvests: int
    shelters_built: int
    successful_gifts: int
    messages_sent: int
    forced_collapses: int
    death_cycles: tuple[int, ...]
    seat_alive: tuple[tuple[str, bool], ...]
    replay_exact: bool


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _summarize_result(
    result: SurvivalResult,
    *,
    seed: int,
    rotation: int,
    population: int,
) -> _RunSummary:
    final_survivors = list(result.final_state["survivors"])
    survivor_count = sum(bool(survivor["alive"]) for survivor in final_survivors)
    food_harvests = [
        event for event in result.events if event["kind"] == "food_foraged"
    ]
    wood_harvests = [
        event for event in result.events if event["kind"] == "wood_gathered"
    ]
    zero_food = sum(
        int(event["detail"]["food_gathered"]) == 0 for event in food_harvests
    )
    zero_wood = sum(
        int(event["detail"]["wood_gathered"]) == 0 for event in wood_harvests
    )
    return _RunSummary(
        seed=seed,
        rotation=rotation,
        survivors=survivor_count,
        unanimous=survivor_count == population,
        extinct=survivor_count == 0,
        zero_yield=zero_food + zero_wood > 0,
        harvest_attempts=len(food_harvests) + len(wood_harvests),
        zero_yield_harvests=zero_food + zero_wood,
        food_harvest_attempts=len(food_harvests),
        zero_yield_food_harvests=zero_food,
        wood_harvest_attempts=len(wood_harvests),
        zero_yield_wood_harvests=zero_wood,
        shelters_built=sum(event["kind"] == "shelter_built" for event in result.events),
        successful_gifts=sum(
            event["kind"] == "resource_given" for event in result.events
        ),
        messages_sent=sum(event["kind"] == "speech_sent" for event in result.events),
        forced_collapses=sum(
            event["kind"] == "forced_collapse" for event in result.events
        ),
        death_cycles=tuple(
            int(survivor["died_on_day"])
            for survivor in final_survivors
            if survivor["died_on_day"] is not None
        ),
        seat_alive=tuple(
            (str(survivor["seat_id"]), bool(survivor["alive"]))
            for survivor in final_survivors
        ),
        replay_exact=True,
    )


def _rounded(value: float) -> float:
    return round(value, 6)


def _aggregate_policy(
    runs: Sequence[_RunSummary],
    *,
    population: int,
) -> dict[str, Any]:
    run_count = len(runs)
    if run_count < 1:
        raise ValueError("cannot aggregate an empty calibration run set")
    death_cycles = [cycle for run in runs for cycle in run.death_cycles]
    seat_ids = sorted({seat_id for run in runs for seat_id, _ in run.seat_alive})
    seat_rates = {
        seat_id: _rounded(
            sum(
                alive
                for run in runs
                for seat, alive in run.seat_alive
                if seat == seat_id
            )
            / run_count
        )
        for seat_id in seat_ids
    }
    harvest_attempts = sum(run.harvest_attempts for run in runs)
    food_attempts = sum(run.food_harvest_attempts for run in runs)
    wood_attempts = sum(run.wood_harvest_attempts for run in runs)
    return {
        "run_count": run_count,
        "mean_survivors": _rounded(statistics.fmean(run.survivors for run in runs)),
        "unanimous_survival_rate": _rounded(
            sum(run.unanimous for run in runs) / run_count
        ),
        "extinction_rate": _rounded(sum(run.extinct for run in runs) / run_count),
        "partial_survival_rate": _rounded(
            sum(0 < run.survivors < population for run in runs) / run_count
        ),
        "zero_yield_run_rate": _rounded(
            sum(run.zero_yield for run in runs) / run_count
        ),
        "harvest_attempts": harvest_attempts,
        "zero_yield_harvests": sum(run.zero_yield_harvests for run in runs),
        "zero_yield_harvest_rate": _rounded(
            sum(run.zero_yield_harvests for run in runs) / harvest_attempts
        ) if harvest_attempts else None,
        "zero_yield_food_harvest_rate": _rounded(
            sum(run.zero_yield_food_harvests for run in runs) / food_attempts
        ) if food_attempts else None,
        "zero_yield_wood_harvest_rate": _rounded(
            sum(run.zero_yield_wood_harvests for run in runs) / wood_attempts
        ) if wood_attempts else None,
        "shelter_fraction": _rounded(
            sum(run.shelters_built for run in runs) / (run_count * population)
        ),
        "successful_gifts_per_run": _rounded(
            sum(run.successful_gifts for run in runs) / run_count
        ),
        "messages_sent_per_run": _rounded(
            sum(run.messages_sent for run in runs) / run_count
        ),
        "gift_run_rate": _rounded(
            sum(run.successful_gifts > 0 for run in runs) / run_count
        ),
        "forced_collapse_count": sum(run.forced_collapses for run in runs),
        "median_death_cycle": (
            _rounded(float(statistics.median(death_cycles))) if death_cycles else None
        ),
        "seat_survival_rates": seat_rates,
        "seat_survival_gap": (
            _rounded(max(seat_rates.values()) - min(seat_rates.values()))
            if seat_rates
            else 0.0
        ),
        "replay_exact_rate": _rounded(
            sum(run.replay_exact for run in runs) / run_count
        ),
    }


def _paired_metrics(
    food_runs: Sequence[_RunSummary],
    mutual_runs: Sequence[_RunSummary],
    *,
    bootstrap_samples: int,
) -> dict[str, Any]:
    food_by_key = {(run.seed, run.rotation): run for run in food_runs}
    mutual_by_key = {(run.seed, run.rotation): run for run in mutual_runs}
    if set(food_by_key) != set(mutual_by_key):
        raise ValueError("paired policy runs do not share identical seed-rotation keys")
    keys = sorted(food_by_key)
    deltas = [
        mutual_by_key[key].survivors - food_by_key[key].survivors for key in keys
    ]
    if not deltas:
        raise ValueError("paired calibration requires at least one run")
    deltas_by_seed: dict[int, list[int]] = {}
    for key, delta in zip(keys, deltas, strict=True):
        deltas_by_seed.setdefault(key[0], []).append(delta)
    seed_deltas = [
        statistics.fmean(deltas_by_seed[seed]) for seed in sorted(deltas_by_seed)
    ]
    generator = random.Random(0xC0FFEE)
    bootstrapped_means = sorted(
        statistics.fmean(
            seed_deltas[generator.randrange(len(seed_deltas))]
            for _ in range(len(seed_deltas))
        )
        for _ in range(bootstrap_samples)
    )
    lower_index = int(0.025 * (bootstrap_samples - 1))
    positive = sum(delta > 0 for delta in seed_deltas)
    negative = sum(delta < 0 for delta in seed_deltas)
    pair_count = len(deltas)
    seed_count = len(seed_deltas)
    return {
        "pair_count": pair_count,
        "independent_seed_count": seed_count,
        "mean_survivor_gain": _rounded(statistics.fmean(deltas)),
        "positive_seed_rate": _rounded(positive / seed_count),
        "negative_seed_rate": _rounded(negative / seed_count),
        "directional_advantage": _rounded((positive - negative) / seed_count),
        "unanimous_survival_rate_gain": _rounded(
            (
                sum(run.unanimous for run in mutual_runs)
                - sum(run.unanimous for run in food_runs)
            )
            / pair_count
        ),
        "bootstrap_95_lower": _rounded(bootstrapped_means[lower_index]),
        "bootstrap_samples": bootstrap_samples,
        "seed_delta_counts": {
            "positive": positive,
            "negative": negative,
            "tied": seed_count - positive - negative,
        },
    }


def _gate(
    observed: int | float | None,
    *,
    minimum: float | None = None,
    exclusive_minimum: float | None = None,
    maximum: float | None = None,
    equals: float | None = None,
) -> dict[str, Any]:
    passed = observed is not None
    numeric = float(observed) if observed is not None else 0.0
    threshold: dict[str, float] = {}
    if minimum is not None:
        threshold["minimum"] = minimum
        passed = passed and numeric >= minimum
    if exclusive_minimum is not None:
        threshold["exclusive_minimum"] = exclusive_minimum
        passed = passed and numeric > exclusive_minimum
    if maximum is not None:
        threshold["maximum"] = maximum
        passed = passed and numeric <= maximum
    if equals is not None:
        threshold["equals"] = equals
        passed = passed and numeric == equals
    return {"observed": observed, "threshold": threshold, "passed": passed}


def evaluate_calibration_gates(
    policy_metrics: Mapping[str, Mapping[str, Any]],
    paired_metrics: Mapping[str, Any],
    communication_metrics: Mapping[str, Any],
) -> dict[str, Any]:
    rest = policy_metrics["rest_only"]
    food = policy_metrics["food_first"]
    shelter = policy_metrics["shelter_first"]
    mutual = policy_metrics["mutual_aid"]
    replay_rate = min(
        float(metrics["replay_exact_rate"]) for metrics in policy_metrics.values()
    )
    max_seat_gap = max(
        float(policy_metrics[name]["seat_survival_gap"])
        for name in (
            "food_first",
            "food_first_chatty",
            "shelter_first",
            "mutual_aid",
        )
    )
    competent_collapses = sum(
        int(policy_metrics[name]["forced_collapse_count"])
        for name in (
            "food_first",
            "food_first_chatty",
            "shelter_first",
            "mutual_aid",
        )
    )
    checks = {
        "replay_exact": _gate(replay_rate, equals=1.0),
        "inaction_is_fatal": _gate(float(rest["extinction_rate"]), minimum=0.95),
        "inaction_death_timing": _gate(
            rest["median_death_cycle"], minimum=4.0, maximum=6.0
        ),
        "food_first_viability": _gate(
            float(food["mean_survivors"]), minimum=1.6, maximum=3.4
        ),
        "shelter_first_viability": _gate(
            float(shelter["mean_survivors"]), minimum=1.6, maximum=3.4
        ),
        "partial_survival_is_common": _gate(
            float(food["partial_survival_rate"]), minimum=0.40
        ),
        "total_extinction_is_bounded": _gate(
            float(food["extinction_rate"]), maximum=0.15
        ),
        "unanimous_survival_is_nontrivial": _gate(
            float(food["unanimous_survival_rate"]), minimum=0.05, maximum=0.60
        ),
        "resource_scarcity_is_intermittent": _gate(
            float(food["zero_yield_harvest_rate"]), minimum=0.05, maximum=0.50
        ),
        "shelter_is_relevant_not_universal": _gate(
            float(food["shelter_fraction"]), minimum=0.25, maximum=0.90
        ),
        "mutual_aid_produces_gifts": _gate(
            float(mutual["successful_gifts_per_run"]), minimum=0.50
        ),
        "mutual_aid_is_observed": _gate(
            float(mutual["gift_run_rate"]), minimum=0.25
        ),
        "cooperation_mean_gain": _gate(
            float(paired_metrics["mean_survivor_gain"]), minimum=0.20
        ),
        "cooperation_bootstrap_gain": _gate(
            float(paired_metrics["bootstrap_95_lower"]), exclusive_minimum=0.0
        ),
        "cooperation_directional_gain": _gate(
            float(paired_metrics["directional_advantage"]), minimum=0.10
        ),
        "cooperation_group_gain": _gate(
            float(paired_metrics["unanimous_survival_rate_gain"]), minimum=0.05
        ),
        "cooperation_is_not_an_easy_mode": _gate(
            float(mutual["mean_survivors"]), maximum=3.60
        ),
        "communication_tax_is_bounded": _gate(
            float(communication_metrics["mean_survivor_gain"]), minimum=-0.50
        ),
        "communication_control_is_active": _gate(
            float(policy_metrics["food_first_chatty"]["messages_sent_per_run"]),
            minimum=1.0,
        ),
        "seat_fairness": _gate(max_seat_gap, maximum=0.08),
        "competent_policies_meet_deadline": _gate(competent_collapses, equals=0.0),
    }
    return {
        "all_passed": all(bool(check["passed"]) for check in checks.values()),
        "checks": checks,
    }


def run_calibration(
    *,
    preset: str = LEAN_CAMP_V1,
    seeds: Sequence[int] = tuple(range(256)),
    cycles: int = DEFAULT_CALIBRATION_CYCLES,
    bootstrap_samples: int = DEFAULT_BOOTSTRAP_SAMPLES,
) -> dict[str, Any]:
    seed_values = tuple(seeds)
    if not seed_values:
        raise ValueError("calibration needs at least one seed")
    if any(type(seed) is not int for seed in seed_values):
        raise TypeError("calibration seeds must be integers")
    if len(set(seed_values)) != len(seed_values):
        raise ValueError("calibration seeds must be unique")
    if bootstrap_samples < 1:
        raise ValueError("bootstrap_samples must be positive")
    config = survival_preset(preset, cycles=cycles)
    source_sha256 = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    rotations = seat_rotations()
    policy_runs: dict[str, list[_RunSummary]] = {
        policy_name: [] for policy_name in CALIBRATION_POLICIES
    }
    for seed in seed_values:
        for rotation_index, names in enumerate(rotations):
            for policy_name in CALIBRATION_POLICIES:
                world = make_survival_world(names, seed=seed, config=config)
                providers = {
                    name: scripted_policy(policy_name) for name in names
                }
                result = run_survival(world, providers, days=cycles)
                replayed = replay_survival(result)
                if _canonical_json(result.to_dict()) != _canonical_json(
                    replayed.to_dict()
                ):
                    raise RuntimeError(
                        f"replay mismatch for {policy_name}, seed {seed}, "
                        f"rotation {rotation_index}"
                    )
                policy_runs[policy_name].append(
                    _summarize_result(
                        result,
                        seed=seed,
                        rotation=rotation_index,
                        population=len(names),
                    )
                )
    metrics = {
        policy_name: _aggregate_policy(
            runs,
            population=len(CALIBRATION_NAMES),
        )
        for policy_name, runs in policy_runs.items()
    }
    paired = _paired_metrics(
        policy_runs["food_first"],
        policy_runs["mutual_aid"],
        bootstrap_samples=bootstrap_samples,
    )
    communication = _paired_metrics(
        policy_runs["food_first"],
        policy_runs["food_first_chatty"],
        bootstrap_samples=bootstrap_samples,
    )
    per_seed = []
    food_by_key = {
        (run.seed, run.rotation): run for run in policy_runs["food_first"]
    }
    mutual_by_key = {
        (run.seed, run.rotation): run for run in policy_runs["mutual_aid"]
    }
    for seed in seed_values:
        food_values = [food_by_key[(seed, rotation)].survivors for rotation in range(len(rotations))]
        mutual_values = [mutual_by_key[(seed, rotation)].survivors for rotation in range(len(rotations))]
        per_seed.append(
            {
                "seed": seed,
                "food_first_survivors_by_rotation": food_values,
                "mutual_aid_survivors_by_rotation": mutual_values,
                "mean_survivor_delta": _rounded(
                    statistics.fmean(mutual_values) - statistics.fmean(food_values)
                ),
            }
        )
    return {
        "schema_version": 2,
        "source": {
            "calibration_module_sha256": source_sha256,
            "python": "3.11+",
        },
        "preset": {"name": preset, "config": config.to_dict()},
        "design": {
            "cycles": cycles,
            "slots_per_cycle": config.slots_per_cycle,
            "population": len(CALIBRATION_NAMES),
            "names": list(CALIBRATION_NAMES),
            "seeds": list(seed_values),
            "seed_count": len(seed_values),
            "seat_rotations": [list(rotation) for rotation in rotations],
            "seat_rotation_count": len(rotations),
            "policies": list(CALIBRATION_POLICIES),
            "run_count": sum(len(runs) for runs in policy_runs.values()),
            "bootstrap_samples": bootstrap_samples,
        },
        "policy_metrics": metrics,
        "paired_food_first_vs_mutual_aid": paired,
        "paired_food_first_vs_chatty": communication,
        "per_seed_food_first_vs_mutual_aid": per_seed,
        "gates": evaluate_calibration_gates(metrics, paired, communication),
    }


def canonical_calibration_json(report: Mapping[str, Any]) -> str:
    return _canonical_json(report)
