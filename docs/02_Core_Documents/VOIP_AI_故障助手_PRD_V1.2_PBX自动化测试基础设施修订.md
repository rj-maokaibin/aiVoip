# VOIP AI 故障助手 PRD V1.2：PBX 自动化测试基础设施修订

状态：APPROVED DESIGN CHANGE / 2026-09-09  
基线：`VOIP_AI_故障助手_PRD_V1.0_终稿.docx`  
兼容修订：`VOIP_AI_故障助手_PRD_V1.1_复现采集修订.md`

## 1. 产品目标补充

在现有 Evidence First、真实 DUT、自动 Cleanup 和可审计 Golden 基础上，增加“可动态准备并恢复 PBX 测试资源”的产品能力，使自动化验收从配置/注册验证继续扩展到真实 SIP Call E2E。

本修订不要求最终用户管理 FusionPBX，也不把 PBX 管理界面作为产品功能；PBX 是内部测试基础设施。

## 2. 用户价值

- 自动化用例不再依赖人工预先创建固定分机。
- 测试可自动获得独占 extension，避免多人/多任务相互污染。
- 配置成功必须同时验证 PBX/FreeSWITCH runtime，降低“配置已写但业务未生效”的假 PASS。
- 失败后自动恢复 DUT/PBX baseline，不把脏环境留给下一轮测试。
- 支持后续无人值守的注册、呼入、呼出、DTMF、RTP、挂断 Golden。

## 3. 产品级能力

1. 系统可从受控号码池申请临时 PBX extension。
2. 系统可自动创建、验证、使用和删除“由自动化拥有”的临时 extension。
3. 系统不得自动删除人工/长期 baseline extension。
4. 系统可观察 FreeSWITCH 的注册与呼叫运行态。
5. 系统可把 PBX resource、DUT、SIP peer 绑定到同一 TestRun/Lease。
6. 用户停止、失败、超时、Worker crash 后仍必须执行 PBX/DUT Cleanup/Recovery。
7. PBX/DUT cleanup 未验证时，相关资源不得再次分配。

## 4. 产品语义

```text
CONFIGURED：FusionPBX Desired State 已写入并 readback。
READY：CONFIGURED 且 FreeSWITCH runtime 已验证。
IN_USE：资源被当前 TestRun/CallSession 独占使用。
DIRTY：无法证明 cleanup 完成，禁止重新分配。
RECOVERING：系统正在执行确定性恢复。
```

## 5. 第一阶段号码池

`7900-7999` 作为自动化临时号码候选池；`7102` 等现有环境号码属于 STATIC_BASELINE。临时号码只有在系统确认 ownership 后才允许删除。

## 6. 验收补充

产品验收新增三条正式 Golden：

- `G-PBX-001`：Extension 生命周期 create/readback/runtime/delete/absence。
- `G-PBX-002`：DUT 绑定动态 extension 后真实 REGISTER，并完整恢复。
- `G-CALL-001`：自动化 peer 与 DUT 完成真实 SIP Call signaling，随后全资源 cleanup。

其中 cleanup/recovery、secret hard-zero、无 ownership 删除 hard-zero、blind mutation retry hard-zero均为发布硬门禁。

## 7. 非目标

本修订不承诺多 PBX HA、PSTN 真实外线、复杂 IVR/Call Center、大规模并发性能、自动物理摘挂机。上述能力可在 Test Lab Infrastructure V2 扩展。

## 增量要求：非数字分机 identity

PBX 自动化测试基础设施不得假设分机号为整数。产品验收范围必须支持字母/数字以及当前市场需求特殊字符 `.`、`+`；默认数字池仅用于资源自动分配。必须提供非数字 extension create/delete、REGISTER、cleanup 的真实 Golden 证据。
