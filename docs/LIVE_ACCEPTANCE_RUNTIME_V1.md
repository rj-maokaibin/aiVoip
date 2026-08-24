# VOIP AI Live Acceptance Runtime V1

## 目标

把需要真实 PostgreSQL / Redis / MinIO / Feishu 的发布验收从临时 CI 脚本固化为可复用能力。Runtime 只借用正在运行的真实 Backend 的 Docker 网络、基础镜像和只读 secret mount，不修改、不重启生产 Backend；当前待验代码始终通过 `/workspace` bind mount 注入。

## 固定契约

- Runtime contract：`deploy/live_acceptance/runtime_contract.json`
- Runtime builder：`deploy/live_acceptance/runtime.py`
- Runtime image：`deploy/live_acceptance/Dockerfile`
- Read-only preflight：`deploy/live_acceptance/preflight.py`
- Human Feishu profile：`human-feishu-golden-001`
- Preflight contract：`voip-live-acceptance-preflight-v1`

Runtime image 的 cache key 同时包含：真实 Backend base image ID、Runtime Contract、Runtime Dockerfile、`backend/requirements.txt`。只要这四项不变，self-hosted runner 直接复用已有 image，不重复安装系统包/Python 依赖；代码 SHA 改变但依赖未改变时也无需重建 Runtime。

## 标准流程

```bash
python deploy/live_acceptance/runtime.py prepare \
  --context "$RUNNER_TEMP/voip-live-acceptance-context.json" \
  --feishu-secret-file /home/github-runner/.config/voip-ai/feishu_app_secret

python deploy/live_acceptance/runtime.py run \
  --context "$RUNNER_TEMP/voip-live-acceptance-context.json" \
  --env-file /home/github-runner/.config/voip-ai/.env \
  --set-env HUMAN_EVIDENCE_RENDERER_ENABLED=true \
  --set-env HUMAN_EVIDENCE_FEISHU_PREFERRED=true \
  -- python deploy/live_acceptance/preflight.py \
       --profile human-feishu-golden-001 \
       --out validation/live_acceptance_preflight.json
```

只有 `status=PASS` 且 `mutation_allowed=true` 才允许后续 mutation command。Human Feishu live acceptance 还会再次读取 preflight JSON 并 fail-closed，防止 workflow 误接线绕过 preflight。

## Preflight 范围

Preflight 一次性聚合检查，不在第一个错误处退出：

- Runtime：Python ABI、所有 pinned requirements、关键 imports、CJK 字体、Runtime fingerprint、exact source SHA。
- 网络：PostgreSQL / Redis / MinIO DNS。
- PostgreSQL：`SELECT 1`、DB Alembic revision 与当前源码 head 一致。
- Redis：PING。
- MinIO：使用正式 secret resolution 读取 bucket，禁止写对象。
- Feishu：live 配置、Golden #001 已有 Document Binding、真实文档只读 blocks API。
- Golden profile：绑定报告必须包含冻结 PCAP SHA256，且已有 Report Artifact / Case Evidence。

所有输出都脱敏；飞书 document 只输出 SHA256 短 fingerprint，不输出 document_id，secret 内容和 host secret path 不进入验收 Artifact。

## 后续复用

新增真实环境验证时优先复用 `base` profile；需要专用业务前置条件时在 Runtime Contract 中新增版本化 profile，再在 Preflight 增加只读 probe。不要在 workflow 中重新复制 Docker discovery、secret mount、依赖安装或服务连通性代码。
