# V2.1-B Real Environment Gate Runbook

> 目标：只验证 Ownership，不把 A/B 当成完整生产 Capture Engine。
> 正式业务仍保持 `CAPTURE_ENGINE_VERSION=V1`。

## Gate 前置

- DB 已升级至 `0027_capture_v2_foundation`。
- DUT 可 SSH root/admin。
- Voice VLAN 配置可解析到唯一 `br-lan_<vlan>` 且 `UP,LOWER_UP`。
- DUT 已确认 `/sbin/start-stop-daemon`、`/usr/bin/tcpdump` 可用。
- B-Gate 通过 `CaptureV2ABBridge` 建立 ownership，不启动 V1 watcher。

## B-G1 Worker Crash Adopt

1. Worker A acquire DUT，记录：
   - `lease_epoch=N`
   - `capture_session_id`
   - `capture_epoch`
   - `producer.pid`
   - `producer.starttime`
2. 只终止 Worker A，不执行 stop tcpdump，不删除 `/tmp/aivoip_capture`。
3. 等待 Lease 到期。
4. Worker B 对**同一 CaptureSession** establish ownership。
5. 验证：
   - `lease_epoch=N+1`
   - PID 不变
   - starttime 不变
   - capture_epoch 不变
   - Recovery=`SAME_SESSION_ALIVE / ADOPTED`
   - 新增 Gap 数=0
   - DUT aiVoip-owned tcpdump 数=1

**Pass**：Worker crash 不打断 Capture Producer。

## B-G2 Legacy Orphan

1. 预先制造或保留一个 V1：`/tmp/aiVoip_ring_*` tcpdump Producer。
2. 启动 V2 A/B ownership establish。
3. 验证：
   - Recovery 首先发现 Legacy Producer；
   - 在 Legacy 被处理完成之前，没有新 V2 Producer；
   - Legacy 对应证据目录不被粗暴删除；
   - Legacy Producer fenced stop；
   - 最终 aiVoip-owned tcpdump 数=1；
   - `RECOVERY_ORPHAN_FOUND`、`PRODUCER_STOPPED` 有审计事件。

**Pass**：不会出现 V1 + V2 双 Producer。

## B-G3 Multiple Producers / Never Third

1. 人工准备两个 aiVoip-owned tcpdump Producer。
2. 启动 V2 ownership establish。
3. 在 Recovery 全过程中高频采样：

```sh
ps | grep '[t]cpdump'
```

4. 验证：
   - 发现 `MULTIPLE_PRODUCERS`；
   - 最大 aiVoip-owned Producer 数从不超过进入 Recovery 时已有数量；
   - **绝不能出现第三个**；
   - 如果唯一当前 V2 Producer 可证明，保留它并停止 stale；
   - 否则先停止冲突 Producer，记录 Gap，再启动唯一新 Producer；
   - 最终数量=1。

**Pass**：冲突处理 fail-closed，never third。

## B-G4 Lease Fencing

1. Worker A 取得 epoch=N。
2. A 停止续约，等待过期。
3. Worker B takeover epoch=N+1，并 publish DUT fence。
4. 使用 A 的旧 token 尝试：
   - STOP Producer
   - 任意 fenced shell mutation
   - 后续 C 阶段的 PCM OFF / Segment DELETE 同样遵循该规则
5. 验证全部返回：

```text
LEASE_FENCED / AIVOIP_FENCED(exit 73)
```

6. DUT `lease_epoch` 仍为 N+1，不允许被 A 发布回 N。

**Pass**：旧 Worker 永久失去 destructive authority。

## PostgreSQL Concurrent Acquire Gate

在真实 PostgreSQL 环境使用两个独立 Session/连接同时 acquire 一个此前没有
`capture_leases` 行的 DUT：

```text
Worker A ─┐
          ├─ acquire(device D)
Worker B ─┘
```

预期：

- 只有一个 winner；
- winner `lease_epoch=1`；
- loser 不泄露 `IntegrityError`，而是稳定 `LEASE_BUSY`；
- Lease row 只有一行；
- 过期 takeover => `lease_epoch=2`。

## Gate 结果记录模板

```text
Device Model:
Software Version:
Platform: MT7621 / MT7981
Test Time:

B-G1 Worker Crash Adopt: PASS/FAIL
  epoch A:
  epoch B:
  pid before/after:
  starttime before/after:
  capture_epoch before/after:
  gaps:

B-G2 Legacy Orphan: PASS/FAIL
  legacy pid:
  final pid:
  max simultaneous owned producers:

B-G3 Multiple Producer: PASS/FAIL
  initial pids:
  maximum observed producer count:
  final pid:
  conflict event:
  gap event:

B-G4 Lease Fencing: PASS/FAIL
  stale epoch:
  current epoch:
  stale stop result:
  stale mutation result:

PostgreSQL Concurrent Acquire: PASS/FAIL
```
