from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .engine import make_world, run_simulation
from .metrics import calculate_metrics, metric_delta
from .models import SimulationResult, VerificationMode, WorldConfig
from .policies import pilot_policies


PILOT_AGENT_IDS = ("receipt", "shortcut", "extractor", "steward")


@dataclass(frozen=True)
class ExperimentRun:
    result: SimulationResult
    metrics: dict[str, int]

    def to_dict(self) -> dict[str, Any]:
        return {"result": self.result.to_dict(), "metrics": self.metrics}


@dataclass(frozen=True)
class CounterfactualPair:
    proxy: ExperimentRun
    receipts: ExperimentRun

    def to_dict(self) -> dict[str, Any]:
        return {
            "shared_world": _shared_world(self.proxy.result.initial_state),
            "proxy": self.proxy.to_dict(),
            "receipts": self.receipts.to_dict(),
            "receipts_minus_proxy": metric_delta(self.proxy.metrics, self.receipts.metrics),
        }


def run_pilot(
    *,
    verification_mode: VerificationMode,
    seed: int,
    turns: int = 12,
) -> ExperimentRun:
    """Run the bundled reference population once in one verification treatment."""

    config = WorldConfig(verification_mode=verification_mode, max_turns=turns)
    world = make_world(PILOT_AGENT_IDS, seed=seed, config=config)
    result = run_simulation(world, pilot_policies(), turns=turns)
    return ExperimentRun(result=result, metrics=calculate_metrics(result))


def run_counterfactual_pair(*, seed: int, turns: int = 12) -> CounterfactualPair:
    """Run the same seeded reference population under the two verification treatments."""

    proxy = run_pilot(verification_mode=VerificationMode.PROXY, seed=seed, turns=turns)
    receipts = run_pilot(verification_mode=VerificationMode.RECEIPTS, seed=seed, turns=turns)
    return CounterfactualPair(proxy=proxy, receipts=receipts)


def _shared_world(initial_state: dict[str, Any]) -> dict[str, Any]:
    config = {
        key: value
        for key, value in initial_state["config"].items()
        if key != "verification_mode"
    }
    config["verification_mode"] = "treatment-controlled"
    return {
        "seed": initial_state["seed"],
        "agents": [
            {
                "id": agent["id"],
                "lineage_id": agent["lineage_id"],
                "parent_lineage_id": agent["parent_lineage_id"],
                "bundle_version": agent["bundle_version"],
                "energy": agent["energy"],
            }
            for agent in initial_state["agents"]
        ],
        "commons": initial_state["commons"],
        "config_except_verification": config,
    }
