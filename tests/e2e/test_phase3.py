import json
from uuid import uuid4

from super_agent.adapters.channels.memory import CliChannelAdapter, WebhookChannelAdapter
from super_agent.adapters.models.mock import MockModelGateway
from super_agent.application.router import RouterService
from super_agent.context.recall import ReferenceEdge, ReferenceGraph
from super_agent.context.service import InlineSpillContextService
from super_agent.delivery.service import DeliveryService
from super_agent.domain import Trigger
from super_agent.ports.channel import ChannelType, DeliveryRequest
from super_agent.ports.context import ContextBuildRequest, ContextCandidate
from super_agent.ports.model import ModelResponse


def test_phase3_cross_channel_continuity_and_recall() -> None:
    thread_id = uuid4()
    first = Trigger(
        source="cli",
        actor="user",
        thread_id=thread_id,
        payload_ref="start task",
        dedupe_key="a",
    )
    model = MockModelGateway([ModelResponse(text="TASK: prepare report")])
    route = RouterService(model).route(first)
    assert route.task is not None

    follow_up = Trigger(
        source="webhook",
        actor="system",
        thread_id=thread_id,
        payload_ref="add deployment details",
        dedupe_key="b",
    )
    graph = ReferenceGraph()
    graph.add(ReferenceEdge(follow_up.id, first.id, "thread-event"))
    graph.add(ReferenceEdge(first.id, route.task.id, "trigger-task"))
    recalled = graph.recall(follow_up.id)
    assert route.task.id in recalled.object_ids

    context = InlineSpillContextService().build(
        ContextBuildRequest(
            follow_up.id,
            (
                ContextCandidate(follow_up.id, "trigger", follow_up.payload_ref, 100),
                ContextCandidate(route.task.id, "task", route.task.title, 80),
            ),
            token_budget=64,
        )
    )
    assert "add deployment details" in context.text
    assert "prepare report" in context.text

    cli = CliChannelAdapter()
    webhook = WebhookChannelAdapter()
    delivery = DeliveryService([cli, webhook])
    cli_result = delivery.send(
        DeliveryRequest(
            ChannelType.CLI,
            "phase3-cli",
            "report prepared",
            {"task_id": str(route.task.id)},
        )
    )
    webhook_result = delivery.send(
        DeliveryRequest(
            ChannelType.WEBHOOK,
            "phase3-webhook",
            "report prepared",
            {"task_id": str(route.task.id), "follow_up": follow_up.payload_ref},
        )
    )
    assert cli_result.status.value == "sent"
    assert webhook_result.status.value == "sent"
    assert json.loads(webhook.messages[0])["summary"] == cli.messages[0]

