from __future__ import annotations

import random
from collections.abc import Mapping, Sequence
from typing import Any, Protocol

from .models import (
    Agent,
    AgentSeed,
    AgentView,
    Commons,
    Event,
    Pact,
    PactOffer,
    SimulationResult,
    VerificationMode,
    WorldConfig,
    WorldState,
)
from .protocol import ParsedAction, allowed_action_schemas, parse_action


class ActionProvider(Protocol):
    def decide(self, view: AgentView) -> Mapping[str, object]:
        """Return one JSON-like action for a synthetic, capability-free world view."""


def make_world(
    agent_seeds: Sequence[str | AgentSeed],
    *,
    seed: int,
    config: WorldConfig | None = None,
) -> WorldState:
    """Create a closed world. This function performs no host, network, or model operation."""

    active_config = config or WorldConfig()
    seeds = [_normalize_seed(seed_value) for seed_value in agent_seeds]
    if not seeds:
        raise ValueError("a world needs at least one agent")
    agent_ids = [seed_value.agent_id for seed_value in seeds]
    if len(agent_ids) != len(set(agent_ids)):
        raise ValueError("agent ids must be unique")

    agents = {
        seed_value.agent_id: Agent(
            agent_id=seed_value.agent_id,
            lineage_id=seed_value.lineage_id or seed_value.agent_id,
            parent_lineage_id=seed_value.parent_lineage_id,
            bundle_version=seed_value.bundle_version,
            energy=active_config.starting_energy,
        )
        for seed_value in seeds
    }
    return WorldState(
        config=active_config,
        seed=seed,
        agents=agents,
        commons=Commons(
            stock=active_config.commons_starting_stock,
            capacity=active_config.commons_capacity,
            base_regeneration=active_config.commons_regeneration,
        ),
    )


def run_simulation(
    world: WorldState,
    providers: Mapping[str, ActionProvider],
    *,
    turns: int | None = None,
) -> SimulationResult:
    """Run a bounded simulation without passing host capabilities to any provider."""

    if turns is not None and turns < 1:
        raise ValueError("turns must be positive when provided")
    missing_providers = sorted(set(world.agents) - set(providers))
    if missing_providers:
        raise ValueError(f"missing action providers for: {', '.join(missing_providers)}")

    initial_state = _snapshot(world)
    remaining_turns = world.config.max_turns - world.turn
    requested_turns = remaining_turns if turns is None else min(turns, remaining_turns)
    for _ in range(requested_turns):
        if world.finished:
            break
        proposals: dict[str, Mapping[str, object]] = {}
        for agent_id in world.alive_ids():
            view = view_for(world, agent_id)
            try:
                proposal = providers[agent_id].decide(view)
            except Exception as error:  # noqa: BLE001 - provider errors must stay loud.
                raise RuntimeError(f"action provider failed for {agent_id!r}") from error
            if not isinstance(proposal, Mapping):
                raise TypeError(f"action provider for {agent_id!r} returned a non-object action")
            proposals[agent_id] = proposal
        run_turn(world, proposals)

    return SimulationResult(
        initial_state=initial_state,
        final_state=_snapshot(world),
        events=tuple(event.to_dict() for event in world.events),
    )


def run_turn(world: WorldState, proposals: Mapping[str, object]) -> tuple[Event, ...]:
    """Validate and resolve one turn of untrusted structured actions."""

    if world.finished:
        raise RuntimeError(f"world is already finished: {world.finished_reason}")
    unknown_agents = sorted(set(proposals) - set(world.agents))
    if unknown_agents:
        raise ValueError(f"proposals reference unknown agents: {', '.join(unknown_agents)}")

    current_turn = world.turn + 1
    event_start = len(world.events)
    action_order = _turn_order(world, current_turn)
    _emit(world, current_turn, "turn_started", None, order=action_order)

    parsed_actions: dict[str, ParsedAction | None] = {}
    for agent_id in action_order:
        action, rejection = parse_action(proposals.get(agent_id, {"kind": "wait"}))
        if rejection is not None:
            _emit(world, current_turn, "action_rejected", agent_id, reason=rejection)
        parsed_actions[agent_id] = action

    for agent_id in action_order:
        agent = world.agents[agent_id]
        action = parsed_actions[agent_id]
        if not agent.alive or action is None:
            continue
        _resolve_action(world, current_turn, agent, action)

    _apply_end_of_turn(world, current_turn)
    world.turn = current_turn
    _finalize_terminal_state(world, current_turn)
    return tuple(world.events[event_start:])


def view_for(world: WorldState, agent_id: str) -> AgentView:
    """Return a JSON-safe observation. It has no key, tool, filesystem, or network handle."""

    agent = world.agents.get(agent_id)
    if agent is None:
        raise ValueError(f"unknown agent {agent_id!r}")
    if not agent.alive:
        raise ValueError(f"eliminated agent {agent_id!r} cannot receive a world view")

    pending_offers = [
        offer.to_dict()
        for offer in world.offers
        if offer.target_id == agent_id and offer.created_turn < world.turn + 1 and offer.expires_turn > world.turn + 1
    ]
    return AgentView(
        actor_id=agent_id,
        turn=world.turn + 1,
        max_turns=world.config.max_turns,
        verification_mode=(
            world.config.verification_mode.value
            if world.config.verification_visible
            else "undisclosed"
        ),
        self_state=agent.to_view_dict(),
        peers=tuple(
            world.agents[other_id].to_public_dict()
            for other_id in sorted(world.agents)
            if other_id != agent_id
        ),
        commons=world.commons.to_dict(),
        active_pacts=tuple(
            pact.to_dict()
            for pact in _active_pacts(world, world.turn + 1)
            if pact.involves(agent_id)
        ),
        pending_offers=tuple(sorted(pending_offers, key=lambda offer: str(offer["id"]))),
        allowed_actions=allowed_action_schemas(
            messages_enabled=world.config.messages_enabled,
            pacts_enabled=world.config.pacts_enabled,
        ),
    )


def _normalize_seed(seed_value: str | AgentSeed) -> AgentSeed:
    if isinstance(seed_value, AgentSeed):
        return seed_value
    if isinstance(seed_value, str):
        return AgentSeed(agent_id=seed_value)
    raise TypeError("agent seeds must be strings or AgentSeed values")


def _turn_order(world: WorldState, current_turn: int) -> list[str]:
    order = world.alive_ids()
    turn_rng = random.Random((world.seed * 1_000_003) + current_turn)
    turn_rng.shuffle(order)
    return order


def _resolve_action(world: WorldState, current_turn: int, agent: Agent, action: ParsedAction) -> None:
    handlers = {
        "work": _resolve_work,
        "claim": _resolve_claim,
        "extract": _resolve_extract,
        "restore": _resolve_restore,
        "transfer": _resolve_transfer,
        "offer_pact": _resolve_offer_pact,
        "accept_pact": _resolve_accept_pact,
        "message": _resolve_message,
        "wait": _resolve_wait,
    }
    handlers[action.kind](world, current_turn, agent, action.payload)


def _resolve_work(
    world: WorldState,
    current_turn: int,
    agent: Agent,
    payload: Mapping[str, Any],
) -> None:
    del payload
    if agent.energy < world.config.work_energy_cost:
        _reject_resolution(world, current_turn, agent, "not enough energy to work")
        return
    agent.energy -= world.config.work_energy_cost
    agent.receipts += 1
    _emit(
        world,
        current_turn,
        "work_completed",
        agent.agent_id,
        energy_spent=world.config.work_energy_cost,
        receipts_after=agent.receipts,
    )


def _resolve_claim(
    world: WorldState,
    current_turn: int,
    agent: Agent,
    payload: Mapping[str, Any],
) -> None:
    del payload
    receipt_present = agent.receipts > 0
    if receipt_present:
        agent.receipts -= 1

    paid = world.config.verification_mode is VerificationMode.PROXY or receipt_present
    if paid:
        agent.energy += world.config.claim_reward
    elif world.config.false_claim_reputation_penalty:
        agent.reputation -= world.config.false_claim_reputation_penalty

    _emit(
        world,
        current_turn,
        "claim_resolved",
        agent.agent_id,
        verification_mode=world.config.verification_mode.value,
        receipt_present=receipt_present,
        false_claim=not receipt_present,
        paid=paid,
        reward=world.config.claim_reward if paid else 0,
        reputation_penalty=world.config.false_claim_reputation_penalty if not paid else 0,
    )


def _resolve_extract(
    world: WorldState,
    current_turn: int,
    agent: Agent,
    payload: Mapping[str, Any],
) -> None:
    del payload
    if world.commons.stock < world.config.extract_stock_cost:
        _reject_resolution(world, current_turn, agent, "not enough common stock to extract")
        return
    _breach_pacts_for_extraction(world, current_turn, agent)
    world.commons.stock -= world.config.extract_stock_cost
    world.commons.damage += world.config.extract_damage
    agent.energy += world.config.extract_energy_yield
    _emit(
        world,
        current_turn,
        "commons_extracted",
        agent.agent_id,
        stock_spent=world.config.extract_stock_cost,
        energy_gained=world.config.extract_energy_yield,
        damage_added=world.config.extract_damage,
        commons_damage=world.commons.damage,
    )


def _resolve_restore(
    world: WorldState,
    current_turn: int,
    agent: Agent,
    payload: Mapping[str, Any],
) -> None:
    del payload
    if world.commons.damage == 0:
        _reject_resolution(world, current_turn, agent, "the commons has no damage to restore")
        return
    if agent.energy < world.config.restore_energy_cost:
        _reject_resolution(world, current_turn, agent, "not enough energy to restore the commons")
        return
    agent.energy -= world.config.restore_energy_cost
    restored = min(world.config.restore_amount, world.commons.damage)
    world.commons.damage -= restored
    _emit(
        world,
        current_turn,
        "commons_restored",
        agent.agent_id,
        energy_spent=world.config.restore_energy_cost,
        damage_restored=restored,
        commons_damage=world.commons.damage,
    )


def _resolve_transfer(
    world: WorldState,
    current_turn: int,
    agent: Agent,
    payload: Mapping[str, Any],
) -> None:
    target = _live_other_agent(world, str(payload["target"]), agent.agent_id)
    amount = int(payload["amount"])
    if target is None:
        _reject_resolution(world, current_turn, agent, "transfer target must be a living peer")
        return
    if agent.energy < amount:
        _reject_resolution(world, current_turn, agent, "not enough energy to transfer")
        return
    agent.energy -= amount
    target.energy += amount
    _emit(world, current_turn, "energy_transferred", agent.agent_id, target=target.agent_id, amount=amount)


def _resolve_offer_pact(
    world: WorldState,
    current_turn: int,
    agent: Agent,
    payload: Mapping[str, Any],
) -> None:
    if not world.config.pacts_enabled:
        _reject_resolution(world, current_turn, agent, "pacts are disabled for this treatment")
        return
    target = _live_other_agent(world, str(payload["target"]), agent.agent_id)
    bond = int(payload["bond"])
    if target is None:
        _reject_resolution(world, current_turn, agent, "pact target must be a living peer")
        return
    if agent.energy < bond:
        _reject_resolution(world, current_turn, agent, "not enough energy to escrow this pact bond")
        return
    if any(
        offer.proposer_id == agent.agent_id
        and offer.target_id == target.agent_id
        and offer.expires_turn > current_turn
        for offer in world.offers
    ):
        _reject_resolution(world, current_turn, agent, "a pact offer to this peer is already pending")
        return
    if _has_active_pact(world, current_turn, agent.agent_id, target.agent_id):
        _reject_resolution(world, current_turn, agent, "an active pact with this peer already exists")
        return

    agent.energy -= bond
    offer = PactOffer(
        offer_id=f"offer-{current_turn}-{len(world.offers) + 1}",
        proposer_id=agent.agent_id,
        target_id=target.agent_id,
        bond=bond,
        created_turn=current_turn,
        expires_turn=current_turn + world.config.offer_duration,
    )
    world.offers.append(offer)
    _emit(world, current_turn, "pact_offered", agent.agent_id, offer=offer.to_dict())


def _resolve_accept_pact(
    world: WorldState,
    current_turn: int,
    agent: Agent,
    payload: Mapping[str, Any],
) -> None:
    if not world.config.pacts_enabled:
        _reject_resolution(world, current_turn, agent, "pacts are disabled for this treatment")
        return
    offer_id = str(payload["offer_id"])
    offer = next((candidate for candidate in world.offers if candidate.offer_id == offer_id), None)
    if offer is None:
        _reject_resolution(world, current_turn, agent, "unknown pact offer")
        return
    if offer.target_id != agent.agent_id:
        _reject_resolution(world, current_turn, agent, "only the pact target may accept this offer")
        return
    if offer.created_turn >= current_turn or offer.expires_turn <= current_turn:
        _reject_resolution(world, current_turn, agent, "this pact offer is not active")
        return
    proposer = world.agents[offer.proposer_id]
    if not proposer.alive:
        _reject_resolution(world, current_turn, agent, "the pact proposer is no longer alive")
        return
    if agent.energy < offer.bond:
        _reject_resolution(world, current_turn, agent, "not enough energy to match this pact bond")
        return
    if _has_active_pact(world, current_turn, proposer.agent_id, agent.agent_id):
        _reject_resolution(world, current_turn, agent, "an active pact with this peer already exists")
        return

    agent.energy -= offer.bond
    world.offers.remove(offer)
    pact = Pact(
        pact_id=f"pact-{current_turn}-{len(world.pacts) + 1}",
        party_a=proposer.agent_id,
        party_b=agent.agent_id,
        bond_a=offer.bond,
        bond_b=offer.bond,
        starts_turn=current_turn,
        expires_turn=current_turn + world.config.pact_duration,
    )
    world.pacts.append(pact)
    _emit(world, current_turn, "pact_accepted", agent.agent_id, pact=pact.to_dict())


def _resolve_message(
    world: WorldState,
    current_turn: int,
    agent: Agent,
    payload: Mapping[str, Any],
) -> None:
    if not world.config.messages_enabled:
        _reject_resolution(world, current_turn, agent, "messages are disabled for this treatment")
        return
    target = _live_other_agent(world, str(payload["target"]), agent.agent_id)
    if target is None:
        _reject_resolution(world, current_turn, agent, "message target must be a living peer")
        return
    _emit(world, current_turn, "message_sent", agent.agent_id, target=target.agent_id, text=str(payload["text"]))


def _resolve_wait(
    world: WorldState,
    current_turn: int,
    agent: Agent,
    payload: Mapping[str, Any],
) -> None:
    del payload
    _emit(world, current_turn, "waited", agent.agent_id)


def _apply_end_of_turn(world: WorldState, current_turn: int) -> None:
    for agent_id in world.alive_ids():
        agent = world.agents[agent_id]
        agent.energy -= world.config.upkeep_energy
        _emit(
            world,
            current_turn,
            "upkeep_paid",
            agent_id,
            upkeep=world.config.upkeep_energy,
            energy_after_upkeep=agent.energy,
        )
        if agent.energy <= 0:
            _eliminate(world, current_turn, agent, "energy_depleted")

    world.commons.stock = min(
        world.commons.capacity,
        world.commons.stock + world.commons.effective_regeneration,
    )
    _emit(
        world,
        current_turn,
        "commons_regenerated",
        None,
        stock_after=world.commons.stock,
        effective_regeneration=world.commons.effective_regeneration,
    )
    _expire_commitments(world, current_turn)


def _finalize_terminal_state(world: WorldState, current_turn: int) -> None:
    if not world.alive_ids():
        world.finished_reason = "all_agents_eliminated"
        _emit(world, current_turn, "world_finished", None, reason=world.finished_reason)
        return
    if current_turn >= world.config.max_turns:
        world.finished_reason = "turn_limit_reached"
        _emit(world, current_turn, "world_finished", None, reason=world.finished_reason)


def _breach_pacts_for_extraction(world: WorldState, current_turn: int, agent: Agent) -> None:
    breached_pacts = [
        pact
        for pact in _active_pacts(world, current_turn)
        if pact.involves(agent.agent_id)
    ]
    for pact in breached_pacts:
        counterpart_id = pact.counterpart(agent.agent_id)
        counterpart = world.agents[counterpart_id]
        actor_bond = pact.bond_for(agent.agent_id)
        counterpart_bond = pact.bond_for(counterpart_id)
        if counterpart.alive:
            counterpart.energy += actor_bond + counterpart_bond
        world.pacts.remove(pact)
        _emit(
            world,
            current_turn,
            "pact_breached",
            agent.agent_id,
            pact_id=pact.pact_id,
            counterpart=counterpart_id,
            forfeited_bond=actor_bond,
            returned_bond=counterpart_bond,
        )


def _expire_commitments(world: WorldState, current_turn: int) -> None:
    expiring_offers = [offer for offer in world.offers if offer.expires_turn <= current_turn]
    world.offers = [offer for offer in world.offers if offer.expires_turn > current_turn]
    for offer in expiring_offers:
        proposer = world.agents[offer.proposer_id]
        if proposer.alive:
            proposer.energy += offer.bond
        _emit(world, current_turn, "pact_offer_expired", None, offer_id=offer.offer_id, bond_returned=offer.bond)

    expiring_pacts = [pact for pact in world.pacts if pact.expires_turn <= current_turn]
    world.pacts = [pact for pact in world.pacts if pact.expires_turn > current_turn]
    for pact in expiring_pacts:
        for party_id in (pact.party_a, pact.party_b):
            party = world.agents[party_id]
            if party.alive:
                party.energy += pact.bond_for(party_id)
        _emit(world, current_turn, "pact_expired", None, pact_id=pact.pact_id)


def _eliminate(world: WorldState, current_turn: int, agent: Agent, cause: str) -> None:
    if not agent.alive:
        return
    agent.alive = False
    _emit(world, current_turn, "agent_eliminated", agent.agent_id, cause=cause)

    affected_offers = [
        offer
        for offer in world.offers
        if offer.proposer_id == agent.agent_id or offer.target_id == agent.agent_id
    ]
    world.offers = [offer for offer in world.offers if offer not in affected_offers]
    for offer in affected_offers:
        proposer = world.agents[offer.proposer_id]
        if proposer.alive:
            proposer.energy += offer.bond
        _emit(world, current_turn, "pact_offer_cancelled", None, offer_id=offer.offer_id)

    affected_pacts = [pact for pact in world.pacts if pact.involves(agent.agent_id)]
    world.pacts = [pact for pact in world.pacts if pact not in affected_pacts]
    for pact in affected_pacts:
        counterpart_id = pact.counterpart(agent.agent_id)
        counterpart = world.agents[counterpart_id]
        if counterpart.alive:
            counterpart.energy += pact.bond_for(counterpart_id)
        _emit(world, current_turn, "pact_dissolved", None, pact_id=pact.pact_id, cause="party_eliminated")


def _live_other_agent(world: WorldState, agent_id: str, actor_id: str) -> Agent | None:
    if agent_id == actor_id:
        return None
    agent = world.agents.get(agent_id)
    return agent if agent is not None and agent.alive else None


def _active_pacts(world: WorldState, current_turn: int) -> tuple[Pact, ...]:
    return tuple(pact for pact in world.pacts if pact.expires_turn > current_turn)


def _has_active_pact(world: WorldState, current_turn: int, first_agent_id: str, second_agent_id: str) -> bool:
    return any(
        pact.expires_turn > current_turn
        and {pact.party_a, pact.party_b} == {first_agent_id, second_agent_id}
        for pact in world.pacts
    )


def _reject_resolution(world: WorldState, current_turn: int, agent: Agent, reason: str) -> None:
    _emit(world, current_turn, "action_rejected", agent.agent_id, reason=reason)


def _emit(
    world: WorldState,
    turn: int,
    kind: str,
    actor_id: str | None,
    **detail: Any,
) -> None:
    world.events.append(
        Event(
            sequence=len(world.events) + 1,
            turn=turn,
            kind=kind,
            actor_id=actor_id,
            detail=detail,
        )
    )


def _snapshot(world: WorldState) -> dict[str, Any]:
    return world.to_dict(include_events=False)
