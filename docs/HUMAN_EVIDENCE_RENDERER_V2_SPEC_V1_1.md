# Human Evidence Renderer V2 / 音频可视化与诊断报告 SPEC V1.1

状态：IMPLEMENTATION BASELINE  
基线：`master`  
目标分支：`feat/human-evidence-renderer-v2`  

## 1. 目标

Human Evidence Renderer V2 用于飞书、HTML 和 Web 的人类可读证据展示；现有 `evidence-renderer-v2` 继续作为 Machine Renderer，服务 Golden / CI / Audit。

核心原则：**Single Truth Source**。Analyzer / Canonical Evidence / Finding 是唯一事实源；Human Renderer 只有 Presentation Authority，没有 Diagnostic Authority。

## 2. 双 Renderer

```text
Analyzer / Canonical Evidence
        |
        +-- Machine Renderer -> Golden / CI / Audit
        |
        +-- Human Renderer V2 -> Feishu / HTML / Web
```

### 2.1 Machine Renderer
- 不删除；
- 不改变旧 deterministic PNG 语义；
- 不因 Human V2 引入而重算旧 Golden checksum；
- Human V2 失败时可作为飞书 fallback。

### 2.2 Human Renderer
- 允许使用标准字体、抗锯齿、高质量绘图；
- 允许连续 Spectrum / 高对比 Spectrogram / Audacity 风格 Waveform；
- 不生成 Finding；
- 不提升 Evidence Level；
- 不确认 Root Cause；
- 不自行新增异常阈值。

## 3. 结论一致性硬门禁

Human 图中展示的事实字段必须来源于 Canonical Result / Finding 或明确标注为新的 Measurement。

禁止：
- `HIGH_DELTA` 翻译成 `PACKET_LOSS`；
- 周期性低频/工频族证据翻译成“电源故障”；
- Human UI 自行发明 PASS/FAIL 阈值；
- 为美观重新计算已有 Analyzer Truth 并覆盖原值。

新增 Measurement（例如连续 FFT 曲线）必须记录 measurement method / source artifact / time window，且不改变 Finding。

## 4. 飞书正文图片解释 P0 Contract

任何放入飞书正文的 Human Visual **必须**存在 `human_explanation`，否则不得以 Human 主图投影；可 fallback Machine 图。

结构：

```json
{
  "what_to_look_at": "这张图用于看什么",
  "observations": ["当前证据实际观察到的事实"],
  "meaning": "这些事实在当前 Finding 范围内意味着什么",
  "evidence_boundary": "当前证据不能证明什么",
  "plain_language_summary": "一句话通俗结论"
}
```

飞书图片后固定按以下顺序输出：
1. **这张图怎么看**
2. **图中发现了什么**
3. **这意味着什么**
4. **证据边界 / 不能说明什么**
5. **一句话结论**

解释只能引用 Canonical Finding 的 observation / interpretation / root_cause_boundary 及已冻结 Measurement，不能把语义说得更强。

## 5. Human Artifact Metadata

Human 图必须包含：

```json
{
  "renderer_family": "HUMAN",
  "renderer_version": "human-evidence-renderer-v2",
  "presentation_profile": "AUDACITY_INSPIRED_V1",
  "presentation_priority": 100,
  "visual_kind": "SPECTRUM|SPECTROGRAM|WAVEFORM|...",
  "source": {},
  "time_window": {},
  "anomaly_window": {},
  "finding_ids": [],
  "human_explanation": {}
}
```

Machine 图保持原 metadata；Evidence Card 选图时，同 Artifact Type 下 Human 优先，Human 不可用则 Machine fallback。

## 6. Waveform V2

- Audacity 风格连续波形；
- 主轴使用归一化 PCM amplitude（-1..1）；
- RMS 不再与主波形使用误导性的双 Y 轴硬叠；
- 异常时间窗使用半透明红色区域；
- 标明 Scope / PCM Tap / Direction / Session / Call；
- 首阶段复用现有 `WAVEFORM_JSON`，不改变 Analyzer。

后续 Multi-track：PCM RX / RTP Uplink / RTP Downlink / PCM TX 共享同一时间轴。

## 7. Spectrum V2

Human 主图必须为：**连续 FFT Spectrum + Canonical Peak Marker**。

- X：Frequency Hz；
- Y：数字满量程相对电平 dBFS（仅当由原始 PCM/WAV 规范化测量得到）；
- 默认 30 Hz ～ min(3800 Hz, Nyquist)；
- 通用音频视图可使用 log frequency scale；
- 标注 Analyzer 已有 peak / 50/60Hz / 周期梳状参考；
- `ENERGY RATIO` 保留在 Analyzer detail，不作为 Human 默认主纵轴。

FFT Measurement 首阶段：
- NumPy rFFT；
- Hann window；
- 去 DC；
- coherent-gain amplitude normalization；
- 记录 source WAV、sample rate、window、FFT size。

## 8. Spectrogram V2

- 高对比时频热力图；
- X：Time；Y：Frequency；
- 对现有未绝对标定的 spectrogram 数值统一转换为 `relative dB`，最大值归一到 0 dB；
- 不把现有 FFT magnitude 冒充 dBFS；
- 显示 color bar；
- 支持异常时间窗 overlay；
- 默认 0～4000Hz（8kHz PCM）。

## 9. DTMF Inspector（下一阶段 P0）

复用现有 Goertzel DTMF Detector；新增 Human Measurement：
- expected / measured row Hz；
- expected / measured column Hz；
- row / column dBFS；
- frequency error；
- twist；
- duration；
- strongest spur / spur margin；
- PCM sequence ↔ SIP target。

未经过 AnalyzerProfile/Golden 标定的 Frequency Tolerance / Spur Margin / THD 只能输出 `MEASURED / UNVERIFIED_THRESHOLD`，不能擅自 PASS/FAIL。

## 10. 周期性干扰

Human 报告组合：
1. Continuous Spectrum；
2. Spectrogram；
3. Periodicity / Autocorrelation summary。

必须保留边界：周期/工频族特征只能证明数字音频中存在相应频域/周期证据，不能单独确认电源、接地、话柄、SLIC 等物理根因。

## 11. RTP Timeline（后续）

主图使用人类语义：Delay Spike / Loss / Burst / Payload Change。
Frame / Seq 保留为二级证据。

`HIGH_DELTA != PACKET_LOSS` 为硬规则。

## 12. 飞书投影

每个重点 Finding：

```text
一句话结论
关键指标
Human Visual
图片详细通俗解析
Evidence Boundary
下一步验证
原始 Artifact
```

选图：
1. 同类型 Human V2；
2. Machine Renderer fallback。

Human Renderer 出错不得导致 Canonical Report 失败。

## 13. Golden

Machine Golden 不变。

Human V2 使用：
- Structural Golden；
- Numeric Golden；
- Authority Boundary Gate；
- Real Offline Golden #001 预览与人工视觉验收。

Human PNG 不作为字节级 Golden authority。

## 14. Phase H1/H2 首批实现

本 PR 范围：
- Human Renderer framework；
- Human metadata / explanation contract；
- Continuous Spectrum V2；
- Spectrogram V2；
- Waveform V2；
- Human/Machine Artifact coexistence；
- Evidence Card Human preference；
- 飞书图片详细解释；
- fallback；
- 单元测试；
- Real Offline Golden #001 预览入口。

DTMF Inspector / Multi-track / Cross-Layer / Human RTP Timeline 独立后续 PR 实现。

## 15. 中文图片语言与字体 P0 Contract

正式飞书 / HTML Human Visual 使用 **中文为主、技术缩写与工程单位保留英文** 的语言策略。

### 15.1 图片内中文范围

必须中文化：
- 主标题中的视觉类型，例如“连续频谱 / 聚焦波形 / 高分辨率时频图”；
- X/Y 坐标语义，例如“时间（s）/ 频率（Hz）/ PCM 归一化幅度 / 频谱电平（dBFS）”；
- Color Bar，例如“相对电平（dB）”；
- 异常/证据标记，例如“证据窗口”；
- 展示行为提示，例如“纵向自动放大”。

保持原工程缩写/单位：
- PCM / RTP / SIP / DTMF / STFT / FFT；
- RX / TX / Uplink / Downlink（可在正文解释中文含义）；
- Hz / ms / s / dB / dBFS / FS；
- Frame / Seq 仅作为二级 Evidence Detail。

### 15.2 字体治理

Human Renderer 不在仓库中保存或分发字体文件。

运行时按以下原则解析 CJK 字体：
1. 显式 `HUMAN_EVIDENCE_CJK_FONT_PATH`（存在且通过 CJK glyph 检查）；
2. 系统 Noto Sans CJK / Source Han Sans / WenQuanYi 等 CJK 字体；
3. 若不可用，则安全回退英文图片，不允许输出中文方框，也不得导致 Canonical Report 失败。

生产 Backend/Worker 镜像必须安装受系统包管理器管理的 CJK 字体；首版使用 `fonts-noto-cjk`。

### 15.3 字体状态与 Gate

Human Measurement metadata 必须能够记录：
- `cjk_available`；
- `font_family`；
- `source=ENV|SYSTEM|FALLBACK`；
- 缺失时 `reason=CJK_FONT_UNAVAILABLE`。

不得把实际字体文件路径投影到飞书正文。

必须有自动测试覆盖：
- CJK 可用时视觉术语中文化；
- CJK 不可用时英文 fallback；
- Spectrum / WAV Spectrogram Measurement 带 presentation font status；
- 字体能力不改变 Analyzer / Finding / Evidence Level / Root Cause Authority。
