"""Application configuration without framework dependencies."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

from super_agent.errors import ConfigurationError


@dataclass(frozen=True, slots=True)
class Settings:
    """Runtime settings shared by the CLI and HTTP adapter."""

    app_name: str = "super-ai-agent"
    version: str = "0.1.0.dev0"
    host: str = "127.0.0.1"
    port: int = 8000
    database_url: str = "sqlite:///./super_agent.db"
    model_base_url: str | None = None
    model_api_key: str | None = None
    model_name: str = ""
    # Long answers (a full HTML page, a report) routinely need more than a
    # minute; 60s produced spurious "model request failed: timed out" errors.
    model_timeout_seconds: float = 180.0
    magpie_base_url: str = "http://127.0.0.1:3425/v1"
    magpie_api_key: str = "magpie-super-ai-agent"
    workspace_root: str = "."
    # Every task writes into <workspace_root>/<cache_dir_name>/<task folder>/
    # so generated artifacts never pile up in the repository root.
    task_workspaces_enabled: bool = True
    cache_dir_name: str = "项目缓存文件"
    webhook_secret: str = ""
    router_batch_limit: int = 8
    speculative_enabled: bool = False
    # Recovery of stuck Runs happens on every worker tick regardless; this knob
    # only controls the extra model-judged control loop. A stalled task used to
    # stay "running" forever because the loop defaulted to off.
    watchdog_interval_seconds: float = 60.0
    worker_max_rounds: int = 12
    tool_manifest: str = ""
    tool_modules: str = ""
    tool_paths: str = ""
    # Skills are instruction playbooks (not callable tools). They live in
    # folders containing SKILL.md; empty means "<workspace_root>/skills".
    skills_enabled: bool = True
    skill_paths: str = ""

    @classmethod
    def from_env(cls) -> Settings:
        """Load optional process settings while keeping safe local defaults."""

        load_dotenv(dotenv_path=Path.cwd() / ".env", override=False)
        defaults = cls()
        raw_port = os.getenv("SUPER_AGENT_PORT", str(defaults.port))
        try:
            port = int(raw_port)
        except ValueError as exc:
            raise ConfigurationError("SUPER_AGENT_PORT must be an integer") from exc
        if not 1 <= port <= 65535:
            raise ConfigurationError("SUPER_AGENT_PORT must be between 1 and 65535")
        raw_timeout = os.getenv(
            "SUPER_AGENT_MODEL_TIMEOUT_SECONDS", str(defaults.model_timeout_seconds)
        )
        try:
            model_timeout = float(raw_timeout)
        except ValueError as exc:
            raise ConfigurationError(
                "SUPER_AGENT_MODEL_TIMEOUT_SECONDS must be a number"
            ) from exc
        if model_timeout <= 0:
            raise ConfigurationError("SUPER_AGENT_MODEL_TIMEOUT_SECONDS must be positive")
        return cls(
            app_name=os.getenv("SUPER_AGENT_APP_NAME", defaults.app_name),
            version=os.getenv("SUPER_AGENT_VERSION", defaults.version),
            host=os.getenv("SUPER_AGENT_HOST", defaults.host),
            port=port,
            database_url=os.getenv("SUPER_AGENT_DATABASE_URL", defaults.database_url),
            model_base_url=os.getenv("SUPER_AGENT_BASE_URL") or None,
            model_api_key=os.getenv("SUPER_AGENT_API_KEY") or None,
            model_name=os.getenv("SUPER_AGENT_MODEL", defaults.model_name),
            model_timeout_seconds=model_timeout,
            magpie_base_url=os.getenv("SUPER_AGENT_MAGPIE_BASE_URL", defaults.magpie_base_url),
            magpie_api_key=os.getenv("SUPER_AGENT_MAGPIE_API_KEY", defaults.magpie_api_key),
            workspace_root=os.getenv("SUPER_AGENT_WORKSPACE_ROOT", str(Path.cwd())),
            task_workspaces_enabled=os.getenv(
                "SUPER_AGENT_TASK_WORKSPACES", "1"
            ).lower() not in {"0", "false", "no"},
            cache_dir_name=os.getenv(
                "SUPER_AGENT_CACHE_DIR_NAME", defaults.cache_dir_name
            ),
            webhook_secret=os.getenv("SUPER_AGENT_WEBHOOK_SECRET", defaults.webhook_secret),
            router_batch_limit=_positive_int(
                os.getenv("SUPER_AGENT_ROUTER_BATCH_LIMIT"), defaults.router_batch_limit
            ),
            speculative_enabled=os.getenv(
                "SUPER_AGENT_SPECULATIVE_ENABLED", ""
            ).lower() in {"1", "true", "yes"},
            worker_max_rounds=_positive_int(
                os.getenv("SUPER_AGENT_WORKER_MAX_ROUNDS"), defaults.worker_max_rounds
            ),
            watchdog_interval_seconds=_non_negative_float(
                os.getenv("SUPER_AGENT_WATCHDOG_INTERVAL_SECONDS"),
                defaults.watchdog_interval_seconds,
            ),
            tool_manifest=os.getenv("SUPER_AGENT_TOOL_MANIFEST", defaults.tool_manifest),
            tool_modules=os.getenv("SUPER_AGENT_TOOL_MODULES", defaults.tool_modules),
            tool_paths=os.getenv("SUPER_AGENT_TOOL_PATHS", defaults.tool_paths),
            skills_enabled=os.getenv("SUPER_AGENT_SKILLS_ENABLED", "1").lower()
            not in {"0", "false", "no"},
            skill_paths=os.getenv("SUPER_AGENT_SKILL_PATHS", defaults.skill_paths),
        )

def _positive_int(raw: str | None, default: int) -> int:
    """Parse a positive integer setting with a safe default."""

    if raw is None or raw == "":
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigurationError("setting must be an integer") from exc
    if value < 1:
        raise ConfigurationError("setting must be positive")
    return value


def _non_negative_float(raw: str | None, default: float) -> float:
    """Parse a non-negative float setting with a safe default."""

    if raw is None or raw == "":
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise ConfigurationError("setting must be a number") from exc
    if value < 0:
        raise ConfigurationError("setting must be non-negative")
    return value
