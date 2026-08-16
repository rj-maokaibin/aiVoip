# VOIP AI 故障助手 PRD V1.1：复现采集稳定性修订

状态：APPROVED CHANGE / 2026-08-15  
基线：`VOIP_AI_故障助手_PRD_V1.0_终稿.docx`

## 产品目标不变

Evidence First、自动 ARM、无需人工点击开始、Multi-Attempt/Multi-Call、自动
Cleanup、确定性 Analyzer 和可审计 Evidence 均保持不变。本修订不降低诊断
标准，只将平台空闲期无 PCM 流量、信号漏检和 Cleanup 失败等真实现场行为纳入
正式产品语义。

## 修订后的用户可见语义

1. `采集路径已就绪`：PCAP Ring、Debug Reader、PCM 控制动作和 Staging 均可用；
   对活动门控平台，空闲期不伪报 PCM 数据流健康。
2. `PCM 数据面已验证`：首次业务活动后的有界窗口内，实际捕获到平台要求的
   40000/50000 流量。
3. `采集降级`：首次活动后仍缺少必要方向；已有证据继续保存和分析，系统输出
   明确 Evidence Gap。
4. `设备诊断隔离`：Cleanup 未验证时禁止新 ARM，但 Recovery 可继续处理，不让
   普通调度锁永久悬挂。

## Call 识别原则

- OFFHOOK 只创建 Attempt。
- SIP INVITE 是首选 Call Binding。
- 平台 Call Connected 或可重建的 RTP progression 是确定性 fallback。
- PCM 只证明本地音频/采集数据面活动，不能单独证明 Call 已建立。
- ONHOOK 前后的最后一个冻结 Segment 必须先完成离线快速重建，之后才允许把
  Attempt 判定为无有效 Call。

## 验收补充

- Burst PCM 检出率不低于 99%。
- OFFHOOK 到 Attempt 小于 500 ms，采集操作不得阻塞 FXS 读取。
- 用户停止不依赖 watcher 退出，Cleanup/Recovery 具有独立高优先级执行能力。
- Worker Crash 后保留已 flush Segment，并在 60 秒内进入 Recovery。
- `WATCHING` 仅表示编排状态；界面必须等待运行时发布 `FXS_MONITOR_READY` 后才提示
  用户操作。AIM debug 命令必须逐条确认，reader 异常或未就绪时禁止继续显示“可采集”。
- Cleanup 失败时设备进入隔离；验证恢复前不得创建新 Active Session。
