"""Command-line adapter for local development and health checks."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from threading import Event
from uuid import UUID, uuid4

import uvicorn

from super_agent import __version__
from super_agent.adapters.models.magpie import MagpieModelGateway
from super_agent.adapters.models.openai_compatible import OpenAICompatibleGateway
from super_agent.api import create_app
from super_agent.application.async_tools import DurableToolRunner
from super_agent.application.background import DurableOutboxWorker
from super_agent.application.orchestrator import AgentOrchestrator
from super_agent.config import Settings
from super_agent.daemon.agent import DaemonConfig, EndpointDaemon
from super_agent.daemon.registry import PersistentDeviceRegistry
from super_agent.domain.health import HealthStatus
from super_agent.errors import AgentError
from super_agent.ingress.schemas import TriggerInput
from super_agent.ports.model import ChatMessage, MessageRole, ModelGateway, ModelRequest
from super_agent.skills.installer import install_skill, uninstall_skill
from super_agent.skills.loader import SkillRegistry
from super_agent.storage.database import (
    create_engine_for_url,
    create_schema,
    create_session_factory,
    session_scope,
)
from super_agent.tools.loading import build_agent_tools
from super_agent.tools.recall import RecallTool
from super_agent.watchdog import PersistentRunWatchdog
from super_agent.watchdog.rules import WatchdogFinding
from super_agent.watchdog.session import WatchdogSession


def _submit_payload(
    message: str, model_name: str | None, skills: list[str] | None
) -> dict[str, object]:
    """Build a trigger payload carrying the skills the user asked for."""

    payload: dict[str, object] = {"message": message, "model": model_name}
    chosen = [name.strip() for name in (skills or []) if name and name.strip()]
    if chosen:
        payload["skills"] = chosen
    return payload


def _skill_library(settings: Settings) -> Path:
    """The folder installs land in: the first configured skill root."""

    from super_agent.tools.loading import resolve_skill_roots

    root = Path(settings.workspace_root).resolve()
    roots = resolve_skill_roots(settings, root)
    return roots[0] if roots else root / "skills"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="super-agent", description="Super AI Agent control CLI")
    parser.add_argument("--version", action="version", version=__version__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("health", help="print local service health")
    subparsers.add_parser("tools", help="list loaded built-in and user tools")
    skills_parser = subparsers.add_parser(
        "skills", help="list, install or remove skills (selectable playbooks)"
    )
    skills_sub = skills_parser.add_subparsers(dest="skills_command", required=False)
    skills_sub.add_parser("list", help="list installed skills (default)")
    skills_install = skills_sub.add_parser(
        "install", help="copy a skill from a folder, a git URL or owner/repo"
    )
    skills_install.add_argument("source", help="local folder, git URL, or owner/repo")
    skills_install.add_argument("--ref", default=None, help="branch or tag to clone")
    skills_install.add_argument(
        "--force", action="store_true", help="overwrite an existing skill folder"
    )
    skills_remove = skills_sub.add_parser("remove", help="remove an installed skill")
    skills_remove.add_argument("name")
    skills_sub.add_parser("path", help="print the library folder skills are installed into")
    subparsers.add_parser("model-check", help="call the configured OpenAI-compatible model")
    subparsers.add_parser("magpie-check", help="inspect a local Magpie gateway and model catalog")
    serve = subparsers.add_parser("serve", help="run the health API")
    serve.add_argument("--host", default=None)
    serve.add_argument("--port", type=int, default=None)
    worker = subparsers.add_parser("worker", help="poll the durable Outbox in a separate process")
    worker.add_argument("--poll-interval", type=float, default=0.5)
    worker.add_argument("--once", action="store_true", help="process at most one pending event")
    watchdog = subparsers.add_parser("watchdog", help="inspect and recover persisted Run health")
    watchdog.add_argument("--stalled-after", type=float, default=300.0)
    watchdog.add_argument("--max-attempts", type=int, default=3)
    watchdog.add_argument(
        "--model",
        action="store_true",
        help="add a model-driven WatchdogSession judgement using the configured gateway",
    )
    watchdog.add_argument(
        "--loop", type=float, default=None, metavar="N", help="tick every N seconds until Ctrl+C"
    )
    submit = subparsers.add_parser("submit", help="submit one task through the Agent runtime")
    submit.add_argument("message")
    submit.add_argument("--model", default=None)
    submit.add_argument("--thread-id", default=None)
    submit.add_argument(
        "--skill",
        action="append",
        default=None,
        help="name of a skill to apply to this task; repeatable",
    )
    daemon = subparsers.add_parser("daemon", help="run the stateless BYOM endpoint daemon")
    daemon.add_argument("--server-url", default="http://127.0.0.1:8000")
    daemon.add_argument("--device-id", required=True)
    daemon.add_argument(
        "--secret-env",
        default="SUPER_AGENT_DEVICE_SECRET",
        help="environment variable holding the device secret (never a plaintext argument)",
    )
    daemon.add_argument("--workspace", default=None)
    daemon.add_argument(
        "--allow", action="append", default=None, help="allowlisted executable; repeatable"
    )
    daemon.add_argument("--once", action="store_true", help="run one poll cycle and exit")
    device = subparsers.add_parser("device", help="manage registered BYOM devices")
    device_sub = device.add_subparsers(dest="device_command", required=True)
    device_register = device_sub.add_parser("register", help="register this machine once")
    device_register.add_argument("--name", required=True)
    device_register.add_argument(
        "--capability", action="append", default=None, help="device.read / device.exec; repeatable"
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    settings = Settings.from_env()

    if args.command == "health":
        payload = HealthStatus("ok", settings.app_name, settings.version)
        print(json.dumps(payload.to_dict(), sort_keys=True))
        return 0

    if args.command == "tools":
        _registry, _capabilities, report = build_agent_tools(settings)
        print(json.dumps(report.to_dict(), ensure_ascii=False, sort_keys=True))
        return 0

    if args.command == "skills":
        library = _skill_library(settings)
        action = getattr(args, "skills_command", None)
        if action == "path":
            print(library)
            return 0
        if action == "install":
            try:
                installed = install_skill(
                    args.source, library, ref=args.ref, force=args.force
                )
            except AgentError as exc:
                raise SystemExit(str(exc)) from exc
            report_payload = installed.to_dict()
            # A running server picks the folder up on its next read, so no
            # restart is needed; claiming otherwise sends people on a pointless
            # detour. Only a process that was already up would have missed it.
            report_payload["restart_required"] = False
            print(json.dumps(report_payload, ensure_ascii=False, sort_keys=True))
            if installed.installed:
                print(
                    "\n已安装。服务会自动识别，刷新页面即可使用。",
                    file=sys.stderr,
                )
            for folder, reason in installed.skipped:
                print(f"skipped {folder}: {reason}", file=sys.stderr)
            return 0 if installed.installed else 1
        if action == "remove":
            try:
                removed = uninstall_skill(args.name, library)
            except AgentError as exc:
                raise SystemExit(str(exc)) from exc
            print(json.dumps(
                {"removed": str(removed), "restart_required": False},
                ensure_ascii=False, sort_keys=True,
            ))
            print("\n已移除。刷新页面即可看到列表变化。", file=sys.stderr)
            return 0
        _registry, _capabilities, report = build_agent_tools(settings)
        catalogue = {
            "enabled": settings.skills_enabled,
            "library": str(library),
            "skills": [skill.to_dict() for skill in report.skills.skills],
            "failed": [list(item) for item in report.skills.failed],
        }
        print(json.dumps(catalogue, ensure_ascii=False, sort_keys=True))
        return 0

    if args.command == "model-check":
        if not settings.model_base_url or not settings.model_api_key or not settings.model_name:
            raise SystemExit(
                "set SUPER_AGENT_BASE_URL, SUPER_AGENT_API_KEY, and SUPER_AGENT_MODEL first"
            )
        gateway = OpenAICompatibleGateway(
            settings.model_base_url,
            settings.model_api_key,
            settings.model_name,
            timeout=settings.model_timeout_seconds,
        )
        try:
            response = gateway.complete(
                ModelRequest(
                    messages=(
                        ChatMessage(MessageRole.USER, "Reply with exactly: model-ok"),
                    )
                )
            )
        except AgentError as exc:
            raise SystemExit(str(exc)) from exc
        print(json.dumps({"text": response.text, "usage": response.usage.total_tokens}))
        return 0

    if args.command == "magpie-check":
        gateway = MagpieModelGateway(
            settings.model_name or "",
            base_url=settings.magpie_base_url,
            api_key=settings.magpie_api_key,
            timeout=settings.model_timeout_seconds,
        )
        try:
            hello = gateway.hello()
            models = gateway.list_models()
        except AgentError as exc:
            raise SystemExit(str(exc)) from exc
        print(
            json.dumps(
                {
                    "hello": hello,
                    "models": [model.id for model in models],
                    "count": len(models),
                },
                ensure_ascii=False,
            )
        )
        return 0

    if args.command == "serve":
        host = args.host or settings.host
        port = args.port if args.port is not None else settings.port
        if not 1 <= port <= 65535:
            raise SystemExit("--port must be between 1 and 65535")
        uvicorn.run(create_app(settings), host=host, port=port)
        return 0

    if args.command == "worker":
        if not settings.model_name:
            raise SystemExit("set SUPER_AGENT_MODEL before starting the durable worker")
        engine = create_engine_for_url(settings.database_url)
        if settings.database_url.startswith("sqlite"):
            create_schema(engine)
        factory = create_session_factory(engine)
        tools, agent_capabilities, _tool_report = build_agent_tools(settings)
        skills = SkillRegistry(_tool_report.skills.skills)
        recall_tool = RecallTool(factory)
        tool_runner = DurableToolRunner(factory, tools)

        def gateway_factory(model_name: str | None) -> ModelGateway:
            selected_model = model_name or settings.model_name
            if settings.model_base_url:
                return OpenAICompatibleGateway(
                    settings.model_base_url,
                    settings.model_api_key or "",
                    selected_model,
                    timeout=settings.model_timeout_seconds,
                )
            return MagpieModelGateway(
                selected_model,
                base_url=settings.magpie_base_url,
                api_key=settings.magpie_api_key,
                timeout=settings.model_timeout_seconds,
            )

        worker = DurableOutboxWorker(
            factory,
            gateway_factory,
            tools=tools,
            granted_capabilities=agent_capabilities,
            tool_runner=tool_runner,
            recall_tool=recall_tool,
            max_rounds=settings.worker_max_rounds,
            skills=skills,
        )
        if args.once:
            print(json.dumps({"processed": worker.process_once()}))
            tool_runner.shutdown()
            return 0
        stop_event = Event()
        try:
            worker.run_forever(stop_event, poll_interval=args.poll_interval)
        except KeyboardInterrupt:
            stop_event.set()
        finally:
            tool_runner.shutdown()
            engine.dispose()
        return 0

    if args.command == "watchdog":
        engine = create_engine_for_url(settings.database_url)
        if settings.database_url.startswith("sqlite"):
            create_schema(engine)
        factory = create_session_factory(engine)
        watchdog = PersistentRunWatchdog(
            stalled_after_seconds=args.stalled_after,
            max_attempts=args.max_attempts,
        )

        def tick_once() -> None:
            if args.model:
                model_name = settings.model_name
                if not model_name:
                    raise SystemExit("set SUPER_AGENT_MODEL before using watchdog --model")
                if settings.model_base_url:
                    gateway: ModelGateway = OpenAICompatibleGateway(
                        settings.model_base_url,
                        settings.model_api_key or "",
                        model_name,
                        timeout=settings.model_timeout_seconds,
                    )
                else:
                    gateway = MagpieModelGateway(
                        model_name,
                        base_url=settings.magpie_base_url,
                        api_key=settings.magpie_api_key,
                        timeout=settings.model_timeout_seconds,
                    )
                with session_scope(factory) as session:
                    outcome = WatchdogSession(watchdog).tick(session, gateway)
                print(
                    json.dumps(
                        {
                            "findings": [_finding_dict(item) for item in outcome.findings],
                            "expired_runs": [str(item.run_id) for item in outcome.expired],
                            "requeued_runs": [str(item.run_id) for item in outcome.retries],
                            "decision": outcome.decision,
                            "reason": outcome.reason,
                            "intervention_runs": [
                                str(item.run_id) for item in outcome.intervention_retries
                            ],
                            "escalation_id": (
                                str(outcome.escalation_id) if outcome.escalation_id else None
                            ),
                            "model_error": outcome.model_error,
                        },
                        ensure_ascii=False,
                    )
                )
                return
            with session_scope(factory) as session:
                findings = watchdog.inspect(session)
                expired, retries = watchdog.recover(session)
                print(
                    json.dumps(
                        {
                            "findings": [_finding_dict(item) for item in findings],
                            "expired_runs": [str(item.run_id) for item in expired],
                            "requeued_runs": [str(item.run_id) for item in retries],
                        },
                        ensure_ascii=False,
                    )
                )

        try:
            if args.loop is not None:
                if args.loop <= 0:
                    raise SystemExit("--loop must be positive")
                try:
                    while True:
                        tick_once()
                        time.sleep(args.loop)
                except KeyboardInterrupt:
                    pass
            else:
                tick_once()
        finally:
            engine.dispose()
        return 0

    if args.command == "submit":
        model_name = args.model or settings.model_name
        if not model_name:
            raise SystemExit("set SUPER_AGENT_MODEL or pass --model before submitting a task")
        try:
            thread_id = UUID(args.thread_id) if args.thread_id else uuid4()
        except ValueError as exc:
            raise SystemExit("--thread-id must be a UUID") from exc
        engine = create_engine_for_url(settings.database_url)
        if settings.database_url.startswith("sqlite"):
            create_schema(engine)
        factory = create_session_factory(engine)
        tools, agent_capabilities, tool_report = build_agent_tools(settings)
        skills = SkillRegistry(tool_report.skills.skills)
        recall_tool = RecallTool(factory)
        tool_runner = DurableToolRunner(factory, tools)
        if settings.model_base_url:
            gateway = OpenAICompatibleGateway(
                settings.model_base_url,
                settings.model_api_key or "",
                model_name,
                timeout=settings.model_timeout_seconds,
            )
        else:
            gateway = MagpieModelGateway(
                model_name,
                base_url=settings.magpie_base_url,
                api_key=settings.magpie_api_key,
                timeout=settings.model_timeout_seconds,
            )
        trigger = TriggerInput(
            source="cli",
            actor="cli-user",
            thread_id=thread_id,
            payload=_submit_payload(args.message, model_name, args.skill),
            occurred_at=datetime.now(UTC),
            dedupe_key=f"cli:{thread_id}:{args.message}",
        )
        try:
            with session_scope(factory) as session:
                result = AgentOrchestrator(
                    session,
                    gateway,
                    tools=tools,
                    granted_capabilities=agent_capabilities,
                    tool_runner=tool_runner,
                    recall_tool=recall_tool,
                    max_rounds=settings.worker_max_rounds,
                    skills=skills,
                ).submit_via_outbox(trigger)
        except AgentError as exc:
            raise SystemExit(str(exc)) from exc
        finally:
            tool_runner.shutdown()
            engine.dispose()
        print(
            json.dumps(
                {
                    "action": result.action.value,
                    "status": result.status,
                    "task_id": str(result.task_id) if result.task_id else None,
                    "worker_id": str(result.worker_id) if result.worker_id else None,
                    "run_id": str(result.run_id) if result.run_id else None,
                    "text": result.text,
                },
                ensure_ascii=False,
            )
        )
        return 0

    if args.command == "daemon":
        secret = os.environ.get(args.secret_env, "")
        if not secret:
            raise SystemExit(
                f"set the device secret in environment variable {args.secret_env} first"
            )
        try:
            device_id = UUID(args.device_id)
        except ValueError as exc:
            raise SystemExit("--device-id must be a UUID") from exc
        workspace = Path(args.workspace or settings.workspace_root)
        config = DaemonConfig(
            server_url=args.server_url,
            device_id=device_id,
            secret_hex=secret,
            workspace_root=workspace,
            command_allowlist=frozenset(args.allow or ()),
        )
        endpoint = EndpointDaemon(config)
        if args.once:
            processed = endpoint.run_once()
            print(json.dumps({"processed": processed, "error": endpoint.last_error}))
            return 0 if endpoint.last_error is None else 1
        stop = Event()
        try:
            endpoint.run_forever(stop)
        except KeyboardInterrupt:
            stop.set()
        return 0

    if args.command == "device" and args.device_command == "register":
        engine = create_engine_for_url(settings.database_url)
        if settings.database_url.startswith("sqlite"):
            create_schema(engine)
        factory = create_session_factory(engine)
        capabilities = frozenset(args.capability or ("device.read",))
        try:
            with session_scope(factory) as session:
                device_id, secret_hex = PersistentDeviceRegistry(session).register(
                    args.name, capabilities
                )
        finally:
            engine.dispose()
        print(
            json.dumps(
                {
                    "device_id": str(device_id),
                    "secret": secret_hex,
                    "capabilities": sorted(capabilities),
                    "notice": (
                        "Shown once. Store it now, e.g. "
                        "export SUPER_AGENT_DEVICE_SECRET=<secret>; it is never printed again."
                    ),
                },
                ensure_ascii=False,
            )
        )
        return 0

    raise SystemExit(f"unsupported command: {args.command}")


def _finding_dict(finding: WatchdogFinding) -> dict[str, object]:
    return {
        "kind": finding.kind.value,
        "subject_id": str(finding.subject_id) if finding.subject_id else None,
        "evidence": finding.evidence,
        "action": finding.action,
    }

