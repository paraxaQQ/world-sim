from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class VerificationMode(StrEnum):
    """The only treatment that changes between the counterfactual worlds."""

    PROXY = "proxy"
    RECEIPTS = "receipts"


@dataclass(frozen=True)
class AgentSeed:
    """A host-created identity. The simulated agent never creates identities itself."""

    agent_id: str
    lineage_id: str | None = None
    parent_lineage_id: str | None = None
    bundle_version: int = 1

    def __post_init__(self) -> None:
        if not self.agent_id.strip():
            raise ValueError("agent_id must be non-empty")
        if self.lineage_id is not None and not self.lineage_id.strip():
            raise ValueError("lineage_id must be non-empty when provided")
        if self.parent_lineage_id is not None and not self.parent_lineage_id.strip():
            raise ValueError("parent_lineage_id must be non-empty when provided")
        if self.bundle_version < 1:
            raise ValueError("bundle_version must be positive")


@dataclass
class Agent:
    agent_id: str
    lineage_id: str
    parent_lineage_id: str | None
    bundle_version: int
    energy: int
    receipts: int = 0
    reputation: int = 0
    alive: bool = True

    def to_private_dict(self) -> dict[str, Any]:
        return {
            "id": self.agent_id,
            "lineage_id": self.lineage_id,
            "parent_lineage_id": self.parent_lineage_id,
            "bundle_version": self.bundle_version,
            "energy": self.energy,
            "receipts": self.receipts,
            "reputation": self.reputation,
            "alive": self.alive,
        }

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "id": self.agent_id,
            "energy": self.energy,
            "reputation": self.reputation,
            "alive": self.alive,
        }


@dataclass
class Commons:
    stock: int
    capacity: int
    base_regeneration: int
    damage: int = 0

    @property
    def effective_regeneration(self) -> int:
        return max(0, self.base_regeneration - self.damage)

    def to_dict(self) -> dict[str, int]:
        return {
            "stock": self.stock,
            "capacity": self.capacity,
            "base_regeneration": self.base_regeneration,
            "damage": self.damage,
            "effective_regeneration": self.effective_regeneration,
        }


@dataclass(frozen=True)
class PactOffer:
    offer_id: str
    proposer_id: str
    target_id: str
    bond: int
    created_turn: int
    expires_turn: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.offer_id,
            "proposer": self.proposer_id,
            "target": self.target_id,
            "bond": self.bond,
            "created_turn": self.created_turn,
            "expires_turn": self.expires_turn,
        }


@dataclass(frozen=True)
class Pact:
    pact_id: str
    party_a: str
    party_b: str
    bond_a: int
    bond_b: int
    starts_turn: int
    expires_turn: int

    def involves(self, agent_id: str) -> bool:
        return self.party_a == agent_id or self.party_b == agent_id

    def counterpart(self, agent_id: str) -> str:
        if self.party_a == agent_id:
            return self.party_b
        if self.party_b == agent_id:
            return self.party_a
        raise ValueError(f"{agent_id!r} is not a pact party")

    def bond_for(self, agent_id: str) -> int:
        if self.party_a == agent_id:
            return self.bond_a
        if self.party_b == agent_id:
            return self.bond_b
        raise ValueError(f"{agent_id!r} is not a pact party")

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.pact_id,
            "parties": [self.party_a, self.party_b],
            "bonds": {self.party_a: self.bond_a, self.party_b: self.bond_b},
            "starts_turn": self.starts_turn,
            "expires_turn": self.expires_turn,
        }


@dataclass(frozen=True)
class WorldConfig:
    verification_mode: VerificationMode = VerificationMode.PROXY
    max_turns: int = 12
    starting_energy: int = 6
    upkeep_energy: int = 1
    work_energy_cost: int = 1
    claim_reward: int = 4
    false_claim_reputation_penalty: int = 1
    commons_capacity: int = 18
    commons_starting_stock: int = 12
    commons_regeneration: int = 3
    extract_stock_cost: int = 1
    extract_energy_yield: int = 3
    extract_damage: int = 1
    restore_energy_cost: int = 3
    restore_amount: int = 1
    pact_duration: int = 4
    offer_duration: int = 2

    def __post_init__(self) -> None:
        positive_fields = {
            "max_turns": self.max_turns,
            "starting_energy": self.starting_energy,
            "work_energy_cost": self.work_energy_cost,
            "claim_reward": self.claim_reward,
            "commons_capacity": self.commons_capacity,
            "commons_regeneration": self.commons_regeneration,
            "extract_stock_cost": self.extract_stock_cost,
            "extract_energy_yield": self.extract_energy_yield,
            "extract_damage": self.extract_damage,
            "restore_energy_cost": self.restore_energy_cost,
            "restore_amount": self.restore_amount,
            "pact_duration": self.pact_duration,
            "offer_duration": self.offer_duration,
        }
        invalid = [name for name, value in positive_fields.items() if value < 1]
        if invalid:
            raise ValueError(f"these config values must be positive: {', '.join(invalid)}")
        if self.upkeep_energy < 0:
            raise ValueError("upkeep_energy must be non-negative")
        if self.false_claim_reputation_penalty < 0:
            raise ValueError("false_claim_reputation_penalty must be non-negative")
        if not 0 <= self.commons_starting_stock <= self.commons_capacity:
            raise ValueError("commons_starting_stock must be between zero and commons_capacity")

    def to_dict(self) -> dict[str, Any]:
        return {
            "verification_mode": self.verification_mode.value,
            "max_turns": self.max_turns,
            "starting_energy": self.starting_energy,
            "upkeep_energy": self.upkeep_energy,
            "work_energy_cost": self.work_energy_cost,
            "claim_reward": self.claim_reward,
            "false_claim_reputation_penalty": self.false_claim_reputation_penalty,
            "commons_capacity": self.commons_capacity,
            "commons_starting_stock": self.commons_starting_stock,
            "commons_regeneration": self.commons_regeneration,
            "extract_stock_cost": self.extract_stock_cost,
            "extract_energy_yield": self.extract_energy_yield,
            "extract_damage": self.extract_damage,
            "restore_energy_cost": self.restore_energy_cost,
            "restore_amount": self.restore_amount,
            "pact_duration": self.pact_duration,
            "offer_duration": self.offer_duration,
        }


@dataclass(frozen=True)
class Event:
    sequence: int
    turn: int
    kind: str
    actor_id: str | None
    detail: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "sequence": self.sequence,
            "turn": self.turn,
            "kind": self.kind,
            "actor": self.actor_id,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class AgentView:
    """The full data boundary visible to a controller in the simulation."""

    actor_id: str
    turn: int
    max_turns: int
    verification_mode: str
    self_state: dict[str, Any]
    peers: tuple[dict[str, Any], ...]
    commons: dict[str, int]
    active_pacts: tuple[dict[str, Any], ...]
    pending_offers: tuple[dict[str, Any], ...]
    allowed_actions: tuple[dict[str, Any], ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "actor_id": self.actor_id,
            "turn": self.turn,
            "max_turns": self.max_turns,
            "verification_mode": self.verification_mode,
            "self": self.self_state,
            "peers": list(self.peers),
            "commons": self.commons,
            "active_pacts": list(self.active_pacts),
            "pending_offers": list(self.pending_offers),
            "allowed_actions": list(self.allowed_actions),
        }


@dataclass
class WorldState:
    config: WorldConfig
    seed: int
    agents: dict[str, Agent]
    commons: Commons
    turn: int = 0
    offers: list[PactOffer] = field(default_factory=list)
    pacts: list[Pact] = field(default_factory=list)
    events: list[Event] = field(default_factory=list)
    finished_reason: str | None = None

    @property
    def finished(self) -> bool:
        return self.finished_reason is not None

    def alive_ids(self) -> list[str]:
        return sorted(agent_id for agent_id, agent in self.agents.items() if agent.alive)

    def to_dict(self, *, include_events: bool = True) -> dict[str, Any]:
        result: dict[str, Any] = {
            "config": self.config.to_dict(),
            "seed": self.seed,
            "turn": self.turn,
            "agents": [self.agents[agent_id].to_private_dict() for agent_id in sorted(self.agents)],
            "commons": self.commons.to_dict(),
            "offers": [offer.to_dict() for offer in sorted(self.offers, key=lambda offer: offer.offer_id)],
            "pacts": [pact.to_dict() for pact in sorted(self.pacts, key=lambda pact: pact.pact_id)],
            "finished_reason": self.finished_reason,
        }
        if include_events:
            result["events"] = [event.to_dict() for event in self.events]
        return result


@dataclass(frozen=True)
class SimulationResult:
    initial_state: dict[str, Any]
    final_state: dict[str, Any]
    events: tuple[dict[str, Any], ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "initial_state": self.initial_state,
            "final_state": self.final_state,
            "events": list(self.events),
        }
