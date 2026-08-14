from __future__ import annotations

import hashlib
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from world_sim.session_catalog import load_session_catalog, parse_session_catalog


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
TOOL_PATH = REPOSITORY_ROOT / "tools" / "session_catalog.py"
SPEC = importlib.util.spec_from_file_location("session_catalog_tool", TOOL_PATH)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError("cannot load session catalog tool")
TOOL = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(TOOL)


class PackageSessionAttemptTests(unittest.TestCase):
    def test_happy_path_writes_only_the_canonical_pair(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = _fixture(Path(directory))
            original = parse_session_catalog(fixture.catalog.read_bytes())

            with patch.object(TOOL, "_load_module", return_value=_FakeScorer()):
                report = TOOL.package_matrix_attempt(
                    repo_root=fixture.root,
                    catalog_path=fixture.catalog,
                    receipt_path=fixture.receipt,
                    manifest_path=fixture.manifest,
                    result_path=fixture.result,
                    proof_path=fixture.proof,
                    require_committed_manifest=False,
                )

            verified = load_session_catalog(fixture.catalog)
            self.assertEqual(report["status"], "appended")
            self.assertEqual(report["attempt"], 2)
            self.assertEqual(report["artifacts"], 15)
            self.assertEqual(verified.format_version, 2)
            self.assertEqual(len(verified.attempts), 2)
            updated = parse_session_catalog(fixture.catalog.read_bytes())
            self.assertEqual(
                updated["attempts"][0]["artifacts"],
                original["artifacts"],
            )
            self.assertEqual(
                {path.name for path in (fixture.root / "outputs").iterdir()},
                {"session-005.json", "session-005.md"},
            )
            self.assertIn(
                "## attempt 002 - replacement matrix",
                fixture.receipt.read_text(encoding="utf-8"),
            )

    def test_tampered_score_refuses_without_changing_pair(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = _fixture(Path(directory))
            fixture.result.write_bytes(fixture.result.read_bytes() + b" ")
            before = (fixture.catalog.read_bytes(), fixture.receipt.read_bytes())

            with (
                patch.object(TOOL, "_load_module", return_value=_FakeScorer()),
                self.assertRaisesRegex(ValueError, "deterministic rescoring"),
            ):
                _package(fixture)

            self.assertEqual(
                (fixture.catalog.read_bytes(), fixture.receipt.read_bytes()),
                before,
            )

    def test_incomplete_matrix_refuses_without_changing_pair(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = _fixture(Path(directory))
            before = (fixture.catalog.read_bytes(), fixture.receipt.read_bytes())

            with (
                patch.object(
                    TOOL,
                    "_load_module",
                    return_value=_FakeScorer(terminal=False),
                ),
                self.assertRaisesRegex(ValueError, "incomplete"),
            ):
                _package(fixture)

            self.assertEqual(
                (fixture.catalog.read_bytes(), fixture.receipt.read_bytes()),
                before,
            )

    def test_duplicate_attempt_refuses_without_changing_pair(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = _fixture(Path(directory))
            with patch.object(TOOL, "_load_module", return_value=_FakeScorer()):
                _package(fixture)
            before = (fixture.catalog.read_bytes(), fixture.receipt.read_bytes())

            with (
                patch.object(TOOL, "_load_module", return_value=_FakeScorer()),
                self.assertRaisesRegex(ValueError, "already contains attempt"),
            ):
                _package(fixture)

            self.assertEqual(
                (fixture.catalog.read_bytes(), fixture.receipt.read_bytes()),
                before,
            )


class _Fixture:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.catalog = root / "outputs" / "session-005.json"
        self.receipt = root / "outputs" / "session-005.md"
        self.manifest = root / "docs" / "SESSION_005_ATTEMPT_002.json"
        self.result = root / "artifacts" / "session-005-attempt-002" / "result.json"
        self.proof = root / "artifacts" / "session-005-attempt-002" / "proof.md"


class _FakeScorer:
    def __init__(self, *, terminal: bool = True) -> None:
        self.terminal = terminal

    def _load_json_object(
        self,
        path: Path,
        *,
        name: str,
    ) -> tuple[dict[str, object], str]:
        del name
        raw = path.read_bytes()
        return json.loads(raw), hashlib.sha256(raw).hexdigest()

    def score_turn_order_matrix(
        self,
        manifest_path: Path,
        *,
        repository_root: Path,
    ) -> dict[str, object]:
        del repository_root
        return _score(manifest_path, terminal=self.terminal)


def _fixture(root: Path) -> _Fixture:
    fixture = _Fixture(root)
    fixture.catalog.parent.mkdir(parents=True)
    fixture.manifest.parent.mkdir(parents=True)
    fixture.result.parent.mkdir(parents=True)
    fixture.catalog.write_bytes(_attempt_one_catalog_bytes())
    fixture.receipt.write_text(
        "# session 005 - turn-order matrix, attempt 001\n\n"
        "**status:** sealed and deviated; not cleanly complete\n",
        encoding="utf-8",
    )
    cells = []
    for position in range(1, 13):
        phase = (position - 1) % 4
        block = ((position - 1) // 4) + 1
        output = (
            "artifacts/session-005-attempt-002/worlds/"
            f"attempt-002-b{block:02d}-p{phase}-x.json"
        )
        path = root.joinpath(*Path(output).parts)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"{}\n")
        cells.append(
            {
                "block": block,
                "execution_position": position,
                "initiative_phase": phase,
                "output": output,
            }
        )
    fixture.manifest.write_text(
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
        + "\n",
        encoding="utf-8",
    )
    fixture.result.write_bytes(_render_score(fixture.manifest))
    fixture.proof.write_text("# attempt 002 proof\n", encoding="utf-8")
    return fixture


def _score(manifest_path: Path, *, terminal: bool = True) -> dict[str, object]:
    raw = manifest_path.read_bytes()
    return {
        "batch": {
            "censored_n": 0,
            "cost_exposure_usd": "0.1",
            "pending_n": 0 if terminal else 1,
            "planned_n": 12,
            "primary_success_count": 6,
            "scoreable_n": 12 if terminal else 11,
            "terminal": terminal,
        },
        "manifest": {
            "artifact_sha256": hashlib.sha256(raw).hexdigest(),
            "path": "docs/SESSION_005_ATTEMPT_002.json",
        },
        "phases": [
            {"initiative_phase": phase, "primary_success_rate": 0.5}
            for phase in range(4)
        ],
        "protocol": {"status": "adhered" if terminal else "incomplete"},
    }


def _render_score(manifest_path: Path) -> bytes:
    return (
        json.dumps(
            _score(manifest_path),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")


def _attempt_one_catalog_bytes() -> bytes:
    source = REPOSITORY_ROOT / "outputs" / "session-005.json"
    raw = source.read_bytes()
    catalog = parse_session_catalog(raw)
    if catalog.get("format_version") == 1:
        return raw
    attempts = catalog.get("attempts")
    if not isinstance(attempts, list) or not attempts:
        raise AssertionError("committed session 5 has no attempt 001")
    attempt = attempts[0]
    if not isinstance(attempt, dict):
        raise AssertionError("committed attempt 001 is invalid")
    provenance = attempt.get("provenance")
    if not isinstance(provenance, dict):
        raise AssertionError("committed attempt 001 provenance is invalid")
    v1 = {
        "artifacts": attempt["artifacts"],
        "format_version": 1,
        "gzip_mtime": 0,
        "mode": "campaign_session_catalog",
        "payload_encoding": "gzip+base64",
        "session": 5,
        "source_commit": provenance["commit"],
    }
    return (
        json.dumps(v1, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")


def _package(fixture: _Fixture) -> dict[str, object]:
    return TOOL.package_matrix_attempt(
        repo_root=fixture.root,
        catalog_path=fixture.catalog,
        receipt_path=fixture.receipt,
        manifest_path=fixture.manifest,
        result_path=fixture.result,
        proof_path=fixture.proof,
        require_committed_manifest=False,
    )


if __name__ == "__main__":
    unittest.main()
