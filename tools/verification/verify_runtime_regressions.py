"""Runnable acceptance evidence for the three reported runtime defects.

Run from the repository root:

    python tools/verification/verify_runtime_regressions.py

Asserts, with no network access or credentials:
  1. A stalled queue advances: a failing event records attempts, backs off,
     and is parked as FAILED instead of blocking the queue head forever.
  2. The Worker can create a file: file.write is offered to the model, write
     succeeds inside the workspace, and the artifact is listable + previewable.
  3. Live work is inspectable: runtime summary and the outbox endpoint return
     the concrete rows behind their counters.
"""

from __future__ import annotations

import json
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

# ruff: noqa: E402

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from fastapi.testclient import TestClient

from super_agent.adapters.models.mock import MockCall, MockModelGateway
from super_agent.api import create_app
from super_agent.application.background import DurableOutboxWorker
from super_agent.config import Settings
from super_agent.errors import ExternalDependencyError
from super_agent.ingress.schemas import TriggerInput
from super_agent.ingress.service import TriggerIngressService
from super_agent.ports.model import ModelRequest, ModelResponse
from super_agent.ports.tool import ExecutionContext
from super_agent.storage.database import (
    create_engine_for_url,
    create_schema,
    create_session_factory,
    session_scope,
)
from super_agent.storage.models import OutboxMessage
from super_agent.tools.loading import build_agent_tools

CHECKS: list[str] = []


def check(label: str, condition: bool) -> None:
    if not condition:
        raise AssertionError(label)
    CHECKS.append(label)


class BatchAwareGateway(MockModelGateway):
    """Answer Router batching and Worker turns, no fixed script."""

    def complete(self, request: ModelRequest) -> ModelResponse:
        self.calls.append(
            MockCall(
                request_id=request.request_id,
                message_count=len(request.messages),
                tool_count=len(request.tools),
                prompt_chars=sum(len(m.content) for m in request.messages),
            )
        )
        system = request.messages[0].content
        user = request.messages[-1].content
        if system.startswith("Return exactly one route") or "backlogged triggers" in system:
            numbered = [
                line.partition(": ")[0].strip()
                for line in user.splitlines()
                if line.partition(": ")[0].strip().isdigit() and line.partition(": ")[2]
            ]
            if numbered:
                return ModelResponse(
                    text="\n".join(f"{n}: TASK: work" for n in numbered)
                )
            return ModelResponse(text="TASK: work")
        return ModelResponse(text="done")


def _factory(db: Path):
    engine = create_engine_for_url(f"sqlite:///{db.as_posix()}")
    create_schema(engine)
    return create_session_factory(engine)


def _queue(factory, count: int) -> None:
    with session_scope(factory) as session:
        for index in range(count):
            TriggerIngressService(session).ingest(
                TriggerInput(
                    source="web",
                    actor="user",
                    thread_id=uuid4(),
                    payload={"message": f"queued {index}"},
                    occurred_at=datetime.now(UTC),
                    dedupe_key=f"vq-{index}",
                )
            )


def verify_queue_no_longer_stalls(tmp: Path) -> None:
    """Defect 1: a failing event must not freeze the whole queue."""

    factory = _factory(tmp / "queue.db")
    _queue(factory, 3)

    def broken(_model: str | None) -> object:
        raise ExternalDependencyError("no model configured")

    worker = DurableOutboxWorker(
        factory, broken, batch_limit=4, retry_backoff_seconds=0.001, max_attempts=2
    )
    worker.process_batch()
    check("background failure is surfaced", worker.last_error is not None)

    with session_scope(factory) as session:
        first = (
            session.query(OutboxMessage)
            .order_by(OutboxMessage.created_at)
            .first()
        )
        attempts = [m.attempts for m in session.query(OutboxMessage).all()]
    check("a failed claim increments attempts", attempts == [1, 1, 1])
    check("the failure reason is persisted", bool(first and first.last_error))

    # Drain repeatedly: the budget is spent, so events park instead of looping.
    for _ in range(10):
        worker.process_batch()
    with session_scope(factory) as session:
        statuses = {m.status for m in session.query(OutboxMessage).all()}
    check("exhausted events are parked, not eternally pending", statuses == {"FAILED"})

    # Recovery: a working gateway drains a fresh queue normally.
    _queue(factory, 2)
    healthy = DurableOutboxWorker(
        factory, lambda _m: BatchAwareGateway([]), batch_limit=4,
        retry_backoff_seconds=0.001,
    )
    for _ in range(6):
        healthy.process_batch()
    with session_scope(factory) as session:
        pending = (
            session.query(OutboxMessage)
            .filter(OutboxMessage.status.in_(("PENDING", "CLAIMED")))
            .count()
        )
    check("a recovered gateway drains the queue", pending == 0)


def verify_worker_can_write_files(tmp: Path) -> None:
    """Defect 2: the model must be offered a write tool and the file must exist."""

    registry, capabilities, report = build_agent_tools(
        Settings(workspace_root=str(tmp)), workspace_root=tmp
    )
    names = {spec.name for spec in registry.specs()}
    check("file.write is a built-in tool", "file.write" in names)
    check("file.write capability is granted", "file.write" in capabilities)
    check("file.write is reported as built-in", "file.write" in report.builtin)
    check("file.list is available for discovery", "file.list" in names)

    html = "<!doctype html><html><body><h1>generated</h1></body></html>"
    result = registry.execute(
        "file.write",
        {"path": "site/index.html", "content": html},
        ExecutionContext(granted_capabilities=capabilities),
    )
    written = tmp / "site" / "index.html"
    check("file.write creates the file on disk", written.is_file())
    check("file.write writes the exact content", written.read_text(encoding="utf-8") == html)
    check("file.write reports success", "created" in result.output)

    read_back = registry.execute(
        "file.read",
        {"path": "site/index.html"},
        ExecutionContext(granted_capabilities=capabilities),
    )
    check("the agent can read back what it wrote", read_back.output == html)

    listed = registry.execute(
        "file.list", {"path": "site"}, ExecutionContext(granted_capabilities=capabilities)
    )
    check("file.list finds the created artifact", "index.html" in listed.output)


def verify_model_is_told_to_write(tmp: Path) -> None:
    """Defect 2b: a model that pastes the document instead of writing it must be corrected.

    The system prompt used to say "read-only tools", so the model answered with
    an HTML document in chat and claimed it was saved while no file existed.
    """

    from super_agent.adapters.models.mock import MockModelGateway
    from super_agent.application.orchestrator import AgentOrchestrator
    from super_agent.ports.model import ModelResponse, ToolCall

    html = "<!doctype html><html><body><h1>written</h1></body></html>"

    class Gateway(MockModelGateway):
        def __init__(self) -> None:
            super().__init__([])
            self.prompts: list[str] = []
            self.requests: list = []

        def complete(self, request):
            system = request.messages[0].content
            if system.startswith("Return exactly one route") or "backlogged" in system:
                return ModelResponse(text="TASK: deliverable")
            self.prompts.append(system)
            self.requests.append(request)
            if len(self.prompts) == 1:
                # Paste the artifact and lie about saving it.
                return ModelResponse(text="```html\n" + html + "\n``` 已保存为 page.html")
            if len(self.prompts) == 2:
                return ModelResponse(
                    text="",
                    tool_calls=(
                        ToolCall(
                            id="c1",
                            name="file_write",
                            arguments={"path": "page.html", "content": html},
                        ),
                    ),
                )
            return ModelResponse(text="已写入 page.html")

    gateway = Gateway()
    registry, capabilities, _ = build_agent_tools(
        Settings(workspace_root=str(tmp)), workspace_root=tmp
    )
    engine = create_engine_for_url("sqlite:///" + (tmp / "nudge.db").as_posix())
    create_schema(engine)
    factory = create_session_factory(engine)
    with session_scope(factory) as session:
        AgentOrchestrator(
            session,
            gateway,
            tools=registry,
            granted_capabilities=capabilities,
            max_rounds=6,
        ).submit(
            TriggerInput(
                source="web",
                actor="user",
                thread_id=uuid4(),
                payload={"message": "做一个 HTML 页面", "explicit": True},
                occurred_at=datetime.now(UTC),
                dedupe_key="nudge-1",
            )
        )

    check("the prompt never says read-only", "read-only" not in gateway.prompts[0].lower())
    check("the prompt names file_write", "file_write" in gateway.prompts[0])
    check("prose-only answer is nudged to write", len(gateway.prompts) >= 2)
    # Tasks write into their own folder under the cache root, so look for the
    # artifact anywhere below the workspace rather than at its root.
    written = sorted(tmp.rglob("page.html"))
    check("the nudged round really wrote the file", bool(written))
    check(
        "generated file stays out of the workspace root",
        written[0].parent != tmp and written[0].parent.parent.name == "项目缓存文件",
    )
    check(
        "the written file holds the deliverable",
        bool(written) and written[0].read_text(encoding="utf-8") == html,
    )
    # Standard tool-calling protocol: the file task is forced, then relaxed.
    check(
        "a file request forces tool_choice=required",
        gateway.requests[0].tool_choice == "required",
    )
    check("tool_choice relaxes after the write", gateway.requests[-1].tool_choice == "auto")
    # A provider that ignores tool_choice forces the nudge path, so inspect the
    # turn that actually carries the tool result instead of assuming an index.
    with_tool = [
        req for req in gateway.requests
        if any(m.role.value == "tool" for m in req.messages)
    ]
    check("a tool result turn was sent back to the model", bool(with_tool))
    tool_turn = with_tool[0]
    roles = [m.role.value for m in tool_turn.messages]
    check(
        "history is a system/user/assistant/tool conversation",
        roles[-2:] == ["assistant", "tool"] and roles[0] == "system",
    )
    tool_msgs = [m for m in tool_turn.messages if m.role.value == "tool"]
    check("each tool result is sent exactly once", len(tool_msgs) == 1)
    check(
        "the user message does not duplicate the tool result",
        "page.html" not in tool_turn.messages[1].content,
    )
    engine.dispose()


def verify_artifacts_are_visible(tmp: Path) -> None:
    """Defect 2 (second half): a human must be able to preview the artifact."""

    (tmp / "out").mkdir(parents=True, exist_ok=True)
    (tmp / "out" / "page.html").write_text(
        "<!doctype html><h1>previewable</h1>", encoding="utf-8"
    )
    settings = Settings(
        database_url=f"sqlite:///{(tmp / 'api.db').as_posix()}",
        workspace_root=str(tmp),
        model_name="test-model",
    )
    with TestClient(create_app(settings)) as client:
        listing = client.get("/api/files", params={"path": "out"}).json()
        entry = next(e for e in listing["entries"] if e["name"] == "page.html")
        check("created artifacts are listed", entry["previewable"] is True)
        served = client.get("/files/out/page.html")
        check("HTML artifacts render over HTTP", served.status_code == 200)
        check("served HTML is the real content", "previewable" in served.text)
        check(
            "paths outside the workspace are refused",
            client.get("/api/files", params={"path": ".."}).status_code == 403,
        )


def verify_live_work_is_inspectable(tmp: Path) -> None:
    """Defect 3: counters must resolve to the actual rows."""

    settings = Settings(
        database_url=f"sqlite:///{(tmp / 'inspect.db').as_posix()}",
        workspace_root=str(tmp),
        model_name="test-model",
    )
    engine = create_engine_for_url(settings.database_url)
    create_schema(engine)
    factory = create_session_factory(engine)

    task_id, worker_id, run_id = uuid4(), uuid4(), uuid4()
    with session_scope(factory) as session:
        from super_agent.storage.models import RunRecord, TaskRecord, WorkerRecord

        session.add(
            TaskRecord(
                id=task_id, title="build an html page", status="running",
                thread_ids="[]", worker_id=worker_id, version=1,
            )
        )
        session.add(
            WorkerRecord(id=worker_id, task_id=task_id, status="running",
                         capabilities="[]", version=1)
        )
        session.add(
            RunRecord(id=run_id, task_id=task_id, trigger_id=None, status="running",
                      attempt=1, started_at=datetime.now(UTC),
                      heartbeat_at=datetime.now(UTC))
        )
        TriggerIngressService(session).ingest(
            TriggerInput(
                source="web", actor="user", thread_id=uuid4(),
                payload={"message": "queued detail"},
                occurred_at=datetime.now(UTC), dedupe_key="detail-1",
            )
        )

    with TestClient(create_app(settings)) as client:
        summary = client.get("/api/runtime/summary").json()
        details = summary.get("active_run_details") or []
        check("runtime summary counts the live run", summary["active_runs"] == 1)
        check("runtime summary names the live run", len(details) == 1)
        check(
            "the live run carries its task title",
            details[0]["task_title"] == "build an html page",
        )
        check("the live run reports heartbeat age", details[0]["heartbeat_age_seconds"] >= 0)
        check(
            "runtime summary exposes a worker error slot",
            "worker_last_error" in summary,
        )

        outbox = client.get("/api/outbox").json()
        check("outbox reports pending depth", outbox["pending"] == 1)
        check("outbox reports a failed bucket", "failed" in outbox)
        check("outbox lists the queued rows", len(outbox["items"]) == 1)
        item = outbox["items"][0]
        check("a queued row shows its text", item["text"] == "queued detail")
        check("a queued row shows its attempts", item["attempts"] == 0)
        check("a queued row shows its errors", "last_error" in item)
    engine.dispose()


def main() -> int:
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp_name:
        root = Path(tmp_name)
        for name, verify in (
            ("q", verify_queue_no_longer_stalls),
            ("w", verify_worker_can_write_files),
            ("n", verify_model_is_told_to_write),
            ("a", verify_artifacts_are_visible),
            ("i", verify_live_work_is_inspectable),
        ):
            directory = root / name
            directory.mkdir()
            verify(directory)
    print(json.dumps({"passed": len(CHECKS), "checks": CHECKS}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
