from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = REPOSITORY_ROOT / "src"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from world_sim.reachability import (  # noqa: E402
    canonical_reachability_json,
    run_shelter_reachability_control,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Check whether a shelter is physically reachable from a live parent."
    )
    parser.add_argument("--parent", type=Path, required=True)
    parser.add_argument("--parent-sha256", required=True)
    parser.add_argument("--ancestor", action="append", type=Path, default=[])
    parser.add_argument("--transition-id", required=True)
    parser.add_argument("--shared-wood-stock", type=int, default=0)
    parser.add_argument("--donor", default="Cinder")
    parser.add_argument("--builder", default="Lumen")
    parser.add_argument("--gift-amount", type=int, default=2)
    parser.add_argument("--output", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    receipt = run_shelter_reachability_control(
        parent_path=args.parent,
        expected_parent_sha256=args.parent_sha256,
        ancestor_paths=args.ancestor,
        transition_reason=args.transition_id,
        shared_wood_stock=args.shared_wood_stock,
        donor=args.donor,
        builder=args.builder,
        gift_amount=args.gift_amount,
    )
    payload = canonical_reachability_json(receipt) + "\n"
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_bytes(payload.encode("utf-8"))
    print(payload, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
