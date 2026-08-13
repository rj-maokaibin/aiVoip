# VOIP AI V1.0 — Phase F3 Production Deployment & Full-stack Release Runner

## 状态

Phase F3 已实现并完成静态/离线合同验证。当前源码状态为：

`IMPLEMENTED_STATIC_VALIDATED_RUNTIME_PENDING`

当前执行环境没有 Docker/Podman，因此无法在本机把真实 Docker Runtime 标记为 PASS。

## 新增生产交付能力

- `deploy/voip-ai` 一键生产部署 CLI：preflight / prepare-host / build / deploy / verify / release / status / logs / backup-db / down。
- 生产 env 与 Docker secret 文件 fail-closed 预检。
- `docker-compose.production.yml`：生产 secret mount、restart policy、release-runner。
- Nginx same-origin `/api/` 代理，SSE 关闭 buffering，前端默认 `/api/v1`。
- 显式 Alembic migrate-before-promote。
- 现有 PostgreSQL 自动逻辑备份（升级前）。
- Reproduction Ring/Staging 持久化主机目录。
- `PRODUCTION_DEPLOYMENT_RUNTIME` source-bound runtime evidence。
- Live Runtime 检查：Backend、Frontend/API Proxy、PostgreSQL Head、Redis、MinIO R/W、Celery queues、Production Config。
- Docker-based frontend build evidence（仍严格要求 source-controlled package-lock.json）。
- 一键严格 release：Deploy → F3 Static → Frontend Build → Docker Full-stack E2E → Field Golden → Strict Release Gate。
- Release Gate 新增 `PRODUCTION_DEPLOYMENT_RUNTIME` 必需证据。
- 生产 `down` 不删除 volumes/data，EC-02/mock 不能绕过 Production Gate。

## 当前精确验证结果

- Backend Tests: 148 / 148 PASS
- F3 Static Gates: 22 项 PASS
- Reproduction Mock E2E: 3 / 3 PASS
- Reproduction Evidence E2E: 5 / 5 PASS
- Reproduction C3 E2E: 4 / 4 PASS
- Synthetic Golden: 21 / 21 PASS
- Synthetic E2E: 53 / 53 PASS
- Baseline Regression: 0 regression / 0 change
- APF1250 Field Golden: PASS，且与当前源码 manifest 精确绑定
- OpenAPI: 78 paths / 84 operations PASS
- Alembic: 11 migrations / single head PASS
- Source Manifest SHA256: `8c4f6e503e3a9ce8d1aa28bac3ab9659b319ae789642dda22401e613a76289db`

## Release Readiness

当前仍为：`STATIC_PASS_PRODUCTION_BLOCKED`

- PASS: 27
- BLOCKED: 12
- UNVERIFIED: 4

剩余 blocker 本质上已收敛为外部/待定条件：EC-02 真机平台、真实生产参数/凭证、Docker/PostgreSQL/Redis/MinIO runtime、frontend package-lock + production build。

## 在 Linux/Docker 服务器上的使用方式

```bash
sudo mkdir -p /etc/voip-ai/secrets
sudo cp deploy/production.env.example /etc/voip-ai/production.env
sudo chmod 600 /etc/voip-ai/production.env /etc/voip-ai/secrets/*

./deploy/voip-ai --env /etc/voip-ai/production.env preflight
sudo ./deploy/voip-ai --env /etc/voip-ai/production.env prepare-host
./deploy/voip-ai --env /etc/voip-ai/production.env deploy
./deploy/voip-ai --env /etc/voip-ai/production.env verify
```

最终严格发布：

```bash
./deploy/voip-ai \
  --env /etc/voip-ai/production.env \
  --field-pcap /data/voip-golden/8b72929e-8a06-4f1e-a922-1d3779ebbd6f.pcap \
  release
```

EC-02 未完成时，最后一步必须失败并明确报告 blocker；这是设计行为，不是测试失败。
