"""Persistence service for Router-produced task results."""

from __future__ import annotations

import json

from sqlalchemy.orm import Session

from super_agent.application.router import RouteAction, RouteResult
from super_agent.storage.models import DeliveryRecord, TaskRecord, WorkerRecord


class RouterStateStore:
    """Persist a successful Router decision in one database transaction."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def persist(self, result: RouteResult) -> None:
        """Persist Task/Worker/Delivery only when Router created a task."""

        if result.action is not RouteAction.CREATED:
            if result.delivery is not None:
                self._session.add(
                    DeliveryRecord(
                        id=result.delivery.id,
                        task_id=result.delivery.task_id,
                        channel=result.delivery.channel,
                        idempotency_key=result.delivery.idempotency_key,
                        payload_ref=result.delivery.payload_ref,
                        status=result.delivery.status.value,
                        version=result.delivery.version,
                    )
                )
            self._session.flush()
            return
        if result.task is None or result.worker is None or result.delivery is None:
            raise ValueError("created route result must contain task, worker, and delivery")

        task = result.task
        worker = result.worker
        delivery = result.delivery
        self._session.add(
            TaskRecord(
                id=task.id,
                title=task.title,
                status=task.status.value,
                thread_ids=json.dumps([str(item) for item in task.thread_ids]),
                worker_id=task.worker_id,
                version=task.version,
            )
        )
        self._session.add(
            WorkerRecord(
                id=worker.id,
                task_id=worker.task_id,
                status=worker.status.value,
                capabilities=json.dumps(list(worker.capabilities)),
                version=worker.version,
            )
        )
        self._session.add(
            DeliveryRecord(
                id=delivery.id,
                task_id=delivery.task_id,
                channel=delivery.channel,
                idempotency_key=delivery.idempotency_key,
                payload_ref=delivery.payload_ref,
                status=delivery.status.value,
                version=delivery.version,
            )
        )
        self._session.flush()

