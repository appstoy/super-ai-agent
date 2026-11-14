"""Run the standalone acceptance script as part of the test suite."""

from __future__ import annotations

import importlib.util
from pathlib import Path


def test_tool_compat_acceptance_script_passes() -> None:
    script = (
        Path(__file__).resolve().parents[2]
        / "tools"
        / "verification"
        / "verify_tool_compat.py"
    )
    assert script.is_file(), f"missing acceptance script: {script}"
    spec = importlib.util.spec_from_file_location("verify_tool_compat", script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.main() == 0
    assert len(module.CHECKS) >= 16
    assert "HTTP fenced legacy call executed the user tool" in module.CHECKS
    assert "HTTP prefixed call executed the builtin tool" in module.CHECKS
