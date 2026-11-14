"""Test configuration for the source-layout package."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

def _find_artifact(root: Path, name: str) -> Path:
    """Return the produced artifact wherever the task folder put it.

    Tasks write into their own folder under the cache root instead of the
    workspace root, so assertions about "the file really exists" must not pin a
    layout. Failing here still reports the searched root.
    """

    matches = sorted(Path(root).rglob(name))
    assert matches, f"{name} was never written under {root}"
    return matches[0]


@pytest.fixture
def artifact():
    """Look up a produced artifact by file name under a tmp workspace."""

    return _find_artifact
