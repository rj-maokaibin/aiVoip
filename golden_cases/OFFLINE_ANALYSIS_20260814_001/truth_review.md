# Ground Truth Review — OFFLINE_ANALYSIS_20260814_001

> 本文只记录人工从原始 PCAP 独立复核得到的 Truth。生产 Analyzer 不读取本文或 `manifest.yaml.expected`。

## Source identity

- 文件：`tcpdump-2026-08-14(2).pcap`
- SHA256：`b038aa7c9a0644581f2815f654fcdee4620860796382265b178823fccba2e3f0`
- Classic PCAP / Ethernet
- Frame 数：20419
- 捕获时长：约 67.179262 s

## SIP / Call

主 Call：

- INVITE Frame 2786，时间 `1786690969.100710`
- Request-URI：`sip:601@192.168.3.200;user=phone`
- SIP Call-ID：`00ad1c804c33b255@192.168.3.200`
- 200 OK：`1786690972.052840`
- ACK：`1786690972.055640`
- BYE Frame 20412：`1786691020.535864`
- BYE 200 OK：`1786691020.556065`

因此该导入 PCAP 至少可以确定性重建 1 通完整的 `INVITE -> 200 OK -> ACK -> RTP -> BYE` Call，报告不得显示 `Call=None`。

## PCM diagnostic UDP

- `pcm_rx`: `192.168.150.4:48741 -> 192.168.3.200:40000`
  - 6525 包
  - UDP payload 160 bytes/包
- `pcm_tx`: `192.168.150.4:46812 -> 192.168.3.200:50000`
  - 6525 包
  - UDP payload 160 bytes/包

已知 `ruijie_aim_diag_v1` Profile 为 8kHz/16-bit little-endian；160 bytes = 80 samples = 10 ms 单声道 PCM。

## DTMF before INVITE

对 `pcm_rx` 独立做 DTMF 频率复核：

- `6`: 约 `1786690964.323755 ~ 1786690964.463755`
- `0`: 约 `1786690964.703755 ~ 1786690964.833755`
- `1`: 约 `1786690965.033755 ~ 1786690965.163755`

序列为 `601`，与随后 SIP INVITE target `601` 一致。

当前报告中最早的 Click/Pop candidate 在约 `1786690964.332755`，仅比数字 `6` 起始晚约 9 ms，因此该候选必须先经过 DTMF Negative Control，不能直接升级为独立爆音故障。

## RTP

主 DUT 上行流：

`192.168.150.4:10000 -> 192.168.3.200:11446`

- SSRC：1937184165
- PT=0 / PCMU
- 2423 packets
- Sequence 连续，`lost_packets=0`

反向流：

`192.168.3.200:11446 -> 192.168.150.4:10000`

- PT=0 / PCMU
- 2425 packets
- Sequence 连续，`lost_packets=0`

PBX 还把 DUT 上行媒体转发至 `192.168.150.8`；该 mirrored stream 不能让主 Call 的 HIGH_DELTA 语义变成“Packet Loss”。

### 主上行 HIGH_DELTA #1

- Previous Frame：20272
- Previous timestamp：`1786691020.135437`
- Previous Seq：46511
- Current Frame：20285
- Current timestamp：`1786691020.281520`
- Current Seq：46512
- Delta：约 146.083 ms

### 主上行 HIGH_DELTA #2

- Previous Frame：20329
- Previous timestamp：`1786691020.304160`
- Previous Seq：46519
- Current Frame：20344
- Current timestamp：`1786691020.479203`
- Current Seq：46520
- Delta：约 175.043 ms

两次事件都满足 `current_seq = previous_seq + 1`，因此 Ground Truth 是“间隔异常增大但 Sequence 未丢失”，禁止将其转写为 RTP Packet Loss。

## Periodic interference boundary

独立复核显示低能量 `pcm_rx` 存在稳定约 20 ms 周期及 150/250/350/450/550...Hz 等 50Hz 相关奇次谐波梳状结构；同类周期特征可在 DUT 上行 RTP 中观察到，反向 RTP 明显更弱/不同。

允许结论：

> 异常在被测设备本地上行音频采集路径已经可观察，并进入上行 RTP。

禁止越级结论：

> 仅凭该 PCAP 直接确认电源、接地、电话机、线路、FXS/SLIC 或 PCM 接口中的任一具体物理根因。

## Golden boundary

本 Case 验证的是 **Analysis Accuracy + Evidence/Report Semantics**。它从已有 PCAP 开始，不覆盖自动采集链，所以不应进入 Acquisition Reliability 成功率统计。
