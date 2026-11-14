"""Deterministic replay runner for routing and context evaluation."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from uuid import UUID, uuid5

from super_agent.adapters.models.mock import MockModelGateway
from super_agent.application.router import RouterService
from super_agent.context.service import InlineSpillContextService
from super_agent.delivery.service import DeliveryPolicy
from super_agent.domain import Trigger
from super_agent.ports.channel import ChannelType, DeliveryRequest
from super_agent.ports.context import ContextBuildRequest, ContextCandidate
from super_agent.ports.model import ModelResponse


@dataclass(frozen=True, slots=True)
class ReplayResult:
    """Stable JSON-compatible replay output."""

    strategy_version: str
    trigger_id: str
    action: str
    reason: str
    task_id: str | None
    worker_id: str | None
    context_text: str
    context_tokens: int
    context_manifest: tuple[dict[str, object], ...]
    delivery_body: str | None

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


class ReplayRunner:
    """Replay a fixed event list using scripted model responses."""

    def __init__(self, *, strategy_version: str = "phase3-v1") -> None:
        self.strategy_version = strategy_version

    def run(
        self,
        events: list[dict[str, object]],
        model_outputs: list[str],
        *,
        context_budget: int = 256,
    ) -> list[ReplayResult]:
        """Run deterministic routing and context construction for each event."""

        model = MockModelGateway([ModelResponse(text=output) for output in model_outputs])
        router = RouterService(model)
        context_service = InlineSpillContextService()
        policy = DeliveryPolicy()
        results: list[ReplayResult] = []
        for event in events:
            trigger = Trigger(
                id=UUID(str(event["id"])),
                source=str(event["source"]),
                actor=str(event["actor"]),
                thread_id=UUID(str(event["thread_id"])),
                payload_ref=str(event["payload_ref"]),
                occurred_at=datetime.fromisoformat(str(event["occurred_at"])).astimezone(UTC),
                dedupe_key=str(event["dedupe_key"]),
            )
            route = router.route(trigger)
            candidate = ContextCandidate(
                trigger.id,
                "trigger",
                trigger.payload_ref,
                priority=100,
                read_ref=f"trigger:{trigger.id}",
            )
            built = context_service.build(
                ContextBuildRequest(trigger.id, (candidate,), context_budget)
            )
            delivery_body: str | None = None
            if route.delivery is not None:
                delivery = policy.format(
                    DeliveryRequest(
                        ChannelType.CLI,
                        route.delivery.idempotency_key,
                        route.reason,
                        {"action": route.action.value},
                    )
                )
                delivery_body = delivery.body
            results.append(
                ReplayResult(
                    self.strategy_version,
                    str(trigger.id),
                    route.action.value,
                    route.reason,
                    (
                        str(uuid5(trigger.id, f"{self.strategy_version}:task"))
                        if route.task
                        else None
                    ),
                    (
                        str(uuid5(trigger.id, f"{self.strategy_version}:worker"))
                        if route.worker
                        else None
                    ),
                    built.text,
                    built.estimated_tokens,
                    tuple(
                        {
                            "object_id": str(item.object_id),
                            "object_type": item.object_type,
                            "placement": item.placement.value,
                            "estimated_tokens": item.estimated_tokens,
                            "reason": item.reason,
                            "read_ref": item.read_ref,
                        }
                        for item in built.manifest
                    ),
                    delivery_body,
                )
            )
        return results


def dump_results(results: list[ReplayResult]) -> str:
    """Serialize replay output in stable form for benchmark artifacts."""

    return json.dumps([result.to_dict() for result in results], ensure_ascii=False, sort_keys=True)

