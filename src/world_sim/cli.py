from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Sequence

from .experiment import run_counterfactual_pair, run_pilot
from .models import VerificationMode


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run a deterministic objective-verification world-sim treatment.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    pilot = subparsers.add_parser("pilot", help="run the bundled reference population in one treatment")
    _add_run_arguments(pilot)
    pilot.add_argument("--verification", choices=[mode.value for mode in VerificationMode], required=True)

    compare = subparsers.add_parser("compare", help="run the proxy and receipt treatments from the same seed")
    _add_run_arguments(compare)

    args = parser.parse_args(argv)
    if args.command == "pilot":
        run = run_pilot(
            verification_mode=VerificationMode(args.verification),
            seed=args.seed,
            turns=args.turns,
        )
        payload: dict[str, Any] = run.to_dict()
        summary = {
            "verification": args.verification,
            "seed": args.seed,
            "turns_completed": payload["result"]["final_state"]["turn"],
            "finished_reason": payload["result"]["final_state"]["finished_reason"],
            "metrics": payload["metrics"],
        }
    elif args.command == "compare":
        pair = run_counterfactual_pair(seed=args.seed, turns=args.turns)
        payload = pair.to_dict()
        summary = {
            "seed": args.seed,
            "proxy": payload["proxy"]["metrics"],
            "receipts": payload["receipts"]["metrics"],
            "receipts_minus_proxy": payload["receipts_minus_proxy"],
        }
    else:
        raise RuntimeError(f"unsupported command {args.command!r}")

    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        summary["output"] = str(args.output)
    print(json.dumps(summary, sort_keys=True))
    return 0


def _add_run_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--seed", type=int, default=17)
    parser.add_argument("--turns", type=int, default=12)
    parser.add_argument("--output", type=Path)
