# aiVoip 生产部署指导（Deployment Guide）

本文档说明如何部署最新代码、本次变更影响哪些服务，以及部署/验收的标准操作。
正式部署入口**唯一**：`deploy/voip-ai`。请勿手工 `docker compose` 绕过部署治理。

---

## 1. 前置条件与约定

- **必须用 `sudo`**：`/etc/voip-ai/production.env` 与 `/etc/voip-ai/secrets/*` 均为 `root:0600`，
  且部署预检强制要求 `0600`（禁止放宽权限）。
- **compose project = `aivoip`**：`deploy/voip-ai` 从 env 文件 `VOIP_PROJECT_NAME` 解析，
  默认兜底也是 `aivoip`。**不再需要传 `--project`**（显式 `--project aivoip` 亦可）。
- **端口约定**：
  - frontend：`0.0.0.0:8088`（**8080 被同机 FusionPBX websockets 占用，禁止使用 8080**）
  - backend：生产环境按 `/etc/voip-ai/production.env` 的 `VOIP_BACKEND_PORT` 暴露（当前站点为 `127.0.0.1:18001`，容器内 8000）
  - MinIO console：按生产 env 绑定
- **BUILD_REVISION 无需人工维护**：`deploy/voip-ai` 默认使用当前 checkout 的 40 位 Git SHA，
  生成 `0600` 临时 effective env 后注入 `BUILD_REVISION`；`/etc/voip-ai/production.env` 不会被修改。
  CI/CD 可显式传 `--revision <SHA>`，但该 SHA 必须与 Git HEAD 精确一致，否则 fail-closed。
- **source manifest 一致性**：`build` 前 `tools/source_manifest_gate.py` 强制 manifest 与当前代码一致。
  `deploy/`、`backend/app`、`backend/run_feishu_long_connection.py`、`frontend/src`、`tools`、compose 等生产源码均受约束。
- **本机 docker 组**：普通 `dev` 会话可能未加载 docker 组，直接敲 `docker` 会 permission denied；
  临时可用 `sg docker -c '...'`，正式部署走 `sudo` 不受影响。

## 2. 部署最新代码（标准流程）

```bash
cd ~/workspace/aiVoip-control

git fetch origin
git checkout master
git reset --hard origin/master

sudo ./deploy/voip-ai --env /etc/voip-ai/production.env preflight
sudo ./deploy/voip-ai --env /etc/voip-ai/production.env deploy
sudo ./deploy/voip-ai --env /etc/voip-ai/production.env status
sudo ./deploy/voip-ai --env /etc/voip-ai/production.env verify
```

`deploy` 前不需要、也不应该手工修改 `production.env` 中的 `BUILD_REVISION`。历史文件中即使残留该字段也会被忽略，
真正的 revision 由当前 checkout（或显式 `--revision`）注入临时 env，部署结束后删除。

`deploy` 是全流程：preflight → prepare-host → 自动 `pg_dump` 备份 → source-bound build →
up postgres/redis/minio → `alembic upgrade head` → up backend/workers/Feishu long-connection/beat/frontend →
等待 backend、frontend 和 Feishu listener 健康 → host Feishu consumer gate → runtime verify。

运行时会生成：

- `validation/production_runtime_result.json`：backend/frontend/DB/Redis/MinIO/Celery/production config/reproduction/capture authority 等运行时门禁；
- `validation/feishu_long_connection_runtime.json`：Feishu 长连接生产实例的 source-bound 运行证据。

## 3. Feishu long-connection 生产治理

`feishu-long-connection` 已纳入正式 deployment lifecycle，不再是手工维护容器：

- `build_images()` 会构建 `feishu-long-connection` 对应镜像；
- `deploy` 会显式 `compose up -d feishu-long-connection`；
- `docker-compose.production.yml` 为其配置 `restart: unless-stopped`、Feishu Docker secrets 和 healthcheck；
- listener 通过 `SecretResolver` 从 `/run/secrets/feishu_app_secret` 等生产 secret ref 获取凭据；
- listener 启动失败或内部监听线程异常退出时以非零状态退出，让 Docker restart policy 接管；
- 生产 `preflight` 强制 `FEISHU_LIVE_ENABLED=true`、Feishu App/目标配置以及 Identity RBAC；
- 当 Feishu Live 启用时，`verify` 全局扫描 `com.docker.compose.service=feishu-long-connection`，要求**运行中的 consumer 恰好 1 个**；
- 唯一 consumer 必须属于当前 `aivoip` project、`BUILD_REVISION` 与本次 immutable deployment revision 一致且 Docker health=`healthy`；
- 通用开发/测试环境若明确关闭 Feishu Live，`status` 的 consumer gate 会输出 `SKIP`，而不是把非 Feishu 部署误判为故障；这不影响生产，因为生产 `preflight` 会先 fail-closed；
- `status` 和 `logs` 均包含 Feishu long-connection；
- `backend/run_feishu_long_connection.py` 已纳入 `source_manifest`，避免入口脚本脱离 exact-source 发布门禁。

因此，实际生产环境中若 legacy `voip-ai` stack 或任何额外 compose project 残留第二个 Feishu consumer，`verify` 会 fail-closed，而不是静默双消费。

## 4. 验收要点

正式生产部署至少确认：

- `ORIGIN_MASTER_SHA` / `CHECKOUT_SHA` / `RUNTIME_SHA` 与部署目标一致；
- aivoip 生产栈仅一套，legacy `voip-ai` 活跃实例为 0；
- Feishu long-connection consumer count = 1；
- Feishu consumer compose project = `aivoip`；
- Feishu consumer `BUILD_REVISION` = 目标 revision；
- Feishu consumer Docker health = `healthy`；
- migration 到当前 Alembic head；
- backend `/health/ready` 正常，frontend HTTP 正常；
- `production_runtime_result.json` 全 PASS；
- `feishu_long_connection_runtime.json` `passed=true`；
- 历史数据不受影响，DB 备份和 `/data/voip` Evidence 目录保留。

> 注意：容器健康证明 listener 进程、生产 secret 和 source revision 正确，不等价于真实飞书租户消息 E2E。真实 tenant 的收消息/回消息仍应使用专用验收消息执行 Live Acceptance。

## 5. 注意事项

- **禁用 8080**：同机 FusionPBX websockets 占用 `127.0.0.1:8080`；前端固定使用生产 env 配置的 8088。
- 禁止手工同步 `BUILD_REVISION` 到 `/etc/voip-ai/production.env`；该值由部署入口运行时注入。
- 修改纳入 source manifest 的文件后必须刷新 manifest，否则 build fail-closed。
- env / secret 为 `root:0600`，查看/修改需 `sudo`，禁止通过 chmod 放宽权限绕过预检。
- `FEISHU_LIVE_ENABLED=true` 是正式生产 deploy 的必需项，由 `deployment_preflight.py` 强制；通用非生产 `status` 场景允许 Feishu disabled 并明确显示 `SKIP`。

## 6. 常用运维命令

```bash
sudo ./deploy/voip-ai --env /etc/voip-ai/production.env status      # 栈状态 + backend/frontend + Feishu consumer
sudo ./deploy/voip-ai --env /etc/voip-ai/production.env logs        # backend / worker / Feishu listener 日志
sudo ./deploy/voip-ai --env /etc/voip-ai/production.env backup-db   # 手动 DB 备份
sudo ./deploy/voip-ai --env /etc/voip-ai/production.env verify      # 运行时 + exactly-one Feishu consumer 验收
sudo ./deploy/voip-ai --env /etc/voip-ai/production.env down        # 停容器（不删数据/卷）
```