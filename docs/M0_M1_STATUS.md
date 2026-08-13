# M0/M1 当前实现状态

## 已实现
- Monorepo + Docker Compose
- FastAPI / PostgreSQL / Alembic / Redis / Celery / MinIO
- Case / Device / Job / ActionRun / Evidence / Audit 数据模型
- Evidence SHA256、MinIO 对象存储、Presigned Download
- CredentialProvider 抽象：Mock / HTTP API
- DeviceAdapter + AsyncSSH Shell
- AIM PTY + Prompt Reader
- Action Registry / Collect Profile YAML
- `voip_basic` 9 项只读采集动作
- Collector Worker 任务执行
- Job 重试时 Action 幂等跳过已成功动作
- 基础 React Case/Evidence 页面
- 安全边界测试：未知 Action 拒绝
- Prompt 跨 chunk/超时测试

## 尚需真实环境联调
1. 公司 Credential API 请求/响应格式与鉴权方式。
2. 实际 VOIP 设备的 SSH 端口、SN、设备可达性。
3. `aim` 启动后的真实 Prompt 是否固定为 `AIM>`。
4. `sys show bind-if` / `voip sip regc show running RC1` 在目标版本上的输出差异。
5. `/data/voip` 正式数据盘路径与权限。

## 下一实现单元
- Device Platform Detection / Platform Profile 映射
- tcpdump start/stop + PCAP Evidence
- Packet Analyzer Worker + TShark Normalizer
- SIP REGISTER/INVITE Session Reconstruction
