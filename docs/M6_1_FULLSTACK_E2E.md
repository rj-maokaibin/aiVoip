# M6.1 Full-Stack E2E

## 目标

将 M6 的“Analyzer → Rule → Diagnosis → Plan”跨层回归升级为真实服务链路：

```text
HTTP Case API
  → PostgreSQL
  → Evidence Upload
  → MinIO
  → Redis / Celery
  → diagnosis-worker
  → media-worker
  → TShark / PCM / RTP / Periodic Analyzer
  → AnalyzerRun / Artifact
  → Rule Engine / Hypothesis
  → Diagnosis Report
  → MinIO HTML/JSON
  → API 回读
```

## 测试模式

### 1. Self-contained Smoke

`make fullstack-smoke`

`tools/fullstack_fixture.py` 自动生成一份约 3 秒的测试 PCAP：

- SIP INVITE / 200 / ACK / BYE
- G.711A 双向 RTP
- UDP 40000 `pcm_rx`
- `pcm_rx` 和上行 RTP 具有 20 ms 周期、10 ms 反相、150/250/350/...Hz 梳状谱
- 下行 RTP 使用非周期噪声作为负对照

目标是自动得到 `LOCAL_CAPTURE_PERIODIC_INTERFERENCE / SUPPORTED`。

### 2. Field Full-stack

```bash
make fullstack-field FIELD_PCAP=/data/voip-golden/8b72929e-8a06-4f1e-a922-1d3779ebbd6f.pcap
```

大 PCAP 不进入 Git；脚本临时复制到 `e2e_runtime/evidence/`，测试后 Docker E2E volume 会被清理。

## Full-stack Assertions

至少检查：

1. `/health/ready` 中 PostgreSQL、Redis、MinIO 均 ready。
2. `media` 与 `diagnosis` Celery queue 可见。
3. Case 写入 PostgreSQL。
4. PCAP 写入 MinIO，SHA256 与 size 持久化。
5. `AI_DIAGNOSIS` Job 创建。
6. 第一轮 Diagnosis 自动生成 `RUN_MEDIA_ANALYSIS`。
7. `ANALYZE_MEDIA` 子 Job 由 media-worker 执行。
8. `AnalyzerRun(media_intelligence)` 为 `SUCCEEDED/PARTIAL_SUCCESS`。
9. Media Result 至少识别 2 路 RTP 和 1 个周期干扰路径。
10. 第二轮 Diagnosis 得到 `LOCAL_CAPTURE_PERIODIC_INTERFERENCE / SUPPORTED`，置信度 ≥0.90。
11. 不自动生成 CONFIRMED 硬件根因。
12. 不把该测试误判成 RTP Packet Loss 根因。
13. MinIO 中存在 WAV 等 Artifact。
14. Audit 包含 Evidence upload / Media analysis / Diagnosis cycle。
15. HTML/JSON Diagnosis Report 能从 MinIO 回读。
16. Case 最终状态为 `DIAGNOSED`。

## 高可用补充

M6.1 同时增加一个 RTP 解析降级：当 TShark 正常运行但没有把动态 UDP 端口绑定为 RTP 时，系统会保留 TShark 的 SIP/SDP 事实，同时使用受限 RTP fallback 补充媒体层。只有满足最小包数、SSRC/PT 一致和 Sequence 连续性门限的流才会进入该 fallback，且 Analyzer 明确标记 `PARTIAL_SUCCESS`。

## 失败诊断

`tools/fullstack_e2e.sh` 使用 `trap` 自动保存：

- `e2e_runtime/logs/compose-ps.txt`
- `e2e_runtime/logs/stack.log`
- `e2e_runtime/results/fullstack_result.json`

设置 `KEEP_E2E_STACK=1` 可在失败后保留容器继续人工排查。

## 发布门禁

```bash
make quality-gate       # Unit + Rules + Golden + deterministic E2E
make fullstack-smoke    # Docker full-stack
make release-gate       # 两者合并
```

真实现场版本发布前：

```bash
make field-release-gate FIELD_PCAP=/data/voip-golden/<field>.pcap
```
