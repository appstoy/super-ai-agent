"""Router decision entry point backed by the resident Router session.

``RouterService`` keeps the original single-trigger ``route`` contract but
delegates to :class:`~super_agent.application.router_session.ResidentRouterSession`,
which owns the durable cross-task transcript, Inline & Spill window management,
and backlog batching (article §2.1). The Router only dispatches work; it never
executes tools or calls channels itself.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy.orm import Session

from super_agent.domain import Delivery, Task, Thread, Trigger, Worker
from super_agent.ports.model import ModelGateway

if TYPE_CHECKING:
    from super_agent.application.router_session import ResidentRouterSession, RouterBatchMetrics


class RouteAction(StrEnum):
    IGNORED = "ignored"
    CLARIFY = "clarify"
    CREATED = "created"


@dataclass(frozen=True, slots=True)
class RouteResult:
    """All domain outputs produced by one Router decision."""

    action: RouteAction
    trigger: Trigger
    task: Task | None = None
    worker: Worker | None = None
    delivery: Delivery | None = None
    reason: str = ""


class RouterService:
    """Route candidate triggers without executing tools or calling channels.

    When a database ``session`` is provided, the Router transcript is persisted
    to ``router_messages`` and survives service rebuilds; otherwise the resident
    session keeps an in-memory transcript for the lifetime of this service.
    """

    def __init__(
        self,
        model: ModelGateway,
        *,
        ignored_sources: frozenset[str] = frozenset({"noise", "healthcheck"}),
        session: Session | None = None,
        tenant_id: str = "default",
        window_budget: int = 40,
    ) -> None:
        from super_agent.application.router_session import ResidentRouterSession

        self._resident: ResidentRouterSession = ResidentRouterSession(
            session,
            model,
            tenant_id=tenant_id,
            window_budget=window_budget,
            ignored_sources=ignored_sources,
        )

    @property
    def metrics(self) -> RouterBatchMetrics:
        """Batching counters and latency of the underlying resident session."""

        return self._resident.metrics

    def route(self, trigger: Trigger, *, thread: Thread | None = None) -> RouteResult:
        """Apply Fast Path rules, then ask the resident session for a decision."""

        return self._resident.route(trigger, thread=thread)

    def route_batch(
        self,
        triggers: Sequence[Trigger],
        *,
        threads: dict[UUID, Thread | None] | None = None,
    ) -> tuple[RouteResult, ...]:
        """Merge backlogged triggers into one resident-session model call."""

        return self._resident.route_batch(triggers, threads=threads)