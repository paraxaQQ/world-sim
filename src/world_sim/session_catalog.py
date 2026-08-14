from __future__ import annotations

import base64
import binascii
import gzip
import hashlib
import json
import os
import subprocess
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path, PurePosixPath
from typing import Any

MIGRATION_SOURCE_COMMIT = "03c04a389a1b3b06edc46a9d0492ee1c0b9e38ba"
SESSION_SOURCE_COMMITS: Mapping[int, str] = {
    1: MIGRATION_SOURCE_COMMIT,
    2: MIGRATION_SOURCE_COMMIT,
    3: MIGRATION_SOURCE_COMMIT,
    4: MIGRATION_SOURCE_COMMIT,
    5: MIGRATION_SOURCE_COMMIT,
}
CATALOG_FIELDS = {
    "artifacts",
    "format_version",
    "gzip_mtime",
    "mode",
    "payload_encoding",
    "session",
    "source_commit",
}
ARTIFACT_FIELDS = {
    "legacy_path",
    "payload",
    "raw_bytes",
    "raw_sha256",
    "role",
}
MAX_RAW_ARTIFACT_BYTES = 64 * 1024 * 1024
MAX_CATALOG_RAW_BYTES = 256 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class ArtifactSpec:
    legacy_path: str
    role: str


@dataclass(frozen=True, slots=True)
class VerifiedArtifact:
    legacy_path: str
    role: str
    raw_sha256: str
    raw: bytes


@dataclass(frozen=True, slots=True)
class VerifiedSessionCatalog:
    session: int
    source_commit: str
    artifacts: tuple[VerifiedArtifact, ...]


def _spec(legacy_path: str, role: str) -> ArtifactSpec:
    return ArtifactSpec(legacy_path=legacy_path, role=role)


SESSION_ARTIFACTS: Mapping[int, tuple[ArtifactSpec, ...]] = {
    1: (
        _spec("outputs/SURVIVAL_CORE_RECEIPT.md", "receipt"),
        _spec("outputs/v0.4.0-model-host-proof.md", "proof"),
        _spec("outputs/v0.4.2-free-live-smoke-proof.md", "proof"),
        _spec("outputs/v0.4.3-paid-live-smoke-attempt.md", "attempt"),
        _spec("outputs/v0.4.4-paid-live-smoke-proof.md", "proof"),
        _spec("outputs/v0.5.0-lean-camp-v1-confirmation.json", "confirmation"),
        _spec("outputs/v0.5.0-timed-cycle-proof.md", "proof"),
        _spec("outputs/v0.5.1-free-interactive-cycle-29996.json", "world"),
        _spec("outputs/v0.5.1-live-readiness-proof.md", "readiness"),
        _spec("outputs/v0.6.0-paid-observation-29995-proof.md", "proof"),
        _spec("outputs/v0.6.0-paid-observation-29995.json", "world"),
        _spec("outputs/v0.6.0-paid-observation-protocol.md", "protocol"),
        _spec("outputs/v0.7.0-paid-reasoning-29994-proof.md", "proof"),
        _spec("outputs/v0.7.0-paid-reasoning-29994.json", "world"),
        _spec("outputs/v0.7.0-paid-reasoning-protocol.md", "protocol"),
        _spec("outputs/v0.8.0-paid-panel-qualification-001-proof.md", "proof"),
        _spec("outputs/v0.8.0-paid-panel-qualification-001.json", "qualification"),
        _spec("outputs/v0.8.0-paid-panel-qualification-002-proof.md", "proof"),
        _spec(
            "outputs/v0.8.0-paid-panel-qualification-002-protocol.md",
            "protocol",
        ),
        _spec(
            "outputs/v0.8.0-paid-panel-qualification-002-readiness.md",
            "readiness",
        ),
        _spec("outputs/v0.8.0-paid-panel-qualification-002.json", "qualification"),
        _spec("outputs/v0.8.0-paid-panel-qualification-protocol.md", "protocol"),
        _spec("outputs/v0.8.0-paid-panel-qualification-readiness.md", "readiness"),
        _spec("outputs/v0.8.0-paid-survival-29993-proof.md", "proof"),
        _spec("outputs/v0.8.0-paid-survival-29993-protocol.md", "protocol"),
        _spec("outputs/v0.8.0-paid-survival-29993-readiness.md", "readiness"),
        _spec("outputs/v0.8.0-paid-survival-29993.json", "world"),
    ),
    2: (
        _spec("outputs/v0.9.0-paid-panel-qualification-003-proof.md", "proof"),
        _spec(
            "outputs/v0.9.0-paid-panel-qualification-003-protocol.md",
            "protocol",
        ),
        _spec("outputs/v0.9.0-paid-panel-qualification-003.json", "qualification"),
        _spec("outputs/v0.9.0-session-002-shelter-dilemma-29993-proof.md", "proof"),
        _spec(
            "outputs/v0.9.0-session-002-shelter-dilemma-29993-protocol.md",
            "protocol",
        ),
        _spec("outputs/v0.9.0-session-002-shelter-dilemma-29993.json", "world"),
    ),
    3: (
        _spec("outputs/v0.10.0-global-beats-v2-confirmation.json", "confirmation"),
        _spec("outputs/v0.10.0-global-beats-v2-proof.md", "proof"),
        _spec(
            "outputs/v0.11.0-session-003-global-beats-shelter-dilemma-29993-proof.md",
            "proof",
        ),
        _spec(
            "outputs/v0.11.0-session-003-global-beats-shelter-dilemma-29993-protocol.md",
            "protocol",
        ),
        _spec(
            "outputs/v0.11.0-session-003-global-beats-shelter-dilemma-29993.json",
            "world",
        ),
    ),
    4: (
        _spec("outputs/v0.12.0-gpt-5.6-luna-qualification-proof.md", "proof"),
        _spec("outputs/v0.12.0-gpt-5.6-luna-qualification.json", "qualification"),
        _spec("outputs/v0.12.0-sequential-dialogue-v3-confirmation.json", "confirmation"),
        _spec("outputs/v0.12.0-sequential-dialogue-v3-proof.md", "proof"),
        _spec("outputs/v0.12.1-gpt-5.6-luna-qualification-proof.md", "proof"),
        _spec("outputs/v0.12.1-gpt-5.6-luna-qualification-protocol.md", "protocol"),
        _spec("outputs/v0.12.1-gpt-5.6-luna-qualification.json", "qualification"),
        _spec(
            "outputs/v0.12.1-session-004-sequential-dialogue-shelter-dilemma-29993-proof.md",
            "proof",
        ),
        _spec(
            "outputs/v0.12.1-session-004-sequential-dialogue-shelter-dilemma-29993-protocol.md",
            "protocol",
        ),
        _spec(
            "outputs/v0.12.1-session-004-sequential-dialogue-shelter-dilemma-29993.json",
            "world",
        ),
        _spec(
            "outputs/v0.13.0-session-004-shelter-reachability-control-29993-proof.md",
            "proof",
        ),
        _spec(
            "outputs/v0.13.0-session-004-shelter-reachability-control-29993.json",
            "control",
        ),
        _spec(
            "outputs/v0.13.1-session-004b-doomed-continuation-29993-postmortem.json",
            "postmortem",
        ),
        _spec(
            "outputs/v0.13.1-session-004b-doomed-continuation-29993-proof.md",
            "proof",
        ),
        _spec(
            "outputs/v0.13.1-session-004b-doomed-continuation-29993.json",
            "world",
        ),
    ),
    5: (
        _spec("outputs/v0.13.0-session-005-turn-order-b01-p0-29993-proof.md", "proof"),
        _spec("outputs/v0.13.0-session-005-turn-order-b01-p0-29993.json", "matrix_cell"),
        _spec("outputs/v0.13.0-session-005-turn-order-b01-p1-29993.json", "matrix_cell"),
        _spec("outputs/v0.13.0-session-005-turn-order-b01-p2-29993.json", "matrix_cell"),
        _spec("outputs/v0.13.0-session-005-turn-order-b01-p3-29993.json", "matrix_cell"),
        _spec("outputs/v0.13.0-session-005-turn-order-b02-p0-29993.json", "matrix_cell"),
        _spec("outputs/v0.13.0-session-005-turn-order-b02-p1-29993.json", "matrix_cell"),
        _spec("outputs/v0.13.0-session-005-turn-order-b02-p2-29993.json", "matrix_cell"),
        _spec("outputs/v0.13.0-session-005-turn-order-b02-p3-29993.json", "matrix_cell"),
        _spec("outputs/v0.13.0-session-005-turn-order-b03-p0-29993.json", "matrix_cell"),
        _spec("outputs/v0.13.0-session-005-turn-order-b03-p1-29993.json", "matrix_cell"),
        _spec("outputs/v0.13.0-session-005-turn-order-b03-p2-29993.json", "matrix_cell"),
        _spec("outputs/v0.13.0-session-005-turn-order-b03-p3-29993.json", "matrix_cell"),
        _spec(
            "outputs/v0.13.0-session-005-turn-order-matrix-protocol.json",
            "matrix_manifest",
        ),
        _spec(
            "outputs/v0.13.0-session-005-turn-order-matrix-protocol.md",
            "protocol",
        ),
        _spec("outputs/v0.14.0-session-005-turn-order-matrix-proof.md", "proof"),
        _spec(
            "outputs/v0.14.0-session-005-turn-order-matrix-results.json",
            "matrix_result",
        ),
        _spec("outputs/v0.14.1-session-005-postmortem-seal-proof.md", "seal"),
        _spec(
            "outputs/v0.14.1-session-005-turn-order-b02-p3-29993-postmortem.json",
            "postmortem",
        ),
        _spec(
            "outputs/v0.14.1-session-005-turn-order-b03-p2-29993-postmortem.json",
            "postmortem",
        ),
    ),
}


def catalog_filename(session: int) -> str:
    _session_specs(session)
    return f"session-{session:03d}.json"


def build_session_catalog(repo_root: Path, session: int) -> dict[str, object]:
    specs = _session_specs(session)
    source_commit = SESSION_SOURCE_COMMITS[session]
    return {
        "format_version": 1,
        "mode": "campaign_session_catalog",
        "payload_encoding": "gzip+base64",
        "gzip_mtime": 0,
        "session": session,
        "source_commit": source_commit,
        "artifacts": [
            _artifact_record(
                spec,
                _read_git_blob(repo_root, source_commit, spec.legacy_path),
            )
            for spec in specs
        ],
    }


def build_session_catalogs(repo_root: Path) -> tuple[dict[str, object], ...]:
    audit_source_mapping(repo_root)
    return tuple(
        build_session_catalog(repo_root, session) for session in SESSION_ARTIFACTS
    )


def audit_source_mapping(repo_root: Path) -> None:
    # ponytail: this exact-inventory audit is the one-time sessions 1-5 migration gate.
    mapped = [
        spec.legacy_path
        for specs in SESSION_ARTIFACTS.values()
        for spec in specs
    ]
    if len(mapped) != 73 or len(set(mapped)) != 73:
        raise RuntimeError("session catalog map must contain 73 unique legacy paths")
    committed = _git_output_paths(repo_root)
    if committed != sorted(mapped):
        missing = sorted(set(committed) - set(mapped))
        extra = sorted(set(mapped) - set(committed))
        raise ValueError(
            "session catalog map does not exactly cover source outputs; "
            f"unmapped={missing}, absent={extra}"
        )


def render_session_catalog(catalog: Mapping[str, object]) -> bytes:
    verify_session_catalog(catalog)
    return (
        json.dumps(catalog, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")


def write_session_catalog(
    catalog: Mapping[str, object],
    path: Path,
    *,
    overwrite: bool = False,
) -> str:
    rendered = render_session_catalog(catalog)
    path.parent.mkdir(parents=True, exist_ok=True)
    _write_bytes(path, rendered, overwrite=overwrite)
    return hashlib.sha256(rendered).hexdigest()


def parse_session_catalog(raw: bytes) -> Mapping[str, object]:
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ValueError("session catalog must be UTF-8 JSON") from error
    try:
        loaded = json.loads(text, object_pairs_hook=_object_without_duplicates)
    except json.JSONDecodeError as error:
        raise ValueError("session catalog must be valid JSON") from error
    if not isinstance(loaded, Mapping):
        raise TypeError("session catalog must be an object")
    return loaded


def load_session_catalog(path: Path) -> VerifiedSessionCatalog:
    return verify_session_catalog(parse_session_catalog(path.read_bytes()))


def verify_catalog_source_blobs(
    repo_root: Path,
    catalog: VerifiedSessionCatalog | Mapping[str, object] | Path,
) -> VerifiedSessionCatalog:
    if isinstance(catalog, Path):
        verified = load_session_catalog(catalog)
    elif isinstance(catalog, VerifiedSessionCatalog):
        verified = catalog
    else:
        verified = verify_session_catalog(catalog)

    for artifact in verified.artifacts:
        source = _read_git_blob(
            repo_root,
            verified.source_commit,
            artifact.legacy_path,
        )
        if source != artifact.raw:
            raise ValueError(
                "session catalog payload does not match its pinned source blob: "
                f"{artifact.legacy_path}"
            )
    return verified


def verify_session_catalog(
    catalog: Mapping[str, object],
) -> VerifiedSessionCatalog:
    if set(catalog) != CATALOG_FIELDS:
        raise ValueError("session catalog has unexpected fields")
    session = catalog.get("session")
    if type(session) is not int or session not in SESSION_ARTIFACTS:
        raise ValueError("session catalog has an invalid session")
    source_commit = SESSION_SOURCE_COMMITS[session]
    if catalog.get("source_commit") != source_commit:
        raise ValueError("session catalog has an invalid source commit")
    if (
        catalog.get("format_version") != 1
        or catalog.get("mode") != "campaign_session_catalog"
        or catalog.get("payload_encoding") != "gzip+base64"
        or type(catalog.get("gzip_mtime")) is not int
        or catalog.get("gzip_mtime") != 0
    ):
        raise ValueError("session catalog format or payload codec is invalid")
    rows = catalog.get("artifacts")
    if not isinstance(rows, list):
        raise TypeError("session catalog artifacts must be an array")

    expected_specs = SESSION_ARTIFACTS[session]
    if len(rows) != len(expected_specs):
        raise ValueError(
            "session catalog artifact inventory does not match the frozen session map"
        )
    declared_raw_bytes = 0
    for index, row in enumerate(rows, start=1):
        if not isinstance(row, Mapping) or set(row) != ARTIFACT_FIELDS:
            raise ValueError(f"session catalog artifact {index} has unexpected fields")
        raw_bytes = row.get("raw_bytes")
        if (
            type(raw_bytes) is not int
            or raw_bytes < 0
            or raw_bytes > MAX_RAW_ARTIFACT_BYTES
        ):
            raise ValueError(
                f"session catalog artifact {index} has an invalid raw byte count"
            )
        declared_raw_bytes += raw_bytes
    if declared_raw_bytes > MAX_CATALOG_RAW_BYTES:
        raise ValueError("session catalog declares too many raw bytes")

    seen_paths: set[str] = set()
    verified: list[VerifiedArtifact] = []
    decompressed_raw_bytes = 0
    for index, row in enumerate(rows, start=1):
        assert isinstance(row, Mapping)
        legacy_path = _validated_legacy_path(row.get("legacy_path"), index=index)
        if legacy_path in seen_paths:
            raise ValueError(f"session catalog contains duplicate path {legacy_path}")
        seen_paths.add(legacy_path)
        role = row.get("role")
        if not isinstance(role, str) or not role:
            raise ValueError(f"session catalog artifact {index} has an invalid role")
        raw_bytes = row.get("raw_bytes")
        assert type(raw_bytes) is int
        raw_sha256 = _validated_sha256(row.get("raw_sha256"), index=index)
        payload = row.get("payload")
        if not isinstance(payload, str) or not payload:
            raise ValueError(f"session catalog artifact {index} has an invalid payload")
        raw = _decode_payload(payload, index=index)
        decompressed_raw_bytes += len(raw)
        if decompressed_raw_bytes > MAX_CATALOG_RAW_BYTES:
            raise ValueError("session catalog contains too many raw bytes")
        if len(raw) != raw_bytes:
            raise ValueError(
                f"session catalog artifact {index} raw byte count does not match payload"
            )
        if hashlib.sha256(raw).hexdigest() != raw_sha256:
            raise ValueError(
                f"session catalog artifact {index} SHA-256 does not match payload"
            )
        verified.append(
            VerifiedArtifact(
                legacy_path=legacy_path,
                role=role,
                raw_sha256=raw_sha256,
                raw=raw,
            )
        )

    actual_specs = tuple(
        ArtifactSpec(artifact.legacy_path, artifact.role) for artifact in verified
    )
    if actual_specs != expected_specs:
        raise ValueError(
            "session catalog artifact inventory does not match the frozen session map"
        )
    return VerifiedSessionCatalog(
        session=session,
        source_commit=source_commit,
        artifacts=tuple(verified),
    )


def materialize_session_catalog(
    catalog: VerifiedSessionCatalog | Mapping[str, object] | Path,
    destination: Path,
    *,
    overwrite: bool = False,
) -> tuple[Path, ...]:
    if isinstance(catalog, Path):
        verified = load_session_catalog(catalog)
    elif isinstance(catalog, VerifiedSessionCatalog):
        verified = catalog
    else:
        verified = verify_session_catalog(catalog)

    root = destination.resolve()
    targets = [
        destination.joinpath(*PurePosixPath(artifact.legacy_path).parts)
        for artifact in verified.artifacts
    ]
    for target in targets:
        try:
            target.resolve().relative_to(root)
        except ValueError as error:
            raise ValueError(
                f"materialized path escapes destination: {target}"
            ) from error
        if not overwrite and (target.exists() or target.is_symlink()):
            raise FileExistsError(f"materialized path already exists: {target}")

    written: list[Path] = []
    for artifact, target in zip(verified.artifacts, targets, strict=True):
        target.parent.mkdir(parents=True, exist_ok=True)
        _write_bytes(target, artifact.raw, overwrite=overwrite)
        written.append(target)
    return tuple(written)


def _artifact_record(spec: ArtifactSpec, raw: bytes) -> dict[str, object]:
    if len(raw) > MAX_RAW_ARTIFACT_BYTES:
        raise ValueError(f"legacy artifact is too large: {spec.legacy_path}")
    return {
        "legacy_path": spec.legacy_path,
        "role": spec.role,
        "raw_bytes": len(raw),
        "raw_sha256": hashlib.sha256(raw).hexdigest(),
        "payload": _encode_payload(raw),
    }


def _encode_payload(raw: bytes) -> str:
    buffer = BytesIO()
    with gzip.GzipFile(
        filename="",
        mode="wb",
        compresslevel=9,
        fileobj=buffer,
        mtime=0,
    ) as compressed:
        compressed.write(raw)
    return base64.b64encode(buffer.getvalue()).decode("ascii")


def _decode_payload(payload: str, *, index: int) -> bytes:
    try:
        compressed = base64.b64decode(payload, validate=True)
    except (binascii.Error, ValueError) as error:
        raise ValueError(
            f"session catalog artifact {index} payload is not valid base64"
        ) from error
    if (
        len(compressed) < 10
        or compressed[:3] != b"\x1f\x8b\x08"
        or compressed[3] != 0
        or compressed[4:8] != b"\x00\x00\x00\x00"
    ):
        raise ValueError(
            f"session catalog artifact {index} has a noncanonical gzip header"
        )
    try:
        with gzip.GzipFile(fileobj=BytesIO(compressed), mode="rb") as stream:
            raw = stream.read(MAX_RAW_ARTIFACT_BYTES + 1)
    except (EOFError, gzip.BadGzipFile, OSError) as error:
        raise ValueError(
            f"session catalog artifact {index} payload is not valid gzip"
        ) from error
    if len(raw) > MAX_RAW_ARTIFACT_BYTES:
        raise ValueError(f"session catalog artifact {index} payload is too large")
    return raw


def _read_git_blob(
    repo_root: Path,
    source_commit: str,
    legacy_path: str,
) -> bytes:
    result = _run_git(
        repo_root,
        "cat-file",
        "blob",
        f"{source_commit}:{legacy_path}",
    )
    return result.stdout


def _git_output_paths(repo_root: Path) -> list[str]:
    result = _run_git(
        repo_root,
        "ls-tree",
        "-rz",
        "--name-only",
        MIGRATION_SOURCE_COMMIT,
        "--",
        "outputs",
    )
    try:
        paths = [
            path.decode("utf-8")
            for path in result.stdout.split(b"\0")
            if path
        ]
    except UnicodeDecodeError as error:
        raise ValueError("source commit contains a non-UTF-8 output path") from error
    return sorted(paths)


def _run_git(repo_root: Path, *arguments: str) -> subprocess.CompletedProcess[bytes]:
    result = subprocess.run(
        ["git", "-C", str(repo_root), *arguments],
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"git {' '.join(arguments)} failed: {detail}")
    return result


def _object_without_duplicates(pairs: Sequence[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"session catalog contains duplicate field {key}")
        result[key] = value
    return result


def _validated_legacy_path(value: object, *, index: int) -> str:
    if not isinstance(value, str) or not value or "\\" in value:
        raise ValueError(f"session catalog artifact {index} has an unsafe legacy path")
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or path.as_posix() != value
        or not path.parts
        or path.parts[0] != "outputs"
        or any(part in {"", ".", ".."} or ":" in part for part in path.parts)
    ):
        raise ValueError(f"session catalog artifact {index} has an unsafe legacy path")
    return value


def _validated_sha256(value: object, *, index: int) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or value != value.casefold()
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ValueError(f"session catalog artifact {index} has an invalid SHA-256")
    return value


def _session_specs(session: int) -> tuple[ArtifactSpec, ...]:
    if type(session) is not int or session not in SESSION_ARTIFACTS:
        raise ValueError(f"unknown session: {session}")
    return SESSION_ARTIFACTS[session]


def _write_bytes(path: Path, payload: bytes, *, overwrite: bool) -> None:
    if not overwrite:
        with path.open("xb") as handle:
            handle.write(payload)
        return
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
    except BaseException:
        temporary_path.unlink(missing_ok=True)
        raise


_ALL_MAPPED_PATHS = tuple(
    spec.legacy_path for specs in SESSION_ARTIFACTS.values() for spec in specs
)
if len(_ALL_MAPPED_PATHS) != 73 or len(set(_ALL_MAPPED_PATHS)) != 73:
    raise RuntimeError("session catalog map must contain 73 unique legacy paths")
