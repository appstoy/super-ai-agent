"""Turn internal runtime records into one human-facing task result.

The system tracks Triggers, Tasks, Workers, Runs, events, tool runs and
deliveries - that vocabulary is what operators need and what a person asking
for "make me an HTML page" must never have to learn. This module is the single
translation layer: given a Task id it answers the only questions a user has.

* did my task finish, and did it work?
* what did it produce, and can I open it right now?
* if it failed, why - in a sentence, not a stack trace.

Keeping the projection in one place (instead of spreading pieces of it across
endpoint handlers and the console) means the answer cannot drift from the
records: every field below is derived from storage, never invented.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from super_agent.storage.models import (
    DeliveryRecord,
    RunRecord,
    TaskRecord,
    ToolRunRecord,
    TriggerRecord,
    WorkerContextRecord,
    WorkerEventRecord,
)
from super_agent.tools.workspace import find_task_workspace

# Suffixes a browser can render or show as text without downloading a file.
_PREVIEWABLE = {".html", ".htm", ".md", ".txt", ".json", ".svg", ".css", ".js", ".py"}
# Tools whose success means "an artifact now exists".
_WRITE_TOOLS = {"file.write", "file_write"}
# Longest conclusion kept when collapsing a multi-line model answer.
_MAX_SENTENCE = 240
# Longest fallback summary kept when there is no model answer at all.
_MAX_FALLBACK = 400

STATE_TEXT = {
    "pending": ("排队中", "waiting"),
    "running": ("正在做", "active"),
    "waiting": ("等你确认", "waiting"),
    "succeeded": ("做好了", "ok"),
    "failed": ("没做成", "error"),
    "cancelled": ("已取消", "muted"),
    "timed_out": ("超时了", "error"),
    "cancel_requested": ("正在停止", "waiting"),
}


@dataclass(frozen=True)
class Artifact:
    """One file the task actually produced."""

    name: str
    path: str
    relative_path: str
    size: int
    previewable: bool
    modified_at: str | None

    def to_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "path": self.path,
            "relative_path": self.relative_path,
            "size": self.size,
            "previewable": self.previewable,
            "modified_at": self.modified_at,
        }


@dataclass(frozen=True)
class StepDetail:
    """Concrete evidence behind one step, for users who want to look inside.

    All fields are optional because different steps have different evidence:
    a tool step carries the input arguments and output; a model step carries
    the prompt excerpt and provider id; a routing step carries the filter
    decision and why it was accepted.
    """

    label: str
    content: str
    badge: str | None = None

    def to_dict(self) -> dict[str, object]:
        out: dict[str, object] = {"label": self.label, "content": self.content}
        if self.badge:
            out["badge"] = self.badge
        return out


@dataclass(frozen=True)
class Step:
    """One timeline step: a headline for everyone, details for the curious."""

    at: str | None
    text: str
    kind: str = "info"
    summary: str = ""
    details: tuple[StepDetail, ...] = ()

    def to_dict(self) -> dict[str, object]:
        out: dict[str, object] = {
            "at": self.at,
            "text": self.text,
            "kind": self.kind,
            "summary": self.summary or self.text,
        }
        if self.details:
            out["details"] = [d.to_dict() for d in self.details]
        return out


@dataclass(frozen=True)
class TaskResult:
    """Everything a user needs to judge and consume one task."""

    task_id: str
    thread_id: str | None
    title: str
    ask: str
    status: str
    state_text: str
    state_tone: str
    conclusion: str
    artifacts: tuple[Artifact, ...]
    steps: tuple[Step, ...]
    error: str | None
    started_at: str | None
    finished_at: str | None
    duration_seconds: float | None
    open_url: str | None

    def to_dict(self) -> dict[str, object]:
        return {
            "task_id": self.task_id,
            "thread_id": self.thread_id,
            "title": self.title,
            "ask": self.ask,
            "status": self.status,
            "state": {"text": self.state_text, "tone": self.state_tone},
            "conclusion": self.conclusion,
            "artifacts": [item.to_dict() for item in self.artifacts],
            "artifacts_count": len(self.artifacts),
            "has_artifacts": bool(self.artifacts),
            "steps": [item.to_dict() for item in self.steps],
            "error": self.error,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "duration_seconds": self.duration_seconds,
            "open_url": self.open_url,
        }


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _squash(text: str, limit: int = _MAX_FALLBACK) -> str:
    """Collapse a multi-line model answer into one readable run of text."""

    cleaned = " ".join(str(text or "").split())
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[:limit].rstrip() + "…"


def _clean_lines(text: str) -> list[str]:
    """Split a model answer into candidate lines, dropping structural noise.

    Models answer a file task with a headline followed by tables, bullet lists
    and fenced code. The headline is the conclusion a person wants; the rest is
    the report body, which belongs in the steps, not in a one-line verdict.
    """

    lines: list[str] = []
    in_fence = False
    for raw in str(text or "").splitlines():
        line = raw.strip()
        if line.startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence or not line:
            continue
        # Table rows, separators and bare horizontal rules carry no verdict.
        if line.startswith("|") or set(line) <= set("-|: "):
            continue
        lines.append(line)
    return lines


def _first_sentence(text: str) -> str:
    """Pull the leading sentence out of an answer, before any markup."""

    for line in _clean_lines(text):
        stripped = line.lstrip("#>*-  ").strip()
        if not stripped:
            continue
        cut = len(stripped)
        for marker in ("。", "！", "？", ". ", "! ", "? "):
            found = stripped.find(marker)
            if 0 < found < cut:
                cut = found + 1
        sentence = stripped[:cut].strip().rstrip("：:")
        if len(sentence) > _MAX_SENTENCE:
            sentence = sentence[:_MAX_SENTENCE].rstrip() + "…"
        return sentence
    return ""


def _step_text_for_event(event_type: str, summary: str) -> tuple[str, str] | None:
    """Translate one internal event into a line a user would understand."""

    detail = _squash(summary, limit=220)
    if event_type == "state_changed":
        if detail.startswith("worker started"):
            return ("开始执行", "active")
        return None
    if event_type == "context_appended":
        role = detail.strip()
        if role == "trigger":
            return ("收到任务", "info")
        if role == "tool":
            return ("用工具产出了内容", "info")
        if role == "assistant":
            return ("整理出结果", "info")
        return None
    if event_type == "result_reported":
        return ("交付结果", "ok")
    return None


def _tool_step(run: ToolRunRecord) -> Step:
    """Describe tool usage by what it achieved, not by registry name."""

    name = str(run.tool_name or "")
    ok = str(run.status) == "succeeded"
    if name in _WRITE_TOOLS:
        text = "写好了一个文件" if ok else "写文件没成功"
    elif name.startswith("file.list") or name in {"file_list", "list_tools"}:
        text = "查看了目录内容" if ok else "查看目录失败"
    elif name.startswith("file.read") or name == "file_read":
        text = "读取了需要的文件" if ok else "读取文件失败"
    else:
        text = ("调用了 " + name) if ok else (name + " 执行失败")
    stamp = _iso(_as_utc(run.finished_at or run.started_at))
    return Step(stamp, text, "ok" if ok else "error")


def _build_steps(
    events: list[WorkerEventRecord],
    tool_runs: list[ToolRunRecord],
    contexts: list[WorkerContextRecord],
    trigger: TriggerRecord | None,
) -> list[Step]:
    """Assemble a two-layer timeline: clean headline on top, deep trace inside."""

    steps: list[Step] = []

    if trigger is not None:
        trig_details: list[StepDetail] = []
        try:
            p = json.loads(trigger.payload_ref)
            if isinstance(p, dict):
                if "message" in p:
                    trig_details.append(
                        StepDetail("用户原始输入", str(p["message"]), badge="Prompt")
                    )
                if "model" in p and p["model"]:
                    trig_details.append(StepDetail("指定模型", str(p["model"]), badge="Model"))
        except (TypeError, json.JSONDecodeError):
            if trigger.payload_ref:
                trig_details.append(StepDetail("用户原始输入", trigger.payload_ref, badge="Raw"))

        decision = trigger.filter_decision or "accepted"
        desc = (
            "通过初筛（判定为需要执行的指令）"
            if decision == "accepted"
            else f"初筛判定：{decision}"
        )
        trig_details.append(StepDetail("快速通道过滤", desc, badge="Fast Path"))
        trig_details.append(
            StepDetail(
                "触发来源与线程",
                f"来源：{trigger.source} · Thread：{trigger.thread_id}",
                badge="Routing",
            )
        )

        first_at = _iso(_as_utc(trigger.received_at))
        steps.append(
            Step(
                at=first_at,
                text="收到任务并通过快速初筛",
                kind="info",
                summary="用户提交任务，快速通道判定有效并分派至执行者。",
                details=tuple(trig_details),
            )
        )

    started_evt = next(
        (
            e
            for e in events
            if e.event_type == "state_changed" and "worker started" in (e.summary or "")
        ),
        None,
    )
    if started_evt is not None:
        steps.append(
            Step(
                at=_iso(_as_utc(started_evt.created_at)),
                text="启动独立 Worker 执行上下文",
                kind="active",
                summary="分配专用执行者沙箱，加载绑定的工具策略与权限。",
                details=(
                    StepDetail(
                        "能力策略",
                        "已授予安全工作区读写能力，严格限制在任务缓存目录下",
                        badge="ToolPolicy",
                    ),
                ),
            )
        )

    for tr in tool_runs:
        tname = str(tr.tool_name or "")
        ok = str(tr.status) == "succeeded"
        kind = "ok" if ok else "error"
        tdetails: list[StepDetail] = []

        try:
            args = json.loads(tr.arguments_json or "{}")
            if isinstance(args, dict) and args:
                pretty_args = json.dumps(args, ensure_ascii=False, indent=2)
                tdetails.append(StepDetail("调用参数", pretty_args, badge="Input"))
        except (TypeError, json.JSONDecodeError):
            if tr.arguments_json:
                tdetails.append(StepDetail("调用参数", str(tr.arguments_json), badge="Input"))

        if tr.result_text:
            out_preview = tr.result_text[:1200] + ("…" if len(tr.result_text) > 1200 else "")
            tdetails.append(StepDetail("工具执行产物", out_preview, badge="Output"))
        if tr.error:
            tdetails.append(StepDetail("错误信息", tr.error, badge="Error"))

        at_stamp = _iso(_as_utc(tr.finished_at or tr.started_at))

        if tname in _WRITE_TOOLS:
            headline = "写入产物文件" if ok else "写文件失败"
            summary = "大模型调用写文件工具，将生成的代码或文档落盘保存。"
        elif tname.startswith("file.list") or tname in {"file_list", "list_tools"}:
            headline = "检查工作区文件目录" if ok else "列举目录失败"
            summary = "大模型检查当前输出目录中的已有文件，辅助确定生成路径。"
        elif tname.startswith("file.read") or tname == "file_read":
            headline = "读取引用文件内容" if ok else "读取文件失败"
            summary = "大模型按需读取前序文件内容或项目代码进行分析。"
        else:
            headline = f"调用外部工具：{tname}"
            summary = f"执行自定义或扩展工具 {tname}。"

        steps.append(
            Step(
                at=at_stamp,
                text=headline,
                kind=kind,
                summary=summary,
                details=tuple(tdetails),
            )
        )

    for ctx in contexts:
        if ctx.role == "tool" and not tool_runs:
            steps.append(
                Step(
                    at=_iso(_as_utc(ctx.created_at)),
                    text="执行工具调用并获取结果",
                    kind="ok",
                    summary="大模型通过工具完成环境交互。",
                    details=(StepDetail("工具返回", ctx.content[:800], badge="Tool"),),
                )
            )

    assistant_ctx = next(
        (c for c in reversed(contexts) if c.role == "assistant" and c.content.strip()), None
    )
    if assistant_ctx is not None:
        meta_details: list[StepDetail] = []
        try:
            m = json.loads(assistant_ctx.metadata_json or "{}")
            if isinstance(m, dict) and "provider_request_id" in m:
                meta_details.append(
                    StepDetail(
                        "供应商请求 ID", str(m["provider_request_id"]), badge="Model Gateway"
                    )
                )
        except (TypeError, json.JSONDecodeError):
            pass
        if assistant_ctx.content:
            meta_details.append(
                StepDetail("模型回答摘要", assistant_ctx.content[:600], badge="Response")
            )

        steps.append(
            Step(
                at=_iso(_as_utc(assistant_ctx.created_at)),
                text="大模型整合最终结论",
                kind="ok",
                summary="模型审阅工具执行结果，生成清晰的交付回答。",
                details=tuple(meta_details),
            )
        )

    result_evt = next((e for e in reversed(events) if e.event_type == "result_reported"), None)
    if result_evt is not None:
        steps.append(
            Step(
                at=_iso(_as_utc(result_evt.created_at)),
                text="完成结果交付与存盘",
                kind="ok",
                summary="任务已闭环，所有产物和执行记录持久化至数据库。",
            )
        )

    if not steps:
        steps.append(Step(None, "任务已记录", "info", summary="系统已收录该任务。"))

    return steps


def _collect_artifacts(
    workspace_root: Path, cache_dir_name: str, task_id: str
) -> tuple[Artifact, ...]:
    """List the files this task wrote, from its own artifact folder."""

    try:
        folder = find_task_workspace(workspace_root, task_id, cache_dir_name)
    except (OSError, ValueError):
        return ()
    if folder is None or not folder.is_dir():
        return ()
    items: list[Artifact] = []
    for path in sorted(folder.rglob("*")):
        if not path.is_file():
            continue
        try:
            stat = path.stat()
        except OSError:
            continue
        items.append(
            Artifact(
                name=path.name,
                path=path.relative_to(workspace_root).as_posix(),
                relative_path=path.relative_to(folder).as_posix(),
                size=stat.st_size,
                previewable=path.suffix.lower() in _PREVIEWABLE,
                modified_at=datetime.fromtimestamp(stat.st_mtime, UTC).isoformat(),
            )
        )
    return tuple(items)


# Internal bookkeeping references that must never be shown as an answer.
_INTERNAL_REF_PREFIXES = ("task-created:", "model:", "tool-run:", "tool-error:", "file:")


def _is_readable(text: object) -> bool:
    """True when a stored reference is prose a user can actually read.

    Deliveries hold either the Worker's answer or a pointer like
    `task-created:<uuid>`. Showing the pointer in the feed produced verdicts
    such as "task-created:e12f4226-..." for tasks that never reported text.
    """

    value = str(text or "").strip()
    if not value:
        return True  # nothing to show; the caller falls through
    return not value.startswith(_INTERNAL_REF_PREFIXES)


def _conclusion_from(
    contexts: list[WorkerContextRecord],
    delivery: DeliveryRecord | None,
    events: list[WorkerEventRecord],
) -> str:
    """Find the best available answer text for this task.

    Precedence matters: the Worker's final assistant entry is the real answer;
    the delivery row holds the same text but a cancelled or timed-out run may
    never have one; the last result_reported event is the weakest fallback.
    """

    for entry in reversed(contexts):
        if entry.role == "assistant" and str(entry.content).strip():
            return str(entry.content)
    if delivery is not None and _is_readable(delivery.payload_ref):
        return str(delivery.payload_ref)
    for event in reversed(events):
        if event.event_type == "result_reported" and _is_readable(event.summary):
            if str(event.summary or "").strip():
                return str(event.summary)
    return ""


def build_task_result(
    session: Session,
    task: TaskRecord,
    *,
    workspace_root: Path,
    cache_dir_name: str,
) -> TaskResult:
    """Project one Task into the single answer a user asked for."""

    task_id = str(task.id)
    runs = (
        session.query(RunRecord)
        .filter(RunRecord.task_id == task.id)
        .order_by(RunRecord.attempt)
        .all()
    )
    events = (
        session.query(WorkerEventRecord)
        .filter(WorkerEventRecord.task_id == task.id)
        .order_by(WorkerEventRecord.sequence)
        .all()
    )
    contexts = (
        session.query(WorkerContextRecord)
        .filter(WorkerContextRecord.task_id == task.id)
        .order_by(WorkerContextRecord.revision)
        .all()
    )
    tool_runs = (
        session.query(ToolRunRecord)
        .filter(ToolRunRecord.task_id == task.id)
        .order_by(ToolRunRecord.started_at)
        .all()
    )
    delivery = session.scalar(select(DeliveryRecord).where(DeliveryRecord.task_id == task.id))

    latest = runs[-1] if runs else None

    # The Task title is written by the Router, so it can read like an internal
    # instruction ("write the page"). A user recognising their own words is
    # what makes the feed feel like a history of what they asked for, so keep
    # the original message alongside it.
    ask = str(task.title)
    trigger: TriggerRecord | None = None
    trigger_id = next((run.trigger_id for run in reversed(runs) if run.trigger_id), None)
    if trigger_id is not None:
        trigger = session.get(TriggerRecord, trigger_id)
        if trigger is not None:
            try:
                payload = json.loads(trigger.payload_ref)
            except (TypeError, json.JSONDecodeError):
                payload = None
            if isinstance(payload, dict) and isinstance(payload.get("message"), str):
                ask = str(payload["message"])[:2000]
            elif trigger.payload_ref.strip():
                ask = trigger.payload_ref.strip()[:2000]

    status = str(task.status)
    # A live Task should read its newest Run, so a finished attempt is never
    # reported as "running"; a terminal Task already records the truth.
    if latest is not None and status in {"pending", "running", "waiting"}:
        status = str(latest.status or status)
    state_text, state_tone = STATE_TEXT.get(status, (status, "muted"))

    raw_conclusion = _conclusion_from(contexts, delivery, events)
    artifacts = _collect_artifacts(workspace_root, cache_dir_name, task_id)

    steps = _build_steps(events, list(tool_runs), contexts, trigger)

    started = _as_utc(latest.started_at) if latest is not None else None
    finished = _as_utc(latest.finished_at) if latest is not None else None
    duration: float | None = None
    if started is not None and finished is not None:
        duration = round((finished - started).total_seconds(), 1)

    error: str | None = None
    if status in {"failed", "timed_out"}:
        error = _squash(str(latest.error)) if latest is not None and latest.error else None
        if not error and raw_conclusion:
            error = _first_sentence(raw_conclusion)

    if status in {"succeeded", "running", "pending", "waiting"}:
        shown = _first_sentence(raw_conclusion)
    else:
        shown = _squash(raw_conclusion)
    if not shown:
        # A task can end without ever producing an answer - stopped early, or
        # killed before the model replied. Say what happened, not a blank card.
        shown = {
            "succeeded": "做好了，但它没有留下文字说明",
            "failed": "这次没有成功",
            "cancelled": "你把它停了，没有产出结果",
            "timed_out": "跑太久被停了，没有产出结果",
        }.get(status, state_text)

    primary = next((item for item in artifacts if item.previewable), None)
    open_url = ("/files/" + primary.path) if primary is not None else None

    resolved_thread_id = str(trigger.thread_id) if trigger and trigger.thread_id else None
    if not resolved_thread_id and task.thread_ids:
        try:
            tids = json.loads(task.thread_ids)
            if isinstance(tids, list) and tids:
                resolved_thread_id = str(tids[0])
        except (TypeError, json.JSONDecodeError):
            pass

    return TaskResult(
        task_id=task_id,
        thread_id=resolved_thread_id,
        title=str(task.title),
        ask=ask,
        status=status,
        state_text=state_text,
        state_tone=state_tone,
        conclusion=shown,
        artifacts=artifacts,
        steps=tuple(steps),
        error=error,
        started_at=_iso(started),
        finished_at=_iso(finished),
        duration_seconds=duration,
        open_url=open_url,
    )
