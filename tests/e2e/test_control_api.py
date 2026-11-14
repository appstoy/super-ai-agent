from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy import create_engine

from super_agent.api import create_app
from super_agent.config import Settings
from super_agent.storage.database import create_schema, create_session_factory, session_scope
from super_agent.storage.models import RunRecord, TaskRecord


def test_control_api_lists_and_updates_tasks(tmp_path, monkeypatch) -> None:
    database_path = tmp_path / "control.db"
    database_url = f"sqlite:///{database_path}"
    engine = create_engine(database_url)
    create_schema(engine)
    factory = create_session_factory(engine)
    task_id = uuid4()
    with session_scope(factory) as session:
        session.add(
            TaskRecord(
                id=task_id,
                title="control task",
                status="pending",
                thread_ids="[]",
                version=0,
            )
        )
        session.add(RunRecord(id=uuid4(), task_id=task_id, status="running"))
    monkeypatch.chdir(tmp_path)
    client = TestClient(create_app(Settings(database_url=database_url)))
    listed = client.get("/tasks")
    assert listed.status_code == 200
    assert listed.json()[0]["title"] == "control task"
    stopped = client.post(f"/tasks/{task_id}/stop")
    assert stopped.status_code == 200
    assert stopped.json()["status"] == "cancelled"
    with session_scope(factory) as session:
        assert session.query(RunRecord).one().status == "cancel_requested"
    resumed = client.post(f"/tasks/{task_id}/resume")
    assert resumed.status_code == 200
    assert resumed.json()["status"] == "running"
    assert client.get("/").status_code == 200

