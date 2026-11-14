"""Run the runtime-regression acceptance script inside the test suite."""

from __future__ import annotations

import importlib.util
from pathlib import Path


def test_runtime_regression_acceptance_script_passes() -> None:
    script = (
        Path(__file__).resolve().parents[2]
        / "tools"
        / "verification"
        / "verify_runtime_regressions.py"
    )
    assert script.is_file(), f"missing acceptance script: {script}"
    spec = importlib.util.spec_from_file_location("verify_runtime_regressions", script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.main() == 0
    # One assertion per reported defect must stay covered.
    assert len(module.CHECKS) >= 39
    assert "exhausted events are parked, not eternally pending" in module.CHECKS
    assert "a recovered gateway drains the queue" in module.CHECKS
    assert "file.write creates the file on disk" in module.CHECKS
    assert "HTML artifacts render over HTTP" in module.CHECKS
    assert "runtime summary names the live run" in module.CHECKS
    assert "outbox lists the queued rows" in module.CHECKS
    assert "prose-only answer is nudged to write" in module.CHECKS
    assert "the nudged round really wrote the file" in module.CHECKS
    assert "a file request forces tool_choice=required" in module.CHECKS
    assert "each tool result is sent exactly once" in module.CHECKS
    assert "history is a system/user/assistant/tool conversation" in module.CHECKS
