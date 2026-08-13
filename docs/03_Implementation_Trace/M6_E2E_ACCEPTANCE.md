# M6 — Golden Dataset & E2E Acceptance

## 目标
M6 将算法单测、Synthetic Golden、Field Golden 和跨层 E2E 分离。Synthetic 场景具有精确 Ground Truth，可进入每次 CI；Field Golden 使用真实现场 PCAP/PCM，不提交到 Git，通过独立 evidence 目录回放。

## 当前 E2E 场景
1. SIP REGISTER 认证后 403 失败
2. INVITE 404 呼叫建立失败
3. SIP 建立成功但仅单方向 RTP
4. SDP PCMA / 实际 RTP PCMU Codec mismatch
5. RTP 连续 4 包丢失
6. PCM DTMF 8803 / SIP 803 首位丢失
7. 86 ms Echo Path
8. Click/Pop
9. Active Media 350 ms Unexpected Silence
10. 正常双向 PCMA 通话负对照

每个场景至少穿过 Analyzer → Rule Facts → Rule Engine → Deterministic Diagnosis → Collection Plan 中适用的层级。

## 质量门禁
```bash
make quality-gate
```
包括：compileall、backend tests、Rule DSL、Synthetic Golden、E2E replay、E2E baseline diff。

## Field Golden
现场大文件不进入 Git：
```bash
make golden-field EVIDENCE_DIR=/data/voip-golden
```
发布验收要求所有 Field Evidence 必须存在：
```bash
make golden-field EVIDENCE_DIR=/data/voip-golden FIELD_REQUIRE_ALL=--require-all
```

当前已登记真实 Field Golden：`APF1250_CS20260807_6886043_PERIODIC_NOISE`。其它真实故障（DTMF首位丢失、单通、注册失败、Codec mismatch等）在收到对应原始 PCAP/PCM/日志后再升级为 Field Golden；在此之前只作为 Synthetic E2E，不伪装成现场 Ground Truth。

## Baseline Diff
`e2e_baselines/v1.json` 保存当前可接受观察基线。`tools/e2e_diff.py` 会标记 PASS→FAIL 为 regression，并把 anomaly/hypothesis/rule/plan 的行为漂移输出到 Markdown，便于代码评审。
