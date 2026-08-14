from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import subprocess
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path, PurePosixPath
from types import ModuleType

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = REPOSITORY_ROOT / "src"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from world_sim.session_catalog import (  # noqa: E402
    SESSION_ARTIFACTS,
    append_direct_matrix_attempt,
    audit_source_mapping,
    build_artifact_record,
    build_session_catalog,
    catalog_filename,
    materialize_session_catalog,
    parse_session_catalog,
    render_session_catalog,
    replace_session_pair,
    verify_catalog_source_blobs,
    write_session_catalog,
)

SCORER_PATH = Path(__file__).with_name("score_turn_order_matrix.py")
POSTMORTEM_VERIFIER_PATH = Path(__file__).with_name(
    "verify_postmortem_artifact.py"
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Manage lossless migration catalogs and seal direct session-5 "
            "matrix attempts."
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

    append = subparsers.add_parser(
        "append-matrix-attempt",
        help="seal one complete direct matrix attempt into the session-5 pair",
    )
    append.add_argument("catalog", type=Path)
    append.add_argument(
        "--manifest",
        type=Path,
        default=REPOSITORY_ROOT / "docs" / "SESSION_005_ATTEMPT_002.json",
    )
    append.add_argument("--result", type=Path, required=True)
    append.add_argument("--proof", type=Path, required=True)
    append.add_argument("--postmortem", type=Path, action="append", default=[])
    append.add_argument("--receipt", type=Path)
    append.add_argument("--repo-root", type=Path, default=REPOSITORY_ROOT)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "build":
            report = _build(args)
        elif args.command == "verify":
            report = _verify(args)
        elif args.command == "materialize":
            report = _materialize(args)
        else:
            report = package_matrix_attempt(
                repo_root=args.repo_root,
                catalog_path=args.catalog,
                receipt_path=args.receipt,
                manifest_path=args.manifest,
                result_path=args.result,
                proof_path=args.proof,
                postmortem_paths=args.postmortem,
            )
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


def package_matrix_attempt(
    *,
    repo_root: Path,
    catalog_path: Path,
    receipt_path: Path | None,
    manifest_path: Path,
    result_path: Path,
    proof_path: Path,
    postmortem_paths: Sequence[Path] = (),
    require_committed_manifest: bool = True,
) -> dict[str, object]:
    repo_root = repo_root.resolve()
    catalog_path = catalog_path.resolve()
    receipt_path = (
        catalog_path.with_suffix(".md")
        if receipt_path is None
        else receipt_path.resolve()
    )
    manifest_path = manifest_path.resolve()
    result_path = result_path.resolve()
    proof_path = proof_path.resolve()
    postmortem_paths = tuple(path.resolve() for path in postmortem_paths)
    if catalog_path != repo_root / "outputs" / "session-005.json":
        raise ValueError("matrix attempts can only update outputs/session-005.json")
    if receipt_path != repo_root / "outputs" / "session-005.md":
        raise ValueError("matrix attempts can only update outputs/session-005.md")
    current = parse_session_catalog(catalog_path.read_bytes())
    if require_committed_manifest:
        verify_catalog_source_blobs(repo_root, current)
    if require_committed_manifest:
        _verify_committed_manifest(repo_root, manifest_path)

    scorer = _load_module(SCORER_PATH, "world_sim_session_catalog_scorer")
    manifest, _ = scorer._load_json_object(manifest_path, name="matrix manifest")
    attempt = manifest.get("attempt")
    if type(attempt) is not int or attempt < 2:
        raise ValueError("matrix manifest must identify a direct attempt of at least 2")
    if current.get("format_version") == 2:
        raw_attempts = current.get("attempts")
        if isinstance(raw_attempts, list) and any(
            isinstance(row, Mapping) and row.get("attempt") == attempt
            for row in raw_attempts
        ):
            raise ValueError(f"session 5 already contains attempt {attempt:03d}")
    cells = manifest.get("cells")
    if not isinstance(cells, list) or len(cells) != 12:
        raise ValueError("matrix manifest must contain exactly 12 cells")
    cell_paths: list[Path] = []
    for position, cell in enumerate(cells, start=1):
        if not isinstance(cell, Mapping) or not isinstance(cell.get("output"), str):
            raise ValueError(f"matrix manifest cell {position} has no output path")
        cell_path = _resolve_repo_input(repo_root, str(cell["output"]))
        if not cell_path.is_file():
            raise ValueError(f"matrix cell {position} is not terminal: {cell_path}")
        cell_paths.append(cell_path)

    score = scorer.score_turn_order_matrix(
        manifest_path,
        repository_root=repo_root,
    )
    batch = score.get("batch")
    protocol = score.get("protocol")
    if (
        not isinstance(batch, Mapping)
        or batch.get("terminal") is not True
        or batch.get("planned_n") != 12
        or batch.get("pending_n") != 0
        or not isinstance(protocol, Mapping)
        or protocol.get("status") != "adhered"
    ):
        raise ValueError("matrix attempt is incomplete or did not adhere to its protocol")
    expected_result = (
        json.dumps(score, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
    if result_path.read_bytes() != expected_result:
        raise ValueError("matrix result does not match deterministic rescoring")
    if not proof_path.is_file() or proof_path.suffix.casefold() != ".md":
        raise ValueError("matrix proof must be a non-empty readable artifact")
    proof_raw = proof_path.read_bytes()
    try:
        proof_text = proof_raw.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ValueError("matrix proof must be readable UTF-8 Markdown") from error
    if not proof_text.strip():
        raise ValueError("matrix proof must be a non-empty readable artifact")

    _verify_postmortems(
        repo_root=repo_root,
        manifest=manifest,
        cell_paths=cell_paths,
        postmortem_paths=postmortem_paths,
    )
    ordered_inputs: list[tuple[str, Path]] = [
        ("matrix_manifest", manifest_path),
        *(("matrix_cell", path) for path in cell_paths),
        ("matrix_result", result_path),
        ("proof", proof_path),
        *(("postmortem", path) for path in sorted(postmortem_paths)),
    ]
    records = [
        build_artifact_record(
            _repo_relative(path, repo_root),
            role,
            path.read_bytes(),
        )
        for role, path in ordered_inputs
    ]
    updated = append_direct_matrix_attempt(
        current,
        attempt=attempt,
        artifacts=records,
    )
    catalog_bytes = render_session_catalog(updated)
    catalog_sha256 = hashlib.sha256(catalog_bytes).hexdigest()
    receipt = _render_attempt_receipt(
        receipt_path.read_bytes(),
        attempt=attempt,
        score=score,
        catalog_sha256=catalog_sha256,
        records=records,
    )
    written_catalog_sha256, receipt_sha256 = replace_session_pair(
        updated,
        catalog_path,
        receipt,
        receipt_path,
    )
    if written_catalog_sha256 != catalog_sha256:
        raise RuntimeError("written session catalog hash changed during replacement")
    return {
        "status": "appended",
        "session": 5,
        "attempt": attempt,
        "artifacts": len(records),
        "postmortems": len(postmortem_paths),
        "catalog_sha256": written_catalog_sha256,
        "receipt_sha256": receipt_sha256,
        "batch": dict(batch),
        "protocol": dict(protocol),
    }


def _load_module(path: Path, name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load tool module: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _verify_committed_manifest(repo_root: Path, manifest_path: Path) -> None:
    relative = _repo_relative(manifest_path, repo_root)
    if not relative.startswith("docs/SESSION_005_ATTEMPT_") or not relative.endswith(
        ".json"
    ):
        raise ValueError("matrix manifest must be a committed session-5 document")
    result = subprocess.run(
        ["git", "-C", str(repo_root), "cat-file", "blob", f"HEAD:{relative}"],
        capture_output=True,
        check=False,
    )
    if result.returncode != 0 or result.stdout != manifest_path.read_bytes():
        raise ValueError("matrix manifest does not match its committed HEAD blob")


def _resolve_repo_input(repo_root: Path, value: str) -> Path:
    if not value or "\\" in value:
        raise ValueError("matrix input paths must be safe repository-relative paths")
    relative = PurePosixPath(value)
    if (
        relative.is_absolute()
        or relative.as_posix() != value
        or any(part in {"", ".", ".."} or ":" in part for part in relative.parts)
    ):
        raise ValueError("matrix input paths must be safe repository-relative paths")
    candidate = repo_root.joinpath(*relative.parts).resolve()
    try:
        candidate.relative_to(repo_root)
    except ValueError as error:
        raise ValueError("matrix input path escapes the repository root") from error
    return candidate


def _repo_relative(path: Path, repo_root: Path) -> str:
    path = path.resolve()
    try:
        relative = path.relative_to(repo_root)
    except ValueError as error:
        raise ValueError(f"matrix input is outside the repository root: {path}") from error
    rendered = relative.as_posix()
    if rendered.startswith("../") or rendered in {"", "."}:
        raise ValueError(f"matrix input has an unsafe repository path: {path}")
    return rendered


def _verify_postmortems(
    *,
    repo_root: Path,
    manifest: Mapping[str, object],
    cell_paths: Sequence[Path],
    postmortem_paths: Sequence[Path],
) -> None:
    if not postmortem_paths:
        return
    if len(set(postmortem_paths)) != len(postmortem_paths):
        raise ValueError("postmortem paths must be unique")
    fixed = manifest.get("fixed_treatment")
    if not isinstance(fixed, Mapping):
        raise ValueError("matrix manifest fixed treatment is invalid")
    ancestor_value = fixed.get("ancestor_artifact")
    parent_value = fixed.get("parent_artifact")
    if not isinstance(ancestor_value, str) or not isinstance(parent_value, str):
        raise ValueError("matrix manifest lineage paths are invalid")
    ancestor_paths = (
        _resolve_repo_input(repo_root, ancestor_value),
        _resolve_repo_input(repo_root, parent_value),
    )
    worlds = {path.name: path for path in cell_paths}
    if len(worlds) != len(cell_paths):
        raise ValueError("matrix cell artifact names must be unique")
    linked_worlds: set[Path] = set()
    verifier = _load_module(
        POSTMORTEM_VERIFIER_PATH,
        "world_sim_session_catalog_postmortem_verifier",
    )
    for path in postmortem_paths:
        raw = path.read_bytes()
        try:
            artifact = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ValueError(f"postmortem is not valid UTF-8 JSON: {path}") from error
        if not isinstance(artifact, Mapping):
            raise ValueError(f"postmortem must be an object: {path}")
        link = artifact.get("world_link")
        if not isinstance(link, Mapping) or not isinstance(
            link.get("artifact_name"), str
        ):
            raise ValueError(f"postmortem world link is invalid: {path}")
        world = worlds.get(str(link["artifact_name"]))
        if world is None:
            raise ValueError(f"postmortem does not link to an attempt cell: {path}")
        if world in linked_worlds:
            raise ValueError(f"multiple postmortems link to the same attempt cell: {world}")
        linked_worlds.add(world)
        verifier.verify_postmortem_artifact(
            path,
            world_artifact_path=world,
            expected_artifact_sha256=hashlib.sha256(raw).hexdigest(),
            ancestor_paths=ancestor_paths,
        )


def _render_attempt_receipt(
    existing: bytes,
    *,
    attempt: int,
    score: Mapping[str, object],
    catalog_sha256: str,
    records: Sequence[Mapping[str, object]],
) -> bytes:
    try:
        text = existing.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ValueError("existing session receipt must be UTF-8") from error
    heading = f"## attempt {attempt:03d} - replacement matrix"
    if heading in text:
        raise ValueError(f"session receipt already contains attempt {attempt:03d}")
    if attempt == 2:
        text = text.replace(
            "# session 005 - turn-order matrix, attempt 001",
            "# session 005 - turn-order matrix",
            1,
        )
        text = text.replace(
            "**status:** sealed and deviated; not cleanly complete",
            "**attempt 001 status:** sealed and deviated; not cleanly complete",
            1,
        )
    batch = score.get("batch")
    protocol = score.get("protocol")
    phases = score.get("phases")
    assert isinstance(batch, Mapping)
    assert isinstance(protocol, Mapping)
    assert isinstance(phases, list)
    phase_rates = [
        phase.get("primary_success_rate")
        for phase in phases
        if isinstance(phase, Mapping)
    ]
    by_role = {
        role: [row for row in records if row.get("role") == role]
        for role in {str(row.get("role")) for row in records}
    }
    manifest = by_role["matrix_manifest"][0]
    result = by_role["matrix_result"][0]
    proof = by_role["proof"][0]
    section = f"""

{heading}

**status:** sealed; protocol {protocol['status']}

attempt {attempt:03d} executed all 12 frozen sibling cells. {batch['scoreable_n']} cells are scoreable, {batch['censored_n']} are censored technical failures, and {batch['primary_success_count']} scoreable cells completed the primary shelter-enabling chain. phase success rates are `{phase_rates}`.

## attempt {attempt:03d} evidence identity

- catalog SHA-256: `{catalog_sha256}`
- embedded artifacts: `{len(records)}`
- matrix manifest SHA-256: `{manifest['raw_sha256']}`
- scored result SHA-256: `{result['raw_sha256']}`
- readable proof SHA-256: `{proof['raw_sha256']}`
- postmortem artifacts: `{len(by_role.get('postmortem', []))}`
- conservative cost exposure: `${batch['cost_exposure_usd']}`

the attempt was packaged directly from ignored `artifacts/` files into this JSON/Markdown pair. no split attempt artifact is committed, and direct artifacts claim no Git source commit.
"""
    return (text.rstrip() + section.rstrip() + "\n").encode("utf-8")


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
