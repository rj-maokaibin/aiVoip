# VOIP AI Acceptance Infrastructure V2

## 1. 目标

Acceptance Infrastructure V2 将已经通过真实环境验证的 Live Acceptance 能力正式版本化，使“验收能力已经具备”和“正式 V2 契约已经交付”成为同一件事。

V2 不推翻 V1。V1 的 Runtime、Preflight、真实 Human Feishu Live Acceptance 证据继续有效；V2 通过新的版本化 Runtime/Preflight/Live 入口复用已经验证的安全原语，并对 V1 保持兼容。

## 2. 正式契约

| 能力 | V2 契约/入口 |
|---|---|
| Runtime Contract | `deploy/live_acceptance/runtime_contract_v2.json` |
| Runtime Orchestrator | `deploy/live_acceptance/runtime_v2.py` |
| Runtime Context | `voip-live-acceptance-runtime-context-v2` |
| Read-only Preflight | `deploy/live_acceptance/preflight_v2.py` |
| Preflight Contract | `voip-live-acceptance-preflight-v2` |
| Explicit Human Feishu Live | `tools/human_evidence_feishu_live_acceptance_v2.py` |
| Static Contract Gate | `deploy/live_acceptance/acceptance_infrastructure_v2_gate.py` |
| Regression Tests | `backend/tests/test_live_acceptance_infrastructure_v2.py` |

Runtime version 为 `2.0.0`，Acceptance Infrastructure version 为 `2.0`。

## 3. V1 兼容策略

V2 采用 additive upgrade：

- `deploy/live_acceptance/runtime.py` 与 `runtime_contract.json` 保持 V1，不做原地重命名。
- `deploy/live_acceptance/preflight.py` 保持 `voip-live-acceptance-preflight-v1`。
- 已完成的 Human V2 Real Feishu Live Acceptance、Golden #001 与历史审计证据继续有效。
- V2 Runtime 复用 V1 已经真实验证的 Docker topology discovery、PostgreSQL route discovery、guarded database recovery 和 runtime container execution 原语。
- V2 Context 与 V1 Context 严格区分，禁止把 V1 Context 当作 V2 Context 使用。
- V2 Live helper 只临时把 legacy helper 的 fail-closed preflight contract guard 切换到 V2，执行结束后立即恢复；不会降低 `status=PASS`、`mutation_allowed=true`、exact source revision、runtime fingerprint 等校验。

## 4. V2 标准流程

### 4.1 Prepare

```bash
python deploy/live_acceptance/runtime_v2.py prepare \
  --context "$RUNNER_TEMP/voip-live-acceptance-v2-context.json" \
  --feishu-secret-file /home/github-runner/.config/voip-ai/feishu_app_secret
```

Prepare 必须：

- 绑定 exact Git source revision；
- 发现真实 Backend，而不是测试 Backend；
- 发现真实 PostgreSQL route；
- 排除 release-gate/test PostgreSQL；
- 使用 Runtime V2 contract 参与 image fingerprint；
- 对 Runtime image 写入 V2 contract/version/fingerprint label；
- 输出 `voip-live-acceptance-runtime-context-v2`。

### 4.2 Read-only Preflight

```bash
python deploy/live_acceptance/runtime_v2.py run \
  --context "$RUNNER_TEMP/voip-live-acceptance-v2-context.json" \
  --env-file /home/github-runner/.config/voip-ai/.env \
  -- python deploy/live_acceptance/preflight_v2.py \
       --profile human-feishu-golden-001 \
       --out validation/live_acceptance_preflight_v2.json
```

Preflight V2 继续执行已经验证的聚合只读检查：Runtime identity、exact source revision、PostgreSQL、Alembic head、Redis、MinIO、CJK、Feishu read-only、Golden #001 exact evidence SHA、required analyzers。

只有：

```text
status = PASS
mutation_allowed = true
contract = voip-live-acceptance-preflight-v2
```

才具备进入真实 mutation 的资格。

### 4.3 Explicit Real Live Acceptance

普通 PR CI 禁止自动修改真实飞书文档。只有明确要求进行真实 Live Acceptance 时，才允许显式执行：

```bash
python deploy/live_acceptance/runtime_v2.py run \
  --context "$RUNNER_TEMP/voip-live-acceptance-v2-context.json" \
  --env-file /home/github-runner/.config/voip-ai/.env \
  -- python tools/human_evidence_feishu_live_acceptance_v2.py \
       --preflight-result validation/live_acceptance_preflight_v2.json \
       --result validation/human_evidence_feishu_live_acceptance_v2.json
```

V2 Live helper 不绕过既有安全检查，只把已验证的 Human Feishu projection core 作为兼容实现复用。

## 5. Acceptance Gates

V2 正式 Gate 集合：

1. Frozen Contract Gate。
2. Full VOIP AI Software Release Gate。
3. Migration / Frontend Gate。
4. Real Offline Golden #001 = 142/142。
5. Human Real Offline Golden #001。
6. Acceptance Infrastructure V2 static contract gate。
7. Read-only Live Preflight V2。
8. Explicit Real Live Acceptance（仅在需要真实外部环境变更时执行）。
9. Human Visual Confirmation（涉及最终人类展示质量的发布时执行）。

其中 1～6 可以作为普通 PR 的非 mutation 验收；7 是外部环境 readiness；8 必须显式触发；9 不应被机器测试伪装替代。

## 6. Safety Invariants

V2 不允许破坏以下不变量：

- `EXACT_SOURCE_REVISION`：验收运行代码必须等于待验 commit。
- `FAIL_CLOSED_BEFORE_MUTATION`：Preflight 不通过不得 mutation。
- `NORMAL_PR_CI_NON_MUTATING`：普通 PR 不允许自动改真实飞书文档。
- `REAL_SECRETS_NEVER_EMITTED`：secret 与真实 secret path 不进入 Artifact。
- `REAL_BACKEND_NOT_RESTARTED`：验收 Runtime 不重启生产 Backend。
- `GOLDEN_EVIDENCE_SHA_BOUND`：Golden 身份使用 Case Evidence SHA，不使用模糊报告内容推断。
- `DIAGNOSTIC_AUTHORITY_NOT_ESCALATED`：Human Renderer/Projection 不提升诊断权限。

## 7. Definition of Done

Acceptance Infrastructure V2 代码完成必须同时满足：

- V2 Runtime Contract 已版本化为 `voip-live-acceptance-runtime-v2@2.0.0`。
- V2 Runtime Context 与 V1 Context 严格隔离。
- V2 Preflight Contract 已版本化为 `voip-live-acceptance-preflight-v2`。
- V2 Runtime image fingerprint 包含 V2 contract 本身。
- V2 image label 明确写入 V2 contract/version/fingerprint。
- V1 Runtime/Preflight contract 保持原样且已有真实验收证据继续有效。
- V2 Live mutation 入口继续强制 `PASS + mutation_allowed + exact revision + runtime fingerprint`。
- 普通 PR CI 保持 non-mutating。
- Real Offline Golden #001 与 Human Golden 仍是发布门禁。
- Static V2 Gate 与 V2 regression tests 全部通过。
- 需要真实 Live 验收时，只能显式执行 V2 Runtime → V2 Preflight → V2 Live helper。

满足上述代码 Gate 后，可以将状态标记为：

```text
Acceptance Infrastructure V2
CODE COMPLETE / V1 BACKWARD COMPATIBLE / NON-MUTATING CI READY
```

在 V2 Preflight 与真实 Live Acceptance 再次于目标环境执行通过后，可以进一步标记为：

```text
Acceptance Infrastructure V2
REAL-ENV VERIFIED / PRODUCTION-READY FOR ACCEPTANCE USE
```

## 8. 与 Capture Engine V2 的边界

Acceptance Infrastructure V2 的完成不代表 Capture Engine V2 已生产切换。Acceptance Infrastructure 只负责“如何可靠地验收代码与真实环境”；Capture Engine 是否启用生产 V2 Authority、是否完成 DUT Gate、是否允许 cutover，仍由 Capture V2 自己的 Release Gate 决定。
