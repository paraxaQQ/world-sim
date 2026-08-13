from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Sequence


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = REPOSITORY_ROOT / "src"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from world_sim.survival.calibration import (  # noqa: E402
    DEFAULT_BOOTSTRAP_SAMPLES,
    DEFAULT_CALIBRATION_CYCLES,
    LEAN_CAMP_V1,
    canonical_calibration_json,
    run_calibration,
)


def _positive_int(raw_value: str) -> int:
    value = int(raw_value)
    if value < 1:
        raise argparse.ArgumentTypeError("value must be positive")
    return value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Calibrate the deterministic survival ecology without model calls."
    )
    parser.add_argument("--preset", choices=(LEAN_CAMP_V1,), default=LEAN_CAMP_V1)
    parser.add_argument("--seed-start", type=int, default=0)
    parser.add_argument("--seed-count", type=_positive_int, default=256)
    parser.add_argument(
        "--cycles", type=_positive_int, default=DEFAULT_CALIBRATION_CYCLES
    )
    parser.add_argument(
        "--bootstrap-samples",
        type=_positive_int,
        default=DEFAULT_BOOTSTRAP_SAMPLES,
    )
    parser.add_argument("--output", type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    seeds = tuple(range(args.seed_start, args.seed_start + args.seed_count))
    report = run_calibration(
        preset=args.preset,
        seeds=seeds,
        cycles=args.cycles,
        bootstrap_samples=args.bootstrap_samples,
    )
    payload = canonical_calibration_json(report)
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(payload + "\n", encoding="utf-8")
    print(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
