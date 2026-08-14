from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .model_host import WORLD_SIM_VERSION, _load_verified_parent_artifact
from .survival.calibration import MutualAidPolicy
from .survival.demo import result_sha256
from .survival.engine import (
    SurvivalChoiceProvider,
    adjust_shared_resource,
    continue_survival_world,
    replay_survival,
    run_survival,
)
from .survival.models import SEQUENTIAL_DIALOGUE_V3, SurvivalResult, SurvivorView


@dataclass
class _ScriptedPolicy(SurvivalChoiceProvider):
    choices: tuple[Mapping[str, object], ...]
    cursor: int = 0

    def decide(self, view: SurvivorView) -> Mapping[str, object]:
        del view
        try:
            selected = self.choices[self.cursor]
        except IndexError as error:
            raise RuntimeError("reachability script exhausted its choices") from error
        self.cursor += 1
        return selected


def run_shelter_reachability_control(
    *,
    parent_path: Path,
    expected_parent_sha256: str,
    ancestor_paths: Sequence[Path] = (),
    transition_reason: str,
    shared_wood_stock: int = 0,
    donor: str = "Cinder",
    builder: str = "Lumen",
    gift_amount: int = 2,
) -> dict[str, Any]:
    parent_artifact, parent_result, parent_sha256 = _load_verified_parent_artifact(
        parent_path,
        expected_sha256=expected_parent_sha256,
        ancestor_paths=ancestor_paths,
    )
    names = tuple(str(row["name"]) for row in parent_result.final_state["survivors"])
    if donor == builder or donor not in names or builder not in names:
        raise ValueError("donor and builder must be distinct verified survivors")
    if isinstance(gift_amount, bool) or not isinstance(gift_amount, int):
        raise TypeError("gift_amount must be an integer")
    if gift_amount < 1:
        raise ValueError("gift_amount must be positive")

    baseline_world = continue_survival_world(
        parent_result,
        additional_cycles=1,
        interaction_protocol=SEQUENTIAL_DIALOGUE_V3,
        initiative_phase=0,
    )
    transition = adjust_shared_resource(
        baseline_world,
        resource="wood",
        stock=shared_wood_stock,
        reason=transition_reason,
    )
    baseline_result = run_survival(
        baseline_world,
        {name: MutualAidPolicy() for name in names},
        days=1,
    )
    _assert_exact_replay(baseline_result)

    phase_controls = []
    for phase in range(len(names)):
        world = continue_survival_world(
            parent_result,
            additional_cycles=1,
            interaction_protocol=SEQUENTIAL_DIALOGUE_V3,
            initiative_phase=phase,
        )
        phase_transition = adjust_shared_resource(
            world,
            resource="wood",
            stock=shared_wood_stock,
            reason=transition_reason,
        )
        if phase_transition.detail != transition.detail:
            raise RuntimeError("initiative phase changed the frozen transition")
        result = run_survival(
            world,
            _shelter_aid_providers(
                names,
                donor=donor,
                builder=builder,
                gift_amount=gift_amount,
            ),
            days=1,
        )
        _assert_exact_replay(result)
        phase_controls.append(
            _phase_receipt(
                phase,
                result,
                donor=donor,
                builder=builder,
                gift_amount=gift_amount,
            )
        )

    baseline_shelters = _events(baseline_result, "shelter_built")
    return {
        "format_version": 1,
        "mode": "deterministic_shelter_reachability_control",
        "source": {
            "world_sim_version": WORLD_SIM_VERSION,
            "reachability_sha256": _sha256(Path(__file__)),
            "model_host_sha256": _sha256(
                Path(__file__).with_name("model_host.py")
            ),
            "engine_sha256": _sha256(
                Path(__file__).with_name("survival") / "engine.py"
            ),
            "models_sha256": _sha256(
                Path(__file__).with_name("survival") / "models.py"
            ),
            "calibration_sha256": _sha256(
                Path(__file__).with_name("survival") / "calibration.py"
            ),
        },
        "parent_link": {
            "artifact_name": parent_path.name,
            "artifact_sha256": parent_sha256,
            "canonical_result_sha256": parent_artifact[
                "canonical_result_sha256"
            ],
            "format_version": parent_artifact["format_version"],
            "ancestor_chain": [
                {
                    "artifact_name": path.name,
                    "artifact_sha256": _sha256(path),
                }
                for path in ancestor_paths
            ],
        },
        "frozen_transition": transition.to_dict(),
        "control": {
            "interaction_protocol": SEQUENTIAL_DIALOGUE_V3,
            "donor": donor,
            "builder": builder,
            "gift_amount": gift_amount,
            "script": {
                donor: ["give_wood", "rest"],
                builder: ["build_shelter", "rest"],
                "other_survivors": ["rest"],
            },
            "criterion": (
                "the scripted donor gift resolves before the builder shelter "
                "by beat 3"
            ),
        },
        "generic_mutual_aid_baseline": {
            "policy": "MutualAidPolicy",
            "known_scope": "food aid only; it has no give_wood rule",
            "shelters_built": len(baseline_shelters),
            "canonical_result_sha256": result_sha256(baseline_result),
            "exact_replay": True,
            "result": baseline_result.to_dict(),
        },
        "phase_controls": phase_controls,
        "conclusion": {
            "reachable_in_every_initiative_phase": all(
                bool(row["reachable"]) for row in phase_controls
            ),
            "generic_policy_failure_is_unreachability_evidence": False,
            "bound": (
                "this proves that at least one fixed valid action tape reaches "
                "the shelter under each initiative phase; it does not estimate "
                "how likely a model is to choose that tape"
            ),
        },
    }


def canonical_reachability_json(receipt: Mapping[str, object]) -> str:
    return json.dumps(
        receipt,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _shelter_aid_providers(
    names: Sequence[str],
    *,
    donor: str,
    builder: str,
    gift_amount: int,
) -> dict[str, SurvivalChoiceProvider]:
    rest = {"action": {"kind": "rest"}, "say": None}
    providers: dict[str, SurvivalChoiceProvider] = {}
    for name in names:
        if name == donor:
            choices = (
                {
                    "action": {
                        "kind": "give_wood",
                        "target": builder,
                        "amount": gift_amount,
                    },
                    "say": None,
                },
                rest,
            )
        elif name == builder:
            choices = (
                {"action": {"kind": "build_shelter"}, "say": None},
                rest,
            )
        else:
            choices = (rest,)
        providers[name] = _ScriptedPolicy(choices)
    return providers


def _phase_receipt(
    phase: int,
    result: SurvivalResult,
    *,
    donor: str,
    builder: str,
    gift_amount: int,
) -> dict[str, object]:
    gifts = [
        event
        for event in _events(result, "resource_given")
        if event["actor"] == donor
        and event["detail"].get("target") == builder
        and event["detail"].get("resource") == "wood"
        and event["detail"].get("amount") == gift_amount
    ]
    builds = [
        event
        for event in _events(result, "shelter_built")
        if event["actor"] == builder and int(event["slot"]) <= 3
    ]
    reachable = bool(
        gifts
        and builds
        and int(gifts[0]["sequence"]) < int(builds[0]["sequence"])
    )
    opener = next(
        event["detail"]["initiative_order"][0]
        for event in result.events
        if event["kind"] == "slot_started" and event["slot"] == 1
    )
    return {
        "initiative_phase": phase,
        "beat_1_opener": opener,
        "reachable": reachable,
        "gift_event": gifts[0] if gifts else None,
        "shelter_event": builds[0] if builds else None,
        "canonical_result_sha256": result_sha256(result),
        "exact_replay": True,
        "result": result.to_dict(),
    }


def _events(result: SurvivalResult, kind: str) -> list[dict[str, Any]]:
    return [dict(event) for event in result.events if event["kind"] == kind]


def _assert_exact_replay(result: SurvivalResult) -> None:
    if replay_survival(result).to_dict() != result.to_dict():
        raise RuntimeError("reachability result failed exact replay")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()
