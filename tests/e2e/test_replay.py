from datetime import UTC, datetime
from uuid import uuid4

from tools.replay.runner import ReplayRunner, dump_results


def test_replay_is_deterministic_and_emits_metrics() -> None:
    trigger_id = uuid4()
    event = {
        "id": str(trigger_id),
        "source": "cli",
        "actor": "user",
        "thread_id": str(uuid4()),
        "payload_ref": "inspect repository",
        "occurred_at": datetime.now(UTC).isoformat(),
        "dedupe_key": "replay-1",
    }
    runner = ReplayRunner()

    first = runner.run([event], ["TASK: inspect repository"])
    second = runner.run([event], ["TASK: inspect repository"])

    assert first[0].strategy_version == "phase3-v1"
    assert first[0].action == "created"
    assert first[0].context_tokens > 0
    assert first[0].context_manifest[0]["placement"] == "inline"
    assert first[0].delivery_body == second[0].delivery_body
    assert dump_results(first) == dump_results(second)

