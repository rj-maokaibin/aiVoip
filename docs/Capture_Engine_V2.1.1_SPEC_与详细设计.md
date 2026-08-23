# Capture Engine V2.1.1 SPEC 与详细设计

> 项目：VOIP AI 故障助手
> 文档类型：架构 / SPEC / Detailed Design
> 版本：V2.1.1 Design Baseline
> 状态：设计基线已收敛并完成双平台真机验证；代码尚未实现
> 基线日期：2026-08-20
> 代码基线：`master`，当前已知基线提交 `9f78b33750643b2e0f9547629238447999b217fd`

---

## 1. 文档目的


## 1.1 V2.1.1 变更说明

V2.1.1 是在 V2.1 架构设计基础上，结合 APF1250/MT7621 与 APF3260-M/MT7981 两轮真机 Golden Call、Hook、DTMF、FULL_VOICE PCAP 验证后的收口版本。

相对 V2.1，本版新增/修订：

1. `segment_seconds=5` 从“待验证参数”提升为 MT7621/MT7981 首版共同默认值；
2. OFFHOOK 从“直接创建 Attempt”调整为“Attempt Candidate Start Anchor”；
3. 新增 `FXS Event Sanitizer`、`PROVISIONAL_ATTEMPT`、`FXS_HOOK_GLITCH`；
4. APF3260-M 实测出现 `ONHOOK -> 20ms -> OFFHOOK -> 20ms -> ONHOOK` Hook rebound，要求业务语义去抖，但原始事件必须完整保留；
5. Stage-2 Data Plane Verification 由单一 timeout 改为按 Channel/Event Expectation 独立起算；
6. Timeline 明确 `Source Time > Collector Receive Time > Processing Time`，Call/Attempt Binding 必须优先使用 AIM 内嵌 Source Timestamp；
7. PCM Coverage 禁止仅以 UDP inter-arrival threshold 判断 Capture Gap；
8. DTMF 改为 `FXS DTMF + Call Manager Number + SIP Request URI + PCM DTMF(需要时)` 多源融合；
9. APF1250 与 APF3260-M 的 FULL_VOICE、5s rollover、RTP continuity、PCM RX/TX、kernel drop 均完成真机验证；
10. Golden Call 已从“未来验证项”升级为已完成 Design Ground Truth；后续真机重点转为 V2.1 Failure Injection。


本文将近期围绕 VOIP AI 故障助手 Capture Engine V2.1 的多轮讨论正式固化，作为后续：

- V2.1 数据库 Migration；
- Profile / Config；
- Capture Supervisor；
- Fenced Lease / Recovery；
- Continuous PCAP Producer；
- DUT Short Spool；
- SFTP + Segment ACK；
- Coverage Ledger；
- Readiness；
- Attempt / Call；
- Quality / Report；

的统一设计基线。

本文只描述系统设计与实现约束。真机操作步骤单独见：

> `Capture_Engine_V2.1_真机验证计划.md`

---

# 2. 当前状态

## 2.1 已完成

目前已经完成以下设计收敛：

1. V1 可靠性缺陷分析；
2. V1 / V2 差异；
3. V2 基础 Continuous Capture 架构；
4. V2.1 五项优化；
5. PCAP 连续性设计；
6. Capture Supervisor；
7. Fenced Lease / `lease_epoch`；
8. Recovery / Orphan / Multi-Producer；
9. Capture Epoch；
10. DUT Short Spool + Server Durable Ring；
11. Immutable Segment；
12. Reliable SFTP；
13. Segment Ledger；
14. ACK-after-persist；
15. Coverage Ledger；
16. 三层质量模型；
17. 两阶段 Readiness；
18. Capture Session / Attempt / Call 状态机；
19. Profile / 参数分层；
20. 数据库 Schema；
21. 模块 / API 边界；
22. 故障注入与 Golden Call 验收方向。

## 2.2 尚未完成

以下内容尚未实际编码：

- `capture_v2/` 新模块；
- V2.1 DB migration；
- Profile Schema / Resolver；
- Fenced Lease；
- DUT-side fence metadata；
- Recovery Scan；
- Single Producer enforcement；
- Continuous PCAP V2；
- Short Spool；
- SFTP exact-segment transfer；
- Segment ACK；
- Server Durable Ring；
- Coverage Ledger；
- Readiness V2；
- Per-Attempt Data Plane Verification；
- Quality Snapshot；
- V2.1 Report integration；
- V2.1 Failure Injection Gate。

当前状态应定义为：

```text
Architecture / SPEC Ready
Implementation Not Started
```

---


## 2.3 双平台真机 Design Gate

### APF1250 / MT7621

已验证：

```text
Voice Context            PASS
FXS Hook Anchor          PASS
FXS DTMF                 PASS
FULL_VOICE PCAP          PASS
5s Segment Rollover      PASS
RTP Continuity           PASS
PCM RX/TX                PASS
Kernel Capture Drop      0
Resource Feasibility     PASS
```

Golden Call 稳定媒体阶段约：

```text
~392 KB / 5s
~4.5 MiB/min
```

说明 FULL_VOICE 本身可承受，但 `/tmp` 约 60 MiB，进一步证明 DUT 必须采用 Short Spool，Server 才是长期 Rolling Ring。

### APF3260-M / MT7981

已验证：

```text
Voice Context            PASS
FXS Hook Anchor          PASS
FXS DTMF                 PASS（有效样本全通过；人工漏按样本排除）
FULL_VOICE PCAP          PASS
5s Segment Rollover      PASS
RTP Continuity           PASS
PCM RX/TX                PASS
Kernel Capture Drop      0
Resource Feasibility     PASS
```

Golden Call 稳定媒体阶段约：

```text
~342 KB / 5s
~3.9 MiB/min
```

同时发现一次：

```text
ONHOOK
  ↓ 20ms
OFFHOOK
  ↓ 20ms
ONHOOK
```

AIM/业务 FSM 确实进入了 OFFHOOK 流程，因此这不是简单日志重复。V2.1.1 必须在 Raw Event 与 Attempt FSM 之间增加 FXS Event Sanitizer。

### 双平台共同结论

可以正式冻结：

```text
capture.mode = FULL_VOICE
snaplen = 0
segment_seconds = 5
Producer = Capture Session scoped continuous producer
OFFHOOK/ONHOOK 不控制 tcpdump
PCM_RX/TX = CONDITIONAL_REQUIRED
AIM Source Timestamp 为主时间源
DUT = Short Spool
Server = Durable Rolling Ring
```


---

# 3. V1 已确认的结构问题

V1 不是“完全不能工作”，而是无法满足高可靠自动采集要求。

主要缺陷：

1. FXS Ready 可能早于真实 Ring Producer Ready；
2. OFFHOOK/ONHOOK 与采集生命周期耦合；
3. PCAP Producer 由 Python 对象 `_ring_prefix` 等进程内状态跟踪；
4. Worker / Object 重建后无法发现旧 Producer；
5. 实机已经发现同一 DUT 存在多个历史 tcpdump Producer；
6. PCAP 下载路径存在“传输 + 删除”耦合；
7. SSH timeout 通用重试可能重复执行有副作用命令；
8. 下载时间 / 分析时间与 Capture Source Time 混用；
9. Session 累积 PCM Health 会掩盖后续 Attempt 异常；
10. Derived Evidence 存在过度乐观地标记 COMPLETE 的风险；
11. Cleanup 某些路径可能吞异常；
12. Ring BPF 当前硬编码 `udp`，未来可能漏 SIP/TCP/TLS 等。

因此 V2.1 不是简单修改 `tcpdump -G` 参数，而是重新定义 Capture Ownership、Evidence Delivery 和 Evidence Trust。

---

# 4. V2.1 目标

## 4.1 核心目标

> AI 判断需要复现后，系统自动建立已经真实就绪的采集平面。现场只正常使用电话，不再人工控制开始/停止采集。

业务链：

```text
AI 判断需要复现
        |
        v
自动 SSH DUT
        |
        v
Fenced Lease + Recovery
        |
        v
PCAP / FXS / PCM / Debug Ready
        |
        v
CAPTURE_PATH_READY
        |
        v
现场自然摘机、拨号、通话、挂机
        |
        v
Attempt / Call 自动识别
        |
        v
Evidence Durable
        |
        v
Coverage / Quality / Analysis
        |
        v
Cleanup
```

## 4.2 可靠性目标

V2.1 不承诺“物理世界任何情况下绝对一包不丢”。

V2.1 承诺：

> 系统自身不能静默造成 Evidence Loss。凡是无法证明完整的窗口，必须明确记录 GAP，并使相关证据降级。

例如：

```text
Producer crash
Kernel drop
Segment missing
Server persist failed
DUT reboot
FXS reader restart
PCM capture gap
```

都不能被后续恢复“洗成 COMPLETE”。

---

# 5. V2.1 硬不变量

以下属于代码级 Invariants，不允许 Profile 关闭：

```text
I-01 ONE_LEGAL_PRODUCER_PER_DUT = true
I-02 CALL/ATTEMPT NEVER START/STOP PCAP PRODUCER
I-03 MUTATION_REQUIRES_FENCED_LEASE = true
I-04 OPEN_SEGMENT_NEVER_TRANSFER_OR_DELETE
I-05 SEALED_SEGMENT_NEVER_DELETE_BEFORE_SERVER_ACK
I-06 ACK_ONLY_AFTER_DURABLE_PERSIST_AND_DB_COMMIT
I-07 RETRY_SAME_IMMUTABLE_SEGMENT
I-08 UNACKED_SEGMENT_NEVER_EVICT_FOR_SPACE
I-09 CONFIRMED_CAPTURE_GAP_NEVER_ERASE
I-10 CAPTURE_COMPLETENESS_IS_DETERMINISTIC
I-11 AI_CANNOT_UPGRADE_CAPTURE_COMPLETENESS
I-12 EACH_ATTEMPT_HAS_OWN_DATA_PLANE_HEALTH
```

---

# 6. 总体架构

```text
                    Reproduction Orchestrator
                              |
                              v
                     Capture Controller
                              |
                   Fenced Lease / Recovery
                              |
          +-------------------+-------------------+
          |                                       |
          v                                       v
        DUT                                     Server
          |                                       |
   Capture Supervisor                             |
          |                                       |
  +-------+-------+                               |
  |       |       |                               |
PCAP    AIM/FXS  PCM                              |
  |                                               |
Continuous tcpdump                                |
  |                                               |
DUT Short Spool                                   |
  |                                               |
  +------------- immutable SFTP ----------------->|
                                                  |
                                         Durable Segment Store
                                                  |
                                          Server Rolling Ring
                                                  |
                                           Segment Ledger
                                                  |
                                          Coverage Ledger
                                                  |
                           +----------------------+------------------+
                           |                      |                  |
                   Capture Completeness   Signal Availability   Analyzer
                           |                      |                  |
                           +----------------------+------------------+
                                                  |
                                      Diagnostic Confidence
                                                  |
                                                Report
```

---

# 7. DUT 控制边界

系统只 SSH 控制被测 DUT。

不 SSH 控制 PBX / Voice Gateway。

命令：

```text
voip dsp diag set <gateway-ip> 40000 1 pcm_rx on
voip dsp diag set <gateway-ip> 50000 1 pcm_tx on
```

其中 `<gateway-ip>` 是 DUT 配置解析出的 Voice Gateway / PCM diag UDP destination。

当前已验证 Voice Context 获取：

```text
dev_config get -m voipServInfo
dev_config get -m voice_vlan
ip -o link show
```

解析得到：

```text
voice_gateway_ip
voice_vlan_id
voice_interface = br-lan_<vlan>
```

该解析逻辑在当前 MT7621 / MT7981 设备上可以作为 Common Layer。

---

# 8. Producer 生命周期

## 8.1 Session Scoped，不是 24x7

PCAP Producer 生命周期：

```text
AI 判定需要复现
    |
    v
Capture Session
    |
    +---- Attempt 1
    +---- Attempt 2
    +---- Attempt N
    |
    v
Target / Timeout
    |
    v
Post Observation
    |
    v
Evidence Drain
    |
    v
Stop Producer
```

同一个 Session 内：

```text
tcpdump =======================================================>
```

Attempt / Call 只决定哪些时间窗口需要 PIN / 保留，不启停 tcpdump。

## 8.2 Exactly One Legal Producer

同一个 DUT 同一时刻必须满足：

```text
legal_capture_producer_count <= 1
```

启动后 Post-condition：

```text
matching_capture_producer_count == 1
```

如果发现：

```text
producer_count > 1
```

必须：

```text
CAPTURE_CONFLICT
```

禁止启动第三个 Producer。

---

# 9. Fenced Lease

## 9.1 两个 Epoch

### lease_epoch

表示当前哪个 Worker 有资格改变 DUT 状态。

例如：

```text
Worker A = 17
Worker B 接管 = 18
```

### capture_epoch

表示一段连续 tcpdump 生命周期。

例如：

```text
CAP100
PID 1833
12:00:00 ===================== 12:30:00
```

Worker 切换但 Producer 还活着：

```text
lease_epoch 17 -> 18
capture_epoch CAP100 -> CAP100
PID 1833 -> 1833
```

不产生 Capture Gap。

Producer 重启：

```text
CAP100 END
GAP
CAP101 START
```

## 9.2 DUT Fence

DUT 控制目录建议：

```text
/tmp/aivoip_capture/
├── control/
│   ├── lease_epoch
│   ├── session_id
│   ├── owner_worker
│   ├── boot_id
│   └── op.lock/
└── epochs/
```

所有 Mutation 必须携带：

```text
expected_lease_epoch
operation_id
```

包括：

- Start/Stop Producer；
- Seal / Rename / Delete；
- PCM ON/OFF；
- Debug ON/OFF；
- Cleanup。

DUT Mutation 执行：

```text
mkdir op.lock                # atomic lock
    |
read current lease_epoch
    |
expected == current ?
    |
 execute
    |
release op.lock
```

旧 Worker 的 epoch 失效后：

```text
FENCED
```

不得修改 DUT。

---

# 10. Recovery

每次 ARM 的正确顺序：

```text
Acquire DB Lease
        |
Publish DUT Fence
        |
Recovery Scan
        |
Classify
        |
Adopt / Repair / Stop Orphan
        |
Verify Exactly One
        |
Prepare Capture
        |
Ready
```

Recovery 分类：

| 类型 | 条件 | 动作 |
|---|---|---|
| R0 Clean | 无 Producer / 无 backlog | Start new epoch |
| R1 Same Session Alive | 当前 Session Producer still alive | Adopt，不重启 |
| R2 Same Session Dead | Producer dead | GAP + new epoch |
| R3 Old Session Alive | 历史 Session Producer | Recover evidence + stop orphan |
| R4 Multiple Producers | >1 owned producer | CAPTURE_CONFLICT，禁止 start |
| R5 DUT Reboot | boot_id changed / tmp lost | DUT_REBOOT_GAP + new epoch |

V2 上线迁移期间还必须识别 Legacy V1：

```text
/tmp/aiVoip_ring_*
```

并扫描 cmdline 中：

```text
-w /tmp/aiVoip_ring_
```

---

# 11. PCAP Producer

首期使用现有成熟 `tcpdump`，不自研 packet capture daemon。

目标命令概念：

```text
tcpdump -ni <voice_interface> -s 0 -U -G <segment_seconds> \
  -w <active-dir>/capture_%Y%m%d_%H%M%S.pcap
```

首期默认：

```text
Capture Mode = FULL_VOICE
```

即不写死：

```text
udp
```

原因：

- 当前现场 SIP 为 UDP/5060，但未来可能 TCP/TLS；
- Capture Plane 应保存事实；
- Observer Plane 负责协议识别。

是否后续引入 VOIP_STANDARD 过滤 Profile，由实际长期流量与隐私/资源数据决定。


V2.1.1 真机 Gate 后，首版默认值冻结为：

```text
segment_seconds = 5
```

原因：

- APF1250 / MT7621 Golden Call 跨多个 5s Segment 的双向 RTP sequence 连续；
- APF3260-M / MT7981 同样跨多个 5s Segment 连续；
- 两平台 tcpdump 均为 `0 packets dropped by kernel`；
- 5s 足够将 DUT 未上传 OPEN/SEALED 风险窗口保持在较小范围，同时 Segment 数量和 Server ingest 开销可控。

仍禁止通过“每 5 秒必须产生非空文件”判断连续性；`-G` 文件生成行为在空闲流量下仍然可能表现为 traffic-dependent。


---

# 12. PCAP 连续性

连续性分五层。

## 12.1 Acquisition Continuity

证明 Producer 是否持续存活：

- PID；
- `/proc/<pid>/stat` starttime；
- cmdline；
- capture_epoch；
- interface；
- heartbeat/watchdog；
- kernel/libpcap drop statistics（能力可用时）。

不能只：

```text
kill -0 pid
```

因为 PID 会复用。

## 12.2 Segment Continuity

Segment 唯一身份：

```text
(device_id, capture_epoch, segment_seq)
```

例如：

```text
CAP100:000123
```

Sequence 在 DUT sealed 后分配，不由 Server 接收顺序决定。

## 12.3 Transfer Continuity

```text
SEALED
 -> SFTP exact segment
 -> .part
 -> remote/local size verify
 -> PCAP validate
 -> Server SHA256
 -> Durable Persist
 -> DB Commit
 -> ACK
 -> DUT Delete
```

## 12.4 Packet / Traffic Continuity

活跃 RTP 时检查 Segment 边界 RTP seq/timestamp。

但 RTP seq gap 只是 Cross-check，不能单独认定 Capture Gap。

## 12.5 Coverage Continuity

针对某次 Call 检查：

```text
pre-trigger + call + post-trigger
```

是否覆盖已知 Producer/Segment/Channel Gap。

---

# 13. Traffic Silence 与 Gap

这是 V2.1 必须冻结的区别：

```text
没有 packet != Capture Failure
没有每 G 秒产生非空 PCAP != Capture Gap
```

空载时 tcpdump 可能产生 header-only PCAP，或文件轮转观测表现与流量有关。

因此状态必须支持：

```text
Capture = HEALTHY
Traffic = SILENT
```

禁止：

```text
if next_file_time - previous_file_time > segment_seconds:
    gap = true
```

---

# 14. DUT Short Spool

DUT 只保存：

```text
current OPEN
+
SEALED but UNACKED
```

结构：

```text
/tmp/aivoip_capture/epochs/<capture_epoch>/
├── active/
│   └── capture_....pcap        # OPEN
└── spool/
    ├── seg_000001.pcap
    ├── seg_000002.pcap
    └── ...
```

DUT 不是长期 Ring。

真正 Rolling Ring 在 Server。

优点：

- MT7621 `/tmp` 压力更低；
- DUT reboot 后已上传 Evidence 仍存在；
- 长时间 WATCHING 不需要把全部 pre-trigger 数据留 DUT。

---

# 15. Segment Seal

OPEN 文件绝不能上传或删除。

一个文件成为 SEALED 的条件：

1. 不再是 tcpdump 当前 active output；
2. 文件 size 稳定；
3. PCAP header 可读取；
4. 原子 rename 到：

```text
spool/seg_<seq>.pcap
```

Header-only 24 byte PCAP：

```text
pcap_valid = true
packet_count = 0
traffic_state = SILENT
```

首期同样上传，避免复杂优化。

---

# 16. Reliable SFTP

V2.1 必须增加专用 SFTP File API。

不得继续使用：

```text
gzip | base64
```

作为 PCAP 主传输路径。

流程：

```text
remote stat #1
    |
SFTP exact path -> local .part
    |
remote stat #2
    |
inode/size unchanged
    |
local_size == remote_size
    |
PCAP validate
    |
Server SHA256
    |
Durable Store
    |
DB commit PERSISTED
```

Retry：

```text
same capture_epoch
same segment_seq
same remote_path
```

不得“重新选择当前最老文件”。

---

# 17. ACK Protocol

ACK 定义：

> Server 授权 DUT 删除该 Segment。

只有：

```text
state == PERSISTED
```

才能 ACK。

ACK Payload 至少：

```text
device_id
session_id
lease_epoch
capture_epoch
segment_seq
remote_path
expected_inode
expected_size
operation_id
```

DUT 删除前，在 op.lock 内再次验证：

```text
lease_epoch
capture_epoch
seq/path
inode
size
not OPEN
```

才允许 delete。

ACK 前 Server crash：

```text
DUT keeps file
```

ACK 后 delete failed：

```text
Evidence still safe
Remote GC = PENDING
```

Cleanup Health 可以 DEGRADED，但 Capture Quality 不降级。

---

# 18. SSH Retry 语义

Transport 必须拆分：

## ReadOnlyDeviceTransport

允许底层自动 Retry：

- stat；
- list；
- ps；
- read；
- proc；
- SFTP GET same immutable segment。

## FencedDeviceMutator

禁止 blind retry：

- start producer；
- stop producer；
- rename/seal；
- ack/delete；
- PCM on/off；
- Debug on/off；
- Cleanup。

Mutation timeout 必须：

```text
command
 -> timeout
 -> read-back actual state
 -> decide success / retry / conflict
```

即：

```text
Observe Before Retry
```

---

# 19. Server Durable Store

开发环境：

```text
local durable filesystem
```

需要：

```text
.part -> fsync -> atomic rename
```

生产环境建议：

```text
Object Store / MinIO
```

流程：

```text
SFTP
 -> local temp
 -> verify
 -> SHA256
 -> Object PUT
 -> Object verify
 -> DB commit
 -> PERSISTED
 -> ACK DUT
```

当前项目已经存在 PostgreSQL / MinIO / local evidence storage 基础能力，可复用。

---

# 20. Segment Ledger

核心状态：

```text
REMOTE_SEALED
 -> DISCOVERED
 -> TRANSFERRING
 -> DOWNLOADED
 -> VERIFIED
 -> PERSISTING
 -> PERSISTED
 -> ACK_PENDING
 -> ACKED
 -> REMOTE_DELETE_PENDING
 -> REMOTE_DELETED
```

Retention：

```text
ROLLING
PINNED
RELEASED
```

`PERSISTED` 是 Evidence Safety Boundary。

`REMOTE_DELETED` 不是 Capture COMPLETE 的必要条件。

---

# 21. Coverage Ledger

Coverage 不是：

```text
has_pcap = true
```

而是：

> 某个 Attempt / Call 的某个 Channel，在理论需要的时间窗是否可以证明完整覆盖。

Channel：

```text
PCAP
FXS
PCM_RX
PCM_TX
DEBUG
```

Requirement：

```text
PCAP    REQUIRED
FXS     REQUIRED
PCM_RX  CONDITIONAL_REQUIRED
PCM_TX  CONDITIONAL_REQUIRED
DEBUG   OPTIONAL
```

---

# 22. Expected Window

## PCAP

```text
OFFHOOK - pre_trigger
~
ONHOOK + post_trigger
```

## FXS

```text
OFFHOOK anchor
~
ONHOOK anchor
+
Observer continuity
```

## PCM

PCM 不要求 pre-trigger 存在业务包。

真机验证表明，摘机后 PCM diag 数据可能在 SIP INVITE 之前已经出现；因此 PCM 存在两个语义：

1. **PCM Data Plane Readiness**：OFFHOOK / DSP start 后是否真的收到 40000/50000；
2. **PCM Media Coverage**：Media Active 期间 PCM RX/TX 是否持续可用。

因此 Coverage 不应简单将 PCM Expected Window 固定成 `RTP_START ~ RTP_END`，而应记录：

```text
PCM_AVAILABLE_WINDOW
MEDIA_EXPECTED_WINDOW
```

并按诊断目标取交集/覆盖关系。

尤其禁止：

```text
PCM inter-arrival > 20ms
=> PCM_CAPTURE_GAP
```

两平台实测都可能出现短时 arrival stall 后紧接 backlog burst；在 packet count、payload、PCAP Coverage 和长期 cadence 均可对账时，这属于 buffering/scheduling 行为，不应误判为 Capture Gap。

真正的 PCM Gap 必须结合：

- PCAP Capture Continuity；
- 40000/50000 长时间不可恢复空窗；
- Channel restart；
- Producer/Capture Gap；
- expected duration vs packet count；
- payload consistency；

综合判定。

---

# 23. Capture Gap

CaptureGap 是一等对象。

字段：

```text
channel
gap_start
gap_end
certainty
reason
source
```

certainty：

```text
CONFIRMED
POSSIBLE
```

如果 Required Window 与 CONFIRMED/POSSIBLE gap 重叠：

```text
不能证明完整
=> PARTIAL
```

真实 gap 永久保留。

---

# 24. 三层质量模型

## 24.1 Capture Completeness

回答：

> 采集链是否完整保存了理论要求的 Evidence？

状态：

```text
COMPLETE
PARTIAL
FAILED
```

## 24.2 Signal Availability

回答：

> 当前 Evidence 中哪些信号能够被观察？

状态：

```text
AVAILABLE
PARTIAL
UNAVAILABLE_ENCRYPTED
UNAVAILABLE_NOT_CAPTURED
UNAVAILABLE_SOURCE_FAILURE
NOT_APPLICABLE
UNKNOWN
```

例如 SIP/TLS：

```text
Capture Completeness = COMPLETE
SIP_TRANSPORT = AVAILABLE(TLS)
SIP_PLAINTEXT = UNAVAILABLE_ENCRYPTED
```

不能把它误判为 PCAP 不完整。


### 24.2.1 DTMF Evidence Fusion

FXS DTMF 不作为跨平台唯一真值。

V2.1.1 采用：

```text
FXS DTMF
+
Call Manager accumulated number
+
SIP Request-URI / called number
+
PCM DTMF（需要深挖时）
```

形成多源 Evidence。

这样系统不仅能判断“最终号码是什么”，还可以判断号码在哪一层开始出现缺失：

```text
物理/PCM存在
FXS缺失
SIP缺失
=> FXS/SLIC → aimd 路径重点怀疑

FXS完整
SIP缺失
=> Call Manager / Dial Plan / SIP assembly 路径重点怀疑
```


## 24.3 Diagnostic Confidence

针对具体 Finding / Root Cause：

```text
HIGH
MEDIUM
LOW
INSUFFICIENT_EVIDENCE
```

先由确定性规则计算 Confidence Ceiling，再允许 Analyzer / AI 在上限内判断。

例如：

```text
Gap overlaps fault moment
=> max confidence = LOW
```

AI 不得突破。

---

# 25. 两阶段 Readiness

## 25.1 Stage 1：CAPTURE_PATH_READY

必须发生在现场开始复现之前。

Required Gates：

- Fenced Lease；
- Recovery completed；
- Exactly one producer；
- Voice Context；
- interface UP；
- Capture Epoch established；
- producer PID/starttime/cmdline verified；
- DUT spool writable；
- Server Durable Store writable；
- SFTP available；
- FXS AIM Reader ready；
- PCM RX command accepted；
- PCM TX command accepted；
- Watchdog running；
- Storage healthy。

注意：

> PCM 此时只要求 CONFIGURED，不要求已经看到 packet。

因为没有实际业务时本来可能没有 40000/50000 流量。

## 25.2 Stage 2：DATA_PLANE_VERIFIED

每个 Attempt 独立执行，绝不能跨 Attempt 继承。

验证 Channel：

```text
FXS
PCAP activity
SIP
RTP if media
PCM_RX
PCM_TX
```

状态：

```text
PENDING
VERIFIED
DEGRADED
MISSING
NOT_APPLICABLE
```

### 25.2.1 禁止使用一个统一的 OFFHOOK+N 秒 Timeout

Golden Call 真机结果显示：

```text
OFFHOOK
 -> 用户拨号
 -> Digit Map / Dial Timeout
 -> DIAL COMPLETE
 -> SIP INVITE
```

OFFHOOK 到 INVITE 可以自然相隔数秒。因此：

```text
OFFHOOK + 3s 无 SIP
=> DATA_PLANE_DEGRADED
```

是错误设计。

V2.1.1 改为 **Expectation-driven timer**：

| Channel | Verification Timer 起点 |
|---|---|
| FXS | Raw OFFHOOK / Confirmed OFFHOOK |
| PCM RX/TX readiness | OFFHOOK / DSP start expectation |
| SIP | `DIAL COMPLETE` / `CALL_REQUESTED` |
| RTP | SDP / 183 / 200 形成 Media Expectation |
| PCM Media | Media Expectation / media-active window |

每个 Channel 独立记录：

```text
expectation_created_at
verification_deadline
first_seen_source_ts
status
reason
```

### 25.2.2 PCM 不用于确认 Attempt 是否真实

APF3260-M 的 20ms Hook rebound 也触发了 `start pcm sample`，因此：

```text
OFFHOOK + PCM
```

不能作为真实用户 Attempt 的充分条件。

Attempt Confirmation 应依赖：

```text
稳定 Hook
或 DTMF
或 SIP/Call business event
```

PCM 只属于 Data Plane Evidence。

---

# 26. Capture Session 状态机

```text
CREATED
 -> ACQUIRING_LEASE
 -> RECOVERING
 -> PREPARING
 -> CAPTURE_PATH_READY
 -> WATCHING
 -> TARGET_CONFIRMED
 -> POST_TARGET_OBSERVATION
 -> EVIDENCE_DRAINING
 -> COVERAGE_FINALIZING
 -> CLEANUP
 -> COMPLETED
```

异常健康可以：

```text
health = DEGRADED
```

不一定立即结束 Session。

例如 Producer 短暂死亡：

```text
GAP
 -> Recover
 -> new Capture Epoch
 -> continue WATCHING
```

覆盖 Gap 的 Attempt 为 PARTIAL，但 Session 可以继续复现。

---

# 27. Attempt 状态机

V2.1.1 将 OFFHOOK 的语义修订为：

> **FXS OFFHOOK = Attempt Candidate Start Anchor；经过 Hook Sanitization 后成为 Confirmed Attempt Start Anchor。**

Fallback 仍然保留：

```text
DTMF
SIP INVITE
RTP
```

## 27.1 FXS Event Sanitizer

新增路径：

```text
Raw AIM Event
    |
    +--> CaptureEvent Ledger        # 原始证据永久保留
    |
    v
FXS Event Sanitizer
    |
    v
Semantic Hook Event
    |
    v
Attempt FSM
```

Sanitizer 不能删除原始 Hook 事件，只改变业务语义。

APF3260-M 真机发现：

```text
正常 ONHOOK
  ↓ 20ms
OFFHOOK
  ↓ 20ms
ONHOOK
```

且 AIM FSM 实际启动/释放了 DSP，因此该现象必须被分类，而不能当成日志重复。

## 27.2 Attempt 状态

```text
IDLE
 -> PROVISIONAL
 -> CONFIRMED
 -> DATA_PLANE_VERIFYING
 -> ENDED
 -> EVIDENCE_FINALIZING
 -> EVALUATED
```

典型正常路径：

```text
Raw OFFHOOK
 -> PROVISIONAL
 -> Hook stable / DTMF / SIP
 -> CONFIRMED
```

Hook rebound：

```text
Raw OFFHOOK
 -> PROVISIONAL
 -> very short ONHOOK
 -> no DTMF
 -> no SIP
 -> no meaningful business activity
 -> FXS_HOOK_GLITCH
```

最终：

```text
Raw Evidence   保留
Attempt Count  不增加
Call           不创建
Target         不触发
```

## 27.3 Hook Glitch Policy

不能简单：

```text
duration < X
=> discard
```

因为需要区分：

- 挂机后的 rebound；
- 用户快速摘挂机；
- 通话中的 Hook Flash；
- 真正的设备 Hook 异常。

因此 Sanitizer 输入至少包括：

```text
previous semantic hook state
raw event direction
raw event source timestamp
pulse duration
time since previous confirmed ONHOOK/OFFHOOK
DTMF/SIP/PCM/Call context
```

建议 Profile 预留：

```text
hook_glitch_max_ms
post_onhook_rebound_window_ms
```

具体阈值继续 Profile 化并通过后续样本校准，不在 V2.1.1 架构层写死。

## 27.4 Provisional Fallback

FXS Observer 不完整但看到 SIP/RTP 时，仍允许创建 `PROVISIONAL_ATTEMPT`。

晚到 OFFHOOK 可进行 Anchor Refinement，并保存：

```text
anchor_revision_history
```

---

# 28. Call 状态机

```text
NONE
 -> CANDIDATE
 -> CONFIRMED
 -> SIGNALING
 -> MEDIA_ACTIVE
 -> ENDED
 -> EVIDENCE_FINALIZING
 -> QUALITY_FINALIZED
 -> ANALYZED
```

Call 不要求一定进入 MEDIA_ACTIVE。

例如：

```text
INVITE -> 486
```

Call 仍然 CONFIRMED，PCM 为 NOT_APPLICABLE。

---

# 29. Timeline 与 Source Time

V2.1.1 正式冻结三种时间：

```text
Source Time
Collector Receive Time
Processing Time
```

优先级：

```text
Source Time > Collector Receive Time > Processing Time
```

Golden Call 中观察到 AIM PTY 外层终端接收时间与 AIM 内嵌时间存在约秒级差异，而 AIM 内嵌时间与 PCAP packet timestamp 接近。因此：

- FXS / DTMF / SIP AIM 日志：优先解析 AIM 内嵌 Source Timestamp；
- PCAP：使用 packet timestamp；
- SFTP download / Analyzer time：绝不能用于业务事件 Binding；
- Source Timestamp 缺失时才降级使用 Collector Time，并在 Signal Availability / Timeline Quality 中显式标注。

---

# 30. Eventual Binding

Call Binding 使用：

```text
source timestamp
```

不使用：

```text
download time
analysis time
```

例如：

```text
OFFHOOK 00
INVITE 02
ONHOOK 05
PCAP 15 才下载
```

仍然能把 INVITE 绑定到该 Attempt。

因此：

```text
Attempt End != Evidence End
```

ONHOOK 后仍进入 Evidence Finalizing。

---

# 31. Target Confirmed

Call 发生和目标故障复现必须分开。

例如：

```text
Call #1 normal -> continue WATCHING
Call #2 normal -> continue WATCHING
Call #3 target anomaly -> TARGET_CONFIRMED
```

TARGET_CONFIRMED 后：

```text
accept_new_attempt = false
```

当前 Attempt 自然结束，再进入：

```text
POST_TARGET_OBSERVATION
```

保留尾部 Evidence。

---

# 32. Evidence Drain / Cleanup

正确顺序：

```text
Stop accepting new Attempt
 -> Post Target Window
 -> Safe Producer Stop
 -> Final OPEN Seal
 -> Required Segments PERSISTED or timeout/explicit PARTIAL
 -> PCM RX OFF
 -> PCM TX OFF
 -> Debug OFF
 -> FXS Observer stop
 -> GC ACKED spool
 -> Final Recovery Scan
 -> Release Lease
```

不能：

```text
release lease
 -> cleanup
```

因为释放后失去 Fencing 权限。

Evidence Durable 后可以并行：

```text
Cleanup DUT
Coverage / Quality / Analysis
```

不需要为了 AI 分析一直开 debug / PCM / tcpdump。

---

# 33. Profile 设计

配置层级：

```text
Hard Invariant
  >
Platform Safety Limit
  >
Capture Profile
  >
Session Request
```

建议：

```text
profiles/capture/v2.1/standard.yaml
profiles/platform/mt7621.yaml
profiles/platform/mt7981.yaml
```

Session 启动时生成 immutable：

```text
EffectiveCaptureProfile
```

并保存 JSON Snapshot，保证历史 Replay。

---

# 34. 已冻结配置

```text
capture.mode = FULL_VOICE
snaplen = 0
segment_seconds = 5

transfer.protocol = SFTP
transfer.parallelism = 1
server_sha256 = true
remote_sha256 = false

PCAP = REQUIRED
FXS = REQUIRED
PCM_RX = CONDITIONAL_REQUIRED
PCM_TX = CONDITIONAL_REQUIRED
DEBUG = OPTIONAL

timeline.primary = SOURCE_TIME
attempt.offhook_semantics = CANDIDATE_ANCHOR
dtmf.mode = MULTI_SOURCE_FUSION
```

---

# 35. 仍需 Profile 化/后续调优参数

```text
pre_trigger_seconds
post_trigger_seconds
post_target_seconds
server_rolling_retention_seconds

mt7621 spool warning/critical
mt7981 spool warning/critical

lease TTL / renew
watchdog intervals

fxs.hook_glitch_max_ms
fxs.post_onhook_rebound_window_ms

pcm_readiness_timeout
sip_expectation_timeout
rtp_expectation_timeout
pcm_media_expectation_timeout

evidence_finalize_timeout
transfer backlog thresholds
```

这些属于 Parameter Tuning，不再属于 Architecture Unknown。

---

# 36. 数据库 Schema

## 35.1 capture_sessions

```text
id UUID PK
reproduction_session_id FK UNIQUE
device_id

state
health_status

capture_profile_id
capture_profile_version
platform_profile_id
platform_profile_version
effective_profile JSONB

created_at
path_ready_at
target_confirmed_at
evidence_durable_at
ended_at

failure_code
cleanup_status
schema_version
```

## 35.2 capture_leases

```text
device_id PK
capture_session_id
owner_worker_id
lease_epoch BIGINT
state
acquired_at
renewed_at
expires_at
updated_at
version
```

一台 DUT 当前最多一个 Lease authority。

## 35.3 capture_epochs

```text
id UUID PK
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
```

## 35.4 capture_segments

```text
id UUID PK
capture_epoch_id
capture_session_id
segment_seq

remote_path
remote_inode
remote_size

state
transfer_attempts
last_transfer_error

server_temp_path
storage_key
server_size
sha256

pcap_valid
packet_count
first_packet_ts
last_packet_ts

retention_state

discovered_at
download_started_at
downloaded_at
verified_at
persisted_at
acked_at
remote_deleted_at
lease_epoch_at_ack

version
created_at
updated_at
```

关键约束：

```text
UNIQUE(capture_epoch_id, segment_seq)
```

## 35.5 capture_gaps

```text
id
capture_session_id
capture_epoch_id nullable
channel
gap_start_ts
gap_end_ts
certainty
reason_code
source
detected_at
recovered_at
details JSONB
```

## 35.6 capture_events

```text
id
capture_session_id
entity_type
entity_id
event_type
source_ts nullable
recorded_at
payload JSONB
schema_version
```

## 35.7 readiness_snapshots

```text
id
capture_session_id
capture_epoch_id

status
lease_status
recovery_status
pcap_status
fxs_status
pcm_rx_status
pcm_tx_status
storage_status
transfer_status
watchdog_status

gates JSONB
created_at
revoked_at
revoke_reason
profile_version
```

## 35.8 attempt_data_plane_verifications

```text
id
attempt_id
capture_session_id
channel
requirement
status
first_seen_ts
last_seen_ts
reason_code
details JSONB
created_at
finalized_at
```

约束：

```text
UNIQUE(attempt_id, channel)
```

## 35.9 coverage_windows

```text
id
owner_type ATTEMPT/CALL
owner_id
capture_session_id

offhook_ts
onhook_ts
expected_start_ts
expected_end_ts
pre_trigger_ms
post_trigger_ms

state
policy_version
created_at
finalized_at
```

## 35.10 coverage_tracks

```text
id
coverage_window_id
channel
requirement
expected_start_ts
expected_end_ts
coverage_status
covered_duration_ms
gap_duration_ms
unknown_duration_ms
reason_codes JSONB
created_at
finalized_at
```

## 35.11 coverage_intervals

```text
id
coverage_track_id
interval_type COVERED/GAP/UNKNOWN
start_ts
end_ts
source_type
source_id
certainty
reason_code
```

## 35.12 quality_snapshots

```text
id
owner_type
owner_id
capture_completeness
diagnostic_confidence_ceiling
coverage_policy_version
quality_policy_version
reason_codes JSONB
supersedes_id
created_at
is_current
```

Snapshot 不覆盖旧行，支持：

```text
PARTIAL (transfer pending)
 -> later COMPLETE
```

历史可审计。

真实 Producer Gap 不能因为重算变 COMPLETE。

## 35.13 signal_availability

```text
id
quality_snapshot_id
signal_type
status
reason_code
first_seen_ts
last_seen_ts
details JSONB
evidence_refs JSONB
```

---

# 37. 模块目录

建议：

```text
backend/app/capture_v2/
├── profiles/
│   ├── schema.py
│   ├── resolver.py
│   └── validator.py
├── supervisor/
│   ├── supervisor.py
│   ├── readiness.py
│   └── watchdog.py
├── lease/
│   ├── manager.py
│   └── fencing.py
├── recovery/
│   ├── scanner.py
│   ├── classifier.py
│   └── manager.py
├── pcap/
│   ├── producer.py
│   ├── spool.py
│   └── segment.py
├── fxs/
│   ├── event.py
│   ├── sanitizer.py
│   └── attempt_anchor.py
├── transfer/
│   ├── sftp.py
│   ├── worker.py
│   ├── persister.py
│   └── acknowledger.py
├── coverage/
│   ├── expected_window.py
│   ├── ledger.py
│   ├── calculator.py
│   └── policy.py
├── quality/
│   ├── completeness.py
│   ├── signals.py
│   ├── confidence.py
│   └── snapshot.py
├── timeline/
│   ├── clock.py
│   └── epoch.py
├── transport/
│   ├── readonly.py
│   └── mutator.py
└── repository/
```

---

# 38. 核心 API

## 37.1 ProfileResolver

```python
resolve(
    device_info,
    requested_profile_id,
) -> EffectiveCaptureProfile
```

负责：

```text
Capture Profile
+ Platform Profile
+ Safety Limits
+ Invariant Validation
```

## 37.2 CaptureSupervisor

```python
arm(
    capture_session_id,
    device,
    effective_profile,
) -> CapturePathReadyResult
```

内部：

```text
Lease
 -> Recovery
 -> Voice Context
 -> PCAP
 -> FXS
 -> PCM
 -> Watchdog
 -> Readiness
```

## 37.3 LeaseManager

```python
acquire(device_id, capture_session_id, worker_id)
renew(device_id, lease_epoch, worker_id)
release(device_id, lease_epoch)
```

## 37.4 RecoveryManager

```python
recover(
    device,
    capture_session_id,
    lease,
) -> RecoveryResult
```

成功 Post-condition：

```text
legal producer count is 0 or 1
```

## 37.5 ProducerManager

```python
inspect()
start(lease, capture_epoch, capture_config)
adopt(capture_epoch, producer_identity)
stop(lease, capture_epoch)
```

ProducerIdentity：

```text
pid
process_starttime
cmdline
interface
capture_epoch
```

## 37.6 SegmentTransferService

```python
transfer_exact(segment_id) -> PersistedSegment
```

严禁：

```python
transfer_oldest()
```

## 37.7 ACK

```python
ack(segment_id, lease_epoch) -> AckResult
```

## 37.8 CoverageCalculator

```python
calculate_attempt(attempt_id, policy_snapshot)
calculate_call(call_id, policy_snapshot)
```

纯数据计算，禁止 SSH DUT。

## 37.9 SignalAvailabilityEvaluator

```python
evaluate(owner_id, coverage_result, analyzed_signals)
```

只判断“可观察什么”，不判断根因。

## 37.10 QualityEvaluator

```python
evaluate_capture(coverage_result)
calculate_confidence_ceiling(
    diagnosis_type,
    capture_quality,
    signal_availability,
)
```

---

# 39. Error Codes

## Ownership

```text
LEASE_BUSY
LEASE_FENCED
LEASE_EXPIRED
LEASE_OWNER_MISMATCH
CAPTURE_CONFLICT
```

## Producer

```text
PRODUCER_START_FAILED
PRODUCER_DIED
PRODUCER_IDENTITY_MISMATCH
PRODUCER_DUPLICATED
```

## Segment

```text
SEGMENT_NOT_SEALED
SEGMENT_MISSING
REMOTE_FILE_CHANGED
SEGMENT_SEQUENCE_MISSING
SEGMENT_INTEGRITY_CONFLICT
```

## Transfer

```text
SFTP_CONNECT_FAILED
SFTP_TIMEOUT
SFTP_SHORT_READ
LOCAL_SIZE_MISMATCH
```

## Storage

```text
SERVER_STORAGE_FULL
EVIDENCE_PERSIST_FAILED
OBJECT_VERIFY_FAILED
```

## Coverage

```text
PRETRIGGER_NOT_RETAINED
POSTTRIGGER_INCOMPLETE
PCAP_PRODUCER_GAP
FXS_OBSERVER_GAP
PCM_RX_GAP
PCM_TX_GAP
EVIDENCE_FINALIZE_TIMEOUT
```

## Readiness

```text
CAPTURE_PATH_NOT_READY
DATA_PLANE_DEGRADED
READY_REVOKED
```

## FXS / Timeline

```text
FXS_HOOK_GLITCH
FXS_OBSERVER_GAP
SOURCE_TIME_UNAVAILABLE
SOURCE_TIME_REGRESSION
CHANNEL_EXPECTATION_TIMEOUT
```

---

# 40. Telemetry / Gate

P0 Telemetry：

```text
producer_count_per_dut
capture_gap_total
unacked_segment_count
unacked_bytes
oldest_unacked_age
segment_generation_rate
segment_transfer_rate
dut_spool_free_bytes
sftp_failure_rate
capture_complete_rate
capture_partial_rate
capture_failed_rate
ready_prepare_latency
```

其中：

```text
producer_count_per_dut > 1
```

必须是 P0 Alert。

---

# 41. V2.1 实施顺序

## Phase A — Foundation

- Enums；
- Profile Schema；
- Profile Resolver；
- DB Migration；
- Repositories。

## Phase B — Ownership

- CaptureLease；
- Fencing；
- Recovery Scan；
- Producer Identity；
- Exactly One Producer。

## Phase C — Reliable PCAP

- Continuous tcpdump；
- Capture Epoch；
- 5s Segment；
- Short Spool；
- Segment Seq；
- SFTP；
- Server Durable Store；
- ACK。

V2.1.1 设计阶段 Golden Call Ground Truth 已完成；C 实现完成后不重复只做正常通话验证，而是进入 **Reliable PCAP Failure Injection Gate**。

## Phase D — Readiness / FXS Semantics

- Stage 1 Capture Path Ready；
- Watchdog；
- FXS Event Sanitizer；
- PROVISIONAL_ATTEMPT；
- Hook Glitch classification；
- Stage 2 Per-Attempt / Per-Channel Data Plane Verification；
- Source Time based correlation。

## Phase E — Coverage

- CaptureGap；
- Expected Window；
- Coverage Tracks / Intervals；
- Capture Completeness。

完成后执行 Golden Call Gate #2。

## Phase F — Quality / Report

- Signal Availability；
- Confidence Ceiling；
- Quality Snapshot；
- Report Integration。

---

# 42. V2.1 Failure Injection Gate

V2.1-C/D 实现之后至少测试：

1. Worker crash while producer alive；
2. SSH disconnect；
3. Kill tcpdump；
4. Old Worker recovers after fencing；
5. Two Workers acquire concurrently；
6. SFTP 50% disconnect；
7. Server crash after download / before DB；
8. DB commit / before ACK crash；
9. ACK response lost；
10. Server storage full；
11. DUT spool pressure；
12. DUT reboot；
13. legacy orphan；
14. multiple producer conflict。

每项必须验证：

> 不静默丢 Evidence；不能证明完整则产生 GAP / PARTIAL。

---

# 43. 设计冻结结论

V2.1 的核心变化可以浓缩为：

```text
Capture 先存在
Event 只切窗口

DUT 只短时缓存
Server 才是 Durable Evidence Authority

Segment 先 Persist
再 ACK
最后 Delete

Ownership 用 Fenced Lease
不是 Python 内存状态

Quality 由 Coverage 证明
不是“有文件就 COMPLETE”

AI 只能分析 Evidence
不能修改 Evidence Truth
```

当前总体架构已经可以进入 Phase A/B/C 实现。

仍需真机冻结的只剩资源与时序参数，详见单独的真机验证计划。
