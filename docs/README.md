# AI Director Platform 文档导航

快照日期：2026-08-21

当前仓库同时包含一个已提交的 MVP 基线和一个未提交的 Phase II 工作树。当前自动化门禁已全绿，但真实 PostgreSQL/Compose/Provider 验证和密钥轮换尚未完成；请不要只读根目录 README 就发布。

## 首先阅读

1. [开发交接文档](DEVELOPMENT_HANDOFF_2026-08-20.md)
   - 当前 Git/迁移/测试冻结快照。
   - 已完成、部分完成和未完成工作。
   - P0/P1/P2 风险、开发顺序、Runbook 和发布验收清单。
2. [当前完整架构](../CURRENT_ARCHITECTURE.md)
   - 当前磁盘上的系统分层、数据流、Provider 矩阵、数据表、API 和安全边界。
   - 严格区分 Stable、WIP、Partial、Mock-only 和 Not implemented。
3. [产品决策与需求台账](PRODUCT_REQUIREMENTS_LEDGER.md)
   - 保留用户的商业产品目标、Agent 层级、创意规则、模型偏好、积分意图和 Hook/R3 学习方案。
   - 它是需求台账，不是实现完成声明。

## 实施与研究记录

- [Visual Runtime 实施记录](VISUAL_RUNTIME_IMPLEMENTATION.md)
  - Passenger/Autopilot 共享运行时、Prompt 分离、Asset Registry、Memory/Evaluation/Trace 等 Phase I 实施历史。
- [Skill 研究与许可证记录](skill-research.md)
  - 公开摄影、运镜、故事板、Provider prompt 和打光资料的研究与边界。
  - 项目没有直接 vendoring 上游 Skill body。
- [源码/依赖审计](source-audit.md)
  - 实现来源、许可证与可依赖性记录。

## 当前发布状态

**仍不可发布。** 2026-08-21 的最新完整套件为 `348 passed, 39 warnings`；Ruff format/lint、Mypy、Node syntax、fresh SQLite migration 与 Alembic check 已通过。主要剩余阻断是：

- 公开生成入口已统一经过服务端 Admission；Free 的 Reserve → Generate → Settle / Refund → Reconcile 状态机已接线，不确定的付费结果保持冻结且只能由内部证据决策解决。
- 50 积分仍不足以购买当前默认 8 秒 Seedance 估价（约 87 积分），需要产品定价决策。
- RunAPI 的公开 metadata/dict 任务声明已被拒绝，prompt refinement 任务 ID/估价由服务端派生并使用强类型内部 `EdgeTask`；`UNCERTAIN` ProviderBudget 已有平台密钥保护、强制证据和幂等审计的人工对账。剩余缺口是产品 prompt 接线、自动账单采集/验真和运营 UI。
- `0024_workspace_credit_lifecycle` 已通过 SQLite fresh/historical/populated round-trip 回归，但 `0021`–`0024` 尚未在真实 PostgreSQL + pgvector 上验证。
- 本地 `data/platform.db` 是 Alembic `0020` 与部分新表并存的混合 schema，需先备份/审计。
- 本次 Phase II 的已记录验证没有执行任何真实 Provider 调用。

用户曾在对话中暴露 Provider API Key。仓库中没有保存这些值，但所有这些 Key 都必须在继续 live 工作前撤销并轮换。
