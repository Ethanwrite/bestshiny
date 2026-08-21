# AI Director Platform 文档导航

快照日期：2026-08-21
当前发布结论：**NOT PRODUCTION-READY**

仓库已将 Phase II 离线算法核心冻结为 commit `0a74d31`、tag
`v0.2.0-algorithm-core-offline`。Phase III 实现提交为 `99f9c60`，离线证据快照 tag 为
`v0.3.0-production-evidence-core-offline`；该 tag 不是生产发布证明。

## 首先阅读

1. [生产证据报告](PRODUCTION_EVIDENCE.md)
   - 区分代码证据、离线 fixture 证据和真实 Provider 证据。
   - 记录 PostgreSQL、Docker、Live Canary、实际支出和剩余阻断。
2. [生产就绪检查表](PRODUCTION_READINESS_CHECKLIST.md)
   - 只勾选有客观证据的项目。
   - 明确保留未执行的 Provider canary 与运营/安全缺口。
3. [开发交接文档](DEVELOPMENT_HANDOFF_2026-08-20.md)
   - 顶部 Phase III 更新是当前事实；后文保留 Phase II/2026-08-20 历史冻结证据。
   - 包含高冲突文件、Runbook、迁移图与不可破坏约束。
4. [当前完整架构](../CURRENT_ARCHITECTURE.md)
   - 当前系统分层、核心数据流、Provider 安全边界、数据表和验证边界。
5. [产品决策与需求台账](PRODUCT_REQUIREMENTS_LEDGER.md)
   - 保留五份原始工程简报的 hash、产品目标、模型偏好、积分和学习政策。
   - 它是需求台账，不是实现完成声明。

## 实施、安全与研究记录

- [Secret Audit](security/secret-audit.md)
  - 仓库/Git/本地路径扫描的脱敏记录。
  - 用户已明确决定当前 Provider Key 无需轮换；这不改变“不落库、不提交、不记日志、默认不 live”边界。
- [Visual Runtime 实施记录](VISUAL_RUNTIME_IMPLEMENTATION.md)
  - Passenger/Autopilot 共享运行时、Prompt 分离、Asset Registry、Memory/Evaluation/Trace 等 Phase I 实施历史。
- [Skill 研究与许可证记录](skill-research.md)
  - 公开摄影、运镜、故事板、Provider prompt 和打光资料的研究与边界。
- [源码/依赖审计](source-audit.md)
  - 实现来源、许可证与可依赖性记录。

## 当前验证摘要

- 历史离线基线：`348 passed, 39 warnings`，冻结于 `0a74d31`。
- Phase III 全仓：`406 passed, 57 warnings in 71.58s`；Mypy 121 source files、Ruff lint、Node syntax 和
  `git diff --check` 通过。warning 主要是已知 Alembic/SQLite/Starlette 弃用项和 SQLAlchemy FK cycle。
- PostgreSQL 17.10 + pgvector 0.8.6：fresh/populated、`vector(16)`、约束与事务验证通过，head
  为 `0027_production_evidence_core`。
- Docker Desktop 29.5.3：Compose config/build/up/health、HTTP 200 smoke 与容器内 Alembic
  head/check 通过；仅使用假 development 凭据，未传入 Provider Key。
- Live Provider：RunAPI/OpenRouter/Voyage/Flow/单视频全部 **NOT EXECUTED**；已知支出 **USD 0**。

主要阻断是具体生产视觉检测/跟踪/编码模型部署与校准、真实 Provider/账单
canary、剩余公网认证/运营控制与备份恢复。
