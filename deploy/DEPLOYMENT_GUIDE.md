# aiVoip 生产部署指导（Deployment Guide）

本文档说明如何部署最新代码、本次变更影响哪些服务，以及部署/验收的标准操作。
正式部署入口**唯一**：`deploy/voip-ai`。请勿手工 `docker compose` 绕过部署治理。

---

## 1. 前置条件与约定

- **必须用 `sudo`**：`/etc/voip-ai/production.env` 与 `/etc/voip-ai/secrets/*` 均为 `root:0600`，
  且部署预检强制要求 `0600`（禁止放宽权限）。
- **compose project = `aivoip`**：`deploy/voip-ai` 从 env 文件 `VOIP_PROJECT_NAME` 解析（当前 `aivoip`），
  默认兜底也是 `aivoip`。**不再需要传 `--project`**（显式 `--project aivoip` 亦可）。
- **端口约定**：
  - frontend：`0.0.0.0:8088`（**8080 被同机 FusionPBX websockets 占用，禁止使用 8080**）
  - backend：`127.0.0.1:18001`（容器内 8000）
  - MinIO console：`127.0.0.1:19001`
- **source manifest 一致性**：`build` 前 `tools/source_manifest_gate.py` 强制 manifest 与当前代码一致。
  改过 `deploy/`、`backend/app`、`frontend/src`、`tools`、compose 等纳入清单的文件后，
  先 `python3 tools/source_manifest_gate.py --update`，否则 build 会 fail-closed。
- **本机 docker 组**：`dev` 会话未加载 docker 组，直接敲 `docker` 会 permission denied；
  临时用 `sg docker -c '...'`，正式部署走 `sudo` 不受影响。

## 2. 部署最新代码（标准流程）

```bash
cd ~/workspace/aiVoip-control

# 1) 同步到最新 master
git fetch origin
git checkout master
git reset --hard origin/master

# 2) 预检（可选但推荐）
sudo ./deploy/voip-ai --env /etc/voip-ai/production.env preflight

# 3) 正式部署
sudo ./deploy/voip-ai --env /etc/voip-ai/production.env deploy

# 4) 状态与运行时验收
sudo ./deploy/voip-ai --env /etc/voip-ai/production.env status
sudo ./deploy/voip-ai --env /etc/voip-ai/production.env verify
```

`deploy` 是全流程：preflight → prepare-host → 自动 `pg_dump` 备份到
`/data/voip/backups/` → build（source-bound gate）→ up postgres/redis/minio →
`alembic upgrade head` → up 应用服务 → wait health（backend `:18001/health/ready`、
frontend `:8088`）→ runtime verify（写 `validation/production_runtime_result.json`，9/9 全 PASS 才算成功）。

## 3. 改了什么 → 需要重新部署什么服务

线上运行栈为 **2026-08-31 17:18 部署**（内容 = commit `a46c38a`），已包含应用代码层的全部修复。
自那之后到最新 master（`d418231`）的变化如下：

| 变更 | 提交 | 影响面 | 是否需要重建容器 |
|---|---|---|---|
| preflight 错误分类修复（permission / daemon / context） | `2688283` / `096c77d` | `deploy/voip-ai`（host 脚本） | 否（不进镜像，拉取 master 即生效） |
| poseidon 生产就绪检查修复 + release-runner 挂载 secret.yaml | `8cf7e62` / `a46c38a` | `backend/app/*`、`docker-compose.production.yml` | 是（backend + 各 worker + beat + release-runner 需重建/recreate）；**线上运行栈已含此修复** |
| compose project 名解析（env 文件 `VOIP_PROJECT_NAME` 权威） | `07e1e9e` | `deploy/voip-ai`（host 脚本） | 否；此后部署无需 `--project aivoip` |
| 8088 端口对齐（compose 默认 / 脚本兜底 / 模板 / README） | `d418231` | `docker-compose.yml`、`deploy/voip-ai`、`production.env.example`、`README.md` | 否（线上 env 已是 8088） |
| source_manifest 刷新 | `310d2e3` 等 | `release/source_manifest.json` | 否（构建证据，须与代码一致） |

**结论**

- 应用代码（`backend/app`）自上次部署以来**零变化** → 运行容器已是最新应用代码，
  **严格来说不需要重建容器**。
- 但为了让镜像与构建证据（source manifest）和最新 master 完全一致、并让 host 侧脚本改动生效，
  **推荐对最新 master 跑一次正式 `deploy`**（幂等：构建走缓存、migrate 到 head 为 no-op、自动备份 DB、runtime verify 9/9）。
- 若只是想让脚本/默认值生效（不改应用代码）：`git pull` 到最新 master 后直接用 `deploy/voip-ai` 即可，无需重启栈。

## 4. 验收要点

- `ORIGIN_MASTER_SHA` / `CHECKOUT_SHA` / `RUNTIME_SHA` 与部署目标一致（`RUNTIME_SHA` 取 backend 容器
  `BUILD_REVISION`，由 env 锁定）。
- aivoip 生产栈**仅一套**；legacy voip-ai 活跃实例 **0**；Feishu consumer **仅 1 个**。
- migration 到 head（当前 `0032_conversation_knowledge_v1`）。
- backend `127.0.0.1:18001/health/ready` ok（postgres / redis / minio）；frontend `:8088` HTTP 200。
- 历史数据不受影响：DB 备份在 `/data/voip/backups/`，`/data/voip` 证据目录原样保留。

## 5. 已知缺口与注意事项

- **`feishu-long-connection` 未纳入 `deploy/voip-ai` 的 `compose up` 服务列表**（历史缺口），
  重跑部署不会更新它（仍为旧容器）。如需纳入正式发布管理，需先把该服务加进脚本并补 secrets 挂载。
- **禁用 8080**：同机 FusionPBX websockets（systemd 自启）占用 `127.0.0.1:8080`；前端固定用 8088。
- 修改纳入 source manifest 的文件后先 `--update` 刷新 manifest，否则 build fail-closed。
- env / secret 为 `root:0600`，查看/修改需 `sudo`，禁止放宽权限（预检强制 `0600`）。

## 6. 常用运维命令

```bash
sudo ./deploy/voip-ai --env /etc/voip-ai/production.env status      # 栈状态 + 健康
sudo ./deploy/voip-ai --env /etc/voip-ai/production.env logs        # 后端 / worker 日志
sudo ./deploy/voip-ai --env /etc/voip-ai/production.env backup-db   # 手动 DB 备份
sudo ./deploy/voip-ai --env /etc/voip-ai/production.env verify      # 运行时验收证据
sudo ./deploy/voip-ai --env /etc/voip-ai/production.env down        # 停容器（不删数据/卷）
```
