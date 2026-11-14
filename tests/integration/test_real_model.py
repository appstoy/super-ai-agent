import os

import pytest

from super_agent.adapters.models.openai_compatible import OpenAICompatibleGateway
from super_agent.ports.model import ChatMessage, MessageRole, ModelRequest


@pytest.mark.skipif(
    not all(
        os.getenv(name)
        for name in ("SUPER_AGENT_BASE_URL", "SUPER_AGENT_API_KEY", "SUPER_AGENT_MODEL")
    ),
    reason="set SUPER_AGENT_BASE_URL, SUPER_AGENT_API_KEY, and SUPER_AGENT_MODEL to run",
)
def test_real_openai_compatible_model() -> None:
    gateway = OpenAICompatibleGateway(
        os.environ["SUPER_AGENT_BASE_URL"],
        os.environ["SUPER_AGENT_API_KEY"],
        os.environ["SUPER_AGENT_MODEL"],
    )
    response = gateway.complete(
        ModelRequest(messages=(ChatMessage(MessageRole.USER, "Reply with exactly: model-ok"),))
    )
    assert response.text.strip()

