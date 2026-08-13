# Phase F2 — Production Hardening

## 1. 目标

Phase F2 在不触碰仍处于 `RESERVED / PENDING_PLATFORM_CONTRACT` 的 EC-02 真机命令合同前提下，收敛 V1.0 中能够由代码直接消除的生产阻断项：生产认证、Secret/Credential、Evidence Storage、飞书 Live Transport 框架、生产配置校验、Compose 安全默认值、前端可重复构建合同，以及 Release Readiness Gate。

本阶段不把缺少真实外部环境、真实凭证或未确认 DUT 命令的事项伪装成 PASS。

## 2. 已完成实现

### 2.1 Production Auth Provider

- 新增统一 Auth Provider 框架。
- Development Header Auth 仅允许 dev/test/e2e。
- 新增 `gateway_hmac` 生产 Provider：校验 actor、role、timestamp、HMAC-SHA256 签名及时间偏差。
- 生产启动时对匿名开发认证、不安全 Auth Provider、通配 CORS 执行 fail-closed。
- 新增生产认证相关标准错误码。

### 2.2 Secret / Credential Provider

- 新增 `SecretResolver`，按 mounted secret file → named environment → direct dev value 的顺序解析。
- Secret 值禁止进入日志、数据库和错误信息。
- Credential API Token、MinIO 凭据、生产 Auth Secret、飞书 Secret/Token/Encrypt Key 均支持 Secret Provider。
- `ApiCredentialProvider` 已具备生产模式；Mock Provider 仅允许开发/测试。

### 2.3 Production Evidence Storage

- MinIO Storage Adapter 使用 SecretResolver 读取凭据。
- 增加读写 probe、清理及 remove 能力。
- 文件系统 Storage 仅作为 dev/mock backend。
- Production Readiness 明确区分“实现已存在”和“真实 MinIO 已配置/已运行验证”。

### 2.4 Feishu Live Transport Framework

- 实现 tenant token 获取、交互卡片发送、已发送卡片更新、token cache。
- 实现单 Case 单主卡持久化绑定。
- 实现 Callback Token/Signature Verification 框架。
- 增加安全 Callback Endpoint，并支持：
  - URL verification；
  - `STOP_REPRODUCTION` → 安全 Cancel/Finalize/Cleanup；
  - `EXTERNAL_ACTION_COMPLETED` → 继续实验编排；
  - `OPEN_CASE` acknowledgement。
- 当前生产 Gate 仍要求真实 App Credential、Chat Target 与 Verification Token；未配置时保持 BLOCKED。

### 2.5 Production Configuration Contract

新增统一生产配置 Readiness：

- `APP_ENV=production`
- immutable `BUILD_REVISION`
- Production Auth
- Restricted CORS
- Production Credential Provider
- MinIO Production Storage
- Feishu Live Config

同时增加：

- 管理 API：`GET /api/v1/system/production-config-readiness`
- CLI：`tools/production_config_gate.py`
- `.env.example`
- `deploy/production.env.example`
- `deploy/SECRETS.md`

### 2.6 Compose / Secret Hardening

- Docker Compose 中 PostgreSQL 密码改为显式必填环境变量。
- MinIO root user/password 改为显式必填环境变量。
- 禁止生产 Compose 使用仓库内默认密码。
- E2E Compose 仍保留自包含测试配置，与 Production Contract 分离。

### 2.7 Frontend Reproducible Build Contract

- Frontend Dockerfile 已切换到 `npm ci`。
- 构建要求 `frontend/package-lock.json` 存在。
- 当前运行环境无法从 npm 获取依赖，未伪造 lockfile，因此该项仍正确保持 BLOCKED/UNVERIFIED。

### 2.8 Release Gate

新增/更新：

- `tools/production_hardening_gate.py`
- `tools/phase_f2_static_gate.sh`
- `make production-hardening-gate`
- `make phase-f2-static-gate`
- `make production-config-gate`
- V1 Release Gate 绑定 Phase F2 exact-source static evidence。
- Source Manifest 已扩展到 `deploy/` 与 Dockerfile。
- Release Policy 升级至 1.1.0。

## 3. 数据库/API

- 新增 Alembic Migration：`0011_phase_f2_production_hardening`。
- 新增 `FeishuCaseBinding`，保存 Case 与飞书主卡 message binding。
- OpenAPI 当前：78 paths / 84 operations。

## 4. 验证结果

当前 exact-source Source Manifest：

`802cd44ff31326eb71b7e37304a3f691832038bdbd522a9b53cdf08ba204b976`

- Backend Tests：143 / 143 PASS
- Static Gates：20 / 20 PASS
- Alembic：11 migrations，single head `0011_phase_f2_production_hardening`
- OpenAPI Contract：PASS
- Compose Contract：PASS
- Security Contract：PASS
- Production Hardening Contract：PASS
- C1 Reproduction Mock E2E：3 / 3 PASS
- C2 Evidence E2E：5 / 5 PASS
- C3 Diagnostic Experiment E2E：4 / 4 PASS
- Synthetic Golden：21 / 21 PASS
- Synthetic E2E：53 / 53 PASS
- Baseline Regression：0 regression / 0 change
- APF1250 Field Golden：15 / 15 PASS，且与当前 Source Manifest 绑定
- Strict Production Gate：按预期 BLOCKED（exit code 2）

## 5. 当前 Release 状态

`STATIC_PASS_PRODUCTION_BLOCKED`

Readiness 计数：

- PASS：25
- BLOCKED：12
- UNVERIFIED：3

这不代表 F2 实现失败，而是 Release Gate 正确拒绝在生产条件未满足时宣称 Production Ready。

## 6. 剩余外部/配置/专项阻断

### EC-02 / 真机

- `EC02_PLATFORM_PRODUCTION_READY`
- `REAL_REPRODUCTION_PLATFORM`

按项目决策继续待定，不允许编码端猜测真实 DUT 命令。

### 生产环境配置

- `PRODUCTION_ENVIRONMENT`
- `BUILD_REVISION_PINNED`
- `PRODUCTION_CREDENTIAL_PROVIDER`
- `PRODUCTION_REPRODUCTION_STORAGE`
- `PRODUCTION_DEFAULT_SECRETS_REPLACED`
- `PRODUCTION_AUTH_PROVIDER`
- `ANONYMOUS_DEV_AUTH_DISABLED`
- `PRODUCTION_CORS_RESTRICTED`
- `FEISHU_LIVE_TRANSPORT`

这些项目对应的代码框架已经实现；当前 BLOCKED 原因是缺实际生产参数、Secret、Endpoint 或飞书凭据。

### Runtime Evidence

- `DOCKER_FULLSTACK_RUNTIME` — 当前环境没有 Docker/Podman，无法生成 exact-source runtime evidence。
- `POSTGRES_MIGRATION_RUNTIME` — 缺真实 PostgreSQL runtime。
- `FRONTEND_LOCKFILE` — 当前环境无法获取 npm dependency，未生成/伪造 lockfile。
- `FRONTEND_PRODUCTION_BUILD` — 因 lockfile/dependency/runtime 条件未满足而 UNVERIFIED。

## 7. 阶段结论

Phase F2 已把能够在当前环境中通过代码完成的生产化能力全部收敛为正式 Contract 和机器 Gate。剩余阻断已经收缩为：

1. 有意后置的 EC-02 真机 Platform Contract；
2. 实际 Production Config / Secret / Credential / Feishu 参数；
3. Docker/PostgreSQL/MinIO 等真实 runtime evidence；
4. npm lockfile 与 production frontend build evidence。

系统不会把这些外部条件缺失转换为假 PASS。
