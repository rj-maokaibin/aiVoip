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

1. DTMF transient mask：候选落在 DTMF 区间及其保护窗内 -> `SUPPRESSED / NEGCTRL_DTMF_TRANSIENT`。
2. Media boundary mask：候选紧邻 active-media 起止边界 -> `SUPPRESSED / NEGCTRL_MEDIA_BOUNDARY_TRANSIENT`。
3. Confidence promotion gate：清除负控但 detector confidence 不足 -> `INCONCLUSIVE`。
4. 仅清除全部负控且达到 confidence gate -> `PROMOTED`。

现场回归锚点：Click candidate `1786690964.332755`，DTMF “6” 起点约 `1786690964.323755`，相差约 9 ms，必须被 DTMF Negative Control 排除。

## Silence V1 Negative Control

1. Raw PCM silence 永远不能直接成为 Finding；没有跨层对照时为 `INCONCLUSIVE`。
2. 必须先有同一 PCM tap/session 与 RTP stream 的可信相关性。
3. 若相关 RTP 在同一时间窗也处于静音/明显能量下降，则为预期源静音：`SUPPRESSED / NEGCTRL_MATCHED_RTP_SOURCE_SILENCE`。
4. 只有相关 RTP 同窗仍明确活动、PCM 却静音时，才可 `PROMOTED / CROSS_LAYER_SILENCE_MISMATCH_CONFIRMED`。
5. 当前 Analyzer 结果无法直接测得同窗 RTP 活动时保持 `INCONCLUSIVE`，禁止通过猜测升级。

## Artifact Gate

Analyzer 可保留 Raw Candidate Clip 供工程下钻；Preliminary Evidence Report、Feishu projection、Evidence Bundle 只接收具有 `candidate_decision_status=PROMOTED` 的 Click/Silence `AUDIO_CLIP`。RTP loss/high-delta 与 periodic interference clip 不受本 Gate 影响。

## 根因权限

CandidateDecision 只决定“候选是否足以成为 Preliminary Evidence Finding”，不确认 SLIC、线路、供电、接地、DSP、Codec、网络等物理 Root Cause。
