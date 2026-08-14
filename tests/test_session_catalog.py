from __future__ import annotations

import base64
import hashlib
import json
import os
import subprocess
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path, PurePosixPath
from unittest.mock import patch

from world_sim.session_catalog import (
    MAX_RAW_ARTIFACT_BYTES,
    SESSION_ARTIFACTS,
    SESSION_SOURCE_COMMITS,
    append_direct_matrix_attempt,
    audit_source_mapping,
    build_artifact_record,
    build_session_catalog,
    load_session_catalog,
    materialize_session_catalog,
    parse_session_catalog,
    render_session_catalog,
    replace_session_pair,
    verify_catalog_source_blobs,
    verify_session_catalog,
    write_session_catalog,
)

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
EXPECTED_CATALOG_SHA256 = {
    1: "7948d04b07148f14e8c8c46380c2982334f7fab6eee805808369b88a3c5dfa50",
    2: "bc6af110c0970da37825db830ad05e90a03bd20bc6ead76f0c78d7eef85cedb9",
    3: "f94b8c44029e360814ce8f9c20593588d17657bea1d741564d3dfd053943776e",
    4: "130af2a86af7f346d75b3ec04262a062ba5117200315a8329d07f25bbfab258c",
}


class SessionCatalogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.catalogs = tuple(
            parse_session_catalog(
                (
                    REPOSITORY_ROOT
                    / "outputs"
                    / f"session-{session:03d}.json"
                ).read_bytes()
            )
            for session in SESSION_ARTIFACTS
        )

    def require_source_commit(self) -> None:
        source_commit = SESSION_SOURCE_COMMITS[1]
        result = subprocess.run(
            [
                "git",
                "-C",
                str(REPOSITORY_ROOT),
                "cat-file",
                "-e",
                f"{source_commit}^{{commit}}",
            ],
            capture_output=True,
            check=False,
        )
        if result.returncode != 0:
            self.skipTest("pinned catalog source commit is unavailable")

    def test_frozen_map_covers_all_73_source_outputs_once(self) -> None:
        counts = [len(SESSION_ARTIFACTS[session]) for session in SESSION_ARTIFACTS]
        paths = [
            spec.legacy_path
            for artifacts in SESSION_ARTIFACTS.values()
            for spec in artifacts
        ]

        self.assertEqual(counts, [27, 6, 5, 15, 20])
        self.assertEqual(len(paths), 73)
        self.assertEqual(len(set(paths)), 73)

    def test_committed_outputs_are_exact_json_markdown_pairs(self) -> None:
        files = tuple(
            path
            for path in (REPOSITORY_ROOT / "outputs").iterdir()
            if path.is_file()
        )
        for path in files:
            self.assertRegex(path.name, r"^session-\d{3}\.(?:json|md)$")

        json_sessions = {path.stem for path in files if path.suffix == ".json"}
        markdown_sessions = {path.stem for path in files if path.suffix == ".md"}
        self.assertEqual(json_sessions, markdown_sessions)
        self.assertEqual(len(files), 2 * len(json_sessions))

    def test_committed_catalog_hashes_are_frozen_without_git_history(self) -> None:
        for session, expected in EXPECTED_CATALOG_SHA256.items():
            path = REPOSITORY_ROOT / "outputs" / f"session-{session:03d}.json"
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), expected)

    def test_catalogs_build_deterministically_from_exact_git_blobs(self) -> None:
        self.require_source_commit()
        audit_source_mapping(REPOSITORY_ROOT)
        first = build_session_catalog(REPOSITORY_ROOT, 3)
        second = build_session_catalog(REPOSITORY_ROOT, 3)

        self.assertEqual(render_session_catalog(first), render_session_catalog(second))
        verified = verify_session_catalog(first)
        artifact = verified.artifacts[0]
        source_commit = SESSION_SOURCE_COMMITS[3]
        git_blob = subprocess.run(
            [
                "git",
                "-C",
                str(REPOSITORY_ROOT),
                "cat-file",
                "blob",
                f"{source_commit}:{artifact.legacy_path}",
            ],
            capture_output=True,
            check=True,
        ).stdout
        self.assertEqual(artifact.raw, git_blob)
        self.assertEqual(artifact.raw_sha256, hashlib.sha256(git_blob).hexdigest())

    def test_committed_catalogs_match_deterministic_rebuilds(self) -> None:
        self.require_source_commit()
        audit_source_mapping(REPOSITORY_ROOT)
        for catalog in self.catalogs:
            session = catalog["session"]
            path = REPOSITORY_ROOT / "outputs" / f"session-{session:03d}.json"
            self.assertEqual(path.read_bytes(), render_session_catalog(catalog))
            verify_catalog_source_blobs(REPOSITORY_ROOT, catalog)

    def test_all_catalogs_verify_with_strict_identity_and_counts(self) -> None:
        verified = [verify_session_catalog(catalog) for catalog in self.catalogs]

        self.assertEqual([item.session for item in verified], [1, 2, 3, 4, 5])
        self.assertEqual(
            [len(item.artifacts) for item in verified[:4]],
            [27, 6, 5, 15],
        )
        for catalog in self.catalogs[:4]:
            self.assertEqual(catalog["format_version"], 1)
            self.assertEqual(catalog["mode"], "campaign_session_catalog")
            self.assertEqual(catalog["payload_encoding"], "gzip+base64")
            self.assertEqual(catalog["gzip_mtime"], 0)
        self.assertIn(verified[4].format_version, {1, 2})
        if verified[4].format_version == 1:
            self.assertEqual(len(verified[4].artifacts), 20)
        else:
            self.assertGreaterEqual(len(verified[4].attempts), 2)
            self.assertEqual(len(verified[4].attempts[0].artifacts), 20)

    def test_source_verification_rejects_an_internally_consistent_wrong_blob(
        self,
    ) -> None:
        self.require_source_commit()
        catalog = deepcopy(self.catalogs[0])
        replacement = catalog["artifacts"][1]
        target = catalog["artifacts"][0]
        target["payload"] = replacement["payload"]
        target["raw_bytes"] = replacement["raw_bytes"]
        target["raw_sha256"] = replacement["raw_sha256"]

        verify_session_catalog(catalog)
        with self.assertRaisesRegex(ValueError, "pinned source blob"):
            verify_catalog_source_blobs(REPOSITORY_ROOT, catalog)

    def test_materialization_restores_exact_paths_and_bytes(self) -> None:
        catalog = self.catalogs[2]
        verified = verify_session_catalog(catalog)
        with tempfile.TemporaryDirectory() as temporary_directory:
            destination = Path(temporary_directory)
            written = materialize_session_catalog(catalog, destination)

            self.assertEqual(len(written), 5)
            for artifact, path in zip(verified.artifacts, written, strict=True):
                self.assertEqual(
                    path,
                    destination.joinpath(
                        *PurePosixPath(artifact.legacy_path).parts
                    ),
                )
                self.assertEqual(path.read_bytes(), artifact.raw)

            with self.assertRaisesRegex(FileExistsError, "already exists"):
                materialize_session_catalog(catalog, destination)

    def test_cli_materializes_a_verified_dependency_chain(self) -> None:
        self.require_source_commit()
        with tempfile.TemporaryDirectory() as temporary_directory:
            destination = Path(temporary_directory) / "restored"
            result = subprocess.run(
                [
                    "py",
                    "-3.11",
                    str(REPOSITORY_ROOT / "tools" / "session_catalog.py"),
                    "materialize",
                    str(REPOSITORY_ROOT / "outputs" / "session-001.json"),
                    str(REPOSITORY_ROOT / "outputs" / "session-002.json"),
                    "--destination",
                    str(destination),
                ],
                capture_output=True,
                check=True,
                text=True,
            )
            report = json.loads(result.stdout)

            self.assertEqual(report["artifacts"], 33)
            self.assertEqual(
                report["catalogs"],
                [
                    {"artifacts": 27, "session": 1},
                    {"artifacts": 6, "session": 2},
                ],
            )
            self.assertEqual(
                len(tuple((destination / "outputs").iterdir())),
                33,
            )

    def test_file_round_trip_is_lossless_and_refuses_implicit_overwrite(self) -> None:
        catalog = self.catalogs[1]
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "session-002.json"
            expected_sha256 = write_session_catalog(catalog, path)

            self.assertEqual(
                expected_sha256,
                hashlib.sha256(path.read_bytes()).hexdigest(),
            )
            self.assertEqual(load_session_catalog(path).session, 2)
            with self.assertRaises(FileExistsError):
                write_session_catalog(catalog, path)

    def test_duplicate_json_fields_are_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "duplicate field session"):
            parse_session_catalog(
                b'{"session":1,"session":1,"source_commit":"x"}'
            )

    def test_schema_source_and_codec_tampering_are_rejected(self) -> None:
        catalog = deepcopy(self.catalogs[0])
        catalog["unexpected"] = True
        with self.assertRaisesRegex(ValueError, "unexpected fields"):
            verify_session_catalog(catalog)

        catalog = deepcopy(self.catalogs[0])
        catalog["source_commit"] = "0" * 40
        with self.assertRaisesRegex(ValueError, "source commit"):
            verify_session_catalog(catalog)

        catalog = deepcopy(self.catalogs[0])
        catalog["gzip_mtime"] = 1
        with self.assertRaisesRegex(ValueError, "payload codec"):
            verify_session_catalog(catalog)

    def test_traversal_and_duplicate_paths_are_rejected(self) -> None:
        catalog = deepcopy(self.catalogs[0])
        catalog["artifacts"][0]["legacy_path"] = "outputs/../escape.json"
        with self.assertRaisesRegex(ValueError, "unsafe legacy path"):
            verify_session_catalog(catalog)

        catalog = deepcopy(self.catalogs[0])
        catalog["artifacts"][1]["legacy_path"] = catalog["artifacts"][0][
            "legacy_path"
        ]
        with self.assertRaisesRegex(ValueError, "duplicate path"):
            verify_session_catalog(catalog)

    def test_payload_hash_and_size_tampering_are_rejected(self) -> None:
        catalog = deepcopy(self.catalogs[0])
        catalog["artifacts"][0]["raw_sha256"] = "0" * 64
        with self.assertRaisesRegex(ValueError, "SHA-256 does not match"):
            verify_session_catalog(catalog)

        catalog = deepcopy(self.catalogs[0])
        catalog["artifacts"][0]["payload"] = "not-base64"
        with self.assertRaisesRegex(ValueError, "not valid base64"):
            verify_session_catalog(catalog)

        catalog = deepcopy(self.catalogs[0])
        catalog["artifacts"][0]["raw_bytes"] += 1
        with self.assertRaisesRegex(ValueError, "byte count does not match"):
            verify_session_catalog(catalog)

        catalog = deepcopy(self.catalogs[0])
        compressed = bytearray(
            base64.b64decode(catalog["artifacts"][0]["payload"])
        )
        compressed[4] = 1
        catalog["artifacts"][0]["payload"] = base64.b64encode(compressed).decode(
            "ascii"
        )
        with self.assertRaisesRegex(ValueError, "noncanonical gzip header"):
            verify_session_catalog(catalog)

    def test_catalog_total_raw_byte_cap_is_checked_before_decompression(self) -> None:
        catalog = deepcopy(self.catalogs[0])
        for row in catalog["artifacts"][:5]:
            row["raw_bytes"] = MAX_RAW_ARTIFACT_BYTES

        with self.assertRaisesRegex(ValueError, "declares too many raw bytes"):
            verify_session_catalog(catalog)

    def test_session_five_v2_wraps_attempt_one_byte_identically(self) -> None:
        self.require_source_commit()
        original = _attempt_one_v1_catalog(self.catalogs[4])
        records = _direct_attempt_records()

        updated = append_direct_matrix_attempt(
            original,
            attempt=2,
            artifacts=records,
        )
        verified = verify_session_catalog(updated)

        self.assertEqual(updated["format_version"], 2)
        self.assertNotIn("source_commit", updated)
        self.assertEqual(updated["attempts"][0]["artifacts"], original["artifacts"])
        self.assertEqual(
            updated["attempts"][0]["provenance"],
            {
                "kind": "git_commit",
                "commit": SESSION_SOURCE_COMMITS[5],
            },
        )
        self.assertEqual(updated["attempts"][1]["provenance"], {"kind": "direct"})
        self.assertEqual(verified.format_version, 2)
        self.assertEqual(len(verified.attempts), 2)
        self.assertEqual(len(verified.attempts[0].artifacts), 20)
        self.assertEqual(len(verified.attempts[1].artifacts), 15)
        self.assertEqual(
            verify_catalog_source_blobs(REPOSITORY_ROOT, updated),
            verified,
        )

    def test_direct_attempt_rejects_duplicate_incomplete_and_tampered_data(self) -> None:
        updated = append_direct_matrix_attempt(
            _attempt_one_v1_catalog(self.catalogs[4]),
            attempt=2,
            artifacts=_direct_attempt_records(),
        )
        with self.assertRaisesRegex(ValueError, "already contains attempt"):
            append_direct_matrix_attempt(
                updated,
                attempt=2,
                artifacts=_direct_attempt_records(),
            )

        incomplete = _direct_attempt_records(pending=1)
        with self.assertRaisesRegex(ValueError, "incomplete"):
            append_direct_matrix_attempt(
                _attempt_one_v1_catalog(self.catalogs[4]),
                attempt=2,
                artifacts=incomplete,
            )

        tampered = deepcopy(updated)
        tampered["attempts"][1]["artifacts"][1]["raw_sha256"] = "0" * 64
        with self.assertRaisesRegex(ValueError, "SHA-256 does not match"):
            verify_session_catalog(tampered)

    def test_v2_materializes_both_attempts_and_pair_replacement_is_exact(self) -> None:
        updated = append_direct_matrix_attempt(
            _attempt_one_v1_catalog(self.catalogs[4]),
            attempt=2,
            artifacts=_direct_attempt_records(),
        )
        verified = verify_session_catalog(updated)
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            destination = root / "restored"
            written = materialize_session_catalog(updated, destination)
            self.assertEqual(len(written), 35)
            self.assertEqual(
                len(tuple(destination.rglob("*.*"))),
                35,
            )

            catalog_path = root / "session-005.json"
            receipt_path = root / "session-005.md"
            catalog_path.write_bytes(
                (REPOSITORY_ROOT / "outputs" / "session-005.json").read_bytes()
            )
            receipt_path.write_bytes(b"# session 005\n")
            receipt = b"# session 005\n\nsealed attempt 002\n"
            catalog_sha256, receipt_sha256 = replace_session_pair(
                updated,
                catalog_path,
                receipt,
                receipt_path,
            )
            self.assertEqual(
                hashlib.sha256(catalog_path.read_bytes()).hexdigest(),
                catalog_sha256,
            )
            self.assertEqual(
                hashlib.sha256(receipt_path.read_bytes()).hexdigest(),
                receipt_sha256,
            )
            self.assertEqual(load_session_catalog(catalog_path), verified)

    def test_pair_replacement_rolls_back_if_the_second_replace_fails(self) -> None:
        updated = append_direct_matrix_attempt(
            _attempt_one_v1_catalog(self.catalogs[4]),
            attempt=2,
            artifacts=_direct_attempt_records(),
        )
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            catalog_path = root / "session-005.json"
            receipt_path = root / "session-005.md"
            original_catalog = b"original catalog\n"
            original_receipt = b"# session 005\noriginal receipt\n"
            catalog_path.write_bytes(original_catalog)
            receipt_path.write_bytes(original_receipt)
            real_replace = os.replace
            calls = 0

            def fail_second_replace(source: Path, destination: Path) -> None:
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise OSError("simulated receipt replacement failure")
                real_replace(source, destination)

            with (
                patch(
                    "world_sim.session_catalog.os.replace",
                    side_effect=fail_second_replace,
                ),
                self.assertRaisesRegex(OSError, "simulated receipt"),
            ):
                replace_session_pair(
                    updated,
                    catalog_path,
                    b"# session 005\nnew receipt\n",
                    receipt_path,
                )

            self.assertEqual(catalog_path.read_bytes(), original_catalog)
            self.assertEqual(receipt_path.read_bytes(), original_receipt)


def _attempt_one_v1_catalog(
    catalog: dict[str, object],
) -> dict[str, object]:
    if catalog.get("format_version") == 1:
        return deepcopy(catalog)
    attempts = catalog.get("attempts")
    if not isinstance(attempts, list) or not attempts:
        raise AssertionError("session 5 has no attempt 001")
    attempt = attempts[0]
    if not isinstance(attempt, dict):
        raise AssertionError("session 5 attempt 001 is invalid")
    provenance = attempt.get("provenance")
    if not isinstance(provenance, dict):
        raise AssertionError("session 5 attempt 001 provenance is invalid")
    return {
        "artifacts": deepcopy(attempt["artifacts"]),
        "format_version": 1,
        "gzip_mtime": 0,
        "mode": "campaign_session_catalog",
        "payload_encoding": "gzip+base64",
        "session": 5,
        "source_commit": provenance["commit"],
    }


def _direct_attempt_records(*, pending: int = 0) -> list[dict[str, object]]:
    cells = [
        {
            "execution_position": position,
            "output": (
                "artifacts/session-005-attempt-002/worlds/"
                f"attempt-002-b{((position - 1) // 4) + 1:02d}-p{(position - 1) % 4}-x.json"
            ),
        }
        for position in range(1, 13)
    ]
    manifest_raw = (
        json.dumps(
            {
                "attempt": 2,
                "cells": cells,
                "format_version": 2,
                "mode": "turn_order_replicate_matrix",
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")
    manifest_path = "docs/SESSION_005_ATTEMPT_002.json"
    result_raw = (
        json.dumps(
            {
                "batch": {
                    "pending_n": pending,
                    "planned_n": 12,
                    "terminal": pending == 0,
                },
                "manifest": {
                    "artifact_sha256": hashlib.sha256(manifest_raw).hexdigest(),
                    "path": manifest_path,
                },
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")
    return [
        build_artifact_record(manifest_path, "matrix_manifest", manifest_raw),
        *[
            build_artifact_record(cell["output"], "matrix_cell", b"{}\n")
            for cell in cells
        ],
        build_artifact_record(
            "artifacts/session-005-attempt-002/matrix-results.json",
            "matrix_result",
            result_raw,
        ),
        build_artifact_record(
            "artifacts/session-005-attempt-002/matrix-proof.md",
            "proof",
            b"# proof\n",
        ),
    ]


if __name__ == "__main__":
    unittest.main()
