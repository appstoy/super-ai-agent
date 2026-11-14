from datetime import UTC, datetime, timedelta
from uuid import uuid4

from sqlalchemy import create_engine

from super_agent.storage.database import create_schema, create_session_factory, session_scope
from super_agent.storage.models import RunRecord
from super_agent.watchdog import PersistentRunWatchdog


def test_persistent_watchdog_finds_stalled_run_and_recovers_it() -> None:
    engine = create_engine("sqlite:///:memory:")
    create_schema(engine)
    factory = create_session_factory(engine)
    run_id = uuid4()
    stale = datetime.now(UTC) - timedelta(minutes=10)
    with session_scope(factory) as session:
        session.add(
            RunRecord(
                id=run_id,
                task_id=uuid4(),
                status="running",
                heartbeat_at=stale,
                lease_expires_at=stale,
                lease_owner="dead-worker",
            )
        )

    with session_scope(factory) as session:
        watchdog = PersistentRunWatchdog(stalled_after_seconds=60)
        findings = watchdog.inspect(session)
        assert findings[0].kind.value == "stalled_run"
        expired, retries = watchdog.recover(session)
        assert expired[0].run_id == run_id
        assert retries == ()
