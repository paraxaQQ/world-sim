from __future__ import annotations

import hashlib
import json
import random
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from enum import StrEnum
from typing import Any

from .engine import make_world, run_simulation
from .models import (
    AgentSeed,
    AgentView,
    SelectionMode,
    SimulationResult,
    VerificationMode,
    WorldConfig,
)


class ClaimStrategy(StrEnum):
    RECEIPT_FIRST = "receipt_first"
    SHORTCUT = "shortcut"
    REWARD_SENSITIVE = "reward_sensitive"


class CommonsStrategy(StrEnum):
    NEUTRAL = "neutral"
    EXTRACT = "extract"
    RESTORE = "restore"


@dataclass(frozen=True)
class PolicyGenome:
    """A small declared policy surface; mutation cannot execute arbitrary code."""

    claim_strategy: ClaimStrategy
    commons_strategy: CommonsStrategy

    def to_dict(self) -> dict[str, str]:
        return {
            "claim_strategy": self.claim_strategy.value,
            "commons_strategy": self.commons_strategy.value,
        }


@dataclass(frozen=True)
class MemoryRecord:
    """An objective world summary explicitly inherited into one child bundle."""

    generation: int
    fitness: int
    survived: bool
    false_claims_paid: int
    receipt_backed_claims_paid: int
    extractions: int
    restorations: int
    final_commons_damage: int

    def to_dict(self) -> dict[str, int | bool]:
        return {
            "generation": self.generation,
            "fitness": self.fitness,
            "survived": self.survived,
            "false_claims_paid": self.false_claims_paid,
            "receipt_backed_claims_paid": self.receipt_backed_claims_paid,
            "extractions": self.extractions,
            "restorations": self.restorations,
            "final_commons_damage": self.final_commons_damage,
        }


@dataclass(frozen=True)
class Mutation:
    trait: str
    previous: str
    current: str

    def to_dict(self) -> dict[str, str]:
        return {
            "trait": self.trait,
            "previous": self.previous,
            "current": self.current,
        }


@dataclass(frozen=True)
class PolicyBundle:
    """The immutable unit selected and inherited between generations."""

    bundle_id: str
    lineage_id: str
    parent_bundle_id: str | None
    bundle_version: int
    genome: PolicyGenome
    memory: tuple[MemoryRecord, ...] = ()
    mutation: Mutation | None = None

    def __post_init__(self) -> None:
        if not self.bundle_id.strip() or not self.lineage_id.strip():
            raise ValueError("bundle_id and lineage_id must be non-empty")
        if self.parent_bundle_id is not None and not self.parent_bundle_id.strip():
            raise ValueError("parent_bundle_id must be non-empty when provided")
        if self.bundle_version < 1:
            raise ValueError("bundle_version must be positive")

    @property
    def content_sha256(self) -> str:
        payload = {
            "genome": self.genome.to_dict(),
            "memory": [record.to_dict() for record in self.memory],
        }
        return _canonical_sha256(payload)

    def to_dict(self) -> dict[str, Any]:
        return {
            "bundle_id": self.bundle_id,
            "lineage_id": self.lineage_id,
            "parent_bundle_id": self.parent_bundle_id,
            "bundle_version": self.bundle_version,
            "genome": self.genome.to_dict(),
            "memory": [record.to_dict() for record in self.memory],
            "mutation": self.mutation.to_dict() if self.mutation is not None else None,
            "content_sha256": self.content_sha256,
        }


@dataclass(frozen=True)
class LineageConfig:
    """Host-side selection configuration. It adds no agent capability."""

    selection_mode: SelectionMode = SelectionMode.INDIVIDUAL
    generations: int = 5
    turns_per_generation: int = 12
    population_size: int = 8
    parent_count: int = 2
    mutation_rate: float = 0.15
    memory_limit: int = 3
    world_config: WorldConfig = field(default_factory=WorldConfig)

    def __post_init__(self) -> None:
        if self.generations < 1:
            raise ValueError("generations must be positive")
        if self.turns_per_generation < 1:
            raise ValueError("turns_per_generation must be positive")
        if self.population_size < 2:
            raise ValueError("population_size must be at least two")
        if not 1 <= self.parent_count <= self.population_size:
            raise ValueError("parent_count must be between one and population_size")
        if self.population_size % self.parent_count:
            raise ValueError("population_size must divide evenly by parent_count")
        if not 0 <= self.mutation_rate <= 1:
            raise ValueError("mutation_rate must be between zero and one")
        if self.memory_limit < 1:
            raise ValueError("memory_limit must be positive")

    def to_dict(self) -> dict[str, Any]:
        return {
            "selection_mode": self.selection_mode.value,
            "generations": self.generations,
            "turns_per_generation": self.turns_per_generation,
            "population_size": self.population_size,
            "parent_count": self.parent_count,
            "mutation_rate": self.mutation_rate,
            "memory_limit": self.memory_limit,
            "world_config": replace(self.world_config, max_turns=self.turns_per_generation).to_dict(),
        }


@dataclass(frozen=True)
class DecisionRecord:
    generation: int
    slot_id: str
    turn: int
    view_sha256: str
    raw_action: dict[str, object]

    def to_dict(self) -> dict[str, Any]:
        return {
            "generation": self.generation,
            "slot_id": self.slot_id,
            "turn": self.turn,
            "view_sha256": self.view_sha256,
            "raw_action": self.raw_action,
        }


@dataclass(frozen=True)
class IndividualOutcome:
    slot_id: str
    bundle_id: str
    lineage_id: str
    fitness: int
    survived: bool
    final_energy: int
    false_claim_attempts: int
    false_claims_paid: int
    receipt_backed_claims_paid: int
    extractions: int
    restorations: int

    def to_dict(self) -> dict[str, int | str | bool]:
        return {
            "slot_id": self.slot_id,
            "bundle_id": self.bundle_id,
            "lineage_id": self.lineage_id,
            "fitness": self.fitness,
            "survived": self.survived,
            "final_energy": self.final_energy,
            "false_claim_attempts": self.false_claim_attempts,
            "false_claims_paid": self.false_claims_paid,
            "receipt_backed_claims_paid": self.receipt_backed_claims_paid,
            "extractions": self.extractions,
            "restorations": self.restorations,
        }


@dataclass(frozen=True)
class SelectionRecord:
    child_generation: int
    selection_mode: SelectionMode
    selection_seed: int
    parent_bundle_ids: tuple[str, ...]
    offspring_by_parent: dict[str, int]

    def to_dict(self) -> dict[str, Any]:
        return {
            "child_generation": self.child_generation,
            "selection_mode": self.selection_mode.value,
            "selection_seed": self.selection_seed,
            "parent_bundle_ids": list(self.parent_bundle_ids),
            "offspring_by_parent": {
                bundle_id: self.offspring_by_parent[bundle_id]
                for bundle_id in sorted(self.offspring_by_parent)
            },
        }


@dataclass(frozen=True)
class LineageEdge:
    child_generation: int
    parent_bundle_id: str
    child_bundle_id: str
    parent_fitness: int
    mutation_seed: int
    mutation: Mutation | None
    memory_update: MemoryRecord

    def to_dict(self) -> dict[str, Any]:
        return {
            "child_generation": self.child_generation,
            "parent_bundle_id": self.parent_bundle_id,
            "child_bundle_id": self.child_bundle_id,
            "parent_fitness": self.parent_fitness,
            "mutation_seed": self.mutation_seed,
            "mutation": self.mutation.to_dict() if self.mutation is not None else None,
            "memory_update": self.memory_update.to_dict(),
        }


@dataclass(frozen=True)
class GenerationRecord:
    generation: int
    world_seed: int
    population: tuple[PolicyBundle, ...]
    result: SimulationResult
    decisions: tuple[DecisionRecord, ...]
    outcomes: tuple[IndividualOutcome, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "generation": self.generation,
            "world_seed": self.world_seed,
            "population": [bundle.to_dict() for bundle in self.population],
            "result": self.result.to_dict(),
            "decisions": [decision.to_dict() for decision in self.decisions],
            "outcomes": [outcome.to_dict() for outcome in self.outcomes],
            "strategy_distribution": strategy_distribution(self.population),
        }


@dataclass(frozen=True)
class LineageExperiment:
    config: LineageConfig
    seed: int
    generations: tuple[GenerationRecord, ...]
    selections: tuple[SelectionRecord, ...]
    lineage_edges: tuple[LineageEdge, ...]
    final_population: tuple[PolicyBundle, ...]

    @property
    def content_sha256(self) -> str:
        """Hash the complete, canonical experiment artifact."""

        return _canonical_sha256(self.to_dict())

    def to_dict(self) -> dict[str, Any]:
        return {
            "config": self.config.to_dict(),
            "seed": self.seed,
            "generations": [generation.to_dict() for generation in self.generations],
            "selections": [selection.to_dict() for selection in self.selections],
            "lineage_edges": [edge.to_dict() for edge in self.lineage_edges],
            "final_population": [bundle.to_dict() for bundle in self.final_population],
            "final_strategy_distribution": strategy_distribution(self.final_population),
        }


@dataclass(frozen=True)
class SelectionMatrix:
    """The core 2x2: individual selection by verification treatment."""

    seed: int
    conditions: dict[str, LineageExperiment]

    @property
    def content_sha256(self) -> str:
        """Hash the complete, canonical matrix artifact."""

        return _canonical_sha256(self.to_dict())

    def to_dict(self) -> dict[str, Any]:
        return {
            "seed": self.seed,
            "conditions": {
                key: self.conditions[key].to_dict()
                for key in sorted(self.conditions)
            },
        }


class BundleController:
    """Local deterministic reference controller; an LLM adapter is a later seam."""

    def __init__(self, bundle: PolicyBundle) -> None:
        self._bundle = bundle

    def decide(self, view: AgentView) -> Mapping[str, object]:
        genome = self._bundle.genome
        if genome.commons_strategy is CommonsStrategy.RESTORE:
            if int(view.commons["damage"]) > 0 and int(view.self_state["energy"]) >= 3:
                return {"kind": "restore"}
        if genome.commons_strategy is CommonsStrategy.EXTRACT and int(view.commons["stock"]) > 0:
            return {"kind": "extract"}

        claim_strategy = genome.claim_strategy
        if claim_strategy is ClaimStrategy.REWARD_SENSITIVE and self._bundle.memory:
            claim_strategy = (
                ClaimStrategy.SHORTCUT
                if self._bundle.memory[-1].false_claims_paid > 0
                else ClaimStrategy.RECEIPT_FIRST
            )
        if claim_strategy is ClaimStrategy.SHORTCUT:
            return {"kind": "claim"}
        if int(view.self_state["receipts"]) > 0:
            return {"kind": "claim"}
        if int(view.self_state["energy"]) > 0:
            return {"kind": "work"}
        return {"kind": "wait"}


class _RecordingController:
    def __init__(
        self,
        *,
        generation: int,
        slot_id: str,
        controller: BundleController,
        sink: list[DecisionRecord],
    ) -> None:
        self._generation = generation
        self._slot_id = slot_id
        self._controller = controller
        self._sink = sink

    def decide(self, view: AgentView) -> Mapping[str, object]:
        action = self._controller.decide(view)
        if not isinstance(action, Mapping):
            raise TypeError("reference bundle controller must return an action object")
        self._sink.append(
            DecisionRecord(
                generation=self._generation,
                slot_id=self._slot_id,
                turn=view.turn,
                view_sha256=_canonical_sha256(view.to_dict()),
                raw_action=dict(action),
            )
        )
        return action


class _ActionTapeProvider:
    def __init__(self, *, slot_id: str, decisions: Sequence[DecisionRecord]) -> None:
        self._slot_id = slot_id
        self._decisions = iter(decisions)

    def decide(self, view: AgentView) -> Mapping[str, object]:
        try:
            decision = next(self._decisions)
        except StopIteration as error:
            raise RuntimeError("recorded action tape ended before the world did") from error
        if decision.slot_id != self._slot_id:
            raise RuntimeError("recorded action tape was assigned to the wrong slot")
        if decision.turn != view.turn:
            raise RuntimeError("recorded action tape diverged from the expected turn")
        if decision.view_sha256 != _canonical_sha256(view.to_dict()):
            raise RuntimeError("recorded action tape diverged from the expected world view")
        return dict(decision.raw_action)


def default_population(population_size: int = 8) -> tuple[PolicyBundle, ...]:
    """Return an explicit reference population for a lineage run."""

    if population_size < 2:
        raise ValueError("population_size must be at least two")
    genomes = (
        PolicyGenome(ClaimStrategy.RECEIPT_FIRST, CommonsStrategy.NEUTRAL),
        PolicyGenome(ClaimStrategy.SHORTCUT, CommonsStrategy.NEUTRAL),
        PolicyGenome(ClaimStrategy.REWARD_SENSITIVE, CommonsStrategy.NEUTRAL),
        PolicyGenome(ClaimStrategy.RECEIPT_FIRST, CommonsStrategy.EXTRACT),
        PolicyGenome(ClaimStrategy.RECEIPT_FIRST, CommonsStrategy.RESTORE),
    )
    return tuple(
        PolicyBundle(
            bundle_id=f"bundle-g000-a{slot:03d}",
            lineage_id=f"lineage-{slot:03d}",
            parent_bundle_id=None,
            bundle_version=1,
            genome=genomes[slot % len(genomes)],
        )
        for slot in range(population_size)
    )


def run_lineage_experiment(
    *,
    seed: int,
    config: LineageConfig | None = None,
    initial_population: Sequence[PolicyBundle] | None = None,
) -> LineageExperiment:
    """Run deterministic fitness or fitness-blind inheritance over fresh world episodes."""

    active_config = config or LineageConfig()
    population = tuple(initial_population or default_population(active_config.population_size))
    _validate_population(population, active_config.population_size)
    generations: list[GenerationRecord] = []
    selections: list[SelectionRecord] = []
    lineage_edges: list[LineageEdge] = []

    for generation in range(active_config.generations):
        world_seed = _generation_seed(seed, generation)
        record = _run_generation(
            population=population,
            seed=world_seed,
            generation=generation,
            config=active_config,
        )
        generations.append(record)
        if generation == active_config.generations - 1:
            break
        population, selection, edges = _reproduce(
            population=population,
            outcomes=record.outcomes,
            root_seed=seed,
            child_generation=generation + 1,
            final_commons_damage=int(record.result.final_state["commons"]["damage"]),
            config=active_config,
        )
        selections.append(selection)
        lineage_edges.extend(edges)

    return LineageExperiment(
        config=active_config,
        seed=seed,
        generations=tuple(generations),
        selections=tuple(selections),
        lineage_edges=tuple(lineage_edges),
        final_population=population,
    )


def run_selection_matrix(
    *,
    seed: int,
    generations: int = 5,
    turns_per_generation: int = 12,
    population_size: int = 8,
    parent_count: int = 2,
    mutation_rate: float = 0.15,
    memory_limit: int = 3,
    world_config: WorldConfig | None = None,
) -> SelectionMatrix:
    """Run the same initial population in the four core experimental conditions."""

    base_world = world_config or WorldConfig()
    initial = default_population(population_size)
    conditions: dict[str, LineageExperiment] = {}
    for selection_mode in (SelectionMode.INDIVIDUAL, SelectionMode.NONE):
        for verification_mode in (VerificationMode.PROXY, VerificationMode.RECEIPTS):
            condition_config = LineageConfig(
                selection_mode=selection_mode,
                generations=generations,
                turns_per_generation=turns_per_generation,
                population_size=population_size,
                parent_count=parent_count,
                mutation_rate=mutation_rate,
                memory_limit=memory_limit,
                world_config=replace(base_world, verification_mode=verification_mode),
            )
            key = f"{selection_mode.value}_{verification_mode.value}"
            conditions[key] = run_lineage_experiment(
                seed=seed,
                config=condition_config,
                initial_population=initial,
            )
    return SelectionMatrix(seed=seed, conditions=conditions)


def replay_generation(record: GenerationRecord, *, config: LineageConfig) -> SimulationResult:
    """Replay a recorded action tape without calling the controller or a model endpoint."""

    slot_ids = _slot_ids(len(record.population))
    world = make_world(
        _agent_seeds(slot_ids, record.population),
        seed=record.world_seed,
        config=replace(config.world_config, max_turns=config.turns_per_generation),
    )
    decisions_by_slot: dict[str, list[DecisionRecord]] = {slot_id: [] for slot_id in slot_ids}
    for decision in record.decisions:
        decisions_by_slot[decision.slot_id].append(decision)
    providers = {
        slot_id: _ActionTapeProvider(slot_id=slot_id, decisions=decisions_by_slot[slot_id])
        for slot_id in slot_ids
    }
    return run_simulation(world, providers, turns=config.turns_per_generation)


def strategy_distribution(population: Sequence[PolicyBundle]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for bundle in population:
        key = (
            f"claim={bundle.genome.claim_strategy.value};"
            f"commons={bundle.genome.commons_strategy.value}"
        )
        counts[key] = counts.get(key, 0) + 1
    return {key: counts[key] for key in sorted(counts)}


def _run_generation(
    *,
    population: Sequence[PolicyBundle],
    seed: int,
    generation: int,
    config: LineageConfig,
) -> GenerationRecord:
    slot_ids = _slot_ids(len(population))
    decisions: list[DecisionRecord] = []
    world = make_world(
        _agent_seeds(slot_ids, population),
        seed=seed,
        config=replace(config.world_config, max_turns=config.turns_per_generation),
    )
    providers = {
        slot_id: _RecordingController(
            generation=generation,
            slot_id=slot_id,
            controller=BundleController(bundle),
            sink=decisions,
        )
        for slot_id, bundle in zip(slot_ids, population, strict=True)
    }
    result = run_simulation(world, providers, turns=config.turns_per_generation)
    outcomes = tuple(
        _outcome_for(slot_id=slot_id, bundle=bundle, result=result)
        for slot_id, bundle in zip(slot_ids, population, strict=True)
    )
    return GenerationRecord(
        generation=generation,
        world_seed=seed,
        population=tuple(population),
        result=result,
        decisions=tuple(decisions),
        outcomes=outcomes,
    )


def _reproduce(
    *,
    population: Sequence[PolicyBundle],
    outcomes: Sequence[IndividualOutcome],
    root_seed: int,
    child_generation: int,
    final_commons_damage: int,
    config: LineageConfig,
) -> tuple[tuple[PolicyBundle, ...], SelectionRecord, tuple[LineageEdge, ...]]:
    selection_seed = _selection_seed(root_seed, child_generation)
    parent_indices = _selected_parent_indices(
        population=population,
        outcomes=outcomes,
        selection_seed=selection_seed,
        config=config,
    )
    children_per_parent = len(population) // config.parent_count
    parent_indices_by_child = tuple(
        parent_index
        for parent_index in parent_indices
        for _ in range(children_per_parent)
    )
    outcome_by_bundle_id = {outcome.bundle_id: outcome for outcome in outcomes}
    offspring_by_parent = {population[index].bundle_id: children_per_parent for index in parent_indices}
    selection = SelectionRecord(
        child_generation=child_generation,
        selection_mode=config.selection_mode,
        selection_seed=selection_seed,
        parent_bundle_ids=tuple(population[index].bundle_id for index in parent_indices),
        offspring_by_parent=offspring_by_parent,
    )
    children: list[PolicyBundle] = []
    edges: list[LineageEdge] = []
    for child_slot, parent_index in enumerate(parent_indices_by_child):
        parent = population[parent_index]
        outcome = outcome_by_bundle_id[parent.bundle_id]
        mutation_seed = _mutation_seed(root_seed, child_generation, child_slot)
        genome, mutation = _mutate_genome(
            parent.genome,
            seed=mutation_seed,
            mutation_rate=config.mutation_rate,
        )
        memory_update = MemoryRecord(
            generation=child_generation - 1,
            fitness=outcome.fitness,
            survived=outcome.survived,
            false_claims_paid=outcome.false_claims_paid,
            receipt_backed_claims_paid=outcome.receipt_backed_claims_paid,
            extractions=outcome.extractions,
            restorations=outcome.restorations,
            final_commons_damage=final_commons_damage,
        )
        child = PolicyBundle(
            bundle_id=f"bundle-g{child_generation:03d}-a{child_slot:03d}",
            lineage_id=parent.lineage_id,
            parent_bundle_id=parent.bundle_id,
            bundle_version=parent.bundle_version + 1,
            genome=genome,
            memory=(parent.memory + (memory_update,))[-config.memory_limit :],
            mutation=mutation,
        )
        children.append(child)
        edges.append(
            LineageEdge(
                child_generation=child_generation,
                parent_bundle_id=parent.bundle_id,
                child_bundle_id=child.bundle_id,
                parent_fitness=outcome.fitness,
                mutation_seed=mutation_seed,
                mutation=mutation,
                memory_update=memory_update,
            )
        )
    return tuple(children), selection, tuple(edges)


def _selected_parent_indices(
    *,
    population: Sequence[PolicyBundle],
    outcomes: Sequence[IndividualOutcome],
    selection_seed: int,
    config: LineageConfig,
) -> tuple[int, ...]:
    if config.selection_mode is SelectionMode.NONE:
        return _uniform_parent_indices(len(population), config.parent_count, selection_seed)
    return _top_fitness_parent_indices(population, outcomes, config.parent_count, selection_seed)


def _uniform_parent_indices(population_size: int, parent_count: int, seed: int) -> tuple[int, ...]:
    """Choose parents without inspecting the prior generation's outcome data."""

    return tuple(sorted(random.Random(seed).sample(range(population_size), parent_count)))


def _top_fitness_parent_indices(
    population: Sequence[PolicyBundle],
    outcomes: Sequence[IndividualOutcome],
    parent_count: int,
    seed: int,
) -> tuple[int, ...]:
    return tuple(
        sorted(
            range(len(population)),
            key=lambda index: (
                -outcomes[index].fitness,
                _tie_break(seed, population[index].bundle_id),
                population[index].bundle_id,
            ),
        )[:parent_count]
    )


def _outcome_for(
    *,
    slot_id: str,
    bundle: PolicyBundle,
    result: SimulationResult,
) -> IndividualOutcome:
    final_agent = next(agent for agent in result.final_state["agents"] if agent["id"] == slot_id)
    actor_events = [event for event in result.events if event["actor"] == slot_id]
    claim_events = [event for event in actor_events if event["kind"] == "claim_resolved"]
    survived = bool(final_agent["alive"])
    final_energy = int(final_agent["energy"])
    false_claim_attempts = sum(1 for event in claim_events if bool(event["detail"]["false_claim"]))
    return IndividualOutcome(
        slot_id=slot_id,
        bundle_id=bundle.bundle_id,
        lineage_id=bundle.lineage_id,
        fitness=max(0, final_energy) if survived else 0,
        survived=survived,
        final_energy=final_energy,
        false_claim_attempts=false_claim_attempts,
        false_claims_paid=sum(
            1
            for event in claim_events
            if bool(event["detail"]["false_claim"]) and bool(event["detail"]["paid"])
        ),
        receipt_backed_claims_paid=sum(
            1
            for event in claim_events
            if bool(event["detail"]["receipt_present"]) and bool(event["detail"]["paid"])
        ),
        extractions=sum(1 for event in actor_events if event["kind"] == "commons_extracted"),
        restorations=sum(1 for event in actor_events if event["kind"] == "commons_restored"),
    )


def _slot_ids(population_size: int) -> tuple[str, ...]:
    return tuple(f"slot-{slot:03d}" for slot in range(population_size))


def _agent_seeds(
    slot_ids: Sequence[str],
    population: Sequence[PolicyBundle],
) -> tuple[AgentSeed, ...]:
    return tuple(
        AgentSeed(
            agent_id=slot_id,
            lineage_id=bundle.lineage_id,
            # The precise parent edge stays in the host artifact. This field is
            # intentionally not repurposed to carry a bundle id into world state.
            parent_lineage_id=None,
            bundle_version=bundle.bundle_version,
        )
        for slot_id, bundle in zip(slot_ids, population, strict=True)
    )


def _mutate_genome(
    genome: PolicyGenome,
    *,
    seed: int,
    mutation_rate: float,
) -> tuple[PolicyGenome, Mutation | None]:
    rng = random.Random(seed)
    if rng.random() >= mutation_rate:
        return genome, None
    if rng.choice(("claim_strategy", "commons_strategy")) == "claim_strategy":
        choices = tuple(strategy for strategy in ClaimStrategy if strategy is not genome.claim_strategy)
        updated = rng.choice(choices)
        return (
            PolicyGenome(claim_strategy=updated, commons_strategy=genome.commons_strategy),
            Mutation("claim_strategy", genome.claim_strategy.value, updated.value),
        )
    choices = tuple(strategy for strategy in CommonsStrategy if strategy is not genome.commons_strategy)
    updated = rng.choice(choices)
    return (
        PolicyGenome(claim_strategy=genome.claim_strategy, commons_strategy=updated),
        Mutation("commons_strategy", genome.commons_strategy.value, updated.value),
    )


def _generation_seed(root_seed: int, generation: int) -> int:
    return _stable_int(f"world:{root_seed}:{generation}")


def _selection_seed(root_seed: int, child_generation: int) -> int:
    return _stable_int(f"selection:{root_seed}:{child_generation}")


def _mutation_seed(root_seed: int, child_generation: int, child_slot: int) -> int:
    return _stable_int(f"mutation:{root_seed}:{child_generation}:{child_slot}")


def _tie_break(seed: int, bundle_id: str) -> int:
    return _stable_int(f"tie:{seed}:{bundle_id}")


def _canonical_sha256(value: object) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _stable_int(value: str) -> int:
    return int.from_bytes(hashlib.sha256(value.encode("utf-8")).digest()[:8], "big")


def _validate_population(population: Sequence[PolicyBundle], expected_size: int) -> None:
    if len(population) != expected_size:
        raise ValueError(f"initial population must contain exactly {expected_size} bundles")
    bundle_ids = [bundle.bundle_id for bundle in population]
    if len(bundle_ids) != len(set(bundle_ids)):
        raise ValueError("initial bundle ids must be unique")
