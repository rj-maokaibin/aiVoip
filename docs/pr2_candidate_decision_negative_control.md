# PR2 CandidateDecision + Negative Control

## 目标

Raw audio detector 只负责发现候选，不直接拥有 Preliminary Evidence Report Finding 权限。
Click/Pop 与 Unexpected Silence 必须经过确定性的 CandidateDecision 后，只有 `PROMOTED` 才能进入报告 Finding。

## Decision 状态

- `PROMOTED`：已通过当前类型的 Negative Control，并具备最小正证据，可进入 Preliminary Evidence Report。
- `SUPPRESSED`：命中已知正常/可解释的负控模式，不进入异常 Finding。
- `INCONCLUSIVE`：没有足够正证据确认异常，也没有足够负证据排除；保留供研发下钻，不进入异常 Finding。

每条 Decision 必须包含 `candidate_id`、`candidate_type`、`status`、`reason_code`、`policy_version`、`time_range`、`scope`、`negative_controls`、`positive_evidence`、`raw_candidate`。

## Click/Pop V1 Negative Control

1. DTMF transient mask：候选落在 DTMF 区间及其 ±80 ms 保护窗内 -> `SUPPRESSED / NEGCTRL_DTMF_TRANSIENT`。
2. Media boundary mask：候选距离 active-media 起止边界不超过 120 ms -> `SUPPRESSED / NEGCTRL_MEDIA_BOUNDARY_TRANSIENT`。
3. Confidence promotion gate：清除负控但 detector confidence < 0.65 -> `INCONCLUSIVE`。
4. 仅清除全部负控且达到 confidence gate -> `PROMOTED`。

现场回归锚点：Click candidate `1786690964.332755`，DTMF “6” 起点约 `1786690964.323755`，相差约 9 ms，必须被 DTMF Negative Control 排除。

## Silence V1 Negative Control

1. Raw PCM silence 永远不能直接成为 Finding；没有跨层对照时为 `INCONCLUSIVE`。
2. 标准 Profile 要求同一 PCM tap/session 与 RTP stream 的 `absolute_correlation >= 0.80`，即 HIGH 相关，才允许该 RTP stream 作为 Silence counterpart。
3. Media Analyzer 必须在 decoded RTP samples 仍可用时，对 PCM silence 的同一绝对时间窗计算 RTP `event_rms_dbfs`，并同时保存前后 context dBFS。
4. 若相关 RTP 在同一时间窗也低于 quiet threshold（-52 dBFS）或相对前后 context 出现 >=6 dB 的明显源能量下降，则视为源侧/内容静音：`SUPPRESSED / NEGCTRL_MATCHED_RTP_SOURCE_SILENCE`。
5. 若相关 RTP 同窗仍 >= -42 dBFS、PCM 却处于 silence candidate，则为跨层不一致：`PROMOTED / CROSS_LAYER_SILENCE_MISMATCH_CONFIRMED`。
6. RTP 活动介于两者之间、相关度不足、counterpart 缺失或无法取得同窗样本时均保持 `INCONCLUSIVE`，禁止通过猜测升级或排除。

## Analyzer 权限

- PCM Analyzer：输出 Raw Candidate + Raw CandidateDecision。Raw Silence 只能 INCONCLUSIVE；Raw Click 仅可被 DTMF 负控 SUPPRESSED，不能自行 PROMOTE。
- Media Analyzer：拥有 active-media CandidateDecision 权限，因为它同时具备 Call media window、PCM DTMF、PCM↔RTP correlation 与 decoded RTP samples。
- Preliminary Evidence Report：只消费 `PROMOTED` Click/Silence。对于旧 Analyzer 结果没有 CandidateDecision 的情况，Report Composer 仍必须 fail-closed 重评估，不能按旧逻辑直接升级。

## Artifact Gate

Raw PCM 始终保留完整 `PCM_WAV`、Waveform、Spectrogram 供研发复核，但 detector-only Click/Silence candidate 不再生成异常试听 Clip。

只有 Media Analyzer 最终 `PROMOTED` 的候选才生成 `AUDIO_CLIP`：

- Click/Pop：默认候选前 0.5 s + 候选 + 后 0.5 s。
- Silence：默认候选前 1 s + 完整 silence interval + 后 1 s。

Promoted Clip 必须携带 `candidate_id`、`candidate_decision_status=PROMOTED`、`reason_code`、source tap/session 与时间窗。报告 Artifact Link 再执行第二层 fail-closed：没有 PROMOTED 元数据的历史 Click/Silence Clip 不进入 Preliminary Evidence Report、Feishu projection 与 Evidence Bundle。

RTP loss/high-delta clip 与 periodic interference clip 不受 CandidateDecision Gate 影响。

## Precision / Recall 边界

本 PR 采用 Precision-first，但不是简单关闭 Silence 检测。存在可信 HIGH 相关 RTP counterpart 且能够测得同窗活动时，真实 PCM/RTP silence mismatch 仍可以 PROMOTE；当正证据不足时保留 `INCONCLUSIVE`，而不是强行判异常或正常。

## 根因权限

CandidateDecision 只决定“候选是否足以成为 Preliminary Evidence Finding”，不确认 SLIC、线路、供电、接地、DSP、Codec、网络等物理 Root Cause。
