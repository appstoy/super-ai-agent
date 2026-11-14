import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

from super_agent.adapters.models.openai_compatible import OpenAICompatibleGateway
from super_agent.ports.model import ChatMessage, MessageRole, ModelRequest


class Handler(BaseHTTPRequestHandler):
    received: dict[str, object] = {}

    def do_POST(self) -> None:
        length = int(self.headers["Content-Length"])
        Handler.received = json.loads(self.rfile.read(length).decode("utf-8"))
        if Handler.received.get("stream"):
            chunks = (
                'data: {"choices":[{"delta":{"content":"hel"}}]}\n\n',
                'data: {"choices":[{"delta":{"content":"lo"}}]}\n\n',
                "data: [DONE]\n\n",
            )
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            for chunk in chunks:
                self.wfile.write(chunk.encode("utf-8"))
                self.wfile.flush()
            return
        body = json.dumps(
            {
                "id": "resp-test",
                "choices": [
                    {
                        "message": {"role": "assistant", "content": "hello"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 3, "completion_tokens": 1, "total_tokens": 4},
            }
        ).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        return


def test_openai_compatible_gateway_round_trip() -> None:
    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        gateway = OpenAICompatibleGateway(
            f"http://127.0.0.1:{server.server_port}/v1", "test-key", "test-model"
        )
        response = gateway.complete(
            ModelRequest(messages=(ChatMessage(MessageRole.USER, "hello"),))
        )
    finally:
        server.shutdown()
        thread.join(timeout=2)
    assert response.text == "hello"
    assert response.usage.total_tokens == 4
    assert Handler.received["model"] == "test-model"
    assert Handler.received["messages"][0]["content"] == "hello"


def test_openai_compatible_gateway_streams_text_deltas() -> None:
    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        gateway = OpenAICompatibleGateway(
            f"http://127.0.0.1:{server.server_port}/v1", "test-key", "test-model"
        )
        chunks = list(
            gateway.stream_text(
                ModelRequest(messages=(ChatMessage(MessageRole.USER, "hello"),))
            )
        )
    finally:
        server.shutdown()
        thread.join(timeout=2)
    assert chunks == ["hel", "lo"]
    assert Handler.received["stream"] is True

