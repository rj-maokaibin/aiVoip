# Capture Engine V2.1-A/B 编码级实现方案

> 项目：VOIP AI 故障助手
> 对应设计基线：Capture Engine V2.1.1
> 范围：Phase A Foundation + Phase B Ownership
> 状态：Ready for Implementation
> 日期：2026-08-20

---

# 1. 实现目标

本阶段不实现完整 SFTP/ACK/Coverage，而先建立所有后续可靠采集的基础：

## V2.1-A Foundation

交付：

1. Capture V2 enums；
2. Capture Profile Schema；
3. Platform Profile Schema；
4. Effective Profile Resolver；
5. V2 核心 DB Schema；
6. Repository / CAS 更新模式；
7. Event Audit 基础；
8. Feature Flag / V1 Compatibility Bridge。

## V2.1-B Ownership

交付：

1. `CaptureLeaseManager`；
2. DB atomic lease acquire/renew/release；
3. `lease_epoch` monotonic fencing；
4. DUT `/tmp/aivoip_capture/control`；
5. DUT atomic `op.lock`；
6. Producer Identity；
7. Recovery Scanner；
8. Legacy `/tmp/aiVoip_ring_*` 扫描；
9. Recovery Classifier；
10. Adopt / Stop Orphan；
11. Exactly-One-Producer invariant；
12. Worker restart without capture restart；
13. Multi-producer conflict fail-closed。

本阶段完成后，应优先解决当前已经真机确认的 P0：

> 同一 DUT 多个历史 tcpdump Producer 并存。

---

# 2. 与当前代码的集成原则

当前系统存在：

```text
backend/app/reproduction/real_platform.py
```

其中已经承担：

- Voice Context；
- AIM Full Debug；
- PCM；
- FXS Reader；
- tcpdump ring；
- download；
- cleanup。

V2.1 不继续向该类增加 Lease/Recovery/Segment/Coverage。

原则：

```text
V1 RealReproductionPlatform
        |
        | Compatibility Bridge
        v
CaptureSupervisorV2
        |
        +-- LeaseManager
        +-- RecoveryManager
        +-- ProducerManager
        +-- Transport
```

初期 V1 与 V2 共存：

```text
capture_engine_version = V1 | V2
```

V2 Session 内，V2 DB / Supervisor 是 Capture Authority；V1 completeness 字段只能作为兼容派生值，不能反向覆盖 V2。

---

# 3. 建议新增目录

```text
backend/app/capture_v2/
├── __init__.py
├── enums.py
├── errors.py
├── profiles/
│   ├── __init__.py
│   ├── schema.py
│   ├── resolver.py
│   └── validator.py
├── lease/
│   ├── __init__.py
│   ├── manager.py
│   ├── repository.py
│   └── fencing.py
├── recovery/
│   ├── __init__.py
│   ├── models.py
│   ├── scanner.py
│   ├── classifier.py
│   └── manager.py
├── producer/
│   ├── __init__.py
│   ├── identity.py
│   └── manager.py
├── transport/
│   ├── __init__.py
│   ├── readonly.py
│   ├── mutator.py
│   └── shell_scripts.py
└── repository/
    ├── __init__.py
    ├── capture_session.py
    ├── capture_epoch.py
    ├── capture_event.py
    └── gap.py
```

后续 C/D/E/F 再加入：

```text
segment/
transfer/
fxs/
coverage/
quality/
```

不要在 A/B 阶段提前堆空壳模块。

---

# 4. Settings 改造

现有 `app.core.config.Settings` 已包含：

```text
profile_root
reproduction_platform_mode
reproduction_storage_mode
reproduction_capture_root
reproduction_object_root
```

V2.1-A 只增加少量系统级开关：

```python
capture_engine_version: str = "V1"

capture_v2_profile_id: str = "voip-standard"
capture_v2_worker_id: str = ""

capture_v2_lease_ttl_seconds: float = <profile/default>
capture_v2_lease_renew_seconds: float = <profile/default>
```

注意：

- `segment_seconds` 不放 `.env`，进入 Capture Profile；
- spool threshold 不放 `.env`，进入 Platform Profile；
- Hard Invariant 不提供 Settings 开关。

校验：

```python
if capture_engine_version not in {"V1", "V2"}:
    raise ValueError("CAPTURE_ENGINE_VERSION_INVALID")
```

---

# 5. Enums

项目当前使用 `StrEnum`，V2 继续一致风格。

建议新增：

```python
class CaptureSessionState(StrEnum):
    CREATED = "CREATED"
    ACQUIRING_LEASE = "ACQUIRING_LEASE"
    RECOVERING = "RECOVERING"
    PREPARING = "PREPARING"
    CAPTURE_PATH_READY = "CAPTURE_PATH_READY"
    WATCHING = "WATCHING"
    TARGET_CONFIRMED = "TARGET_CONFIRMED"
    POST_TARGET_OBSERVATION = "POST_TARGET_OBSERVATION"
    EVIDENCE_DRAINING = "EVIDENCE_DRAINING"
    COVERAGE_FINALIZING = "COVERAGE_FINALIZING"
    CLEANUP = "CLEANUP"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class CaptureHealth(StrEnum):
    HEALTHY = "HEALTHY"
    DEGRADED = "DEGRADED"
    FAILED = "FAILED"


class CaptureLeaseState(StrEnum):
    ACTIVE = "ACTIVE"
    EXPIRED = "EXPIRED"
    FENCED = "FENCED"
    RELEASING = "RELEASING"
    RELEASED = "RELEASED"


class CaptureEpochState(StrEnum):
    STARTING = "STARTING"
    RUNNING = "RUNNING"
    ENDED = "ENDED"
    FAILED = "FAILED"


class RecoveryClassification(StrEnum):
    CLEAN = "CLEAN"
    SAME_SESSION_ALIVE = "SAME_SESSION_ALIVE"
    SAME_SESSION_DEAD = "SAME_SESSION_DEAD"
    OLD_SESSION_ALIVE = "OLD_SESSION_ALIVE"
    MULTIPLE_PRODUCERS = "MULTIPLE_PRODUCERS"
    DUT_REBOOT = "DUT_REBOOT"


class RecoveryResultStatus(StrEnum):
    CLEAN = "CLEAN"
    ADOPTED = "ADOPTED"
    REPAIRED = "REPAIRED"
    CONFLICT_RESOLVED = "CONFLICT_RESOLVED"
    FAILED = "FAILED"
```

Reason code 建议继续用稳定字符串，而不是到处散落 Literal。

---

# 6. Capture Profile Schema

使用 Pydantic。

```python
class ChannelRequirement(StrEnum):
    REQUIRED = "REQUIRED"
    CONDITIONAL_REQUIRED = "CONDITIONAL_REQUIRED"
    OPTIONAL = "OPTIONAL"


class CaptureConfig(BaseModel):
    mode: Literal["FULL_VOICE"] = "FULL_VOICE"
    snaplen: int = 0
    segment_seconds: int = 5


class TransferConfig(BaseModel):
    protocol: Literal["SFTP"] = "SFTP"
    parallelism: int = 1
    server_sha256: bool = True
    remote_sha256: bool = False


class CaptureChannels(BaseModel):
    pcap: ChannelRequirement = REQUIRED
    fxs: ChannelRequirement = REQUIRED
    pcm_rx: ChannelRequirement = CONDITIONAL_REQUIRED
    pcm_tx: ChannelRequirement = CONDITIONAL_REQUIRED
    debug: ChannelRequirement = OPTIONAL


class CaptureProfile(BaseModel):
    schema_version: int
    profile_id: str
    profile_version: str
    capture: CaptureConfig
    transfer: TransferConfig
    channels: CaptureChannels
    coverage: CoverageConfig
    lease: LeaseConfig
    watchdog: WatchdogConfig
    fxs: FxsSemanticConfig
```

Hard validation：

```python
assert profile.capture.mode == "FULL_VOICE"  # V2.1 first release
assert profile.capture.snaplen == 0
assert profile.capture.segment_seconds == 5
assert profile.transfer.protocol == "SFTP"
assert profile.transfer.parallelism >= 1
assert profile.transfer.server_sha256 is True
```

不能提供：

```text
delete_before_ack
allow_multiple_producers
evict_unacked
disable_fencing
```

此类配置字段。

---

# 7. Platform Profile

```python
class PlatformProfile(BaseModel):
    schema_version: int
    platform_id: Literal["mt7621", "mt7981"]
    models: list[str]

    spool: SpoolSafetyConfig
    resource: CaptureResourceConfig
```

文件：

```text
profiles/platform/mt7621.yaml
profiles/platform/mt7981.yaml
```

模型映射首版：

```text
MT7621:
  APF1250
  APF1250-M
  APF1250-S

MT7981:
  APF3260-M
  APF1260(MG-P)
  APF1261(MG)
```

平台识别失败：

```text
PLATFORM_PROFILE_NOT_FOUND
```

禁止偷偷 fallback 到任意平台安全参数。

---

# 8. EffectiveProfileResolver

API：

```python
@dataclass(frozen=True)
class EffectiveCaptureProfile:
    capture_profile_id: str
    capture_profile_version: str
    platform_profile_id: str
    platform_profile_version: str
    resolved: dict


class EffectiveProfileResolver:
    def resolve(
        self,
        *,
        device: CaseDevice,
        requested_profile_id: str,
    ) -> EffectiveCaptureProfile:
        ...
```

顺序：

```text
read Capture Profile
 -> validate schema
 -> identify platform
 -> read Platform Profile
 -> apply platform safety cap
 -> validate invariants
 -> freeze JSON snapshot
```

DB 中保存完整 `effective_profile`。

Session 启动后禁止重新 resolve 替换。

---

# 9. A/B 阶段数据库表

A/B 先落以下表：

```text
capture_sessions
capture_leases
capture_epochs
capture_events
capture_gaps
```

`capture_segments/coverage/quality` 可在 C/E 阶段 migration 加入，避免一次 Migration 过大。

---

# 10. capture_sessions

沿用当前项目 SQLAlchemy `Mapped/mapped_column` 风格。

```python
class CaptureSession(Base):
    __tablename__ = "capture_sessions"

    id: Mapped[str]
    reproduction_session_id: Mapped[str]
    device_id: Mapped[str]

    state: Mapped[str]
    health_status: Mapped[str]

    capture_profile_id: Mapped[str]
    capture_profile_version: Mapped[str]
    platform_profile_id: Mapped[str]
    platform_profile_version: Mapped[str]
    effective_profile: Mapped[dict]

    created_at
    path_ready_at
    target_confirmed_at
    evidence_durable_at
    ended_at

    failure_code
    cleanup_status
    schema_version
```

约束：

```text
UNIQUE(reproduction_session_id)
INDEX(device_id, state)
```

---

# 11. capture_leases

建议一台 DUT 一行，`device_id` 作为 PK。

```python
class CaptureLease(Base):
    __tablename__ = "capture_leases"

    device_id: Mapped[str]  # PK

    capture_session_id: Mapped[str | None]
    owner_worker_id: Mapped[str | None]

    lease_epoch: Mapped[int]
    state: Mapped[str]

    acquired_at
    renewed_at
    expires_at
    updated_at

    version: Mapped[int]
```

为什么不是每次 Lease 一行：

- 当前需要的是“该 DUT 当前 fencing authority”；
- historical takeover 通过 CaptureEvent 保存；
- 当前行便于 DB row lock / CAS；
- `lease_epoch` 永远单调递增。

---

# 12. capture_epochs

```python
class CaptureEpoch(Base):
    __tablename__ = "capture_epochs"

    id
    capture_session_id
    device_id

    epoch_index
    epoch_token
    boot_id

    producer_pid
    producer_starttime
    producer_cmdline

    interface
    capture_mode

    lease_epoch_started
    state

    started_at
    ended_at
    end_reason

    packets_captured
    packets_received
    packets_dropped_kernel
```

约束：

```text
UNIQUE(device_id, epoch_token)
UNIQUE(capture_session_id, epoch_index)
INDEX(device_id, state)
```

---

# 13. capture_events

用于 Audit / Replay。

```python
class CaptureEvent(Base):
    id
    capture_session_id
    entity_type
    entity_id
    event_type
    source_ts
    recorded_at
    payload
    schema_version
```

B 阶段至少记录：

```text
CAPTURE_LEASE_ACQUIRED
CAPTURE_LEASE_RENEWED
CAPTURE_LEASE_FENCED
DUT_FENCE_PUBLISHED

RECOVERY_STARTED
RECOVERY_CLASSIFIED
RECOVERY_ADOPTED
RECOVERY_ORPHAN_FOUND
RECOVERY_CONFLICT_FOUND
RECOVERY_COMPLETED
RECOVERY_FAILED

CAPTURE_EPOCH_STARTED
PRODUCER_STARTING
PRODUCER_READY
PRODUCER_STOPPED
PRODUCER_DIED

DUT_REBOOT_DETECTED
CAPTURE_GAP_START
CAPTURE_GAP_END
```

---

# 14. capture_gaps

B 阶段先支持 PCAP control-plane gap：

```python
class CaptureGap(Base):
    id
    capture_session_id
    capture_epoch_id | None

    channel = "PCAP"

    gap_start_ts
    gap_end_ts

    certainty
    reason_code
    source

    detected_at
    recovered_at
    details
```

禁止 hard delete confirmed gaps。

后续 E 复用该表扩展 FXS/PCM。

---

# 15. Repository CAS 规则

不要直接：

```python
row.state = ...
session.commit()
```

用于并发敏感状态。

提供：

```python
transition(
    id,
    *,
    expected_state,
    next_state,
    values,
) -> bool
```

SQL 语义：

```sql
UPDATE ...
SET state = :next
WHERE id = :id
  AND state = :expected
```

affected rows != 1：

```text
CAPTURE_STATE_CONFLICT
```

---

# 16. LeaseManager — Acquire

API：

```python
@dataclass(frozen=True)
class LeaseToken:
    device_id: str
    capture_session_id: str
    owner_worker_id: str
    lease_epoch: int
    expires_at: datetime


class CaptureLeaseManager:
    def acquire(
        self,
        *,
        device_id: str,
        capture_session_id: str,
        owner_worker_id: str,
        now: datetime,
    ) -> LeaseToken:
        ...
```

事务语义：

```text
BEGIN
SELECT capture_leases WHERE device_id=? FOR UPDATE

if no row:
    create epoch=1 ACTIVE
elif current ACTIVE and not expired and owner != me:
    LEASE_BUSY
else:
    lease_epoch += 1
    set new owner/session ACTIVE

COMMIT
```

Takeover **必须**增加 `lease_epoch`。

即使同一个 Worker crash 后重新 acquire，也不要复用旧 epoch。

---

# 17. Lease Renew

```python
renew(token: LeaseToken, now) -> LeaseToken
```

SQL 条件：

```text
device_id
lease_epoch
owner_worker_id
state=ACTIVE
```

任一不匹配：

```text
LEASE_FENCED
```

不要通过 `device_id` 单独 renew。

---

# 18. Lease Loss 行为

冻结规则：

> `Lease Loss DOES NOT Stop Capture.`

Worker 检测 renew 失败：

```text
control_authority = LOST
stop issuing mutations
mark CONTROL_PLANE_DEGRADED
exit/control handoff
```

绝不能：

```text
renew failed
 -> kill tcpdump
```

因为这会主动制造 Capture Gap。

---

# 19. DUT Control Layout

```text
/tmp/aivoip_capture/
├── control/
│   ├── lease_epoch
│   ├── session_id
│   ├── owner_worker
│   ├── boot_id
│   └── op.lock/
└── epochs/
    └── <capture_epoch>/
```

文件用简单 text，避免要求 DUT jq/python。

---

# 20. Atomic op.lock

BusyBox compatible：

```sh
mkdir /tmp/aivoip_capture/control/op.lock
```

成功即获得锁。

锁内写：

```text
owner_pid
owner_starttime
operation_id
created_at
```

释放：

```sh
rm files...
rmdir op.lock
```

不使用普通：

```text
touch lock
```

因为不是原子竞争锁。

---

# 21. Stale op.lock Recovery

如果 `mkdir` 失败：

1. 读取 owner_pid；
2. 读取 owner_starttime；
3. `kill -0`；
4. `/proc/<pid>/stat` starttime 对比。

若：

```text
PID alive + same starttime
```

：

```text
LOCK_BUSY
```

不得 steal。

若：

```text
PID dead
or
PID reused(starttime differs)
```

：

```text
STALE_OPERATION_LOCK
```

允许清理并重新 acquire。

---

# 22. FencedDeviceMutator

接口：

```python
class FencedDeviceMutator:
    async def publish_fence(...)
    async def start_producer(...)
    async def stop_producer(...)
    async def pcm_on(...)
    async def pcm_off(...)
    async def debug_on(...)
    async def debug_off(...)
```

所有 mutation 参数：

```text
LeaseToken
operation_id
```

通用 shell retry = 0。

Timeout：

```text
execute once
 -> read back
 -> classify result
```

---

# 23. ReadOnlyDeviceTransport

接口：

```python
async def read_text(path)
async def stat(path)
async def list_dir(path)
async def list_processes()
async def read_proc_starttime(pid)
async def read_proc_cmdline(pid)
async def boot_id()
```

这些可以自动 retry。

A/B 阶段先做 read-only 和 fenced mutation 分层，即使底层仍复用当前 AsyncSSH connection。

---

# 24. ProducerIdentity

```python
@dataclass(frozen=True)
class ProducerIdentity:
    pid: int
    process_starttime: int
    cmdline: str
    interface: str
    capture_epoch: str | None
    output_root: str
    legacy: bool
```

PID 单独绝不作为 identity。

---

# 25. Recovery Scanner

扫描：

## V2

```text
/tmp/aivoip_capture/control
/tmp/aivoip_capture/epochs/*
```

## Legacy V1

```text
/tmp/aiVoip_ring_*
```

并扫描 process table：

```text
tcpdump ... -w /tmp/aiVoip_ring_...
tcpdump ... -w /tmp/aivoip_capture/...
```

输出：

```python
@dataclass
class RecoveryInventory:
    boot_id: str
    dut_fence_epoch: int | None
    v2_producers: list[ProducerIdentity]
    legacy_producers: list[ProducerIdentity]
    epoch_dirs: list[...]
    legacy_ring_dirs: list[...]
```

---

# 26. Recovery Classifier

纯函数：

```python
classify(
    *,
    session_id,
    expected_boot_id,
    inventory,
) -> RecoveryClassification
```

不要在 classifier 内执行 SSH/kill。

分类逻辑：

```text
0 producer
  -> CLEAN

1 producer + same session + identity valid
  -> SAME_SESSION_ALIVE

same session metadata but process dead
  -> SAME_SESSION_DEAD

1 old/legacy producer
  -> OLD_SESSION_ALIVE

>1 producer
  -> MULTIPLE_PRODUCERS

boot changed
  -> DUT_REBOOT
```

---

# 27. Recovery Manager

执行：

```text
Lease acquired
 -> publish fence
 -> scan
 -> classify
 -> adopt / stop / repair
 -> rescan
 -> assert legal producer count <= 1
```

必须先 resolve existing producer，再考虑 START。

---

# 28. R1 Same Session Alive

```text
old lease epoch = 30
new worker lease = 31

capture epoch CAP80
PID 5000
starttime S1
```

如果重新扫描：

```text
PID = 5000
starttime = S1
cmdline/interface/path valid
```

则：

```text
ADOPT
capture_epoch remains CAP80
producer restart = 0
capture gap = 0
```

这是 B 阶段最核心成功场景之一。

---

# 29. R2 Same Session Dead

步骤：

```text
record CAPTURE_GAP_START
close old CaptureEpoch
create new CaptureEpoch
start producer
verify
record CAPTURE_GAP_END
```

旧 Gap 不允许删除。

---

# 30. R3 Old / Legacy Producer

步骤：

```text
inventory old evidence dirs
record RECOVERY_ORPHAN_FOUND
stop exact old PID with fenced mutation
verify PID/starttime no longer exists
preserve unprocessed evidence directory
```

B 阶段可以先只“保留目录 + 记录 inventory”，C 阶段再做 legacy segment recovery/upload。

---

# 31. R4 Multiple Producers

规则：

> NEVER START THIRD PRODUCER.

步骤：

```text
record CAPTURE_CONFLICT
collect all identities
```

如果唯一能证明某一个属于当前 Session/CaptureEpoch：

```text
keep valid one
stop all others
```

如果不能唯一证明：

```text
stop all aiVoip-owned producers
verify zero
create explicit recovery gap
start exactly one clean producer
```

最终必须：

```text
producer_count == 1
```

否则：

```text
RECOVERY_FAILED
CAPTURE_PATH_READY forbidden
```

---

# 32. Producer Start

Start state：

```text
ABSENT
 -> START_REQUESTED
 -> PROCESS_CREATED
 -> PID_VERIFIED
 -> STARTTIME_VERIFIED
 -> CMDLINE_VERIFIED
 -> INTERFACE_VERIFIED
 -> OUTPUT_PATH_VERIFIED
 -> RUNNING
```

`start-stop-daemon` 返回成功不是 READY。

必须再次扫描：

```text
matching producer count == 1
```

0：

```text
PRODUCER_START_FAILED
```

>1：

```text
PRODUCER_DUPLICATED
```

---

# 33. Capture Epoch Token

建议：

```text
CAP_<session_short>_<epoch_index>_<random8>
```

例如：

```text
CAP_6A07A5_0001_a93f41e2
```

不要只用 wall-clock timestamp。

---

# 34. B 阶段 CaptureSupervisor Skeleton

```python
class CaptureSupervisorV2:
    def __init__(
        self,
        *,
        lease_manager,
        recovery_manager,
        producer_manager,
        event_repo,
        session_repo,
    ):
        ...

    async def acquire_and_recover(
        self,
        *,
        capture_session_id,
        device,
        worker_id,
    ) -> RecoveryResult:
        lease = lease_manager.acquire(...)
        await mutator.publish_fence(lease)

        session_repo.transition(
            expected=ACQUIRING_LEASE,
            next=RECOVERING,
        )

        result = await recovery_manager.recover(...)

        if not result.ok:
            ...
        return result
```

A/B 阶段先不要把 PCM/FXS/SFTP/Coverage 塞进 Supervisor。

---

# 35. CaptureEvent 的事务要求

每个重要状态变化最好做到：

```text
state update
+
event append
```

同 DB transaction。

例如 Acquire：

```text
capture_lease update
capture_session state
CAPTURE_LEASE_ACQUIRED event
```

避免状态变化成功但 audit event 丢失。

DUT mutation result 则在 SSH 返回后新事务落 Event。

---

# 36. V1 Compatibility Bridge

初期 Orchestrator：

```python
if settings.capture_engine_version == "V2":
    capture_v2_supervisor.acquire_and_recover(...)
else:
    existing_real_platform_path(...)
```

不要在 `RealReproductionPlatform` 内根据版本自我切换，这会让依赖方向混乱。

推荐由 reproduction orchestration composition root 选择实现。

---

# 37. Phase A 单元测试

必须覆盖：

### Profile

- valid standard profile；
- invalid mode；
- snaplen != 0；
- segment != 5 首版被拒或按 policy 限制；
- platform unknown；
- Platform safety cap；
- Effective Profile immutable serialization。

### Repository

- valid state CAS；
- stale expected state；
- concurrent update simulation；
- event append；
- CaptureEpoch unique constraints。

### Enums

- DB string round trip；
- unknown value fails fast。

---

# 38. Phase B 单元/集成测试

## Lease

1. first acquire => epoch 1；
2. same DUT two workers race => exactly one active；
3. expired takeover => epoch increments；
4. stale renew => FENCED；
5. stale release => FENCED；
6. current release => success。

## op.lock

1. one acquire；
2. concurrent acquire => LOCK_BUSY；
3. dead owner => stale recovery；
4. PID reused with different starttime => stale recovery；
5. live same starttime => never steal。

## Recovery

1. clean => zero producer；
2. same session alive => ADOPT；
3. same session dead => GAP；
4. one old producer => stop；
5. two legacy producers => never start third；
6. ambiguous two producers => stop all owned, then one new；
7. boot_id changed => DUT_REBOOT；
8. failed recovery => never READY。

## Producer

1. start response timeout but process exists => success via read-back；
2. start returns success but zero process => fail；
3. start results in two process => conflict；
4. stop timeout but process absent => success via read-back；
5. PID reused => identity mismatch。

---

# 39. 真机 B Gate

A/B 编码完成后，在 APF1250/APF3260-M 做：

### B-G1 Worker Crash Adopt

```text
Worker A owns lease
Producer running
kill Worker A
do not kill tcpdump
Worker B acquire new epoch
Recovery scan
ADOPT same PID/starttime
```

通过：

```text
capture_epoch unchanged
producer PID unchanged
Gap = 0
```

### B-G2 Legacy Orphan

预先启动一个 `/tmp/aiVoip_ring_*` producer。

ARM V2：

```text
must discover
must not start second before recovery
must stop/preserve orphan
must end with exactly one producer
```

### B-G3 Multiple Producers

人为制造两个 aiVoip-owned producer。

ARM：

```text
must emit CAPTURE_CONFLICT
must never briefly create a third
```

### B-G4 Lease Fencing

Worker A epoch N。
Worker B takeover epoch N+1。

A 再执行：

```text
STOP
PCM OFF
DELETE
```

全部：

```text
FENCED
```

---

# 40. A/B Definition of Done

V2.1-A/B 完成必须同时满足：

```text
[ ] Profile schema/versioning
[ ] Effective snapshot persisted
[ ] New DB models + migration
[ ] Repository CAS tests
[ ] One active lease per DUT
[ ] lease_epoch monotonic
[ ] DUT fence published
[ ] atomic op.lock
[ ] PID+starttime producer identity
[ ] V1 legacy producer scan
[ ] recovery classifier
[ ] same producer adopt after worker restart
[ ] multiple producer never starts third
[ ] failed recovery cannot READY
[ ] CaptureEvent audit complete
[ ] unit/integration tests green
[ ] APF1250 ownership gate pass
[ ] APF3260-M ownership gate pass
```

通过后才进入 V2.1-C Reliable Segment/SFTP/ACK。
