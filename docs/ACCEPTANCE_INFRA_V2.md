# VOIP Acceptance Infrastructure V2

## 目标

把“代码失败”和“基础设施阻塞”彻底分开。非代码问题必须在重型 Gate 前发现；安全可恢复的问题自动恢复；同一 exact SHA 已通过的软件 Gate 可以复用，不因 Live/网络故障重复执行。

状态模型：

- `READY`：基础设施满足当前 Gate。
- `INFRA_RECOVERED`：发现并自动恢复了安全基础设施问题。
- `TRANSIENT_INFRA_RETRYING`：网络等瞬态问题，尚未进入代码判定。
- `INFRA_BLOCKED`：基础设施无法自动恢复；不得标记为代码失败。
- `CODE_FAIL`：Runner Doctor 已 READY 后的软件/Golden Gate 失败。

## 持久目录

默认根目录：

```text
/opt/voip-acceptance/
├── golden-cache/
├── runtime/
├── state/
│   └── software-evidence/
├── logs/
└── work/
```

权威 Golden 不允许依赖 `/tmp`。`/tmp/tcpdump-2026-08-14.pcap` 只允许在首次 bootstrap 时作为迁移源，普通 CI 永远不读取它。

## Golden Registry

仓库只保存 immutable manifest：

`golden_registry/real_offline_001/manifest.json`

二进制 PCAP 位于持久 cache 或外部 registry。任何来源进入 cache 前都必须 SHA256 校验；cache 损坏时隔离并重新拉取。

## Runner Doctor

普通 Offline Gate：

```bash
python3 tools/acceptance_runner_doctor.py \
  --require-network --deep-network \
  --require-docker --require-golden --require-tshark
```

需要 Integration Stack 时：

```bash
python3 tools/acceptance_runner_doctor.py \
  --require-docker --require-stack --repair
```

`--repair` 仅允许受控启动独立 Acceptance Stack，不操作生产 Compose。

## Acceptance Stack

`deploy/acceptance_v2/docker-compose.yml` 是独立 PostgreSQL/Redis/MinIO 测试栈：

- project/network：`voip-acceptance-v2`
- 数据使用 tmpfs；reset 后无历史 Case/AnalyzerRun 漂移。
- 不发现、不启动、不连接生产 `aivoip` Compose。

命令：

```bash
python3 tools/acceptance_stack.py reset
python3 tools/acceptance_stack.py status
python3 tools/acceptance_stack.py down
```

## Exact-SHA Software Evidence

`tools/acceptance_evidence.py` 将 workflow、Frozen/Release Gate、Golden/Human Gate、Acceptance Contract、Golden manifest 和 backend requirements 一起计算 contract fingerprint。

只有 exact commit + exact fingerprint + exact Golden SHA + runtime identity 全匹配时才复用 PASS。任一输入变化都会自动 cache miss 并重跑软件 Gate。

## Host Bootstrap

宿主机只需要执行一次：

```bash
sudo VOIP_GOLDEN_001_SOURCE=/path/or/url/to/golden001.pcap \
  bash tools/bootstrap_acceptance_host.sh
```

如果首次迁移时旧 `/tmp/tcpdump-2026-08-14.pcap` 仍存在，可省略 `VOIP_GOLDEN_001_SOURCE`；bootstrap 会迁移到持久 cache。普通测试不再使用 `/tmp`。

Bootstrap 会：

1. 创建持久目录并配置 runner 权限；
2. 初始化/验证 Golden cache；
3. 准备固定 TShark 4.2.2 userspace runtime；
4. 构建 `voip-acceptance-runtime:v2.0.0`；
5. 启动独立 Acceptance Stack；
6. 运行完整 Runner Doctor。

## Workflow 策略

PR Gate 在 checkout 前先做快速 GitHub DNS/TCP/git transport 探测，避免等待十几分钟后才发现网络不可用。`actions/checkout` 有两次受控尝试；Runner Doctor READY 后才进入 Frozen/Full/Golden。

普通测试期间禁止 `apt-get install/download` 来临时补依赖；Golden 只从持久 cache 读取；TShark 只从 bootstrap 准备好的 runtime 读取。

独立 Acceptance Stack smoke 暂时是 non-blocking observability job：目的是先证明隔离栈的稳定性，不让其基础设施问题反向阻塞 Offline Merge Gate。稳定后再升级为 Integration Gate。

## 非代码人工介入边界

正常只在以下情况需要人工：

1. Runner 主机离线/硬件故障；
2. GitHub/外网持续完全不可达；
3. secret/token 被撤销或过期；
4. DUT/物理环境不可用。

PostgreSQL/Redis/MinIO 停止、Golden cache miss（有 registry source 时）、Acceptance network/service 未启动等应由基础设施层自动处理或 fail-fast 分类。
