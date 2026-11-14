# ADR-0009：持久化 Run Watchdog

## 状态

已接受（Phase 7）

## 决策

- Watchdog 直接检查持久化 Run 的 heartbeat、lease 和 attempt，不把 Python 进程存活视为健康。
- Watchdog 只执行两类受限动作：回收过期 lease，以及调用有界 Run recovery 重放原 Trigger。
- 发现重复失败时只报告人工检查，不绕过 Tool Policy 或无限重试。
- CLI `super-agent watchdog` 提供一次性检查；独立 worker 在消费循环中执行同一恢复逻辑。

## 后果

- 崩溃、停滞和重复失败有可查询的证据和明确状态。
- 告警发送和复杂自动修复仍需接入后续观测/渠道系统。
