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
from copy import deepcopy
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
CATALOG_V2_FIELDS = {
    "attempts",
    "format_version",
    "gzip_mtime",
    "mode",
    "payload_encoding",
    "session",
}
ATTEMPT_FIELDS = {"artifacts", "attempt", "kind", "provenance"}
GIT_PROVENANCE_FIELDS = {"commit", "kind"}
DIRECT_PROVENANCE_FIELDS = {"kind"}
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
class VerifiedSessionAttempt:
    attempt: int
    kind: str
    provenance_kind: str
    source_commit: str | None
    artifacts: tuple[VerifiedArtifact, ...]


@dataclass(frozen=True, slots=True)
class VerifiedSessionCatalog:
    format_version: int
    session: int
    source_commit: str | None
    artifacts: tuple[VerifiedArtifact, ...]
    attempts: tuple[VerifiedSessionAttempt, ...]


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


def build_artifact_record(
    legacy_path: str,
    role: str,
    raw: bytes,
) -> dict[str, object]:
    return _artifact_record(ArtifactSpec(legacy_path, role), raw)


def append_direct_matrix_attempt(
    catalog: Mapping[str, object],
    *,
    attempt: int,
    artifacts: Sequence[Mapping[str, object]],
) -> dict[str, object]:
    if type(attempt) is not int or attempt < 2:
        raise ValueError("direct matrix attempt must be an integer of at least 2")
    if catalog.get("format_version") == 1:
        upgraded = _upgrade_session_five_catalog(catalog)
    else:
        verified = verify_session_catalog(catalog)
        if verified.format_version != 2 or verified.session != 5:
            raise ValueError("direct attempts can only append to session 5 format v2")
        upgraded = deepcopy(dict(catalog))
    raw_attempts = upgraded.get("attempts")
    assert isinstance(raw_attempts, list)
    existing = [row.get("attempt") for row in raw_attempts if isinstance(row, Mapping)]
    if attempt in existing:
        raise ValueError(f"session 5 already contains attempt {attempt:03d}")
    if attempt != len(raw_attempts) + 1:
        raise ValueError("session attempts must append contiguously")
    raw_attempts.append(
        {
            "attempt": attempt,
            "kind": "turn_order_matrix",
            "provenance": {"kind": "direct"},
            "artifacts": [deepcopy(dict(row)) for row in artifacts],
        }
    )
    verify_session_catalog(upgraded)
    return upgraded


def _upgrade_session_five_catalog(
    catalog: Mapping[str, object],
) -> dict[str, object]:
    verified = _verify_v1_session_catalog(catalog)
    if verified.session != 5:
        raise ValueError("only the session 5 migration catalog can upgrade to v2")
    rows = catalog.get("artifacts")
    assert isinstance(rows, list)
    return {
        "format_version": 2,
        "mode": "campaign_session_catalog",
        "payload_encoding": "gzip+base64",
        "gzip_mtime": 0,
        "session": 5,
        "attempts": [
            {
                "attempt": 1,
                "kind": "legacy_migration",
                "provenance": {
                    "kind": "git_commit",
                    "commit": verified.source_commit,
                },
                "artifacts": deepcopy(rows),
            }
        ],
    }


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


def replace_session_pair(
    catalog: Mapping[str, object],
    catalog_path: Path,
    receipt: bytes,
    receipt_path: Path,
) -> tuple[str, str]:
    if (
        catalog_path.parent.resolve() != receipt_path.parent.resolve()
        or catalog_path.suffix != ".json"
        or receipt_path.suffix != ".md"
        or catalog_path.stem != receipt_path.stem
    ):
        raise ValueError("session catalog and receipt must be a same-directory pair")
    if not catalog_path.is_file() or not receipt_path.is_file():
        raise FileNotFoundError("existing canonical session pair is required")
    try:
        receipt_text = receipt.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ValueError("session receipt must be UTF-8") from error
    if not receipt_text.startswith("# session 005"):
        raise ValueError("session receipt must identify session 005")

    catalog_bytes = render_session_catalog(catalog)
    original_catalog = catalog_path.read_bytes()
    original_receipt = receipt_path.read_bytes()
    staged_catalog = _stage_bytes(catalog_path, catalog_bytes)
    staged_receipt = _stage_bytes(receipt_path, receipt)
    replaced_catalog = False
    replaced_receipt = False
    try:
        os.replace(staged_catalog, catalog_path)
        replaced_catalog = True
        os.replace(staged_receipt, receipt_path)
        replaced_receipt = True
        if catalog_path.read_bytes() != catalog_bytes:
            raise OSError("session catalog replacement did not persist exact bytes")
        if receipt_path.read_bytes() != receipt:
            raise OSError("session receipt replacement did not persist exact bytes")
    except BaseException:
        if replaced_catalog:
            _write_bytes(catalog_path, original_catalog, overwrite=True)
        if replaced_receipt:
            _write_bytes(receipt_path, original_receipt, overwrite=True)
        raise
    finally:
        staged_catalog.unlink(missing_ok=True)
        staged_receipt.unlink(missing_ok=True)
    return (
        hashlib.sha256(catalog_bytes).hexdigest(),
        hashlib.sha256(receipt).hexdigest(),
    )


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

    if verified.format_version == 1:
        assert verified.source_commit is not None
        source_attempts = ((verified.source_commit, verified.artifacts),)
    else:
        source_attempts = tuple(
            (attempt.source_commit, attempt.artifacts)
            for attempt in verified.attempts
            if attempt.provenance_kind == "git_commit"
        )
    for source_commit, artifacts in source_attempts:
        assert source_commit is not None
        for artifact in artifacts:
            source = _read_git_blob(
                repo_root,
                source_commit,
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
    format_version = catalog.get("format_version")
    if format_version == 1:
        return _verify_v1_session_catalog(catalog)
    if format_version == 2:
        return _verify_v2_session_catalog(catalog)
    raise ValueError("session catalog format_version must be 1 or 2")


def _verify_v1_session_catalog(
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
        format_version=1,
        session=session,
        source_commit=source_commit,
        artifacts=tuple(verified),
        attempts=(),
    )


def _verify_v2_session_catalog(
    catalog: Mapping[str, object],
) -> VerifiedSessionCatalog:
    if set(catalog) != CATALOG_V2_FIELDS:
        raise ValueError("session catalog v2 has unexpected fields")
    if (
        catalog.get("format_version") != 2
        or catalog.get("mode") != "campaign_session_catalog"
        or catalog.get("payload_encoding") != "gzip+base64"
        or type(catalog.get("gzip_mtime")) is not int
        or catalog.get("gzip_mtime") != 0
        or catalog.get("session") != 5
    ):
        raise ValueError("session catalog v2 identity or payload codec is invalid")
    raw_attempts = catalog.get("attempts")
    if not isinstance(raw_attempts, list) or not raw_attempts:
        raise ValueError("session catalog v2 attempts must be a non-empty array")

    attempts: list[VerifiedSessionAttempt] = []
    flattened: list[VerifiedArtifact] = []
    seen_paths: set[str] = set()
    total_raw_bytes = 0
    for expected_attempt, row in enumerate(raw_attempts, start=1):
        if not isinstance(row, Mapping) or set(row) != ATTEMPT_FIELDS:
            raise ValueError(
                f"session catalog attempt {expected_attempt} has unexpected fields"
            )
        if row.get("attempt") != expected_attempt:
            raise ValueError("session catalog attempts must be contiguous and ordered")
        kind = row.get("kind")
        if expected_attempt == 1:
            if kind != "legacy_migration":
                raise ValueError("session 5 attempt 001 must be the legacy migration")
            source_commit = _verify_git_provenance(row.get("provenance"))
            if source_commit != SESSION_SOURCE_COMMITS[5]:
                raise ValueError("attempt 001 has an invalid source commit")
            expected_specs: tuple[ArtifactSpec, ...] | None = SESSION_ARTIFACTS[5]
            allowed_roots = {"outputs"}
        else:
            if kind != "turn_order_matrix":
                raise ValueError("direct session 5 attempts must be turn-order matrices")
            _verify_direct_provenance(row.get("provenance"))
            source_commit = None
            expected_specs = None
            allowed_roots = {"artifacts", "docs"}
        raw_artifacts = row.get("artifacts")
        artifacts = _verify_artifact_rows(
            raw_artifacts,
            expected_specs=expected_specs,
            allowed_roots=allowed_roots,
            index_prefix=f"attempt {expected_attempt}",
        )
        if expected_attempt > 1:
            _verify_direct_matrix_inventory(expected_attempt, artifacts)
        for artifact in artifacts:
            if artifact.legacy_path in seen_paths:
                raise ValueError(
                    "session catalog contains duplicate path "
                    f"{artifact.legacy_path}"
                )
            seen_paths.add(artifact.legacy_path)
            total_raw_bytes += len(artifact.raw)
            if total_raw_bytes > MAX_CATALOG_RAW_BYTES:
                raise ValueError("session catalog contains too many raw bytes")
        attempt = VerifiedSessionAttempt(
            attempt=expected_attempt,
            kind=str(kind),
            provenance_kind="git_commit" if source_commit is not None else "direct",
            source_commit=source_commit,
            artifacts=artifacts,
        )
        attempts.append(attempt)
        flattened.extend(artifacts)
    return VerifiedSessionCatalog(
        format_version=2,
        session=5,
        source_commit=None,
        artifacts=tuple(flattened),
        attempts=tuple(attempts),
    )


def _verify_git_provenance(value: object) -> str:
    if not isinstance(value, Mapping) or set(value) != GIT_PROVENANCE_FIELDS:
        raise ValueError("git attempt provenance is invalid")
    commit = value.get("commit")
    if value.get("kind") != "git_commit" or not isinstance(commit, str):
        raise ValueError("git attempt provenance is invalid")
    if (
        len(commit) != 40
        or commit != commit.casefold()
        or any(character not in "0123456789abcdef" for character in commit)
    ):
        raise ValueError("git attempt provenance commit is invalid")
    return commit


def _verify_direct_provenance(value: object) -> None:
    if (
        not isinstance(value, Mapping)
        or set(value) != DIRECT_PROVENANCE_FIELDS
        or value.get("kind") != "direct"
    ):
        raise ValueError("direct attempt provenance is invalid")


def _verify_artifact_rows(
    value: object,
    *,
    expected_specs: tuple[ArtifactSpec, ...] | None,
    allowed_roots: set[str],
    index_prefix: str,
) -> tuple[VerifiedArtifact, ...]:
    if not isinstance(value, list):
        raise ValueError(f"session catalog {index_prefix} artifacts must be an array")
    if expected_specs is not None and len(value) != len(expected_specs):
        raise ValueError(
            f"session catalog {index_prefix} inventory does not match its frozen map"
        )
    declared_raw_bytes = 0
    for index, row in enumerate(value, start=1):
        if not isinstance(row, Mapping) or set(row) != ARTIFACT_FIELDS:
            raise ValueError(
                f"session catalog {index_prefix} artifact {index} has unexpected fields"
            )
        raw_bytes = row.get("raw_bytes")
        if (
            type(raw_bytes) is not int
            or raw_bytes < 0
            or raw_bytes > MAX_RAW_ARTIFACT_BYTES
        ):
            raise ValueError(
                f"session catalog {index_prefix} artifact {index} "
                "has an invalid raw byte count"
            )
        declared_raw_bytes += raw_bytes
    if declared_raw_bytes > MAX_CATALOG_RAW_BYTES:
        raise ValueError(f"session catalog {index_prefix} declares too many raw bytes")

    seen: set[str] = set()
    verified: list[VerifiedArtifact] = []
    for index, row in enumerate(value, start=1):
        assert isinstance(row, Mapping)
        legacy_path = _validated_legacy_path(
            row.get("legacy_path"),
            index=index,
            allowed_roots=allowed_roots,
        )
        if legacy_path in seen:
            raise ValueError(
                f"session catalog {index_prefix} contains duplicate path {legacy_path}"
            )
        seen.add(legacy_path)
        role = row.get("role")
        if not isinstance(role, str) or not role:
            raise ValueError(
                f"session catalog {index_prefix} artifact {index} has an invalid role"
            )
        raw_bytes = row.get("raw_bytes")
        assert type(raw_bytes) is int
        raw_sha256 = _validated_sha256(row.get("raw_sha256"), index=index)
        payload = row.get("payload")
        if not isinstance(payload, str) or not payload:
            raise ValueError(
                f"session catalog {index_prefix} artifact {index} "
                "has an invalid payload"
            )
        raw = _decode_payload(payload, index=index)
        if len(raw) != raw_bytes:
            raise ValueError(
                f"session catalog {index_prefix} artifact {index} raw byte count "
                "does not match payload"
            )
        if hashlib.sha256(raw).hexdigest() != raw_sha256:
            raise ValueError(
                f"session catalog {index_prefix} artifact {index} SHA-256 "
                "does not match payload"
            )
        verified.append(
            VerifiedArtifact(
                legacy_path=legacy_path,
                role=role,
                raw_sha256=raw_sha256,
                raw=raw,
            )
        )
    if expected_specs is not None:
        actual_specs = tuple(
            ArtifactSpec(artifact.legacy_path, artifact.role)
            for artifact in verified
        )
        if actual_specs != expected_specs:
            raise ValueError(
                f"session catalog {index_prefix} inventory does not match its frozen map"
            )
    return tuple(verified)


def _verify_direct_matrix_inventory(
    attempt: int,
    artifacts: tuple[VerifiedArtifact, ...],
) -> None:
    roles = [artifact.role for artifact in artifacts]
    if (
        roles.count("matrix_manifest") != 1
        or roles.count("matrix_cell") != 12
        or roles.count("matrix_result") != 1
        or roles.count("proof") != 1
        or any(
            role
            not in {
                "matrix_manifest",
                "matrix_cell",
                "matrix_result",
                "proof",
                "postmortem",
            }
            for role in roles
        )
    ):
        raise ValueError(
            f"session 5 attempt {attempt:03d} has an invalid matrix inventory"
        )
    manifest = next(
        artifact for artifact in artifacts if artifact.role == "matrix_manifest"
    )
    expected_manifest = f"docs/SESSION_005_ATTEMPT_{attempt:03d}.json"
    if manifest.legacy_path != expected_manifest:
        raise ValueError(
            f"session 5 attempt {attempt:03d} has the wrong frozen manifest path"
        )
    manifest_payload = _strict_json_object(
        manifest.raw,
        name=f"session 5 attempt {attempt:03d} manifest",
    )
    if manifest_payload.get("attempt") != attempt:
        raise ValueError("direct matrix manifest attempt does not match catalog")
    raw_cells = manifest_payload.get("cells")
    if not isinstance(raw_cells, list) or len(raw_cells) != 12:
        raise ValueError("direct matrix manifest must contain 12 cells")
    manifest_outputs: list[str] = []
    for cell in raw_cells:
        if not isinstance(cell, Mapping) or not isinstance(cell.get("output"), str):
            raise ValueError("direct matrix manifest cell output is invalid")
        manifest_outputs.append(str(cell["output"]))
    cell_paths = [
        artifact.legacy_path
        for artifact in artifacts
        if artifact.role == "matrix_cell"
    ]
    if cell_paths != manifest_outputs:
        raise ValueError("direct matrix cells do not match manifest execution order")

    result = next(
        artifact for artifact in artifacts if artifact.role == "matrix_result"
    )
    result_payload = _strict_json_object(
        result.raw,
        name=f"session 5 attempt {attempt:03d} result",
    )
    batch = result_payload.get("batch")
    result_manifest = result_payload.get("manifest")
    if (
        not isinstance(batch, Mapping)
        or batch.get("terminal") is not True
        or batch.get("planned_n") != 12
        or batch.get("pending_n") != 0
        or not isinstance(result_manifest, Mapping)
        or result_manifest.get("path") != manifest.legacy_path
        or result_manifest.get("artifact_sha256") != manifest.raw_sha256
    ):
        raise ValueError("direct matrix result is incomplete or not bound to manifest")


def _strict_json_object(raw: bytes, *, name: str) -> Mapping[str, object]:
    parsed = parse_session_catalog(raw)
    if not isinstance(parsed, Mapping):
        raise TypeError(f"{name} must be an object")
    return parsed


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


def _validated_legacy_path(
    value: object,
    *,
    index: int,
    allowed_roots: set[str] | None = None,
) -> str:
    roots = {"outputs"} if allowed_roots is None else allowed_roots
    if not isinstance(value, str) or not value or "\\" in value:
        raise ValueError(f"session catalog artifact {index} has an unsafe legacy path")
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or path.as_posix() != value
        or not path.parts
        or path.parts[0] not in roots
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


def _stage_bytes(path: Path, payload: bytes) -> Path:
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".pending",
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        temporary_path.unlink(missing_ok=True)
        raise
    return temporary_path


_ALL_MAPPED_PATHS = tuple(
    spec.legacy_path for specs in SESSION_ARTIFACTS.values() for spec in specs
)
if len(_ALL_MAPPED_PATHS) != 73 or len(set(_ALL_MAPPED_PATHS)) != 73:
    raise RuntimeError("session catalog map must contain 73 unique legacy paths")
