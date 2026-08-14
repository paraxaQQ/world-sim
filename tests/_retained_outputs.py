from __future__ import annotations

import tempfile
from functools import lru_cache
from pathlib import Path

from world_sim.session_catalog import (
    SESSION_ARTIFACTS,
    catalog_filename,
    materialize_session_catalog,
)


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


@lru_cache(maxsize=1)
def _retained_outputs() -> tuple[tempfile.TemporaryDirectory[str], Path]:
    catalogs = tuple(
        REPOSITORY_ROOT / "outputs" / catalog_filename(session)
        for session in SESSION_ARTIFACTS
    )

    directory = tempfile.TemporaryDirectory(prefix="world-sim-retained-")
    root = Path(directory.name)
    try:
        for catalog in catalogs:
            materialize_session_catalog(catalog, root)
    except BaseException:
        directory.cleanup()
        raise
    return directory, root


def retained_outputs_root() -> Path:
    return _retained_outputs()[1]
