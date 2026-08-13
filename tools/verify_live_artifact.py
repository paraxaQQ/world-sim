from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = REPOSITORY_ROOT / "src"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from world_sim.survival.demo import result_sha256  # noqa: E402
from world_sim.survival.engine import replay_survival  # noqa: E402
from world_sim.survival.models import SurvivalResult  # noqa: E402


SOURCE_FILES = {
    "cli_sha256": SOURCE_ROOT / "world_sim" / "cli.py",
    "model_host_sha256": SOURCE_ROOT / "world_sim" / "model_host.py",
    "demo_sha256": SOURCE_ROOT / "world_sim" / "survival" / "demo.py",
    "engine_sha256": SOURCE_ROOT / "world_sim" / "survival" / "engine.py",
    "models_sha256": SOURCE_ROOT / "world_sim" / "survival" / "models.py",
    "prompt_sha256": SOURCE_ROOT / "world_sim" / "survival" / "prompt.py",
    "protocol_sha256": SOURCE_ROOT / "world_sim" / "survival" / "protocol.py",
    "calibration_sha256": SOURCE_ROOT
    / "world_sim"
    / "survival"
    / "calibration.py",
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Verify a retained live artifact without provider calls."
    )
    parser.add_argument("artifact", type=Path)
    parser.add_argument("--artifact-sha256")
    return parser


def verify_live_artifact(
    path: Path,
    *,
    expected_artifact_sha256: str | None = None,
) -> dict[str, Any]:
    raw = path.read_bytes()
    artifact_sha256 = hashlib.sha256(raw).hexdigest()
    if (
        expected_artifact_sha256 is not None
        and artifact_sha256 != expected_artifact_sha256.casefold()
    ):
        raise ValueError(
            f"artifact SHA-256 mismatch: expected {expected_artifact_sha256}, "
            f"got {artifact_sha256}"
        )

    artifact = _mapping(json.loads(raw), name="artifact")
    source = _mapping(artifact.get("source"), name="source")
    for key, source_path in SOURCE_FILES.items():
        recorded = source.get(key)
        actual = hashlib.sha256(source_path.read_bytes()).hexdigest()
        if recorded != actual:
            raise ValueError(
                f"source SHA-256 mismatch for {source_path.relative_to(REPOSITORY_ROOT)}: "
                f"expected {recorded}, got {actual}"
            )

    payload = _mapping(artifact.get("result"), name="result")
    result = SurvivalResult(
        initial_state=dict(_mapping(payload.get("initial_state"), name="initial_state")),
        final_state=dict(_mapping(payload.get("final_state"), name="final_state")),
        events=tuple(_sequence(payload.get("events"), name="events")),
        choice_tape=tuple(_sequence(payload.get("choice_tape"), name="choice_tape")),
        event_sequence_base=int(payload.get("event_sequence_base", 0)),
    )
    if replay_survival(result).to_dict() != result.to_dict():
        raise ValueError("exact replay mismatch")
    canonical_result_sha256 = result_sha256(result)
    if artifact.get("canonical_result_sha256") != canonical_result_sha256:
        raise ValueError("canonical result SHA-256 mismatch")

    return {
        "artifact_sha256": artifact_sha256,
        "canonical_result_sha256": canonical_result_sha256,
        "exact_replay": True,
        "source_hashes_matched": len(SOURCE_FILES),
    }


def _mapping(value: object, *, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{name} must be an object")
    return value


def _sequence(value: object, *, name: str) -> Sequence[Any]:
    if not isinstance(value, list):
        raise ValueError(f"{name} must be an array")
    return value


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    receipt = verify_live_artifact(
        args.artifact,
        expected_artifact_sha256=args.artifact_sha256,
    )
    print(json.dumps(receipt, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
