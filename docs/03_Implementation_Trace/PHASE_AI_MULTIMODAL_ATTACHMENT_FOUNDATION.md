# Phase AI：多模态现场附件基础能力

日期：2026-08-16  
范围：仅当前开发环境，不包含生产部署。

## 已实现

- 飞书群聊和机器人私聊中的录音、图片继续按 Evidence First 登记，不触发设备复现。
- WAV/OGG/Opus 等常见现场录音通过 FFmpeg 受限解码后自动创建 `ANALYZE_FIELD_AUDIO`
  Job，由 `field_audio_intelligence@1.0.0` 提取格式、响度、削波、静音、Click/Pop、频谱和波形摘要。
- PNG/JPEG/GIF/WebP 自动创建 `ANALYZE_IMAGE_METADATA` Job，由
  `image_attachment_intelligence@1.0.0` 校验文件头、格式和可获得的尺寸，并使用
  Tesseract 中英文 OCR 提取脱敏文字及注册状态/Codec/版本/告警候选。OCR 结果固定为 L4。
- 当同一 Case 同时存在现场录音和媒体 PCAP 时，`field_media_alignment@1.0.0` 将录音与
  Media Analyzer 生成的 RTP/PCM WAV Artifact 做信号相关，输出偏移、抓包绝对时间和 Call-ID 映射。
- 两类 Job 均经过 L0 Policy、Case Evidence 归属校验、AnalyzerRun 审计和幂等活动 Job
  保护；结束后自动唤醒原 DiagnosisRun。
- 现场录音异常只生成 `OPEN/L3` 候选；未可靠对齐时明确跨层对齐缺口，对齐成功时展示
  偏移、绝对抓包时间和 Call-ID，但不把相关性直接提升为根因。
- 图片基础结果仍为 `METADATA_ONLY`；OCR 可读取并脱敏明确显示的文字，注册状态、Codec、
  版本和告警仅作为 `L4/OCR_CANDIDATE`。几何边缘密度、可能连接布局和主色桶也只是
  `L4/VISUAL_CANDIDATE`，不将颜色自动映射为告警，不确认拓扑关系。
- Raw PCM 在显式给出采样率、位宽、声道、符号和字节序后可分析；参数不全仍 fail closed。
- `golden_cases/multimodal_field_v1.json` 已登记 8 个合成开发基线场景并进入测试门禁；
  真实飞书语音、设备截图和成对现场录音/PCAP 仍须按现场验证清单验收。

## 当前降级边界

- Raw PCM 缺少采样率、位宽、声道和字节序时不可安全解释，系统不猜格式。
- OCR 和几何/颜色候选不理解业务语义或图片中未明确显示的信息；候选必须由原始日志或设备状态核对。
- 信号相关找不到 MEDIUM/HIGH 匹配时明确返回 `NO_RELIABLE_MATCH`；即使匹配成功，也只能
  证明媒体内容和时间偏移相似，不能单独确认故障因果或具体硬件根因。

这些限制在当前开发阶段是合理的：它们避免新增大型媒体依赖和未经 Eval 的视觉模型，
同时保证系统能够接收附件、完成可验证的预分析，并给技服一个可回答的下一步问题。

## 验证口径

- 自动化测试验证真实 Opus/OGG 解码、实际 Tesseract OCR、敏感行脱敏、信号相关、
  Worker 持久化及 DiagnosisRun 消费，不以 Mock 结果代替关键解析器行为。
- 合成 Golden 仅用于开发回归，不等同于现场效果证明。
- 现场验收输入和通过标准见 `docs/多模态现场验证清单_20260816.md`。

## 2026-08-16 开发环境验收

- 后端全量 361 项通过；Synthetic Golden 21/21、Synthetic E2E 53/53，回归数 0。
- 开发容器已重建，`/health` 正常，Alembic 为
  `0017_ai_recommendation_feedback (head)`。
- `attachment.analyze_field_audio`、`attachment.inspect_image` 和
  `attachment.align_field_media` 已注册到 Worker。
- Celery active/reserved/scheduled 队列均为空，`WATCHING=0`，活动设备锁为 0。
- 数据库仍保留 10 条 2026-08-14 的历史 `AI_DIAGNOSIS/PENDING` 记录；其无状态历史，
  当前也不在 Celery 执行或排队。本轮不删除业务记录。
