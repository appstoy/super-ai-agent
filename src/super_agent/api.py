"""HTTP adapter for the local control panel and API."""

# The page is intentionally embedded so the single-process quick start needs no frontend build.
# ruff: noqa: E501

from __future__ import annotations

import hashlib
import hmac
import json
import time
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import asynccontextmanager
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from threading import Event, Thread
from uuid import UUID, uuid4

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from super_agent.adapters.channels.memory import CliChannelAdapter
from super_agent.adapters.models.magpie import MagpieModelGateway
from super_agent.adapters.models.openai_compatible import OpenAICompatibleGateway
from super_agent.application.async_tools import DurableToolRunner
from super_agent.application.background import BackgroundAgentRunner, DurableOutboxWorker
from super_agent.application.orchestrator import AgentOrchestrator
from super_agent.application.result import build_task_result
from super_agent.application.run_leases import RunLeaseService
from super_agent.config import Settings
from super_agent.daemon.registry import PersistentDeviceRegistry
from super_agent.daemon.service import DeviceCommandService, DeviceCommandStore
from super_agent.delivery.service import DeliveryService
from super_agent.domain import TaskStatus
from super_agent.domain.health import HealthStatus
from super_agent.errors import AgentError
from super_agent.ingress.schemas import TriggerInput
from super_agent.policies.speculative_runner import SpeculativeRunner
from super_agent.ports.model import ChatMessage, MessageRole, ModelGateway, ModelRequest
from super_agent.skills.installer import install_skill, uninstall_skill
from super_agent.skills.library import LiveSkillLibrary
from super_agent.skills.market import (
    MarketEntry,
    find_market_dir,
    load_market,
    resolve_ref,
    search_market,
)
from super_agent.storage.database import (
    create_engine_for_url,
    create_schema,
    create_session_factory,
    session_scope,
)
from super_agent.storage.models import (
    DeliveryRecord,
    DeviceRecord,
    OutboxMessage,
    RunRecord,
    TaskFlagRecord,
    TaskRecord,
    TriggerRecord,
    WorkerContextRecord,
    WorkerEventRecord,
    WorkerRecord,
)
from super_agent.tools.loading import build_agent_tools, resolve_skill_roots
from super_agent.tools.recall import RecallTool
from super_agent.tools.registry import ToolRegistry
from super_agent.tools.workspace import find_task_workspace
from super_agent.watchdog import PersistentRunWatchdog
from super_agent.watchdog.session import WatchdogSession


class ChatRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    message: str = Field(min_length=1, max_length=20_000)
    model: str | None = Field(default=None, max_length=255)


class ChatResponse(BaseModel):
    text: str
    model: str
    total_tokens: int


class TaskRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    message: str = Field(min_length=1, max_length=20_000)
    model: str | None = Field(default=None, max_length=255)
    thread_id: UUID | None = None
    actor: str = Field(default="web-user", min_length=1, max_length=255)
    dedupe_key: str | None = Field(default=None, max_length=255)
    # Names of skills the user picked for THIS task. The Worker inlines their
    # instructions into the prompt; an unknown name is reported, never ignored.
    skills: list[str] = Field(default_factory=list, max_length=16)


class TaskFlagRequest(BaseModel):
    """Partial update of a card's presentation flags.

    A module-level model, not a nested one: a class defined inside the endpoint
    is not a recognised Pydantic parameter type, so FastAPI reads the body as a
    missing query field and rejects every request with 422.
    """

    model_config = ConfigDict(extra="forbid")

    pinned: bool | None = None
    archived: bool | None = None
    color: str | None = Field(default=None, max_length=16)


class SkillInstallRequest(BaseModel):
    """One install request: a market ref, a git URL, or a local folder."""

    model_config = ConfigDict(extra="forbid")

    source: str = Field(min_length=1, max_length=500)
    from_magpie: bool = False
    ref: str | None = Field(default=None, max_length=255)
    force: bool = False


class WebhookRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    message: str = Field(min_length=1, max_length=20_000)
    model: str | None = Field(default=None, max_length=255)
    thread_id: UUID | None = None
    actor: str = Field(default="webhook", min_length=1, max_length=255)
    dedupe_key: str | None = Field(default=None, max_length=255)


def _task_payload(message: str, model_name: str | None, skills: list[str]) -> dict[str, object]:
    """Build the trigger payload, including the skills the user selected.

    Skills travel in the payload (not in the message text) so the Router title
    and the model's user message stay exactly what the person typed; the Worker
    reads the selection separately and inlines it into the system prompt.
    """

    payload: dict[str, object] = {"message": message, "model": model_name}
    chosen = [name.strip() for name in skills if name and name.strip()]
    if chosen:
        payload["skills"] = chosen
    return payload


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the HTTP application with explicit settings injection."""

    active_settings = settings or Settings.from_env()
    database_url = active_settings.database_url
    runtime_engine = create_engine_for_url(database_url)
    # A local quick start should work before a user knows the migration command.
    # Production deployments still run Alembic as their schema authority.
    if database_url.startswith("sqlite"):
        create_schema(runtime_engine)
    runtime_factory = create_session_factory(runtime_engine)
    # One resolved root is shared by the write tools and the artifact endpoints
    # so "where the model may write" and "what the console may show" agree.
    workspace_root = Path(active_settings.workspace_root).resolve()
    agent_tools, agent_capabilities, tool_load_report = build_agent_tools(
        active_settings, workspace_root=workspace_root
    )
    # One live view shared by the picker, the model's skill_* tools and the
    # task payload: installing a folder and refreshing the page is enough, no
    # restart. The object re-scans only when the library's mtime moves.
    skill_registry = LiveSkillLibrary(
        resolve_skill_roots(active_settings, workspace_root),
        enabled=active_settings.skills_enabled,
    )
    recall_tool = RecallTool(runtime_factory)
    tool_runner = DurableToolRunner(runtime_factory, agent_tools)
    agent_runner = BackgroundAgentRunner(
        runtime_factory,
        tools=agent_tools,
        granted_capabilities=agent_capabilities,
        tool_runner=tool_runner,
        recall_tool=recall_tool,
        max_rounds=active_settings.worker_max_rounds,
        skills=skill_registry,
    )

    def build_gateway(model_name: str | None) -> ModelGateway:
        if not model_name:
            raise HTTPException(
                status_code=400,
                detail="请先在页面选择模型，或设置 SUPER_AGENT_MODEL",
            )
        if active_settings.model_base_url:
            return OpenAICompatibleGateway(
                active_settings.model_base_url,
                active_settings.model_api_key or "",
                model_name,
                timeout=active_settings.model_timeout_seconds,
            )
        return MagpieModelGateway(
            model_name,
            base_url=active_settings.magpie_base_url,
            api_key=active_settings.magpie_api_key,
            timeout=active_settings.model_timeout_seconds,
        )

    def recovery_gateway(model_name: str | None) -> ModelGateway:
        selected = model_name or active_settings.model_name
        if not selected:
            raise AgentError("set SUPER_AGENT_MODEL before recovering persisted Agent work")
        return build_gateway(selected)

    speculative_runner: SpeculativeRunner | None = None
    if active_settings.speculative_enabled:
        speculative_runner = SpeculativeRunner(
            runtime_factory,
            _speculation_gateway(active_settings, build_gateway),
            DeliveryService([CliChannelAdapter()]),
            tools=agent_tools,
            granted_capabilities=agent_capabilities,
            skills=skill_registry,
        )
    durable_worker = DurableOutboxWorker(
        runtime_factory,
        recovery_gateway,
        tools=agent_tools,
        granted_capabilities=agent_capabilities,
        tool_runner=tool_runner,
        recall_tool=recall_tool,
        speculative_runner=speculative_runner,
        batch_limit=active_settings.router_batch_limit,
        max_rounds=active_settings.worker_max_rounds,
        skills=skill_registry,
    )
    recovery_stop = Event()
    recovery_thread = Thread(
        target=durable_worker.run_forever,
        args=(recovery_stop,),
        kwargs={"poll_interval": 0.5},
        name="super-agent-outbox-recovery",
        daemon=True,
    )

    def watchdog_loop() -> None:
        """External control loop (article §6) running beside the main system."""

        interval = active_settings.watchdog_interval_seconds
        while not recovery_stop.is_set():
            try:
                with session_scope(runtime_factory) as session:
                    if active_settings.model_name:
                        WatchdogSession(PersistentRunWatchdog()).tick(
                            session, recovery_gateway(None)
                        )
                    else:
                        PersistentRunWatchdog().recover(session)
            except Exception:
                # The control loop must survive provider outages; the next
                # tick retries, exactly like the rule-only watchdog CLI.
                pass
            recovery_stop.wait(interval)

    watchdog_thread: Thread | None = None
    if active_settings.watchdog_interval_seconds > 0:
        watchdog_thread = Thread(
            target=watchdog_loop,
            name="super-agent-watchdog-loop",
            daemon=True,
        )

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        recovery_thread.start()
        if watchdog_thread is not None:
            watchdog_thread.start()
        try:
            yield
        finally:
            recovery_stop.set()
            recovery_thread.join(timeout=5)
            if watchdog_thread is not None:
                watchdog_thread.join(timeout=5)
            agent_runner.shutdown()
            tool_runner.shutdown()
            runtime_engine.dispose()

    app = FastAPI(
        title="Super AI Agent",
        version=active_settings.version,
        lifespan=lifespan,
    )

    def _page(name: str, fallback: str) -> str:
        """Read one console page, preferring a repo checkout over the wheel.

        Two audiences, two pages: `/` is the task surface a person uses, and
        `/ops` is the runtime console an operator uses. Each one passes its own
        fallback: the embedded HTML is the runtime console, so serving it at `/`
        would greet a normal user with Watchdog and Outbox panels.
        """

        for candidate in (
            Path(__file__).resolve().parents[2] / "web" / name,
            Path(__file__).resolve().parent / "web" / name,
        ):
            if candidate.is_file():
                body = candidate.read_text(encoding="utf-8")
                if body.strip():
                    return body
        return fallback

    @app.get("/", response_class=HTMLResponse)
    def task_console() -> str:
        """The user-facing surface: one input, then a feed of results."""

        return _page("index.html", _TASK_PANEL_FALLBACK)

    @app.get("/ops", response_class=HTMLResponse)
    def control_panel() -> str:
        """The operator console: Router, Watchdog, Outbox, runs, tools."""

        return _page("ops.html", _CONTROL_PANEL_HTML)

    @app.get("/api/tools")
    def list_tools() -> dict[str, object]:
        """Report built-in, user-loaded and failed tools without exposing secrets."""

        return tool_load_report.to_dict()

    @app.get("/api/skills")
    def list_skills() -> dict[str, object]:
        """List selectable skills so the page can offer them.

        Load failures are included too: a skill that silently fails to appear
        is impossible to debug from the UI, and the fix (usually a missing
        description) is something the user can act on.
        """

        skills = skill_registry.all()
        return {
            "enabled": active_settings.skills_enabled,
            "library": str(skill_registry.roots[0]) if skill_registry.roots else None,
            "skills": [skill.to_dict() for skill in skills],
            "failed": [list(item) for item in skill_registry.failed],
            "count": len(skills),
        }

    # ---- Magpie skill market ------------------------------------------------
    # Magpie ships a local catalogue of skills; browsing and installing from it
    # here means the page is not limited to whatever is already in the folder.

    def _market() -> tuple[list[MarketEntry], Path]:
        """Load Magpie's catalogue, or raise a 503 that says what to do."""

        market_dir = find_market_dir(active_settings.magpie_market_dir)
        if market_dir is None:
            raise HTTPException(
                status_code=503,
                detail=(
                    "没有找到 Magpie 的技能目录。确认 Magpie 已安装，"
                    "或用 SUPER_AGENT_MAGPIE_MARKET_DIR 指定路径。"
                ),
            )
        try:
            return load_market(market_dir), market_dir
        except AgentError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

    @app.get("/api/skills/market")
    def search_skill_market(
        q: str = "", limit: int = 30, official: bool = False
    ) -> dict[str, object]:
        """Search Magpie's catalogue so the page can offer skills to install."""

        entries, market_dir = _market()
        rows = search_market(entries, q, limit=limit, official_only=official)
        return {
            "market_dir": str(market_dir),
            "total": len(entries),
            "query": q,
            "count": len(rows),
            "skills": [entry.to_dict() for entry in rows],
        }

    @app.post("/api/skills/install")
    def install_from_market(payload: SkillInstallRequest) -> dict[str, object]:
        """Install one skill from the market (or from any git URL / folder)."""

        library = skill_registry.roots[0] if skill_registry.roots else workspace_root
        source = payload.source
        only: str | None = None
        if payload.from_magpie:
            entries, _ = _market()
            try:
                entry = resolve_ref(entries, source)
            except AgentError as exc:
                raise HTTPException(status_code=404, detail=str(exc)) from exc
            source, only = entry.source, entry.skill_id
        try:
            result = install_skill(
                source, library, ref=payload.ref, force=payload.force, only=only
            )
        except AgentError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        # The library re-stamps itself, so the next /api/skills read picks this
        # up without a restart; report what landed so the page can refresh.
        return result.to_dict()

    @app.delete("/api/skills/{name}")
    def delete_skill(name: str) -> dict[str, object]:
        """Remove an installed skill.

        Only skills the installer owns are deleted; a hand-written folder is
        refused with an explanation, because silently deleting someone's own
        work on a name collision cannot be undone.
        """

        library = skill_registry.roots[0] if skill_registry.roots else workspace_root
        try:
            removed = uninstall_skill(name, library)
        except AgentError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"removed": str(removed)}

    @app.get("/api/config")
    def config_summary() -> dict[str, object]:
        return {
            "service": active_settings.app_name,
            "version": active_settings.version,
            "magpie_base_url": active_settings.magpie_base_url,
            "model_base_url": active_settings.model_base_url,
            "model": active_settings.model_name or None,
            "workspace_root": str(workspace_root),
            "magpie_mode": active_settings.model_base_url == active_settings.magpie_base_url
            or active_settings.model_base_url is None,
        }

    # ---- workspace artifacts ------------------------------------------------
    # Without this the Worker could write a file and no human could ever look at
    # it, which made "build me an HTML page" useless even once writing worked.

    def _workspace_file(relative_path: str) -> Path:
        """Resolve a workspace-relative path, refusing anything outside it."""

        candidate = (workspace_root / relative_path).resolve()
        if candidate != workspace_root and workspace_root not in candidate.parents:
            raise HTTPException(status_code=403, detail="path is outside the workspace")
        if not candidate.is_file():
            raise HTTPException(status_code=404, detail="file not found")
        return candidate

    @app.get("/api/files")
    def list_workspace_files(path: str = ".", limit: int = 500) -> dict[str, object]:
        """List workspace files so created artifacts are discoverable."""

        bounded = max(1, min(limit, 2000))
        target = (workspace_root / path).resolve()
        if target != workspace_root and workspace_root not in target.parents:
            raise HTTPException(status_code=403, detail="path is outside the workspace")
        if not target.is_dir():
            raise HTTPException(status_code=404, detail="directory not found")
        skipped = {".git", ".venv", "node_modules", "__pycache__", ".mypy_cache",
                   ".pytest_cache", ".ruff_cache", ".alma-snapshots"}
        entries: list[dict[str, object]] = []
        for child in sorted(target.iterdir(), key=lambda item: (item.is_file(), item.name)):
            if child.name in skipped:
                continue
            if len(entries) >= bounded:
                break
            try:
                stat = child.stat()
            except OSError:
                continue
            entries.append(
                {
                    "name": child.name,
                    "path": child.relative_to(workspace_root).as_posix(),
                    "is_dir": child.is_dir(),
                    "size": stat.st_size if child.is_file() else None,
                    "modified_at": datetime.fromtimestamp(
                        stat.st_mtime, UTC
                    ).isoformat(),
                    "previewable": child.is_file()
                    and child.suffix.lower()
                    in {".html", ".htm", ".md", ".txt", ".json", ".svg", ".css", ".js", ".py"},
                }
            )
        return {
            "path": path,
            "workspace_root": str(workspace_root),
            "entries": entries,
            "truncated": len(entries) >= bounded,
        }

    @app.get("/api/files/content")
    def read_workspace_file(path: str, max_bytes: int = 512_000) -> dict[str, object]:
        """Return one workspace file's text so the console can preview it."""

        bounded = max(1, min(max_bytes, 5_000_000))
        target = _workspace_file(path)
        raw = target.read_bytes()
        truncated = len(raw) > bounded
        text = raw[:bounded].decode("utf-8", errors="replace")
        return {
            "path": path,
            "size": len(raw),
            "truncated": truncated,
            "text": text,
            "media_type": "text/html"
            if target.suffix.lower() in {".html", ".htm"}
            else "text/plain",
        }

    @app.get("/api/tasks/{task_id}/files")
    def list_task_files(task_id: str) -> dict[str, object]:
        """List the artifacts one Task produced.

        Every task writes into its own folder under the cache root, so this is
        the endpoint a console uses to show "what did this task actually make"
        without asking a human to guess a filename.
        """

        try:
            folder = find_task_workspace(
                workspace_root, task_id, active_settings.cache_dir_name
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if folder is None or not folder.is_dir():
            return {
                "task_id": task_id,
                "folder": None,
                "files": [],
                "exists": False,
            }
        files: list[dict[str, object]] = []
        for path in sorted(folder.rglob("*")):
            if not path.is_file():
                continue
            try:
                stat = path.stat()
            except OSError:
                continue
            rel = path.relative_to(workspace_root).as_posix()
            files.append(
                {
                    "name": path.name,
                    "path": rel,
                    "relative_path": path.relative_to(folder).as_posix(),
                    "size": stat.st_size,
                    "modified_at": datetime.fromtimestamp(
                        stat.st_mtime, UTC
                    ).isoformat(),
                    "previewable": path.suffix.lower()
                    in {".html", ".htm", ".md", ".txt", ".json", ".svg", ".css", ".js"},
                }
            )
        return {
            "task_id": task_id,
            "folder": str(folder),
            "relative_folder": folder.relative_to(workspace_root).as_posix(),
            "files": files,
            "exists": True,
        }

    @app.get("/api/tasks/{task_id}/result")
    def task_result(task_id: UUID) -> dict[str, object]:
        """Answer the only question a user has: what did my task produce?

        The runtime's own vocabulary (Trigger, Worker, Run, event, delivery) is
        for operators. This endpoint collapses all of it into one object the
        console can render directly: a state, a conclusion, the artifacts with
        preview URLs, and a plain-language step list.
        """

        engine = create_engine_for_url(database_url)
        factory = create_session_factory(engine)
        try:
            with session_scope(factory) as session:
                task = session.get(TaskRecord, task_id)
                if task is None:
                    raise HTTPException(status_code=404, detail="task not found")
                return build_task_result(
                    session,
                    task,
                    workspace_root=workspace_root,
                    cache_dir_name=active_settings.cache_dir_name,
                ).to_dict()
        finally:
            engine.dispose()

    @app.get("/api/results")
    def recent_results(limit: int = 30) -> dict[str, object]:
        """Return the newest tasks already projected as results.

        The home screen is a feed of outcomes, not a task table, so it needs one
        request that is already in user language. Newest first, bounded.
        """

        bounded = max(1, min(limit, 100))
        engine = create_engine_for_url(database_url)
        factory = create_session_factory(engine)
        try:
            with session_scope(factory) as session:
                # Tasks carry no timestamp of their own, so order by the newest
                # Run of each task. This stays portable (no SQLite rowid) and
                # matches what a user means by "most recent".
                newest_run = (
                    select(RunRecord.task_id, func.max(RunRecord.started_at).label("t"))
                    .group_by(RunRecord.task_id)
                    .subquery()
                )
                rows = (
                    session.query(TaskRecord)
                    .outerjoin(newest_run, newest_run.c.task_id == TaskRecord.id)
                    .order_by(newest_run.c.t.desc().nullslast())
                    .limit(bounded)
                    .all()
                )
                items = [
                    build_task_result(
                        session,
                        row,
                        workspace_root=workspace_root,
                        cache_dir_name=active_settings.cache_dir_name,
                    ).to_dict()
                    for row in rows
                ]
                return {"items": items, "count": len(items)}
        finally:
            engine.dispose()

    @app.get("/api/artifacts")
    def all_artifacts(limit: int = 200) -> dict[str, object]:
        """Aggregate all generated artifacts across all tasks for the files shelf.

        Users shouldn't have to scroll through dozens of historical task cards
        just to find an HTML file or script that was created earlier. This
        scans the cache root, groups files with their originating task title,
        and provides direct preview URLs.
        """

        bounded = max(1, min(limit, 1000))
        root = (workspace_root / active_settings.cache_dir_name).resolve()
        if not root.is_dir():
            return {"artifacts": [], "count": 0}

        # Build task prefix -> task title map from database
        engine = create_engine_for_url(database_url)
        factory = create_session_factory(engine)
        title_map: dict[str, str] = {}
        try:
            with session_scope(factory) as session:
                for row in session.query(TaskRecord.id, TaskRecord.title).all():
                    short_id = str(row[0]).replace("-", "")[:8]
                    title_map[short_id] = str(row[1])
        finally:
            engine.dispose()

        artifacts: list[dict[str, object]] = []
        preview_suffixes = {".html", ".htm", ".md", ".txt", ".json", ".svg", ".css", ".js", ".py"}

        for child in sorted(root.iterdir(), key=lambda p: p.stat().st_mtime if p.is_dir() else 0, reverse=True):
            if not child.is_dir():
                continue
            folder_prefix = child.name.split("-")[0]
            task_title = title_map.get(folder_prefix, child.name)

            for f in sorted(child.rglob("*")):
                if not f.is_file():
                    continue
                if len(artifacts) >= bounded:
                    break
                try:
                    stat = f.stat()
                except OSError:
                    continue

                rel_path = f.relative_to(workspace_root).as_posix()
                suffix = f.suffix.lower()
                is_previewable = suffix in preview_suffixes
                artifacts.append({
                    "name": f.name,
                    "path": rel_path,
                    "size": stat.st_size,
                    "modified_at": datetime.fromtimestamp(stat.st_mtime, UTC).isoformat(),
                    "task_title": task_title,
                    "task_folder": child.name,
                    "previewable": is_previewable,
                    "open_url": f"/files/{rel_path}" if suffix in {".html", ".htm"} else None,
                })
            if len(artifacts) >= bounded:
                break

        return {"artifacts": artifacts, "count": len(artifacts)}

    @app.patch("/api/tasks/{task_id}/flags")
    def set_task_flags(task_id: UUID, request: TaskFlagRequest) -> dict[str, object]:
        """Set the presentation flags a user owns for one card.

        Pin, archive and colour describe how a person arranges their own view,
        not what the runtime did, so they live in their own table and never
        touch the task's lifecycle fields.
        """

        allowed = {"", "red", "orange", "yellow", "green", "blue", "purple", "gray"}
        if request.color is not None and request.color not in allowed:
            raise HTTPException(
                status_code=422,
                detail=f"color must be one of {sorted(allowed)}",
            )

        engine = create_engine_for_url(database_url)
        factory = create_session_factory(engine)
        try:
            with session_scope(factory) as session:
                task = session.get(TaskRecord, task_id)
                if task is None:
                    raise HTTPException(status_code=404, detail="task not found")
                flag = session.get(TaskFlagRecord, task_id)
                if flag is None:
                    flag = TaskFlagRecord(task_id=task_id)
                    session.add(flag)
                if request.pinned is not None:
                    flag.pinned = 1 if request.pinned else 0
                if request.archived is not None:
                    flag.archived = 1 if request.archived else 0
                if request.color is not None:
                    flag.color = request.color
                session.flush()
                return {
                    "task_id": str(task_id),
                    "pinned": bool(flag.pinned),
                    "archived": bool(flag.archived),
                    "color": flag.color,
                }
        finally:
            engine.dispose()

    @app.delete("/api/files")
    def delete_workspace_file(path: str) -> dict[str, object]:
        """Remove one generated artifact from disk.

        Scoped to the workspace like every other file endpoint, and refused for
        directories so a stray request cannot wipe a task's whole folder.
        """

        target = _workspace_file(path)
        if target.is_dir():
            raise HTTPException(status_code=400, detail="path is a directory")
        try:
            target.unlink()
        except OSError as exc:
            raise HTTPException(status_code=500, detail=f"could not delete: {exc}") from exc
        return {"path": path, "deleted": True}

    @app.get("/files/{path:path}", response_class=HTMLResponse)
    def serve_workspace_file(path: str) -> HTMLResponse:
        """Serve a workspace artifact directly so HTML previews actually render."""

        target = _workspace_file(path)
        suffix = target.suffix.lower()
        if suffix not in {".html", ".htm"}:
            raise HTTPException(
                status_code=415,
                detail="only HTML artifacts are served directly; use /api/files/content",
            )
        return HTMLResponse(target.read_text(encoding="utf-8", errors="replace"))

    @app.get("/api/magpie/integration")
    def magpie_integration() -> dict[str, object]:
        """Explain the supported gateway integration and its Magpie limitation."""
        return {
            "gateway_url": active_settings.magpie_base_url,
            "api_key_configured": bool(active_settings.magpie_api_key),
            "protocol": "OpenAI Chat Completions",
            "agent_registration": False,
            "agent_registration_reason": (
                "Magpie does not expose runtime registration; an Agents-page row requires a"
                " Magpie source adapter and release."
            ),
            "recommended_mode": "Use Super AI Agent as an OpenAI-compatible client through Magpie.",
        }

    @app.get("/api/runtime/router")
    def runtime_router() -> dict[str, object]:
        """Expose resident Router batching counters (article §2.1 / §7.2)."""

        with session_scope(runtime_factory) as session:
            from super_agent.storage.models import RouterMessageRecord

            transcript_rows = session.query(RouterMessageRecord).count()
        return {
            "batch_limit": active_settings.router_batch_limit,
            "transcript_entries": transcript_rows,
            "last_drain": durable_worker.router_metrics,
            "speculative_enabled": active_settings.speculative_enabled,
            "watchdog_interval_seconds": active_settings.watchdog_interval_seconds,
        }

    @app.get("/api/runtime/watchdog")
    def runtime_watchdog() -> dict[str, object]:
        """Return persisted Run health without mutating execution state."""

        with session_scope(runtime_factory) as session:
            findings = PersistentRunWatchdog().inspect(session)
            return {
                "status": "healthy"
                if all(finding.kind.value == "healthy" for finding in findings)
                else "attention",
                "findings": [
                    {
                        "kind": finding.kind.value,
                        "subject_id": str(finding.subject_id) if finding.subject_id else None,
                        "evidence": finding.evidence,
                        "action": finding.action,
                    }
                    for finding in findings
                ],
            }

    @app.get("/api/models")
    def list_models() -> dict[str, object]:
        if active_settings.model_base_url:
            configured_model = active_settings.model_name or None
            return {
                "gateway": {
                    "name": "OpenAI-compatible",
                    "base_url": active_settings.model_base_url,
                },
                "models": (
                    [{"id": configured_model, "display_name": configured_model}]
                    if configured_model
                    else []
                ),
            }
        gateway = MagpieModelGateway(
            active_settings.model_name or "__catalog__",
            base_url=active_settings.magpie_base_url,
            api_key=active_settings.magpie_api_key,
            timeout=active_settings.model_timeout_seconds,
        )
        try:
            return {
                "gateway": gateway.hello(),
                "models": [asdict(model) for model in gateway.list_models()],
            }
        except AgentError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

    @app.post("/api/chat", response_model=ChatResponse)
    def chat(request: ChatRequest) -> ChatResponse:
        model_name = request.model or active_settings.model_name
        if not model_name:
            raise HTTPException(
                status_code=400,
                detail="请先在页面选择模型，或设置 SUPER_AGENT_MODEL",
            )
        gateway = build_gateway(model_name)
        try:
            response = gateway.complete(
                ModelRequest(
                    model=model_name,
                    messages=(ChatMessage(MessageRole.USER, request.message),),
                )
            )
        except AgentError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        return ChatResponse(
            text=response.text,
            model=model_name,
            total_tokens=response.usage.total_tokens,
        )

    @app.post("/api/chat/stream")
    def chat_stream(request: ChatRequest) -> StreamingResponse:
        """Stream text deltas from an OpenAI-compatible model gateway."""

        model_name = request.model or active_settings.model_name
        if not model_name:
            raise HTTPException(
                status_code=400,
                detail="请先在页面选择模型，或设置 SUPER_AGENT_MODEL",
            )
        gateway = build_gateway(model_name)
        model_request = ModelRequest(
            model=model_name,
            messages=(ChatMessage(MessageRole.USER, request.message),),
        )

        def generate() -> Iterator[str]:
            try:
                if isinstance(gateway, OpenAICompatibleGateway):
                    chunks = gateway.stream_text(model_request)
                else:
                    chunks = iter((gateway.complete(model_request).text,))
                for chunk in chunks:
                    yield f"event: token\ndata: {json.dumps({'text': chunk}, ensure_ascii=False)}\n\n"
                yield "event: done\ndata: {}\n\n"
            except AgentError as exc:
                yield f"event: error\ndata: {json.dumps({'error': str(exc)}, ensure_ascii=False)}\n\n"

        return StreamingResponse(
            generate(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.post("/api/tasks")
    def create_task(request: TaskRequest) -> dict[str, object]:
        """Submit a message through the full Trigger -> Router -> Worker path."""

        thread_id = request.thread_id or uuid4()
        model_name = request.model or active_settings.model_name
        trigger_input = TriggerInput(
            source="web",
            actor=request.actor,
            thread_id=thread_id,
            payload=_task_payload(request.message, model_name, request.skills),
            occurred_at=datetime.now(UTC),
            dedupe_key=request.dedupe_key or f"web:{thread_id}:{request.message}",
        )
        gateway = build_gateway(model_name or active_settings.model_name)
        engine = create_engine_for_url(database_url)
        factory = create_session_factory(engine)
        try:
            with session_scope(factory) as session:
                result = AgentOrchestrator(
                    session,
                    gateway,
                    tools=agent_tools,
                    granted_capabilities=agent_capabilities,
                    tool_runner=tool_runner,
                    recall_tool=recall_tool,
                    max_rounds=active_settings.worker_max_rounds,
                    skills=skill_registry,
                ).submit_via_outbox(trigger_input)
        except AgentError as exc:
            # A provider timeout is an outcome, not a server crash: report the
            # reason so the console can show it instead of an opaque 500.
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        except Exception as exc:  # noqa: BLE001 - a failed task is not a 500
            # The Worker can surface any provider/transport exception. Surfacing
            # it as a 502 keeps "the task failed" distinct from "the server is
            # broken", which is what a console needs to say something useful.
            raise HTTPException(
                status_code=502, detail=f"{type(exc).__name__}: {exc}"
            ) from exc
        finally:
            engine.dispose()
        return {
            "trigger_id": str(result.trigger_id),
            "action": result.action.value,
            "task_id": str(result.task_id) if result.task_id else None,
            "worker_id": str(result.worker_id) if result.worker_id else None,
            "status": result.status,
            "text": result.text,
            "run_id": str(result.run_id) if result.run_id else None,
        }

    @app.post("/api/tasks/async", status_code=202)
    def create_task_async(request: TaskRequest) -> dict[str, object]:
        """Accept a task and execute it outside the HTTP request thread."""

        thread_id = request.thread_id or uuid4()
        model_name = request.model or active_settings.model_name
        dedupe_key = request.dedupe_key or f"web:{thread_id}:{request.message}"
        trigger_input = TriggerInput(
            source="web",
            actor=request.actor,
            thread_id=thread_id,
            payload=_task_payload(request.message, model_name, request.skills),
            occurred_at=datetime.now(UTC),
            dedupe_key=dedupe_key,
        )
        gateway = build_gateway(model_name)
        agent_runner.submit(trigger_input, gateway)
        return {
            "action": "accepted",
            "status": "queued",
            "dedupe_key": dedupe_key,
            "thread_id": str(thread_id),
        }

    @app.post("/api/webhooks/{source}", status_code=202)
    def webhook(source: str, request: WebhookRequest, http_request: Request) -> dict[str, object]:
        """Normalize a signed HTTP webhook into the same background Agent path."""

        if active_settings.webhook_secret:
            signature = http_request.headers.get("X-Super-Agent-Signature", "")
            canonical = json.dumps(
                request.model_dump(mode="json"), ensure_ascii=False, sort_keys=True
            ).encode("utf-8")
            expected = hmac.new(
                active_settings.webhook_secret.encode("utf-8"), canonical, hashlib.sha256
            ).hexdigest()
            supplied = signature.removeprefix("sha256=")
            if not hmac.compare_digest(supplied, expected):
                raise HTTPException(status_code=401, detail="invalid webhook signature")
        thread_id = request.thread_id or uuid4()
        model_name = request.model or active_settings.model_name
        dedupe_key = request.dedupe_key or f"{source}:{thread_id}:{request.message}"
        trigger_input = TriggerInput(
            source=source,
            actor=request.actor,
            thread_id=thread_id,
            # A webhook has no skill picker; keep the plain payload so the signed
            # canonical body never grows a field silently.
            payload=_task_payload(request.message, model_name, []),
            occurred_at=datetime.now(UTC),
            dedupe_key=dedupe_key,
        )
        gateway = build_gateway(model_name)
        agent_runner.submit(trigger_input, gateway)
        return {
            "action": "accepted",
            "status": "queued",
            "source": source,
            "dedupe_key": dedupe_key,
            "thread_id": str(thread_id),
        }

    @app.get("/api/tasks/status")
    def task_status(dedupe_key: str) -> dict[str, object]:
        """Read durable progress for a synchronous or background submission."""

        with session_scope(runtime_factory) as session:
            trigger = session.scalar(
                select(TriggerRecord).where(TriggerRecord.dedupe_key == dedupe_key)
            )
            if trigger is None:
                return {"dedupe_key": dedupe_key, "status": "queued"}
            run = session.scalar(
                select(RunRecord)
                .where(RunRecord.trigger_id == trigger.id)
                .order_by(RunRecord.attempt.desc())
            )
            if run is None:
                outbox = session.scalar(
                    select(OutboxMessage).where(
                        OutboxMessage.dedupe_key == f"trigger.received:{dedupe_key}"
                    )
                )
                clarification = session.scalar(
                    select(DeliveryRecord).where(
                        DeliveryRecord.task_id == UUID(int=0),
                        DeliveryRecord.idempotency_key == f"clarify:{dedupe_key}",
                    )
                )
                if outbox and outbox.status == "PUBLISHED":
                    state = "ignored" if trigger.filter_decision == "ignored" else "completed"
                    return {
                        "dedupe_key": dedupe_key,
                        "trigger_id": str(trigger.id),
                        "status": state,
                        "text": clarification.payload_ref if clarification else None,
                    }
                state = "processing" if outbox and outbox.status == "CLAIMED" else "queued"
                return {
                    "dedupe_key": dedupe_key,
                    "trigger_id": str(trigger.id),
                    "status": state,
                }
            task = session.get(TaskRecord, run.task_id)
            delivery = session.scalar(
                select(DeliveryRecord).where(DeliveryRecord.task_id == run.task_id)
            )
            return {
                "dedupe_key": dedupe_key,
                "trigger_id": str(trigger.id),
                "task_id": str(run.task_id),
                "run_id": str(run.id),
                "status": run.status,
                "text": delivery.payload_ref if delivery and delivery.status == "sent" else None,
                "error": run.error,
                "heartbeat_at": run.heartbeat_at.isoformat() if run.heartbeat_at else None,
                "lease_expires_at": (
                    run.lease_expires_at.isoformat() if run.lease_expires_at else None
                ),
                "task_status": task.status if task else None,
            }

    @app.get("/tasks")
    def list_tasks() -> list[dict[str, object]]:
        engine = create_engine_for_url(database_url)
        factory = create_session_factory(engine)
        with session_scope(factory) as session:
            rows = session.query(TaskRecord).order_by(TaskRecord.id).all()
            items: list[dict[str, object]] = []
            for row in rows:
                latest_run = session.scalar(
                    select(RunRecord)
                    .where(RunRecord.task_id == row.id)
                    .order_by(RunRecord.started_at.desc(), RunRecord.attempt.desc())
                )
                delivery = session.scalar(
                    select(DeliveryRecord).where(DeliveryRecord.task_id == row.id)
                )
                try:
                    thread_ids = json.loads(row.thread_ids)
                except (TypeError, json.JSONDecodeError):
                    thread_ids = []
                items.append(
                    {
                        "id": str(row.id),
                        "title": row.title,
                        "status": row.status,
                        "version": row.version,
                        "worker_id": str(row.worker_id) if row.worker_id else None,
                        "thread_ids": thread_ids,
                        "channel": delivery.channel if delivery else None,
                        "updated_at": (
                            latest_run.finished_at.isoformat()
                            if latest_run and latest_run.finished_at
                            else latest_run.started_at.isoformat()
                            if latest_run and latest_run.started_at
                            else None
                        ),
                    }
                )
            return items

    @app.get("/api/tasks/{task_id}/stream")
    def stream_task(task_id: UUID) -> StreamingResponse:
        """Stream durable task progress as Server-Sent Events."""

        def generate() -> Iterator[str]:
            deadline = time.monotonic() + 120
            last_sequence = 0
            while time.monotonic() < deadline:
                engine = create_engine_for_url(database_url)
                factory = create_session_factory(engine)
                try:
                    with session_scope(factory) as session:
                        task = session.get(TaskRecord, task_id)
                        if task is None:
                            payload: dict[str, object] = {
                                "task_id": str(task_id),
                                "status": "not_found",
                            }
                            yield f"event: error\ndata: {json.dumps(payload)}\n\n"
                            return
                        events = (
                            session.query(WorkerEventRecord)
                            .filter(
                                WorkerEventRecord.task_id == task_id,
                                WorkerEventRecord.sequence > last_sequence,
                            )
                            .order_by(WorkerEventRecord.sequence)
                            .all()
                        )
                        if events:
                            last_sequence = max(event.sequence for event in events)
                        run = (
                            session.query(RunRecord)
                            .filter(RunRecord.task_id == task_id)
                            .order_by(RunRecord.attempt.desc())
                            .first()
                        )
                        payload = {
                            "task_id": str(task_id),
                            "task_status": task.status,
                            "run_status": run.status if run else None,
                            "run_id": str(run.id) if run else None,
                            "events": [
                                {
                                    "sequence": event.sequence,
                                    "type": event.event_type,
                                    "status": event.status,
                                    "summary": event.summary,
                                }
                                for event in events
                            ],
                        }
                        yield f"event: progress\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"
                        if task.status in {"succeeded", "failed", "cancelled"}:
                            yield f"event: done\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"
                            return
                finally:
                    engine.dispose()
                time.sleep(0.5)

        return StreamingResponse(
            generate(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.get("/api/tasks/{task_id}")
    def task_detail(task_id: UUID) -> dict[str, object]:
        engine = create_engine_for_url(database_url)
        factory = create_session_factory(engine)
        with session_scope(factory) as session:
            task = session.get(TaskRecord, task_id)
            if task is None:
                raise HTTPException(status_code=404, detail="task not found")
            worker = session.get(WorkerRecord, task.worker_id) if task.worker_id else None
            runs = session.query(RunRecord).filter(RunRecord.task_id == task_id).all()
            events = (
                session.query(WorkerEventRecord)
                .filter(WorkerEventRecord.task_id == task_id)
                .order_by(WorkerEventRecord.sequence)
                .all()
            )
            context = (
                session.query(WorkerContextRecord)
                .filter(WorkerContextRecord.task_id == task_id)
                .order_by(WorkerContextRecord.revision)
                .all()
            )
            from super_agent.storage.models import SubscriptionRecord, ToolRunRecord

            subscriptions = (
                session.query(SubscriptionRecord)
                .filter(SubscriptionRecord.worker_id == task.worker_id)
                .all()
                if task.worker_id
                else []
            )
            tool_runs = (
                session.query(ToolRunRecord)
                .filter(ToolRunRecord.task_id == task_id)
                .order_by(ToolRunRecord.started_at)
                .all()
            )
            return {
                "subscriptions": [
                    {
                        "thread_id": str(sub.thread_id),
                        "status": sub.status,
                        "worker_id": str(sub.worker_id),
                    }
                    for sub in subscriptions
                ],
                "tool_runs": [
                    {
                        "id": str(tr.id),
                        "tool_name": tr.tool_name,
                        "status": tr.status,
                        "result_text": tr.result_text,
                        "error": tr.error,
                        "started_at": tr.started_at.isoformat() if tr.started_at else None,
                        "finished_at": tr.finished_at.isoformat() if tr.finished_at else None,
                    }
                    for tr in tool_runs
                ],
                "task": {
                    "id": str(task.id),
                    "title": task.title,
                    "status": task.status,
                    "version": task.version,
                },
                "worker": (
                    {
                        "id": str(worker.id),
                        "status": worker.status,
                        "version": worker.version,
                    }
                    if worker
                    else None
                ),
                "runs": [
                    {
                        "id": str(run.id),
                        "status": run.status,
                        "attempt": run.attempt,
                        "output_ref": run.output_ref,
                        "error": run.error,
                        "heartbeat_at": run.heartbeat_at.isoformat()
                        if run.heartbeat_at
                        else None,
                        "lease_expires_at": run.lease_expires_at.isoformat()
                        if run.lease_expires_at
                        else None,
                    }
                    for run in runs
                ],
                "events": [
                    {
                        "id": str(event.id),
                        "type": event.event_type,
                        "status": event.status,
                        "summary": event.summary,
                        "sequence": event.sequence,
                    }
                    for event in events
                ],
                "context": [
                    {
                        "id": str(entry.id),
                        "role": entry.role,
                        "content": entry.content,
                        "revision": entry.revision,
                    }
                    for entry in context
                ],
            }

    @app.delete("/api/tasks/{task_id}")
    def delete_task(task_id: UUID) -> dict[str, object]:
        """Delete one finished task and its owned records from the console.

        Live work must be stopped first: deleting a running Task would orphan
        an executing Worker, so the request is refused instead.
        """

        from super_agent.storage.models import SubscriptionRecord, ToolRunRecord

        with session_scope(runtime_factory) as session:
            row = session.get(TaskRecord, task_id)
            if row is None:
                raise HTTPException(status_code=404, detail="task not found")
            runs = session.query(RunRecord).filter(RunRecord.task_id == task_id).all()
            if any(r.status in {"pending", "running", "cancel_requested"} for r in runs):
                raise HTTPException(
                    status_code=409, detail="task is still running; stop it before deleting"
                )
            for model in (
                WorkerEventRecord,
                WorkerContextRecord,
                ToolRunRecord,
                DeliveryRecord,
                RunRecord,
            ):
                for record in session.query(model).filter(model.task_id == task_id).all():
                    session.delete(record)
            if row.worker_id is not None:
                for sub in (
                    session.query(SubscriptionRecord)
                    .filter(SubscriptionRecord.worker_id == row.worker_id)
                    .all()
                ):
                    session.delete(sub)
                worker = session.get(WorkerRecord, row.worker_id)
                if worker is not None:
                    session.delete(worker)
            session.delete(row)
            return {"id": str(task_id), "deleted": True}

    @app.patch("/api/tasks/{task_id}/title")
    def rename_task(task_id: UUID, request: TaskTitleRequest) -> dict[str, object]:
        """Rename one task from the console; domain state stays authoritative."""

        with session_scope(runtime_factory) as session:
            row = session.get(TaskRecord, task_id)
            if row is None:
                raise HTTPException(status_code=404, detail="task not found")
            row.title = request.title
            row.version += 1
            return {"id": str(row.id), "title": row.title, "version": row.version}

    @app.post("/tasks/{task_id}/stop")
    def stop_task(task_id: UUID) -> dict[str, object]:
        return _update_task(database_url, task_id, "cancelled")

    @app.post("/tasks/{task_id}/resume")
    def resume_task(task_id: UUID) -> dict[str, object]:
        return _update_task(database_url, task_id, "running")

    @app.get("/api/events")
    def list_events(limit: int = 100) -> dict[str, object]:
        """Recent Trigger stream with two-level filter decisions (article §3.1)."""

        bounded = max(1, min(limit, 500))
        with session_scope(runtime_factory) as session:
            rows = (
                session.query(TriggerRecord)
                .order_by(TriggerRecord.received_at.desc())
                .limit(bounded)
                .all()
            )
            items = []
            for row in reversed(rows):
                run = session.scalar(
                    select(RunRecord).where(RunRecord.trigger_id == row.id)
                )
                items.append(
                    {
                        "id": str(row.id),
                        "source": row.source,
                        "actor": row.actor,
                        "thread_id": str(row.thread_id),
                        "text": row.payload_ref,
                        "filter_decision": row.filter_decision,
                        "occurred_at": row.occurred_at.isoformat(),
                        "received_at": row.received_at.isoformat(),
                        "task_id": str(run.task_id) if run else None,
                    }
                )
            return {"events": items}

    @app.get("/api/threads")
    def list_threads() -> dict[str, object]:
        """Threads grouped from Triggers with their subscription fan-out (§3.2)."""

        with session_scope(runtime_factory) as session:
            from super_agent.storage.models import SubscriptionRecord

            rows = (
                session.query(TriggerRecord)
                .order_by(TriggerRecord.received_at.desc())
                .limit(2000)
                .all()
            )
            counts: dict[str, int] = {}
            sources_by_thread: dict[str, list[str]] = {}
            latest: dict[str, TriggerRecord] = {}
            for row in rows:
                key = str(row.thread_id)
                counts[key] = counts.get(key, 0) + 1
                bucket = sources_by_thread.setdefault(key, [])
                if row.source not in bucket:
                    bucket.append(row.source)
                latest.setdefault(key, row)
            workers_by_thread: dict[str, list[dict[str, str]]] = {}
            for sub in session.query(SubscriptionRecord).all():
                workers_by_thread.setdefault(str(sub.thread_id), []).append(
                    {"worker_id": str(sub.worker_id), "status": sub.status}
                )
            threads = [
                {
                    "thread_id": key,
                    "sources": sources_by_thread[key],
                    "event_count": counts[key],
                    "last_text": latest[key].payload_ref,
                    "last_at": latest[key].received_at.isoformat(),
                    "workers": workers_by_thread.get(key, []),
                }
                for key in sorted(
                    counts, key=lambda k: latest[k].received_at, reverse=True
                )
            ]
            return {"threads": threads}

    @app.get("/api/router/transcript")
    def router_transcript(limit: int = 60) -> dict[str, object]:
        """Resident Router global memory window (article §2.1)."""

        bounded = max(1, min(limit, 200))
        with session_scope(runtime_factory) as session:
            from super_agent.storage.models import RouterMessageRecord

            rows = (
                session.query(RouterMessageRecord)
                .order_by(RouterMessageRecord.sequence.desc())
                .limit(bounded)
                .all()
            )
            return {
                "messages": [
                    {
                        "id": str(row.id),
                        "sequence": row.sequence,
                        "role": row.role,
                        "content": row.content,
                        "created_at": row.created_at.isoformat(),
                    }
                    for row in reversed(rows)
                ]
            }

    @app.get("/api/outbox")
    def outbox_stats(limit: int = 100) -> dict[str, object]:
        """Durable event queue depth by status plus the actual queued rows.

        Counts alone left the operator unable to answer "10 pending what?".
        Every non-published message is now listed with its topic, the message
        text, attempt count, backoff and last error, so a stuck queue is
        diagnosable from the console instead of from SQLite.
        """

        bounded = max(1, min(limit, 500))
        now = datetime.now(UTC)
        with session_scope(runtime_factory) as session:
            messages = session.query(OutboxMessage).all()
            status_counts: dict[str, int] = {}
            for message in messages:
                status_counts[message.status] = status_counts.get(message.status, 0) + 1
            queued = (
                session.query(OutboxMessage)
                .filter(OutboxMessage.status.in_({"PENDING", "CLAIMED", "FAILED"}))
                .order_by(OutboxMessage.created_at)
                .limit(bounded)
                .all()
            )
            items: list[dict[str, object]] = []
            for message in queued:
                items.append(
                    {
                        "id": str(message.id),
                        "topic": message.topic,
                        "dedupe_key": message.dedupe_key,
                        "status": message.status,
                        "attempts": message.attempts,
                        "text": _outbox_text(message),
                        "last_error": message.last_error,
                        "created_at": message.created_at.isoformat()
                        if message.created_at
                        else None,
                        "available_at": message.available_at.isoformat()
                        if message.available_at
                        else None,
                        "retry_in_seconds": (
                            round((_as_utc(message.available_at) - now).total_seconds(), 1)
                            if message.status == "PENDING" and message.available_at
                            else None
                        ),
                        "claimed_at": message.claimed_at.isoformat()
                        if message.claimed_at
                        else None,
                    }
                )
            return {
                "pending": status_counts.get("PENDING", 0),
                "claimed": status_counts.get("CLAIMED", 0),
                "published": status_counts.get("PUBLISHED", 0),
                "failed": status_counts.get("FAILED", 0),
                "topics": sorted({message.topic for message in messages}),
                "items": items,
            }

    @app.get("/api/runtime/summary")
    def runtime_summary() -> dict[str, object]:
        """One polling snapshot for the mission-control header and rails."""

        with session_scope(runtime_factory) as session:
            from super_agent.storage.models import SubscriptionRecord, ToolRunRecord

            tasks = session.query(TaskRecord).all()
            task_counts: dict[str, int] = {}
            for row in tasks:
                task_counts[row.status] = task_counts.get(row.status, 0) + 1
            active_runs = (
                session.query(RunRecord)
                .filter(RunRecord.status.in_({"pending", "running", "cancel_requested"}))
                .count()
            )
            active_workers = (
                session.query(WorkerRecord)
                .filter(WorkerRecord.status.in_({"starting", "running", "waiting"}))
                .count()
            )
            active_subs = (
                session.query(SubscriptionRecord)
                .filter(SubscriptionRecord.status == "active")
                .count()
            )
            tool_active = (
                session.query(ToolRunRecord)
                .filter(ToolRunRecord.status == "running")
                .count()
            )
            outbox_rows = session.query(OutboxMessage).all()
            outbox_counts: dict[str, int] = {}
            for message in outbox_rows:
                outbox_counts[message.status] = outbox_counts.get(message.status, 0) + 1
            findings = PersistentRunWatchdog().inspect(session)
            return {
                "tasks": task_counts,
                "task_total": len(tasks),
                "active_runs": active_runs,
                "active_workers": active_workers,
                "active_subscriptions": active_subs,
                "active_tool_runs": tool_active,
                "outbox": {
                    "pending": outbox_counts.get("PENDING", 0),
                    "claimed": outbox_counts.get("CLAIMED", 0),
                    "published": outbox_counts.get("PUBLISHED", 0),
                    "failed": outbox_counts.get("FAILED", 0),
                },
                # Counts alone could not answer "which tasks are running?".
                "active_run_details": _active_run_details(session),
                "watchdog": (
                    "healthy"
                    if all(f.kind.value == "healthy" for f in findings)
                    else "attention"
                ),
                "batch_limit": active_settings.router_batch_limit,
                "speculative_enabled": active_settings.speculative_enabled,
                "watchdog_interval_seconds": active_settings.watchdog_interval_seconds,
                # Why the background loop is not progressing, if it isn't.
                "worker_last_error": durable_worker.last_error,
            }

    @app.post("/api/speculations/{speculation_id}/discard")
    def discard_speculation(speculation_id: UUID) -> dict[str, object]:
        """Drop one speculative draft without delivering it (article §3.4)."""


        for holder in app.state.speculation_runners.values():
            if holder.get(speculation_id) is not None:
                holder.discard(speculation_id)
                return {"status": "discarded"}
        raise HTTPException(status_code=404, detail="speculation not found")

    @app.get("/health", response_model=HealthStatus)
    def health() -> HealthStatus:
        return HealthStatus(
            status="ok",
            service=active_settings.app_name,
            version=active_settings.version,
        )

    _register_speculation_routes(app, runtime_factory, build_gateway, agent_tools)
    _register_device_routes(app, runtime_factory)
    return app


def _as_utc(value: datetime | None) -> datetime:
    """Normalize a stored timestamp so age arithmetic never mixes timezones."""

    if value is None:
        return datetime.now(UTC)
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _outbox_text(message: OutboxMessage) -> str:
    """Best-effort readable summary of a queued event payload."""

    try:
        payload = json.loads(message.payload)
    except (TypeError, json.JSONDecodeError):
        return ""
    if not isinstance(payload, dict):
        return ""
    inner = payload.get("payload")
    if isinstance(inner, dict) and isinstance(inner.get("message"), str):
        return str(inner["message"])[:200]
    if message.topic == "tool.completed":
        tool = payload.get("tool_name")
        status = payload.get("status")
        return f"后台工具 {tool} 结束：{status}"
    trigger = payload.get("trigger")
    if isinstance(trigger, dict) and isinstance(trigger.get("payload_ref"), str):
        return str(trigger["payload_ref"])[:200]
    return ""


def _active_run_details(session: Session) -> list[dict[str, object]]:
    """Return the concrete live Runs behind the ``active_runs`` counter."""

    rows = (
        session.query(RunRecord)
        .filter(RunRecord.status.in_({"pending", "running", "cancel_requested"}))
        .order_by(RunRecord.started_at.desc())
        .limit(50)
        .all()
    )
    now = datetime.now(UTC)
    details: list[dict[str, object]] = []
    for run in rows:
        task = session.get(TaskRecord, run.task_id)
        worker = session.get(WorkerRecord, task.worker_id) if task and task.worker_id else None
        heartbeat = _as_utc(run.heartbeat_at or run.started_at)
        details.append(
            {
                "run_id": str(run.id),
                "task_id": str(run.task_id),
                "task_title": task.title if task else None,
                "task_status": task.status if task else None,
                "worker_id": str(worker.id) if worker else None,
                "worker_status": worker.status if worker else None,
                "run_status": run.status,
                "attempt": run.attempt,
                "started_at": run.started_at.isoformat() if run.started_at else None,
                "heartbeat_age_seconds": round((now - heartbeat).total_seconds(), 1),
                "lease_expires_at": (
                    run.lease_expires_at.isoformat() if run.lease_expires_at else None
                ),
                "error": run.error,
            }
        )
    return details


def _update_task(database_url: str, task_id: UUID, status: str) -> dict[str, object]:
    """Apply an operator stop/resume without corrupting terminal outcomes.

    A finished Task must never be resurrected into ``running``: its Runs are
    already terminal, so the console derives its state from them. Stop only
    affects Tasks with live Runs; resume restores the previous status instead
    of inventing one, and requeues work only when a retryable Run exists.
    """

    engine = create_engine_for_url(database_url)
    factory = create_session_factory(engine)
    with session_scope(factory) as session:
        row = session.get(TaskRecord, task_id)
        if row is None:
            raise HTTPException(status_code=404, detail="task not found")
        runs = session.query(RunRecord).filter(RunRecord.task_id == task_id).all()
        live = [r for r in runs if r.status in {"pending", "running", "cancel_requested"}]
        previous = row.status

        if status == "cancelled":
            if not live:
                # Nothing is executing: report the terminal truth untouched.
                return {
                    "id": str(row.id),
                    "status": row.status,
                    "version": row.version,
                    "changed": False,
                    "reason": "no active run to stop",
                }
            for run in live:
                if run.status != "cancel_requested":
                    RunLeaseService(session).request_cancel(run.id)
            row.status = "cancelled"
            row.version += 1
            return {
                "id": str(row.id),
                "status": row.status,
                "version": row.version,
                "changed": True,
                "previous_status": previous,
            }

        # resume: only meaningful for a cancelled Task.
        if previous != "cancelled":
            return {
                "id": str(row.id),
                "status": previous,
                "version": row.version,
                "changed": False,
                "reason": "task is not cancelled",
            }
        latest = max(runs, key=lambda r: (r.attempt, str(r.id)), default=None)
        # A Run still holding a cancel request is live work: resuming returns
        # the Task to running so the Worker can pick it up again.
        still_live = latest is not None and latest.status in {
            "pending",
            "running",
            "cancel_requested",
        }
        retryable = latest is not None and latest.status in {"failed", "timed_out", "cancelled"}
        if still_live or retryable:
            row.status = TaskStatus.RUNNING.value
        elif latest is not None and latest.status == "succeeded":
            row.status = TaskStatus.SUCCEEDED.value
        elif latest is not None and latest.status == "failed":
            row.status = TaskStatus.FAILED.value
        else:
            row.status = TaskStatus.PENDING.value
        row.version += 1
        return {
            "id": str(row.id),
            "status": row.status,
            "version": row.version,
            "changed": True,
            "requeued": retryable,
            "resumed_live": still_live,
        }


# Minimal task page used only when web/index.html is missing from the install.
# It is deliberately tiny: the real page is the product, this is the safety net
# so \`/\` never degrades into the operator console.
_TASK_PANEL_FALLBACK = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Super Agent</title>
<style>
body{margin:0;background:#fbfbfd;color:#1d1d1f;font:15px/1.6 -apple-system,"PingFang SC","Segoe UI",system-ui,sans-serif}
main{max-width:680px;margin:0 auto;padding:64px 22px}
h1{font-size:34px;letter-spacing:-.02em;margin:0 0 10px}
p.sub{color:#6e6e73;margin:0 0 26px}
form{display:flex;gap:10px;align-items:flex-end;background:#fff;border:1px solid #e6e6ea;border-radius:18px;padding:12px 12px 12px 18px}
textarea{flex:1;border:0;outline:none;resize:none;font:inherit;padding:8px 0}
button{border:0;border-radius:12px;background:#0071e3;color:#fff;padding:9px 18px;font-weight:550;cursor:pointer}
#out{margin-top:26px}
article{background:#fff;border:1px solid #e6e6ea;border-radius:16px;padding:16px 18px;margin-bottom:10px}
.st{font-size:11.5px;font-weight:600;padding:3px 9px;border-radius:99px;background:#eaf7ef;color:#1f9d55}
.ask{color:#6e6e73;font-size:13.5px;margin:8px 0}
.verdict{font-size:16px}
a.file{display:inline-block;margin-top:10px;font-size:13px;color:#0071e3;text-decoration:none}
</style></head><body><main>
<h1>\u4F60\u60F3\u8BA9\u5B83\u505A\u4EC0\u4E48\uFF1F</h1>
<p class="sub">\u8BF4\u4E00\u53E5\u8BDD\u5C31\u884C\u3002\u5B83\u4F1A\u771F\u7684\u628A\u4E1C\u897F\u505A\u51FA\u6765\u7ED9\u4F60\u3002</p>
<form id="f"><textarea id="m" rows="1" placeholder="\u4F8B\u5982\uFF1A\u505A\u4E00\u4E2A\u4ECB\u7ECD CSS Grid \u7684\u7F51\u9875\uFF0C\u5B58\u6210 grid.html"></textarea><button>\u5F00\u59CB</button></form>
<div id="out"></div>
<p style="color:#a1a1a6;font-size:12px;margin-top:40px">\u754C\u9762\u6587\u4EF6\u672A\u5B89\u88C5 \u00B7 <a href="/ops">\u8FD0\u7EF4</a></p>
</main>
<script>
const $=id=>document.getElementById(id);
const state={pending:["\u6392\u961F\u4E2D"],running:["\u6B63\u5728\u505A"],waiting:["\u7B49\u4F60\u786E\u8BA4"],succeeded:["\u505A\u597D\u4E86"],failed:["\u6CA1\u505A\u6210"],cancelled:["\u5DF2\u53D6\u6D88"],timed_out:["\u8D85\u65F6\u4E86"]};
function card(r){
  const s=(r.state&&r.state.text)||((state[r.status]||[r.status])[0]);
  const files=(r.artifacts||[]).map(a=>'<a class="file" href="/files/'+a.path.split("/").map(encodeURIComponent).join("/")+'" target="_blank">'+a.name+'</a>').join(" ");
  return '<article><span class="st">'+s+'</span><div class="ask">'+(r.ask||r.title||"")+'</div><div class="verdict">'+(r.conclusion||"")+'</div>'+files+'</article>';
}
async function job(text){
  const ph={task_id:"t"+Date.now(),ask:text,title:text,status:"pending",state:{text:"\u6392\u961F\u4E2D"},conclusion:"\u5DF2\u7ECF\u6536\u5230\u3002",artifacts:[]};
  $("out").insertAdjacentHTML("afterbegin",card(ph));
  const r=await fetch("/api/tasks/async",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({message:text})});
  const d=await r.json();
  for(let i=0;i<600;i++){
    await new Promise(s=>setTimeout(s,1000));
    const st=await (await fetch("/api/tasks/status?dedupe_key="+encodeURIComponent(d.dedupe_key))).json();
    if(st.task_id){
      const res=await (await fetch("/api/tasks/"+st.task_id+"/result")).json();
      const first=$("out").firstElementChild; if(first) first.remove();
      $("out").insertAdjacentHTML("afterbegin",card(res));
      if(["succeeded","failed","cancelled","timed_out"].includes(res.status)) return;
    }
  }
}
$("f").onsubmit=e=>{e.preventDefault();const v=$("m").value.trim();if(!v)return;$("m").value="";job(v);};
</script></body></html>"""

_CONTROL_PANEL_HTML = r"""
<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Super AI Agent 控制台</title>
  <style>
    :root { --ink:#17231f; --muted:#64736d; --paper:#f5f7f2; --card:#ffffff; --accent:#d86b38; --accent2:#1c7867; --line:#dbe4de; }
    * { box-sizing:border-box; } body { margin:0; color:var(--ink); background:radial-gradient(circle at 10% 0%,#dceee3 0,transparent 36%),linear-gradient(135deg,#f5f7f2,#edf2ed); font:15px/1.5 ui-sans-serif,Segoe UI,sans-serif; }
    .shell { min-height:100vh; max-width:1180px; margin:auto; padding:34px 22px 52px; }
    header { display:flex; justify-content:space-between; gap:24px; align-items:flex-start; margin-bottom:28px; }
    .eyebrow { text-transform:uppercase; letter-spacing:.16em; color:var(--accent2); font-weight:700; font-size:12px; }
    h1 { font:700 clamp(34px,6vw,66px)/.98 Georgia,serif; letter-spacing:-.045em; max-width:650px; margin:10px 0; }
    .lede { color:var(--muted); max-width:620px; margin:0; }
    .status { background:#173b32; color:#e9fff5; border-radius:18px; padding:16px 18px; min-width:240px; box-shadow:0 14px 35px #173b3226; }
    .status strong { display:block; font-size:18px; margin-bottom:4px; }
    .grid { display:grid; grid-template-columns:minmax(0,1.45fr) minmax(280px,.75fr); gap:20px; align-items:start; }
    .card { background:#ffffffd9; border:1px solid var(--line); border-radius:24px; padding:22px; box-shadow:0 18px 50px #24483b12; backdrop-filter:blur(12px); }
    .card h2 { margin:0 0 14px; font-size:19px; }
    textarea, select, input { width:100%; border:1px solid var(--line); border-radius:14px; padding:14px; background:#fbfdfb; color:var(--ink); font:inherit; }
    textarea { min-height:150px; resize:vertical; }
    .row { display:flex; gap:10px; align-items:center; margin-top:12px; }
    button { border:0; border-radius:999px; padding:12px 20px; cursor:pointer; color:#fff; background:var(--accent); font-weight:700; }
    button:disabled { opacity:.55; cursor:wait; }
    .ghost { color:var(--accent2); background:#e5f1eb; }
    .answer { white-space:pre-wrap; background:#f2f6f1; border-radius:14px; padding:16px; min-height:80px; margin-top:16px; }
    .meta { color:var(--muted); font-size:13px; }
    .pill { display:inline-block; background:#e8efe9; border-radius:999px; padding:4px 9px; margin:4px 4px 0 0; font-size:12px; }
    .tasks { display:grid; gap:8px; } .task { border:1px solid var(--line); border-radius:12px; padding:10px; }
    @media (max-width:760px) { header,.grid { display:block; } .status { margin-top:18px; } .card { margin-top:18px; } }
  </style>
</head>
<body><main class="shell">
  <header><div><div class="eyebrow">Local Agent Workspace</div><h1>让 Super AI Agent 真正开始工作。</h1><p class="lede">从这里选择 Magpie 托管模型，发送第一条消息，并查看任务状态。所有请求都留在你的本机网关配置内。</p></div><div class="status"><strong id="status">正在连接…</strong><span id="status-detail">检查 Super AI Agent 与 Magpie</span></div></header>
  <section class="grid">
    <div class="card"><h2>Super Agent 任务</h2><label class="meta" for="model">Magpie 模型</label><select id="model"><option>加载模型中…</option></select><label class="meta" for="thread">Thread（同一 Thread 会续接同一 Worker）</label><div class="row"><input id="thread" aria-label="Thread ID" /><button class="ghost" id="new-thread">新 Thread</button></div><textarea id="message" placeholder="例如：读取 Agent.md 并总结目标"></textarea><div class="row"><button id="send">启动 Agent 任务</button><button class="ghost" id="send-bg">后台执行</button><button class="ghost" id="refresh">刷新模型</button></div><div class="answer" id="answer">提交后将经过 Trigger → Outbox → Router → Worker → Model → Delivery 完整链路。</div><div class="meta" id="usage"></div></div>
    <aside><div class="card"><h2>当前连接</h2><div id="connection" class="meta">加载中…</div><div id="models" style="margin-top:10px"></div></div><div class="card"><h2>任务时间线</h2><p class="meta">查看 Task、Worker、Run、事件和上下文。</p><button class="ghost" id="tasks-btn">刷新任务</button><button class="ghost" id="stop-task" disabled>停止当前任务</button><div class="tasks" id="tasks"></div><div class="answer" id="detail">选择一个任务查看详情。</div></div></aside>
  </section>
</main><script>
const $=id=>document.getElementById(id);
function ensureThread(){ let id=localStorage.getItem('super-agent-thread'); if(!id){ id=crypto.randomUUID(); localStorage.setItem('super-agent-thread',id); } $('thread').value=id; }
function newThread(){ const id=crypto.randomUUID(); localStorage.setItem('super-agent-thread',id); $('thread').value=id; $('answer').textContent='已切换到新 Thread。'; }
async function load(){ ensureThread(); try { const [cfg,mods,watch]=await Promise.all([fetch('/api/config').then(r=>r.json()),fetch('/api/models').then(r=>r.json()),fetch('/api/runtime/watchdog').then(r=>r.json())]); $('status').textContent='服务在线'; $('status-detail').textContent=mods.gateway.name+' · Watchdog '+watch.status; $('connection').textContent='Magpie: '+cfg.magpie_base_url+' · '+(cfg.model||'页面选择模型'); const list=mods.models||[]; $('model').innerHTML=list.map(m=>`<option value="${m.id}">${m.display_name||m.id}</option>`).join(''); $('models').innerHTML=list.map(m=>`<span class="pill">${m.id}</span>`).join(''); } catch(e){ $('status').textContent='连接失败'; $('status-detail').textContent=e.message; $('connection').textContent='请确认 Magpie 正在运行'; } }
async function send(background=false){ const button=background?$('send-bg'):$('send'); const message=$('message').value.trim(); if(!message)return; button.disabled=true; $('answer').textContent=background?'任务已进入 Outbox，后台 Agent 正在执行…':'Agent 正在路由和执行…'; try { const r=await fetch(background?'/api/tasks/async':'/api/tasks',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({message,model:$('model').value,thread_id:$('thread').value})}); const data=await r.json(); if(!r.ok) throw new Error(data.detail||'请求失败'); $('usage').textContent=`action：${data.action} · task：${data.task_id||data.dedupe_key||'未创建'}`; if(background) await poll(data.dedupe_key); else $('answer').textContent=data.text||data.action; tasks(); } catch(e){ $('answer').textContent='任务失败：'+e.message; } finally { button.disabled=false; } }
async function poll(key){ for(let i=0;i<120;i++){ await new Promise(resolve=>setTimeout(resolve,750)); const r=await fetch('/api/tasks/status?dedupe_key='+encodeURIComponent(key)); const d=await r.json(); if(d.status==='succeeded'||d.status==='completed'||d.status==='ignored'){ $('answer').textContent=d.text||'Agent 已完成'; $('usage').textContent=`status：${d.status} · task：${d.task_id||'未创建'}`; return; } if(d.status==='failed'){ throw new Error(d.error||'后台任务失败'); } } throw new Error('后台任务等待超时，请在任务时间线查看'); }
let selectedTask=null; let taskStream=null;
async function showTask(id){ selectedTask=id; $('stop-task').disabled=false; if(taskStream){ taskStream.close(); taskStream=null; } const r=await fetch('/api/tasks/'+id); const d=await r.json(); $('detail').textContent=JSON.stringify(d,null,2); if(d.task&&['pending','running','waiting'].includes(d.task.status)){ taskStream=new EventSource('/api/tasks/'+id+'/stream'); taskStream.addEventListener('progress',event=>{ $('detail').textContent=event.data; }); taskStream.addEventListener('done',()=>{ taskStream.close(); taskStream=null; tasks(); }); } }
async function stopSelected(){ if(!selectedTask)return; const r=await fetch('/tasks/'+selectedTask+'/stop',{method:'POST'}); const d=await r.json(); $('usage').textContent='任务状态：'+d.status; showTask(selectedTask); tasks(); }
async function tasks(){ const r=await fetch('/tasks'); const data=await r.json(); $('tasks').innerHTML=data.length?data.map(t=>'<div class="task" onclick="showTask(\''+t.id+'\')"><b>'+t.title+'</b><div class="meta">'+t.status+' · '+t.id+'</div></div>').join(''):'<div class="meta">暂无任务</div>'; }
$('send').onclick=()=>send(false); $('send-bg').onclick=()=>send(true); $('new-thread').onclick=newThread; $('refresh').onclick=load; $('tasks-btn').onclick=tasks; $('stop-task').onclick=stopSelected; load(); tasks();
</script></body></html>
"""



# ---------------------------------------------------------------------------
# BYOM endpoint device routes (article §5.2). Appended after the control panel
# so existing routes stay untouched. The command queue lives in a per-app
# module-level store; durable device state lives in the devices table.
# ---------------------------------------------------------------------------


class DeviceRegisterRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=255)
    capabilities: list[str] = Field(default_factory=list, max_length=16)


class DeviceCommandRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    capability: str = Field(min_length=1, max_length=64)
    payload: dict[str, object] = Field(default_factory=dict)
    ttl_seconds: float = Field(default=30.0, gt=0, le=60.0)


class DeviceResultRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    command_id: UUID
    output: str = Field(default="", max_length=65_536)
    error: str | None = Field(default=None, max_length=65_536)


def _register_device_routes(app: FastAPI, factory: sessionmaker[Session]) -> None:
    """Attach the device API using one shared in-memory command store."""

    store = DeviceCommandStore()
    # Exposed for operator tooling and tests; holds no durable state.
    app.state.device_command_store = store

    def service(session: Session) -> DeviceCommandService:
        return DeviceCommandService(session, store=store)

    def authenticate(session: Session, device_id: UUID, http_request: Request, body: bytes) -> None:
        try:
            service(session).authenticate_request(
                device_id,
                timestamp=http_request.headers.get("X-Device-Timestamp", ""),
                method=http_request.method,
                path=http_request.url.path,
                body=body,
                signature_hex=http_request.headers.get("X-Device-Signature", ""),
            )
        except AgentError as exc:
            raise HTTPException(status_code=401, detail=str(exc)) from exc

    @app.post("/api/devices/register", status_code=201)
    def register_device(request: DeviceRegisterRequest) -> dict[str, object]:
        """Register one endpoint; the secret is shown exactly once."""

        with session_scope(factory) as session:
            device_id, secret_hex = service(session).register_device(
                request.name, frozenset(request.capabilities)
            )
            return {
                "device_id": str(device_id),
                "secret": secret_hex,
                "capabilities": sorted(request.capabilities),
                "notice": "Store the secret in an environment variable now; it is never shown again.",
            }

    @app.post("/api/devices/{device_id}/heartbeat")
    async def device_heartbeat(device_id: UUID, http_request: Request) -> dict[str, object]:
        body = await http_request.body()
        with session_scope(factory) as session:
            authenticate(session, device_id, http_request, body)
            info = service(session).heartbeat(device_id)
            return {
                "device_id": str(info.device_id),
                "online": True,
                "last_seen_at": info.last_seen_at.isoformat() if info.last_seen_at else None,
            }

    @app.get("/api/devices/{device_id}/commands")
    async def device_commands(device_id: UUID, http_request: Request) -> dict[str, object]:
        body = await http_request.body()
        with session_scope(factory) as session:
            authenticate(session, device_id, http_request, body)
            try:
                commands = service(session).claim_pending(device_id)
            except AgentError as exc:
                raise HTTPException(status_code=403, detail=str(exc)) from exc
            return {
                "commands": [
                    {
                        "command_id": str(command.command_id),
                        "device_id": str(command.device_id),
                        "capability": command.capability,
                        "payload": command.payload,
                        "expires_at": command.expires_at.isoformat(),
                        "signature": command.signature,
                    }
                    for command in commands
                ]
            }

    @app.post("/api/devices/{device_id}/results")
    async def device_results(
        device_id: UUID, request: DeviceResultRequest, http_request: Request
    ) -> dict[str, object]:
        body = await http_request.body()
        with session_scope(factory) as session:
            authenticate(session, device_id, http_request, body)
            svc = service(session)
            issued_device = svc.store.device_for(request.command_id)
            if issued_device != device_id:
                raise HTTPException(status_code=403, detail="command belongs to another device")
            try:
                result = svc.submit_result(request.command_id, request.output, request.error)
            except AgentError as exc:
                raise HTTPException(status_code=404, detail=str(exc)) from exc
            return {"command_id": str(result.command_id), "status": "recorded"}

    @app.post("/api/devices/{device_id}/commands/enqueue")
    def enqueue_device_command(
        device_id: UUID, request: DeviceCommandRequest
    ) -> dict[str, object]:
        """Operator-side enqueue used by tools and tests; not daemon-facing."""

        with session_scope(factory) as session:
            try:
                command = service(session).enqueue_command(
                    device_id,
                    request.capability,
                    request.payload,
                    ttl_seconds=request.ttl_seconds,
                )
            except AgentError as exc:
                raise HTTPException(status_code=403, detail=str(exc)) from exc
            return {"command_id": str(command.command_id), "status": "queued"}

    @app.get("/api/devices/{device_id}/results/{command_id}")
    def device_result(device_id: UUID, command_id: UUID) -> dict[str, object]:
        """Operator-side result polling for tools and review surfaces."""

        with session_scope(factory) as session:
            svc = service(session)
            if svc.store.device_for(command_id) != device_id:
                raise HTTPException(status_code=404, detail="command not found for device")
            result = svc.get_result(command_id)
            if result is None:
                return {"command_id": str(command_id), "status": "pending"}
            return {
                "command_id": str(result.command_id),
                "status": "completed",
                "output": result.output,
                "error": result.error,
                "submitted_at": result.submitted_at.isoformat(),
            }

    @app.post("/api/devices/{device_id}/revoke")
    def revoke_device(device_id: UUID) -> dict[str, object]:
        """Revoke a device so its future heartbeats and commands are denied."""

        with session_scope(factory) as session:
            registry = PersistentDeviceRegistry(session)
            if registry.get(device_id) is None:
                raise HTTPException(status_code=404, detail="device not found")
            registry.revoke(device_id)
            return {"device_id": str(device_id), "revoked": True}

    @app.post("/api/devices/{device_id}/restore")
    def restore_device(device_id: UUID) -> dict[str, object]:
        """Undo a revocation; the device keeps its original secret."""

        with session_scope(factory) as session:
            row = session.get(DeviceRecord, device_id)
            if row is None:
                raise HTTPException(status_code=404, detail="device not found")
            row.revoked = 0
            session.flush()
            return {"device_id": str(device_id), "revoked": False}

    @app.get("/api/devices")
    def list_devices() -> dict[str, object]:
        """List devices with online status; never includes secret material."""

        with session_scope(factory) as session:
            svc = service(session)
            devices = svc.list_devices()
            return {
                "devices": [
                    {
                        "device_id": str(info.device_id),
                        "name": info.name,
                        "capabilities": sorted(info.capabilities),
                        "revoked": info.revoked,
                        "online": svc.is_online(info.device_id),
                        "last_seen_at": (
                            info.last_seen_at.isoformat() if info.last_seen_at else None
                        ),
                    }
                    for info in devices
                ]
            }


class TaskTitleRequest(BaseModel):
    """Console-side task rename payload."""

    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=500)


class SpeculationRequest(BaseModel):
    """One bounded, side-effect-free background speculation."""

    model_config = ConfigDict(extra="forbid")

    message: str = Field(min_length=1, max_length=8_000)
    model: str | None = None
    thread_id: UUID | None = None
    ttl_seconds: float = Field(default=300.0, gt=0)


def _register_speculation_routes(
    app: FastAPI,
    factory: sessionmaker[Session],
    build_gateway: object,
    tools: ToolRegistry,
) -> None:
    """Expose article §3.4 speculative execution with human-gated delivery."""

    runner_holder: dict[str, SpeculativeRunner] = {}
    app.state.speculation_runners = runner_holder

    def _runner(model_name: str | None) -> SpeculativeRunner:
        key = model_name or "__default__"
        if key not in runner_holder:
            gateway = build_gateway(model_name)  # type: ignore[operator]
            runner_holder[key] = SpeculativeRunner(
                factory,
                gateway,
                DeliveryService([CliChannelAdapter()]),
                tools=tools,
            )
        return runner_holder[key]

    @app.post("/api/speculations")
    def submit_speculation(request: SpeculationRequest) -> dict[str, object]:
        runner = _runner(request.model)
        thread_id = request.thread_id or uuid4()
        trigger = TriggerInput(
            source="web",
            actor="speculation",
            thread_id=thread_id,
            payload={"message": request.message, "model": request.model},
            occurred_at=datetime.now(UTC),
            dedupe_key=f"speculation:{thread_id}:{request.message}",
        )
        speculation_id = runner.submit(trigger, ttl_seconds=request.ttl_seconds)
        return {"speculation_id": str(speculation_id), "status": "running"}

    @app.get("/api/speculations")
    def list_speculations() -> dict[str, object]:
        items = []
        for runner in runner_holder.values():
            for outcome in runner.pending():
                items.append(
                    {
                        "speculation_id": str(outcome.speculation_id),
                        "status": outcome.status,
                        "text": outcome.text,
                    }
                )
        return {"speculations": items}

    @app.post("/api/speculations/{speculation_id}/promote")
    def promote_speculation(speculation_id: UUID) -> dict[str, object]:
        for runner in runner_holder.values():
            if runner.get(speculation_id) is not None:
                result = runner.promote(speculation_id)
                return {"status": result.status.value, "body": result.body}
        raise HTTPException(status_code=404, detail="speculation not found")

def _speculation_gateway(
    settings: Settings, build_gateway: Callable[[str | None], ModelGateway]
) -> ModelGateway:
    """Gateway for background speculations; requires a configured model."""

    if not settings.model_name:
        raise AgentError("set SUPER_AGENT_MODEL before enabling speculative execution")
    return build_gateway(settings.model_name)
