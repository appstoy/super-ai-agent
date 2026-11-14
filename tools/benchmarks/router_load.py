"""Reproducible Router batching stress test for article §7.2.

The Router calls a latency-simulated model (prefill grows sub-linearly with
batch size, mirroring real batched inference); Workers are mocked. Triggers
arrive at a configurable rate; while one Router turn is in flight, arrivals
accumulate and are merged into the next single call.

Run: ``python -m tools.benchmarks.router_load --rates 1,10,20,40``
"""

from __future__ import annotations

import argparse
import json
import random
import tempfile
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from super_agent.application.router_session import ResidentRouterSession
from super_agent.domain import Trigger
from super_agent.ports.model import ModelRequest, ModelResponse
from super_agent.storage.database import (
    create_engine_for_url,
    create_schema,
    create_session_factory,
    session_scope,
)

BASE_LATENCY_SECONDS = 2.5
PREFILL_EXPONENT = 0.35


class LatencySimulatedRouterModel:
    """Model stand-in whose latency grows sub-linearly with batch size.

    A real batched LLM call amortizes prefill across the batch: request time
    grows much slower than the number of merged triggers. This mock reproduces
    that shape deterministically so the experiment is reproducible offline.
    """

    def __init__(self, *, base: float = BASE_LATENCY_SECONDS, exponent: float = PREFILL_EXPONENT):
        self.base = base
        self.exponent = exponent
        self.calls = 0
        self.batch_sizes: list[int] = []
        self.durations: list[float] = []

    def complete(self, request: ModelRequest) -> ModelResponse:
        user = request.messages[-1].content
        if "backlogged triggers" in user:
            count = sum(
                1
                for line in user.splitlines()
                if line.partition(": ")[0].strip().isdigit() and line.partition(": ")[2]
            )
            count = max(1, count)
        else:
            count = 1
        latency = self.base * (count**self.exponent)
        time.sleep(latency)
        self.calls += 1
        self.batch_sizes.append(count)
        self.durations.append(latency)
        if count == 1:
            return ModelResponse(text="TASK: synthetic request")
        lines = [f"{n}: TASK: synthetic request {n}" for n in range(1, count + 1)]
        return ModelResponse(text="\n".join(lines))


@dataclass(frozen=True, slots=True)
class RateResult:
    """Aggregated batching behaviour at one arrival rate."""

    rate_per_minute: float
    triggers: int
    router_calls: int
    avg_batch_size: float
    max_batch_size: int
    avg_request_seconds: float
    seconds_per_decision: float
    wall_seconds: float


def run_rate(rate_per_minute: float, *, duration_seconds: float, seed: int) -> RateResult:
    """Drive one arrival rate against the resident Router session."""

    rng = random.Random(seed)
    model = LatencySimulatedRouterModel()
    with tempfile.TemporaryDirectory() as tmp:
        engine = create_engine_for_url(f"sqlite:///{Path(tmp) / 'router-load.db'}")
        create_schema(engine)
        factory = create_session_factory(engine)
        interval = 60.0 / rate_per_minute
        started = time.perf_counter()
        next_arrival = started
        pending: list[Trigger] = []
        total = 0
        with session_scope(factory) as session:
            resident = ResidentRouterSession(session, model)
            while time.perf_counter() - started < duration_seconds:
                now = time.perf_counter()
                while next_arrival <= now:
                    pending.append(
                        Trigger(
                            source="im",
                            actor="load",
                            payload_ref=f"request {total}",
                            dedupe_key=f"load-{seed}-{total}",
                        )
                    )
                    total += 1
                    next_arrival += interval * rng.uniform(0.5, 1.5)
                if pending:
                    batch, pending = pending, []
                    resident.route_batch(batch)
                else:
                    time.sleep(0.001)
        wall = time.perf_counter() - started
        engine.dispose()
    calls = model.calls
    return RateResult(
        rate_per_minute=rate_per_minute,
        triggers=total,
        router_calls=calls,
        avg_batch_size=total / calls if calls else 0.0,
        max_batch_size=max(model.batch_sizes, default=0),
        avg_request_seconds=sum(model.durations) / calls if calls else 0.0,
        seconds_per_decision=sum(model.durations) / total if total else 0.0,
        wall_seconds=wall,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Article §7.2 Router batching stress test")
    parser.add_argument("--rates", default="1,10,20,40", help="comma-separated arrivals/minute")
    parser.add_argument("--duration", type=float, default=20.0, help="seconds per rate")
    parser.add_argument("--seed", type=int, default=11)
    parser.add_argument("--out", default=None)
    args = parser.parse_args()
    results = []
    for index, rate in enumerate(float(item) for item in args.rates.split(",")):
        result = run_rate(rate, duration_seconds=args.duration, seed=args.seed + index)
        results.append(asdict(result))
        print(
            f"rate={rate:>5.1f}/min batch={result.avg_batch_size:.2f} "
            f"request={result.avg_request_seconds:.3f}s "
            f"per-decision={result.seconds_per_decision:.3f}s"
        )
    report = {
        "experiment": "article-7.2-router-batching",
        "model": "latency-simulated (sub-linear prefill), Workers mocked",
        "base_latency_seconds": BASE_LATENCY_SECONDS,
        "prefill_exponent": PREFILL_EXPONENT,
        "duration_seconds_per_rate": args.duration,
        "rates": results,
    }
    text = json.dumps(report, ensure_ascii=False, indent=2)
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())