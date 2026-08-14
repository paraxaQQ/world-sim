from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections.abc import Sequence
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = REPOSITORY_ROOT / "src"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from world_sim.session_catalog import (  # noqa: E402
    SESSION_ARTIFACTS,
    audit_source_mapping,
    build_session_catalog,
    catalog_filename,
    materialize_session_catalog,
    verify_catalog_source_blobs,
    write_session_catalog,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Build, verify, or materialize the lossless sessions 1-5 migration "
            "catalogs."
        )
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    build = subparsers.add_parser(
        "build",
        help="build catalogs from their pinned source commits",
    )
    build.add_argument(
        "--session",
        type=_session_number,
        action="append",
        help="session to build; repeat as needed (default: all)",
    )
    build.add_argument("--repo-root", type=Path, default=REPOSITORY_ROOT)
    build.add_argument(
        "--output-dir",
        type=Path,
        default=REPOSITORY_ROOT / "outputs",
    )
    build.add_argument("--overwrite", action="store_true")

    verify = subparsers.add_parser(
        "verify",
        help="verify strict schemas, lossless payloads, and pinned source blobs",
    )
    verify.add_argument("catalog", type=Path, nargs="+")
    verify.add_argument("--repo-root", type=Path, default=REPOSITORY_ROOT)

    materialize = subparsers.add_parser(
        "materialize",
        help="verify and restore exact legacy paths from one or more catalogs",
    )
    materialize.add_argument("catalog", type=Path, nargs="+")
    materialize.add_argument("--destination", type=Path, required=True)
    materialize.add_argument("--repo-root", type=Path, default=REPOSITORY_ROOT)
    materialize.add_argument("--overwrite", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "build":
            report = _build(args)
        elif args.command == "verify":
            report = _verify(args)
        else:
            report = _materialize(args)
    except (OSError, RuntimeError, ValueError) as error:
        sys.stderr.write(f"error: {error}\n")
        return 1
    sys.stdout.write(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    )
    return 0


def _build(args: argparse.Namespace) -> dict[str, object]:
    repo_root = args.repo_root.resolve()
    output_dir = args.output_dir.resolve()
    audit_source_mapping(repo_root)
    sessions = (
        sorted(set(args.session))
        if args.session
        else list(SESSION_ARTIFACTS)
    )
    catalogs: list[dict[str, object]] = []
    for session in sessions:
        catalog = build_session_catalog(repo_root, session)
        path = output_dir / catalog_filename(session)
        catalog_sha256 = write_session_catalog(
            catalog,
            path,
            overwrite=args.overwrite,
        )
        catalogs.append(
            {
                "session": session,
                "path": _display_path(path, repo_root),
                "catalog_sha256": catalog_sha256,
                "artifacts": len(catalog["artifacts"]),
            }
        )
    return {"status": "built", "catalogs": catalogs}


def _verify(args: argparse.Namespace) -> dict[str, object]:
    catalogs: list[dict[str, object]] = []
    repo_root = args.repo_root.resolve()
    for path in args.catalog:
        raw = path.read_bytes()
        verified = verify_catalog_source_blobs(repo_root, path)
        catalogs.append(
            {
                "session": verified.session,
                "path": path.as_posix(),
                "catalog_sha256": hashlib.sha256(raw).hexdigest(),
                "source_commit": verified.source_commit,
                "artifacts": len(verified.artifacts),
                "raw_bytes": sum(
                    len(artifact.raw) for artifact in verified.artifacts
                ),
            }
        )
    return {"status": "verified", "catalogs": catalogs}


def _materialize(args: argparse.Namespace) -> dict[str, object]:
    repo_root = args.repo_root.resolve()
    catalogs: list[dict[str, int]] = []
    total_artifacts = 0
    for path in args.catalog:
        verified = verify_catalog_source_blobs(repo_root, path)
        written = materialize_session_catalog(
            verified,
            args.destination,
            overwrite=args.overwrite,
        )
        count = len(written)
        total_artifacts += count
        catalogs.append({"session": verified.session, "artifacts": count})
    return {
        "status": "materialized",
        "destination": args.destination.resolve().as_posix(),
        "artifacts": total_artifacts,
        "catalogs": catalogs,
    }


def _session_number(value: str) -> int:
    try:
        session = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("session must be an integer") from error
    if session not in SESSION_ARTIFACTS:
        valid = ", ".join(str(item) for item in SESSION_ARTIFACTS)
        raise argparse.ArgumentTypeError(f"session must be one of: {valid}")
    return session


def _display_path(path: Path, repo_root: Path) -> str:
    try:
        return path.relative_to(repo_root).as_posix()
    except ValueError:
        return path.as_posix()


if __name__ == "__main__":
    raise SystemExit(main())
