import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

from super_agent.adapters.models.magpie import MagpieModelGateway
from super_agent.ports.model import ChatMessage, MessageRole, ModelRequest


class MagpieHandler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:
        if self.path != "/v1/chat/completions":
            self.send_error(404)
            return
        length = int(self.headers.get("Content-Length", "0"))
        self.rfile.read(length)
        body = {
            "id": "magpie-test-request",
            "choices": [
                {
                    "message": {"role": "assistant", "content": "magpie-ok"},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5},
        }
        self._send_json(body)

    def do_GET(self) -> None:
        if self.path == "/api/hello":
            body = {"name": "magpie", "version": "test"}
        else:
            body = {"object": "list", "data": [{"id": "ollama/qwen3", "owned_by": "ollama"}]}
        self._send_json(body)

    def _send_json(self, body: dict[str, object]) -> None:
        encoded = json.dumps(body).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def log_message(self, format: str, *args: object) -> None:
        return


def test_magpie_gateway_hello_and_model_catalog() -> None:
    server = HTTPServer(("127.0.0.1", 0), MagpieHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        gateway = MagpieModelGateway(
            "ollama/qwen3",
            base_url=f"http://127.0.0.1:{server.server_port}/v1",
        )
        assert gateway.hello()["name"] == "magpie"
        assert gateway.list_models()[0].id == "ollama/qwen3"
        response = gateway.complete(
            ModelRequest(
                messages=(ChatMessage(MessageRole.USER, "hello"),),
            )
        )
        assert response.text == "magpie-ok"
        assert response.usage.total_tokens == 5
    finally:
        server.shutdown()
        thread.join(timeout=2)

