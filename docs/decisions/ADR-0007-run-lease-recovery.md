# ADR-0007：Run 租约和协作式恢复

## 状态

已接受（Phase 7）

## 决策

- 每个持久化 Run 记录 `started_at`、`heartbeat_at`、`lease_owner`、`lease_expires_at` 和 `finished_at`。
- Worker 在每个模型/工具轮次前续租，并读取 `cancel_requested`；取消只在安全检查点生效，不伪造强制中断。
- 独立消费者通过 `RunLeaseService.recover_expired()` 将过期运行标记为 `timed_out`，并同步 Task/Worker 为失败健康状态。
- Outbox 和 Run 租约分开管理：Outbox 保证事件至少一次，Run 租约保证执行所有权和崩溃可解释性。

## 后果

- 服务重启后可以识别“仍在运行”与“已经失去执行者”的区别。
- 当前模型请求必须由 Gateway 自己提供超时；正在执行的同步网络调用只能在返回后响应取消请求。
- 自动重试仍由后续 Recovery/Watchdog 任务决定，避免租约回收直接重复有副作用的工具。
