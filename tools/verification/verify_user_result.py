#!/usr/bin/env python
"""Assert the user-facing result contract end to end, without a network model.

The point of this check is the *user's* view: a task must answer three things -
did it work, what came out, where can I see it - and it must never leak runtime
vocabulary (Worker, Run, Trigger, output_ref) or an internal pointer.

Run it directly:

    python tools/verification/verify_user_result.py

It exits non-zero on the first broken guarantee and prints one line per check.
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from fastapi.testclient import TestClient  # noqa: E402

from super_agent.api import create_app  # noqa: E402
from super_agent.config import Settings  # noqa: E402
from super_agent.ports.model import ModelRequest, ModelResponse  # noqa: E402

CHECKS: list[str] = []


def check(label: str, condition: object) -> None:
    if not condition:
        raise SystemExit(f"FAIL: {label}")
    CHECKS.append(label)
    print(f"  ok  {label}")


class Gateway:
    """Route to a task, then answer with a real-looking model reply."""

    def complete(self, request: ModelRequest) -> ModelResponse:
        if request.messages[0].content.startswith("Return exactly one route"):
            return ModelResponse(text="TASK: build the page")
        verdict = "做好了，页面已经写完。"
        report = "\n\n**结构：**\n\n| 区块 | 内容 |\n|---|---|\n| Hero | 标题 |"
        return ModelResponse(text=verdict + report)


class BrokenGateway(Gateway):
    def complete(self, request: ModelRequest) -> ModelResponse:
        if request.messages[0].content.startswith("Return exactly one route"):
            return ModelResponse(text="TASK: fail")
        raise RuntimeError("provider exploded")


def build_client(tmp: Path, gateway: type[Gateway]) -> TestClient:
    import super_agent.api as api

    api.MagpieModelGateway = lambda *a, **k: gateway()
    settings = Settings(
        database_url=f"sqlite:///{tmp / 'acceptance.db'}",
        model_name="test-model",
        router_batch_limit=4,
        workspace_root=str(tmp / "ws"),
    )
    return TestClient(create_app(settings))


def main() -> int:
    with tempfile.TemporaryDirectory() as raw:
        tmp = Path(raw)
        with build_client(tmp, Gateway) as client:
            print("1) a task answers the user's three questions")
            created = client.post(
                "/api/tasks",
                json={"message": "做一个介绍页", "model": "test-model", "thread_id": str(uuid4())},
            )
            check("submit accepted", created.status_code == 200)
            task_id = created.json()["task_id"]
            check("task id returned", bool(task_id))

            body = client.get(f"/api/tasks/{task_id}/result").json()
            check("did it work -> status", body["status"] == "succeeded")
            check("did it work -> plain state", body["state"]["text"] == "做好了")
            check("what came out -> conclusion", body["conclusion"] == "做好了，页面已经写完。")
            check("conclusion drops the table body", "|" not in body["conclusion"])
            check("user recognises their own words", body["ask"] == "做一个介绍页")
            check(
                "steps explain the work",
                "初筛" in body["steps"][0]["text"] or "收到任务" in body["steps"][0]["text"],
            )
            check("step carries summary and details", bool(body["steps"][0].get("summary")))
            check(
                "result carries thread_id for conversation continuation",
                bool(body.get("thread_id")),
            )
            check(
                "repeated tool noise collapsed",
                len([s for s in body["steps"] if s["text"] == "用工具产出了内容"]) == 0,
            )

            print("2) runtime vocabulary never reaches the user")
            flat = json.dumps(body)
            for word in ("worker_id", "run_id", "output_ref", "trigger_id", "lease"):
                check(f"no '{word}' in the payload", word not in flat)
            for ref in ("task-created:", "model:", "tool-run:"):
                check(f"no internal ref '{ref}' in the verdict", ref not in body["conclusion"])

            print("3) an artifact is consumable from the result alone")
            short = task_id.replace("-", "")[:8]
            folder = tmp / "ws" / "项目缓存文件" / f"{short}-probe"
            folder.mkdir(parents=True, exist_ok=True)
            page = folder / "index.html"
            page.write_text("<h1>hi</h1>", encoding="utf-8")
            body = client.get(f"/api/tasks/{task_id}/result").json()
            artifact = next((a for a in body["artifacts"] if a["name"] == "index.html"), None)
            check("artifact is listed", artifact is not None)
            check("artifact reports its size", artifact and artifact["size"] == len("<h1>hi</h1>"))
            check("artifact is marked previewable", artifact and artifact["previewable"])
            check("result advertises a preview url", bool(body["open_url"]))
            if body["open_url"]:
                served = client.get(body["open_url"])
                check("advertised url actually serves the file", served.status_code == 200)
                check("served body is the file", "hi" in served.text)

        print("4) failure is explained, not raised")
        with build_client(tmp, BrokenGateway) as client:
            created = client.post(
                "/api/tasks",
                json={"message": "故意失败", "model": "test-model", "thread_id": str(uuid4())},
            )
            check("a provider error is not a 500", created.status_code in {200, 502})

        print("5) the two surfaces stay separate")
        with build_client(tmp, Gateway) as client:
            user = client.get("/").text.lower()
            ops = client.get("/ops").text.lower()
            check("user page has the composer", "composer" in user)
            check("user page reads results", "/api/results" in user)
            check("user page hides operator jargon", "watchdog" not in user)
            check("ops page still has the runtime console", "watchdog" in ops)
            check("user page links to ops", "/ops" in user)

    print(f"\n{len(CHECKS)} checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
