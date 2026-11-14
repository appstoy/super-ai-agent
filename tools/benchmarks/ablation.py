"""Reproducible ablation experiment for article §7.1.

Drives four end-to-end scheduling variants with the same synthetic event
stream (group-chat noise + work requests + follow-ups + CI messages). Coding
work is replaced by a time-only tool, so the experiment measures scheduling
and collaboration, not single-task execution quality.

Variants:
- ``super-agent``: resident Router + subscription direct path (full design).
- ``super-agent-no-direct``: every event re-enters the Router (no fast path).
- ``single-session-neutral``: one session, neutral subagent prompt.
- ``single-session-delegate``: one session, delegation-first prompt.

Run: ``python -m tools.benchmarks.ablation --runs 5 --events 300``
"""

from __future__ import annotations

import argparse
import json
import random
import statistics
import tempfile
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

from sqlalchemy.orm import Session, sessionmaker

from super_agent.application.orchestrator import AgentOrchestrator
from super_agent.ingress.schemas import TriggerInput
from super_agent.ports.model import ModelRequest, ModelResponse
from super_agent.ports.tool import ExecutionContext, ToolEffect, ToolResult, ToolSpec
from super_agent.storage.database import (
    create_engine_for_url,
    create_schema,
    create_session_factory,
    session_scope,
)
from super_agent.storage.models import SubscriptionRecord
from super_agent.tools.registry import ToolRegistry

WORK_SECONDS = 0.05
FOLLOW_UP_PROBABILITY = 0.5


@dataclass(frozen=True, slots=True)
class SyntheticEvent:
    """One event of the synthetic stream."""

    kind: str  # work | chat | ci | follow_up
    text: str
    thread_id: UUID
    work_id: int | None = None


@dataclass
class VariantReport:
    """Aggregated metrics for one variant across repeated runs."""

    variant: str
    runs: int
    p50_minutes: float
    p95_minutes: float
    router_calls_mean: float
    worker_calls_mean: float
    prompt_chars_mean: float
    completed_work: int
    interrupted_work: int = 0


class TimeOnlyTool:
    """Coding work stand-in: consumes wall time, produces a fixed report."""

    def __init__(self, seconds: float = WORK_SECONDS) -> None:
        self._seconds = seconds
        self._spec = ToolSpec(
            name="code_work",
            description="Simulated coding work that only consumes time.",
            effect=ToolEffect.MOCK,
            input_schema={},
        )

    def describe(self) -> ToolSpec:
        return self._spec

    def execute(self, arguments: dict[str, object], context: ExecutionContext) -> ToolResult:
        del arguments, context
        time.sleep(self._seconds)
        return ToolResult(tool_name="code_work", output="work report: done")


class ScriptedSuperAgentGateway:
    """Router/Worker gateway for the two Super-agent variants.

    Router turns answer the routing contract; Worker turns request the
    time-only tool once, then deliver the final report.
    """

    def __init__(self) -> None:
        self.router_calls = 0
        self.worker_calls = 0
        self.prompt_chars = 0
        self._worker_tool_done: set[str] = set()

    def complete(self, request: ModelRequest) -> ModelResponse:
        self.prompt_chars += sum(len(message.content) for message in request.messages)
        system = request.messages[0].content
        if system.startswith("Return exactly one route") or "backlogged triggers" in system:
            self.router_calls += 1
            user = request.messages[-1].content
            if "backlogged triggers" in user:
                lines = []
                for line in user.splitlines():
                    head, _, rest = line.partition(": ")
                    if head.strip().isdigit() and rest:
                        lines.append(f"{head.strip()}: TASK: synthetic work")
                return ModelResponse(text="\n".join(lines) or "IGNORE")
            return ModelResponse(text="TASK: synthetic work")
        self.worker_calls += 1
        key = request.request_id or ""
        if any(message.role.value == "tool" for message in request.messages):
            return ModelResponse(text="work report delivered")
        if key in self._worker_tool_done:
            return ModelResponse(text="work report delivered")
        self._worker_tool_done.add(key)
        from super_agent.ports.model import ToolCall

        return ModelResponse(
            tool_calls=(ToolCall(id=f"work-{key}", name="code_work", arguments={}),)
        )


class SingleSessionGateway:
    """One session deciding per work request whether to delegate.

    Mirrors the article's prompt-sensitive baseline: with a neutral prompt the
    model usually executes work itself (blocking the session); with a
    delegation-first prompt it usually hands work to a subagent, but still
    self-executes occasionally, producing the long tail.
    """

    def __init__(self, *, delegate: bool, rng: random.Random) -> None:
        self.delegate = delegate
        self.rng = rng
        self.router_calls = 0  # always zero: no separate Router layer
        self.worker_calls = 0
        self.prompt_chars = 0
        self.delegated = 0
        self.self_executed = 0

    def decide(self) -> bool:
        """Return True when this work request is handed to a subagent."""

        self.worker_calls += 1
        probability = 0.8 if self.delegate else 0.0
        if self.rng.random() < probability:
            self.delegated += 1
            return True
        self.self_executed += 1
        return False


def build_stream(event_count: int, work_count: int, seed: int) -> list[SyntheticEvent]:
    """Deterministic stream: work requests, chatter, CI, and follow-ups."""

    rng = random.Random(seed)
    threads = [uuid4() for _ in range(work_count)]
    events: list[SyntheticEvent] = []
    work_ids = list(range(work_count))
    rng.shuffle(work_ids)
    for work_id in work_ids:
        events.append(
            SyntheticEvent("work", f"please implement feature {work_id}", threads[work_id], work_id)
        )
    while len(events) < event_count:
        roll = rng.random()
        if roll < 0.62:
            chatter = rng.choice(["lunch?", "lol", "+1", "nice"])
            events.append(SyntheticEvent("chat", chatter, uuid4()))
        elif roll < 0.82:
            events.append(SyntheticEvent("ci", "CI pipeline finished: green", uuid4()))
        else:
            work_id = rng.randrange(work_count)
            events.append(
                SyntheticEvent(
                    "follow_up", f"any update on feature {work_id}?", threads[work_id], work_id
                )
            )
    rng.shuffle(events)
    return events[:event_count]


def _percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(round(pct / 100 * (len(ordered) - 1)))))
    return ordered[index]


def run_super_agent(
    factory: sessionmaker[Session],
    events: list[SyntheticEvent],
    *,
    direct: bool,
) -> tuple[dict[str, float], ScriptedSuperAgentGateway]:
    """Full design (direct=True) or the no-direct-path ablation."""

    gateway = ScriptedSuperAgentGateway()
    registry = ToolRegistry([TimeOnlyTool()])
    completion: dict[int, float] = {}
    start_times: dict[int, float] = {}
    begun = time.perf_counter()
    for event in events:
        trigger_input = TriggerInput(
            source="im" if event.kind in {"work", "chat", "follow_up"} else "github",
            actor="teammate",
            thread_id=event.thread_id,
            payload={"message": event.text},
            occurred_at=datetime.now(UTC),
            dedupe_key=f"ablation:{event.kind}:{uuid4()}",
        )
        if event.kind == "work" and event.work_id is not None:
            start_times[event.work_id] = time.perf_counter()
        with session_scope(factory) as session:
            orchestrator = AgentOrchestrator(
                session, gateway, tools=registry, max_rounds=4
            )
            if not direct:
                # Ablation: drop active subscriptions so every event is
                # re-decided by the Router instead of going to its Worker.
                for row in session.query(SubscriptionRecord).all():
                    row.status = "expired"
            result = orchestrator.submit(trigger_input)
            if (
                event.kind == "work"
                and event.work_id is not None
                and result.status == "succeeded"
                and event.work_id not in completion
            ):
                completion[event.work_id] = time.perf_counter()
    durations = [
        (completion[work_id] - start_times[work_id]) / 60.0
        for work_id in completion
        if work_id in start_times
    ]
    metrics = {
        "p50": _percentile(durations, 50),
        "p95": _percentile(durations, 95),
        "router_calls": float(gateway.router_calls),
        "worker_calls": float(gateway.worker_calls),
        "prompt_chars": float(gateway.prompt_chars),
        "completed": float(len(completion)),
        "wall_seconds": time.perf_counter() - begun,
    }
    return metrics, gateway


def run_single_session(
    events: list[SyntheticEvent], *, delegate: bool, seed: int
) -> dict[str, float]:
    """One ReAct session processes the stream serially (baseline).

    Inline work blocks the session: later events wait until the current tool
    call finishes, so self-executed work stretches every following request.
    Delegated work runs in a mock subagent and does not block the session.
    """

    rng = random.Random(seed)
    gateway = SingleSessionGateway(delegate=delegate, rng=rng)
    completion: dict[int, float] = {}
    start_times: dict[int, float] = {}
    history: list[str] = []
    busy_until = 0.0
    simulated_now = 0.0
    begun = time.perf_counter()
    arrival = 0.0
    for event in events:
        # Requests arrive on the stream timeline (one small step apart); the
        # session can only look at an event once it is free, so queueing delay
        # caused by inline work counts toward completion time.
        arrival += 0.002
        simulated_now = max(arrival, busy_until)
        if event.kind == "work" and event.work_id is not None:
            start_times[event.work_id] = arrival
        history.append(event.text)
        gateway.prompt_chars += len("\n".join(history[-20:]))
        if event.kind == "work" and event.work_id is not None:
            delegated = gateway.decide()
            if delegated:
                # Subagent runs in parallel; the session stays free.
                completion[event.work_id] = simulated_now + WORK_SECONDS
                busy_until = simulated_now + 0.002
            else:
                # Inline execution blocks the session; the result message only
                # goes out when the tool call finishes.
                busy_until = simulated_now + WORK_SECONDS
                completion[event.work_id] = busy_until
        else:
            # Chat/CI/follow-up turns consume a small reasoning slice.
            busy_until = simulated_now + 0.002
    durations = [
        (completion[work_id] - start_times[work_id]) / 60.0
        for work_id in completion
        if work_id in start_times
    ]
    return {
        "p50": _percentile(durations, 50),
        "p95": _percentile(durations, 95),
        "router_calls": 0.0,
        "worker_calls": float(gateway.worker_calls),
        "prompt_chars": float(gateway.prompt_chars),
        "completed": float(len(completion)),
        "wall_seconds": time.perf_counter() - begun,
        "delegated": float(gateway.delegated),
        "self_executed": float(gateway.self_executed),
    }


def run_experiment(*, runs: int, event_count: int, work_count: int, seed: int) -> dict[str, object]:
    """Repeat every variant and aggregate comparable metrics."""

    variants: dict[str, list[dict[str, float]]] = {
        "super-agent": [],
        "super-agent-no-direct": [],
        "single-session-neutral": [],
        "single-session-delegate": [],
    }
    for run_index in range(runs):
        events = build_stream(event_count, work_count, seed + run_index)
        for name, direct in (("super-agent", True), ("super-agent-no-direct", False)):
            with tempfile.TemporaryDirectory() as tmp:
                engine = create_engine_for_url(f"sqlite:///{Path(tmp) / 'ablation.db'}")
                create_schema(engine)
                factory = create_session_factory(engine)
                metrics, _ = run_super_agent(factory, events, direct=direct)
                variants[name].append(metrics)
                engine.dispose()
        variants["single-session-neutral"].append(
            run_single_session(events, delegate=False, seed=seed + run_index)
        )
        variants["single-session-delegate"].append(
            run_single_session(events, delegate=True, seed=seed + run_index)
        )
    reports: list[VariantReport] = []
    for name, samples in variants.items():
        reports.append(
            VariantReport(
                variant=name,
                runs=len(samples),
                p50_minutes=statistics.mean(sample["p50"] for sample in samples),
                p95_minutes=statistics.mean(sample["p95"] for sample in samples),
                router_calls_mean=statistics.mean(sample["router_calls"] for sample in samples),
                worker_calls_mean=statistics.mean(sample["worker_calls"] for sample in samples),
                prompt_chars_mean=statistics.mean(sample["prompt_chars"] for sample in samples),
                completed_work=int(statistics.mean(sample["completed"] for sample in samples)),
            )
        )
    return {
        "experiment": "article-7.1-ablation",
        "events_per_run": event_count,
        "work_requests_per_run": work_count,
        "runs": runs,
        "seed": seed,
        "work_seconds": WORK_SECONDS,
        "variants": [report.__dict__ for report in reports],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Article §7.1 ablation experiment")
    parser.add_argument("--runs", type=int, default=5)
    parser.add_argument("--events", type=int, default=300)
    parser.add_argument("--work", type=int, default=20)
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--out", default=None, help="write JSON report to this path")
    args = parser.parse_args()
    report = run_experiment(
        runs=args.runs, event_count=args.events, work_count=args.work, seed=args.seed
    )
    text = json.dumps(report, ensure_ascii=False, indent=2)
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())