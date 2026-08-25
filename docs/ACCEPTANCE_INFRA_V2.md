# VOIP Acceptance Infrastructure V2

## 目标

把“代码失败”和“基础设施阻塞”彻底分开。非代码问题在重型 Gate 前发现；安全可恢复的问题自动恢复；同一 exact SHA 已通过的软件 Gate 可复用，不因网络/Live 故障重复执行。

状态模型：

- `READY`：基础设施满足当前 Gate。
- `INFRA_RECOVERED`：安全基础设施问题已自动恢复。
- `TRANSIENT_INFRA_RETRYING`：网络等瞬态问题正在重试，尚未进入代码判定。
- `INFRA_BLOCKED`：基础设施无法自动恢复；不得标记为代码失败。
- `CODE_FAIL`：Runner Doctor READY 后的软件/Golden Gate 失败。

## 持久目录

```text
/opt/voip-acceptance/
├── golden-cache/
├── runtime/
│   ├── python/
│   ├── npm-cache/
│   ├── bin/tshark
│   └── state.json
├── state/
│   └── software-evidence/
├── logs/
└── work/
```

权威 Golden 不允许依赖 `/tmp`。`/tmp/tcpdump-2026-08-14.pcap` 只允许在首次 bootstrap 时作为迁移源，普通 CI 永远不读取它。

## Golden Registry

仓库只保存 immutable manifest：`golden_registry/real_offline_001/manifest.json`。

PCAP 位于持久 cache 或外部 registry。任何来源进入 cache 前必须进行 SHA256 校验；cache 损坏会先隔离再重新拉取。

## Prepared Runtime

`tools/acceptance_runtime.py` 对以下输入计算 fingerprint：

- `backend/requirements.txt`
- `frontend/package-lock.json`
- Acceptance Runtime Dockerfile
- Acceptance V2 contract

Bootstrap 阶段一次性准备：

- Python virtualenv + exact backend requirements；
- npm package cache；
- `postgres:16` / `redis:7-alpine` / MinIO image；
- `voip-acceptance-runtime:v2.0.0` image。

PR 测试只调用 `acceptance_runtime.py verify/env`，不执行 pip install、不在线 npm audit、不动态拉 Docker image。Frontend 使用 `npm ci --offline`。依赖变更导致 fingerprint stale 时会在 Runner Doctor 阶段明确 `INFRA_BLOCKED: PREPARED_RUNTIME`，而不是跑到 Full Gate 中途失败。

## Runner Doctor

普通 Offline Merge Gate：

```bash
python3 tools/acceptance_runner_doctor.py \
  --require-docker --require-golden --require-tshark --require-runtime
```

深度网络检查只用于 bootstrap/周期健康检查，不作为已成功 checkout 后的重复 Merge Gate：

```bash
python3 tools/acceptance_runner_doctor.py \
  --require-network --deep-network
```

需要 Integration Stack 时：

```bash
python3 tools/acceptance_runner_doctor.py \
  --require-docker --require-stack --repair
```

`--repair` 仅允许启动独立 Acceptance Stack，不操作生产 Compose。

## Checkout 策略

workflow 在 checkout 前做网络观测，但该 probe 本身是 `continue-on-error`：它不能因为一次公共 `ls-remote` 超时就阻断测试。

真正的 source contract 是 exact-head checkout：

1. checkout attempt 1；
2. 失败则 cooldown；
3. checkout attempt 2；
4. 两次都失败才视为基础设施阻塞。

只要 exact-head checkout 已成功，Offline Gate 后续依赖都来自本地 prepared runtime/cache，不再要求额外公网传输。

## Acceptance Stack

`deploy/acceptance_v2/docker-compose.yml` 提供独立 PostgreSQL/Redis/MinIO：

- project/network：`voip-acceptance-v2`
- 数据使用 tmpfs，reset 后没有历史 Case/AnalyzerRun 漂移；
- 不发现、不启动、不连接生产 `aivoip` Compose。

```bash
python3 tools/acceptance_stack.py reset
python3 tools/acceptance_stack.py status
python3 tools/acceptance_stack.py down
```

当前 stack smoke 是 non-blocking observability job。先证明隔离栈稳定，再升级为 Integration Merge Gate，避免新的基础设施组件反向阻塞 Offline Gate。

## Exact-SHA Software Evidence

`tools/acceptance_evidence.py` 将 workflow、Frozen/Release Gate、Golden/Human Gate、Acceptance Contract、Golden manifest 和 backend requirements 计算 contract fingerprint。

只有 exact commit + exact contract fingerprint + exact Golden SHA + prepared runtime fingerprint 全匹配时才允许复用 PASS。任一输入变化自动 cache miss 并重跑。

## Host Bootstrap

宿主机只需要一次：

```bash
sudo VOIP_GOLDEN_001_SOURCE=/path/or/url/to/golden001.pcap \
  bash tools/bootstrap_acceptance_host.sh
```

若旧 `/tmp/tcpdump-2026-08-14.pcap` 仍存在，可首次省略 `VOIP_GOLDEN_001_SOURCE`；bootstrap 会迁移到持久 cache。普通 CI 不再读取 `/tmp`。

Bootstrap 负责所有允许联网的准备动作：持久目录、Golden、TShark 4.2.2、Python deps、npm cache、Docker images、Acceptance Runtime image、Acceptance Stack 和完整 Doctor。

## 人工介入边界

正常只在以下情况需要人工：

1. Runner 主机离线/硬件故障；
2. GitHub/外网持续完全不可达；
3. secret/token 被撤销或过期；
4. DUT/物理环境不可用。

PostgreSQL/Redis/MinIO 停止、Golden cache、prepared runtime、Acceptance network/service 等问题应在 Doctor 阶段自动恢复或 fail-fast 分类，不再混入代码失败。
