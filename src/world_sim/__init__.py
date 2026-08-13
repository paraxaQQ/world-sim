"""A deterministic, closed-world instrument for social-survival experiments."""

from .engine import make_world, run_simulation, run_turn, view_for
from .experiment import run_counterfactual_pair, run_pilot
from .models import AgentSeed, AgentView, SelectionMode, SimulationResult, VerificationMode, WorldConfig, WorldState
from .selection import (
    ClaimStrategy,
    CommonsStrategy,
    LineageConfig,
    LineageExperiment,
    PolicyBundle,
    PolicyGenome,
    SelectionMatrix,
    default_population,
    replay_generation,
    run_lineage_experiment,
    run_selection_matrix,
)

__all__ = [
    "AgentSeed",
    "AgentView",
    "ClaimStrategy",
    "CommonsStrategy",
    "LineageConfig",
    "LineageExperiment",
    "PolicyBundle",
    "PolicyGenome",
    "SelectionMatrix",
    "SelectionMode",
    "SimulationResult",
    "VerificationMode",
    "WorldConfig",
    "WorldState",
    "make_world",
    "default_population",
    "replay_generation",
    "run_counterfactual_pair",
    "run_lineage_experiment",
    "run_pilot",
    "run_selection_matrix",
    "run_simulation",
    "run_turn",
    "view_for",
]
