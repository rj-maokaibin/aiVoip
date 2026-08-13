# M5 Gamma — Audio/Media Golden Quality Gate

本阶段把 Audio Analyzer 从“单案例专项”扩展为可持续回归的 Golden Case 体系。

## 新增确定性能力

- Active Media Window 内异常静音：仅提升被有效音频上下文包围的长静音，避免把整段空闲/对话停顿直接当故障。
- Click/Pop V2：同时要求波形突变、短时能量抬升和宽带高频成分，降低 DTMF/普通语音瞬态误报。
- Echo Path：在 PCM TX→PCM RX 之间搜索稳定正延迟相关峰，输出 delay/correlation；只证明回声路径，不直接确认 AEC/SLIC/声学根因。
- DTMF PCM↔SIP mismatch：PCM拨号序列与后续 SIP 目标不一致时生成跨层 L2 证据，并要求补采驱动/aimd时序。
- RTP Burst Loss Golden：连续4包、ptime=20ms 必须稳定计算为 80ms 媒体缺口。

## Golden Cases

Field:
- APF1250_CS20260807_6886043_PERIODIC_NOISE：真实持续电流音/周期底噪。

Synthetic:
- SYNTH_RTP_BURST_LOSS_4X20MS
- SYNTH_ACTIVE_MEDIA_SILENCE_350MS
- SYNTH_AUDIO_CLICK_POP_IMPULSE
- SYNTH_DTMF_8803_TO_SIP_803
- SYNTH_AUDIO_ECHO_86MS

## CI Gate

```bash
make quality-gate
```

执行：
1. Python compileall
2. Backend pytest
3. Rule DSL compile
4. 全部 Synthetic Golden Regression

真实 Field Golden 不放入公开/普通 CI，因为源 PCAP 可能含现场敏感数据；在内部发布候选环境通过指定源文件单独执行。

## Evidence Discipline

- Burst Loss: L1 协议事实可以确认“丢包事件”，但网络具体丢包区间仍需多点抓包。
- Silence: Active Media Window 内为 L2 中断证据；不直接确认网络/DSP/PCM具体层。
- Click/Pop: V2 仍为 L3 候选，必须和用户异常时刻/跨层证据对齐后才能提高证据等级。
- DTMF mismatch: L2 跨层证据，证明 PCM 输入与 SIP 目标不一致；具体丢号层仍需驱动/aimd时序。
- Echo: L2/L3 回声路径证据；不能单独确认 AEC、SLIC 或声学耦合根因。
