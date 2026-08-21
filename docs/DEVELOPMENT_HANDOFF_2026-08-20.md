# AI Director Platform — 开发交接文档

快照日期：2026-08-21（文件名保留初始交接日期）
仓库：`ai-director-platform`
当前结论：**Phase III Production Evidence Core 已完成离线、PostgreSQL 与 Docker 门禁并形成可恢复检查点，但没有真实 Provider/生产视觉 QA/账单证据，因此不可发布或开启商用 live。**

## 2026-08-21 Phase III 当前交接（优先于下文所有 Phase II 历史段落）

### 当前冻结、门禁与真实执行状态

- Phase II 离线算法核心已冻结为 commit `0a74d31`、tag
  `v0.2.0-algorithm-core-offline`；冻结前历史套件为 `348 passed, 39 warnings`。
- Phase III 实现提交为 `99f9c60`，离线证据快照 tag 为
  `v0.3.0-production-evidence-core-offline`；该 tag 不代表生产就绪。
- Phase III 最终合并后全仓复跑：`406 passed, 57 warnings in 71.58s (0:01:11)`。
  Ruff lint 通过，Ruff format 报告 226 files already formatted，Mypy 121 source files 通过，Node
  syntax 和 `git diff --check` 通过。57 个 warning 主要是已知的 Alembic/SQLite/Starlette
  弃用警告与 SQLAlchemy FK cycle warning。
- Alembic 为单 head `0027_production_evidence_core`。PostgreSQL 17.10 + pgvector 0.8.6 在临时
  数据库通过 fresh/populated/supported round-trip、`vector(16)`、索引/唯一性/外键、积分预占
  事务与生成 enqueue 事务验证。
- Docker Desktop 29.5.3 上 `docker compose config -q`、API/worker/Web 三镜像 build、Compose
  up、PostgreSQL/MinIO/API health、Web/Worker Up、MinIO init/bucket、宿主 API/Web/MinIO HTTP 200 以及
  容器内 Alembic current/check 通过；pgvector 为 0.8.6。使用 development + 纯假 smoke 凭据，
  没有 Provider key，结束后已 `compose down` 且未删除 volume。
- 本轮 RunAPI/OpenRouter/Voyage/Flow/Seedance/Wan 及其他 Provider 真实调用数为 **0**，
  已知 Provider 支出为 **USD 0**；五个 live canary 全部 **NOT EXECUTED**，其中单个付费
  视频明确 **NOT EXECUTED**。

### Phase III 已落地

1. **Flow automatic affinity**：`0025` 引入状态机、sticky account/project、本地项目 active 唯一与远端
   project 跨全部历史状态永久 owner 索引、显式 `FlowMigrationPlan` 与 local job/account/project/provider job
   四元 poll 标识。默认 project provisioner fail closed；没有执行真实 Flow。
2. **单一 Capability 真相**：`0026` 持久 `ModelCapabilityProfile`，UI/Policy/Router/Cost/Adapter
   统一读取，旧 `config/video-models/*.json` 多头事实源已移除，Wan 统一为 2.7。
3. **ModelRoleRuntime 收口**：当前产品中真正执行外部 chat/embedding/refinement 的
   调用方 100% 经 runtime；确定性 Narrative/Continuity/Policy 仍可保持本地，这不表示所有
   算法都必须用 LLM。Narrative Memory 不再直连 Voyage，而是请求
   `MULTIMODAL_EMBEDDING`；失败时记 `MEMORY_VECTOR_DEGRADED` 并保留 SQL Timeline。
4. **生产证据 schema**：`0027` 增加 `ModelExecutionRecord`、`EmbeddingEvidence`、
   `ProviderBillingEvidence`、`DecisionOutcomeRecord`、`RunAPIBenchmark`、`LiveCanaryPermit/Usage`、
   Auth/reset/throttle 与 storage reservation。向量/prompt/raw Provider response/secret 不进审计记录。
5. **CharacterEvidenceProducer V1**：真实 FFmpeg 读取自生成非用户 MP4，经可注入
   detector/tracker/face/appearance encoder、视角感知参考、置信加权、时序聚合与版本阈值进
   QAPipeline。当前具体推理为确定性测试替身，生产检测/跟踪/encoder 没有部署；
   tracking uncertain 要求 VLM review，hair/costume 是 `UNAVAILABLE`。
6. **Timeline v3**：关系表显式表达九类 transition、branch/reconciliation/reset；编辑前镜
   只标记下游 `RECOMPUTE_REQUIRED`，planning recompute 不改写 committed media。
7. **真实/估算成本分离**：Provider 没有可信金额时 `actual_cost = null`；accepted-shot 统计
   包含失败、接受与 repair attempts。Router 按 prior 0.80 / observation 0.20、minimum 20 样本混合。
8. **Live Canary**：持久 Permit 按 provider/model/expiry/request/cost 硬限制，已接到 ModelRole 和
   media generation 最终 live 边界。内部 create/list API 需平台 key、显式确认和幂等键；
   创建 Permit 不会自动调 Provider。
9. **Commercial Auth/storage**：HttpOnly + production Secure + SameSite=Lax Cookie、double-submit CSRF、
   持久登录限流、一次性找回密码、工作空间真实字节原子 reserve/settle/release 已落地。
10. **Starter credits 决策**：用户未显式给 Passenger video duration 时默认 4 秒，约 44
    credits，50 starter credits 可预占一次；显式 8 秒仍约 87 credits 并 fail closed。
11. **开发观测 API**：`GET /internal/production-evidence` 按 project/job/shot 返回脱敏的 model
    execution、Provider job/billing、Flow binding、QA、DecisionOutcome、Cost 与 Timeline。

### Secret 决策

[`security/secret-audit.md`](security/secret-audit.md) 未在 tracked repo 或 practical Git history 发现用户的
Provider key。用户已明确决定“当前 Provider Key 不需要轮换”；该决定优先于 Phase III
草案的 blanket `ROTATION_REQUIRED`。仍不得把 key 写入源码/测试/文档/日志/提交，也不会
因为 key 存在就自动打开 live gate。

### 当前剩余阻断

1. 具体生产 character detector/tracker/face/appearance 推理模型、校准和不确定证据 VLM
   运营路径未部署。
2. 真实 RunAPI/OpenRouter/Voyage/Flow/单视频 canary 与真实 Provider billing/credits 证据未执行。
3. 邮箱验证、MFA、成员邀请/移除、设备会话、生产 HTTPS/secret manager、备份恢复、
   监控告警与运营策略仍未完成。
4. 充值/购买、周期 grant、expiry 和管理员调账未实现。

当前证据索引：[`PRODUCTION_EVIDENCE.md`](PRODUCTION_EVIDENCE.md) 与
[`PRODUCTION_READINESS_CHECKLIST.md`](PRODUCTION_READINESS_CHECKLIST.md)。

## Phase II 历史更新（仅保留冻结证据；已被上文 Phase III 现状取代）

下文保留了接手时的原始失败证据和实施计划；本节记录其后已完成的变化，防止把历史状态误读为当前状态。

- 完整套件：`348 passed, 39 warnings`。
- Ruff format/lint（209 files）、Mypy（117 source files）、Node syntax、`git diff --check` 已通过。
- 2026-08-20 当时仍是未提交的混合 WIP：`git status --short` 为 92 个路径（57 tracked modified + 35 untracked）；当时没有替用户自动 commit/tag。下文的 75 路径 manifest 也只是 2026-08-20 历史冻结。
- fresh SQLite `upgrade head` + `alembic check` 已通过；两种 historical recovery snapshot 与带数据的 `0023 → 0024 → 0023 → 0024` 往返回归已通过；Alembic 为单 head `0024_workspace_credit_lifecycle`。
- `0023` 已在无 `workspaces` 的受支持 recovery snapshot 上安全跳过，对存在 workspace 但缺少 project/job 依赖的部分 schema fail closed。
- Seedance 未完成部署验证时的目录信任收紧为 `STANDARD`；完整 Ark key/base/model 运行配置才会恢复 `PRODUCTION` 和 Hero/Canonical 可用范围。
- 新增 `GenerationAdmissionService`：已认证且有 workspace 的 Passenger、generic、OpenAI-compatible image/video 与 Shot Candidate 入口都使用服务端 plan/role/deployment/trust/pricing 解析。Free raw provider/model 无法再解锁 OpenRouter/Kling 等付费路径；Free image 在没有正式 `IMAGE_*` role 前 fail closed。
- Gateway 在一个事务中创建 GenerationJob、Free `WorkspaceCreditEntry`、`CostRecord`、trace/candidate 和幂等记录；并发/重放同项目同键只预占一次，不同项目可独立使用同键。
- 生成积分已完成 `RESERVED → SETTLED / REFUNDED / RECONCILIATION_REQUIRED`。完成结算，明确提交前终态原子退回，已跨付费边界的不确定/失败/取消保持冻结。只有 `PLATFORM_API_KEY` 保护的内部路由可以通过明确 Provider 证据全额结算或退回；禁止传入金额/成本/钱包状态。
- 取消、重试、错误处理、轮询、重启恢复、Provider 迟到响应和积分转移均使用条件 CAS；已退回/终态 Job 不能被迟到 Worker 复活，新尝试必须新 Job/幂等键/预占。
- RunAPI 不再从公开 `GenerationRequest.metadata.edge_task` 接收任务声明；ModelRole prompt refinement 的 task ID 和估价改为服务端确定性派生，旧调用参数保留兼容但被忽略。
- RunAPI Adapter 只接受强类型内部 `EdgeTask`；FactLock 校验 echo、candidate 文本/locked spans、人数和有界否定极性。LIVE transport 前的首次预算交易会直接插入 `UNCERTAIN` 并冻结 estimate；不存在 `RESERVED → UNCERTAIN` 的两交易窗口，崩溃不会遗留无法对账的 `RESERVED`，同步 actual response 与人工对账只有一个线性化赢家。
- Timeline 升级为 v2：仅 COMMITTED source 可传播，只能覆盖 DRAFT/PLANNED/READY target，并按结构 delta 重基下镜 planned output；HARD/HYBRID/RE_ANCHOR 的强首帧与软参考语义已分离。Autopilot 另保存服务端 input/output state ID + hash 围栏，Gateway 在 Job/Candidate/CostRecord/积分预占/排队的同一交易中锁定并复核。
- 新增真正的三镜离线 Fixture E2E：3 Jobs/outputs/QA/commits/end frames/snapshots/accepted CostRecords；保留原有 0-Job 算法测试。
- Gateway 在调度前和付费边界按 persisted model identity + `enabled/live_enabled` fail closed；ModelRoleRuntime 也从服务端 provider mode 强制 LIVE 语义，并在 transport 边界最后重解 binding/模型 ID/开关。环境 bootstrap 不再在重启时覆盖管理员 kill switch。
- Provider reference upload 只写 `MediaProviderBinding`，不再覆盖 `MediaAsset` 生成来源；Provider 下载的 origin 与新 MediaAsset 在同一原子插入中写入，命中已有用户上传或其他生成来源时不重标。canonical promotion 与 character identity confirmation 共用不可洗白的来源信任校验。
- 参考素材首次跨过远端上传边界时，`MediaProviderBinding=UPLOADING`、`GenerationJob=SENT_UNCONFIRMED`、禁止退款/重试与 workspace credit 边界事件同一交易提交；异常或 claim 丢失保持冻结对账。
- 已建立 `tests/live/` 合同、`live_provider` marker、`--run-live-provider` 开关和普通测试强制离线环境；根 `.dockerignore` 也阻止 `.env`/密钥/本地数据进入 build context。
- Compose 结构检查通过；本机没有可用 Docker daemon，因此未做 build/up/health 或 PostgreSQL + pgvector 实机迁移。

仍然阻断发布的事项：

1. 生成使用的 reservation/settlement/refund/reconcile 已完成；仍未完成 purchase/grant 周期/expiry/admin adjustment，Provider invoice/USD 成本对账仍是独立账务域。
2. 50 starter credits 与默认 8 秒 Seedance 约 87 credits 的产品矛盾未决策；当前会在创建 Provider Job 前返回余额不足，不会越权补贴或透支。
3. RunAPI 强类型/fact-lock/server pricing primitive 已完成；`UNCERTAIN` ProviderBudget 已有平台密钥保护、强证据和幂等 DecisionRecord 的人工对账。产品 prompt 接线、自动账单采集/验真和运营 UI 仍未完成。
4. PostgreSQL `0021`–`0024`、Compose build/up/health、真实 Provider fixture/live smoke、Flow 自动 affinity/受限迁移、真实视觉 QA、商业 Auth 收尾和 UI 镜头修订仍未完成。
5. `data/platform.db` 仍是交接中记录的混合 schema；本次只读验证其 SHA-256 仍为 `d0590d17e8a25984e9b5d7a6c8023b813d0dd5b19d3af4f509d25d1a19ebf863`，没有对它执行 upgrade/stamp/repair。
6. 对话中出现过的所有 Provider Key 仍必须在真实调用前由用户在 Provider 控制台撤销并轮换。

## 0. 这份文档的用法

这是一份面向下一个开发对话、工程师或 Agent 的冻结快照。它只记录以下三类事实：

1. 当前磁盘上可读取的代码、配置、迁移与测试。
2. 已经实际运行过的验证命令及其结果。
3. 用户明确提出的产品决策，但这些决策不会被冒充为已实现功能。

文档中的状态标签：

| 标签 | 含义 |
| --- | --- |
| `STABLE` | 已存在于提交的 MVP 基线，且曾通过完整门禁。 |
| `WIP` | 当前工作树有代码，但未提交、未全绿或未进入真实产品路径。 |
| `PARTIAL` | 边界、适配器或数据结构存在，但闭环不完整。 |
| `MOCK ONLY` | 仅验证 Mock/录制/单元路径，没有真实付费调用证据。 |
| `NOT IMPLEMENTED` | 只有需求或概念，没有可用执行路径。 |

接手顺序：

1. 先读本文档的“紧急阻断”和“不可破坏的约束”。
2. 再读 [`../CURRENT_ARCHITECTURE.md`](../CURRENT_ARCHITECTURE.md) 了解当前完整架构。
3. 读 [`PRODUCT_REQUIREMENTS_LEDGER.md`](PRODUCT_REQUIREMENTS_LEDGER.md) 了解用户意图和未实现决策。
4. 读 [`VISUAL_RUNTIME_IMPLEMENTATION.md`](VISUAL_RUNTIME_IMPLEMENTATION.md) 了解 Phase I Visual Runtime 的逐阶段实施。
5. 最后根据本文档的优先级继续开发，不要先扩展新 Skill 或新 Provider。

## 1. 2026-08-20 冻结执行摘要（历史）

产品是一个面向 AI 短剧和商业视觉生产的多租户 Web 平台，不是为某一部影片提供一次性制作服务。

当前有一个已提交、可恢复的 MVP 基线，以及一个未提交的 Phase II 工作树：

- `STABLE` 基线：商业化 Auth/RBAC/租户隔离、Passenger + Autopilot 共享运行时、任务网关与付费安全边界、逻辑资产版本、媒体来源链、QA/人工复核/正式采纳、响应式工作台。
- `WIP` Phase II：统一 Model Registry/Role Binding、Provider 信任等级与资产重要性、OpenRouter/Ark/Seedance/Wan/RunAPI/DeepSeek 适配器、确定性 Narrative/Timeline/Continuity/Generation Policy 核心、RunAPI 预算和工作空间积分账本半成品。
- `BLOCKED` 发布门禁：Free 用户仍可绕过套餐路由直接创建付费任务；50 积分未接入生成原子交易；RunAPI 任务 ID/估价可由公开请求自报；`0023` 会阻断两类支持的历史恢复快照；集成测试与 Ruff format 未全绿。

没有执行任何真实付费 Provider 调用。Phase II 的 Provider 验证都是 Mock/录制/单元级。

## 2. 仓库与 Git 冻结快照

| 项目 | 当前事实 |
| --- | --- |
| 仓库绝对路径 | `/Users/a1-6/Desktop/AI短剧分镜工作流/ai-director-platform` |
| 分支 | `main` |
| HEAD | `d16e4ac feat: establish commercial AI director MVP foundation` |
| 可恢复 tag | `v0.1.0-mvp-foundation` |
| `pyproject.toml` 版本 | `0.1.0` |
| 当前 Alembic head | `0024_workspace_credit_lifecycle` |
| 工作树 | 2026-08-20 冻结时 75 个路径：47 modified/tracked + 28 untracked；这是历史 manifest，当前数量以 `git status --short` 为准 |
| Phase II 是否提交 | 否 |
| Phase II 是否打 tag | 否 |
| Phase II 是否可发布 | 否 |

不要对这个工作树执行 `git reset --hard`、`git checkout -- .` 或删除 untracked 文件。Phase II 全部未提交实现都在这些变更中。如需分批提交，应先修复 P0/红色门禁，再按迁移、模型基础设施、Provider、算法、文档分组提交。

## 3. 2026-08-20 冻结验证证据（历史）

| 检查 | 最后实际结果 | 结论 |
| --- | --- | --- |
| `uv run pytest -q` | `264 passed, 3 failed, 31 warnings`（两次完整重跑的用时不同，计数一致） | **FAIL** |
| `uv run ruff format --check . --exclude references` | 4 个文件需重排 | **FAIL** |
| `uv run ruff check . --exclude references` | 通过 | PASS |
| `uv run mypy` | `115 source files` 通过 | PASS |
| `node --check apps/web/app.js` | 通过 | PASS |
| Python compile/compileall | 通过 | PASS |
| `git diff --check` | 文档继续编辑前通过 | 交付前重跑 |
| `uv run alembic heads` | 单 head `0023_workspace_credit_wallet` | PASS |
| 空 SQLite `upgrade head` + `alembic check` | `No new upgrade operations detected` | PASS |
| 历史/恢复 SQLite 快照升级 | 2 个回归失败 | **FAIL** |
| PostgreSQL 17 + pgvector | 只实测到 `0020` | `0021`–`0023` 未验证 |
| `docker compose config -q` | 提供安全占位内部密钥时通过 | 只是结构检查 |
| Compose build/up/health | Phase II 后未执行 | 未验证 |
| 真实 Provider smoke | 未执行 | 正确的安全停留状态 |

当前 3 个失败测试：

1. `tests/test_asset_registry.py::test_0008_skips_assetless_recovery_snapshot_but_rejects_partial_registry`
2. `tests/test_model_infrastructure.py::test_unconfigured_free_seedance_binding_fails_closed`
3. `tests/test_runtime_data_integrity.py::test_0006_migrates_legacy_duplicates`

已核实根因：

- 失败 1/3：`migrations/versions/0023_workspace_credit_wallet.py` 无条件对 `workspaces` 做 batch alter，但这两种项目已支持的 recovery snapshot 没有 `workspaces` 表。
- 失败 2：原始 `WorkspaceModelResolver` 仍可在 Seedance Adapter 未配置时返回默认 Free binding；新 `ModelRoleRuntime` 的 capability 路径会阻断，但两套解析契约未统一。

Ruff format 当前报告的 4 个文件：

- `core/asset-registry/asset_registry_core/service.py`
- `core/narrative/narrative_core/compiler.py`
- `core/production/director_production/pipeline.py`
- `packages/domain/production_domain/models.py`

## 4. 已完成的 MVP 基线

以下是 Phase II 开始前已完成并曾通过完整门禁的主要能力。历史稳定套件的记录是 `205 passed`；它不代表当前 Phase II 工作树的测试数。

### 4.1 商业账号与租户

- 邮箱注册、登录、退出和 `/me`。
- PBKDF2-SHA256（60 万轮 + 随机 salt）密码存储。
- 仅存 token hash 的可过期、可撤销会话。
- `OWNER / ADMIN / EDITOR / VIEWER` 工作空间权限。
- 项目、镜头、候选、人物、媒体、逻辑资产和生成路由的租户隔离。
- 工作空间/项目非 ACTIVE 时 fail closed。
- 生产环境禁止关闭 Auth。
- Legacy workspace 不再由“第一个注册者”自动接管；只能用受保护的内部路由、指定现有用户、幂等键和审计记录显式转移。

仍未完成：邮箱验证、找回密码、MFA、成员邀请/移除 UI、登录限流与异常风控、设备会话管理、安全审计事件、Secure/HttpOnly Cookie + CSRF。

### 4.2 共享生成运行时

- Passenger 与 Autopilot 共用 `VisualProductionRuntime`、`GenerationGateway`、`MediaRegistry`、Storage、Provider Scheduler 和成本记录。
- 项目级请求 hash + 幂等键。
- 数据库 CAS 账号/Worker 容量预约。
- 提交与轮询租约、fencing token、队列公平轮询。
- `NOT_SENT / SENT_UNCONFIRMED` 付费边界，不对未知结果盲目重试。
- 终态不回退、容量幂等释放、账号预约恢复。
- 每个 GenerationJob 最多一条成本记录。
- Browser command 数据库原子 claim 并绑定获胜 connection。
- Provider media 首次上传 claim/租约，未知远程结果必须通过受保护的 reconciliation 路径恢复。

### 4.3 资产、版本和媒体

- 内容字节按 SHA-256 共享，但镜头/候选的来源链分离。
- `Asset` + 不可变 `AssetVersion` + `AssetVersionMedia`。
- 人物、场景、商品、道具、服装、车辆、生物、声音、风格和通用参考类型。
- 用户可重新上传修改后的人物/场景/商品/道具图作为新版本。
- 只有显式 `AssetCanonicalPromotion` 才会切换正式版。
- 数据库触发器阻断跨资产 canonical/parent/promotion，阻断不可变历史的 update/delete。
- 上传类型、MIME/魔数、大小、文件下载头、Provider URL SSRF/重定向/私网/流式大小安全边界。

### 4.4 QA、复核和采纳

- 候选状态机和多维 QAResult。
- 证据不完整不会被当成自动 PASS。
- 非有限值、越界分数与畸形身份 evidence fail closed。
- 需人工确认的候选必须由有写权限的真实用户填写理由并显式勾选，产生单独 QAResult 和 DecisionRecord，之后仍需单独“采用”。
- 正式采纳以数据库 CAS/原子交易保证单一获胜候选、时间线快照、尾帧和成本状态一致。
- 终态候选不能被重新 QA 覆写。

仍未完成：真实生产抽帧、人物跟踪、视角分类、人脸身份 embedding 和真实 VLM Reviewer。

### 4.5 工作台与通俗化交互

- 注册/登录界面和退出。
- Passenger 与 Autopilot 双模式。
- 图片提示词纠正、保留事实、变更说明和撤销。
- 人物主参考 v1/v2 、场景/商品/道具版本上传与正式版切换。
- 指定镜头重做、质量检查、人工复核与采用。
- 圆角、高对比、科技感配色，完整响应式布局。
- 新建项目已用可访问的原生 `dialog` 表单替代 `prompt()`，支持 Enter/Escape、焦点恢复、提交锁定和 `aria-live`。

已知 UI 问题：镜头景别、角度、运镜、灯光控件都没有进入 `generateShot()` 的生成规格。角度/运镜另外只被压缩成一个二值 continuity-risk 提示，景别/灯光未被使用。发布前必须把四项都序列化到有版本的 Shot revision，或隐藏。

### 4.6 Prompt 与 Skill

- `ImagePromptCorrector` v2 保留原语言、右手/颜色/场所/视线/否定等具体事实，引号中精确商品文字不改写，记录 preserved constraints 与 undo。
- `VideoShotPromptCompiler` 只从已批准 CanonicalShotSpec 编译模型 prompt/payload，不应创造新动作、身份、道具、凝视或运镜。
- 当前 Prompt Compiler 是项目自写的确定性 `v2-compat` 实现，不是 Qwen/Claude/OpenRouter 在线生成。
- 当前本地 12 个 Skill：`director`、`short-drama`、`cinematography`、`composition`、`camera-movement`、`lighting`、`continuity`、`character-consistency`、`commercial`、`image-prompt-corrector`、`prompt-compiler`、`model-prompting`。
- Director Skill 与 Prompt Compiler 相对 `v0.1.0-mvp-foundation` 没有 Phase II diff。
- GitHub 公开仓库只用于方法/许可证研究，项目没有 vendored 上游 Skill body 或 runtime dependency。

### 4.7 已完成的安全/并发加固历史

这些是在 MVP 发布审计中已修复的重要问题，后续不得回归：

- 公开路由不再使用全局空 API Key 跨租户访问；内部路由 fail closed。
- 项目/人物/镜头/媒体/参考资产的跨项目注入在路由、服务或 DB invariant 被阻断。
- Provider target 在创建 job 之前 fail fast；历史/恶意未知 target 转为结构化失败，不会杀死全局 Worker。
- 付费提交、轮询、完成下载均有 claim lease/fencing；跨过付费边界的未知异常保持 `SENT_UNCONFIRMED`。
- Account/Worker 容量预约和释放改为数据库条件 UPDATE/CAS，cancel/restart/reconcile 也使用每 Job 预约所有权。
- Provider RUNNING 任务有轮询间隔，不会忙轮询饿死队列。
- Browser Worker 使用独立、可撤销凭据和一次性 WebSocket ticket；Command claim 绑定 connection，阻断重复付费执行。
- Candidate commit 对同 Shot 多候选使用原子 CAS，只有一个 winner；commit 与 re-QA 也以条件更新互斥。
- CostRecord 对 candidate-bound GenerationJob 有 DB 唯一约束和幂等插入，避免并发双记账。Passenger 缺口仍在 Phase II 阻断中单列。
- 文件上传使用唯一临时文件、流式大小上限、媒体解码/魔数校验；下载阻断主动内容和 MIME 欺骗。
- Provider media URL 必须 HTTPS/允许主机，每跳 DNS/实际 peer IP/重定向重新验证，拒绝私网/元数据网址和超大响应。
- 普通注册者不再自动认领 Legacy 数据；转移必须 internal key + 指定用户 + 幂等键 + 审计。
- 生产弱 platform key/弱 credential-encryption key/禁用 Auth 均在启动时 fail closed。

## 5. Phase II 当前已落地的代码

本节中的内容都是当前工作树中可读取的代码，但由于全仓门禁未全绿，统一标记为 `WIP`。

### 5.1 统一模型注册表

`0021_unified_model_registry` 引入：

- `ModelDefinition`：逻辑名、Provider、Provider 模型 ID、模态、能力、质量档、成本档、信任级别、允许的资产重要性、enabled/live-enabled 和 metadata。
- `ModelRoleBinding`：业务角色、套餐级别、逻辑模型与优先级。
- `ModelInfrastructureService`：从 `config/model-registry/defaults.json` 幂等导入默认值，区分默认值和管理员运行时修改。
- `WorkspaceModelResolver`/`ModelRoleRuntime`：按 workspace plan + role + trust + criticality 解析。

当前 JSON 中有 13 个逻辑模型定义和 30 个角色绑定。关键定义：

| 逻辑模型 | Provider 路径 | 当前事实 |
| --- | --- | --- |
| `gpt-5.6-sol-openrouter` | OpenRouter | enabled，live disabled；无真实调用证据 |
| `claude-sonnet-5-openrouter` | OpenRouter | enabled，live disabled；无真实调用证据 |
| `doubao-free-reasoner` | Ark/Seedance adapter | 默认是占位 ID 且 disabled；需显式运行时配置 |
| `runapi-prompt-refiner-edge` | RunAPI | 默认是占位 ID 且 disabled |
| `voyage-multimodal-3.5-openrouter` | OpenRouter | 逻辑定义存在；实际记忆路径仍是 Voyage 官方直连客户端 |
| `kling-3-standard-openrouter` | OpenRouter | 有 reviewed capability/pricing alias，Mock 通路已测 |
| `kling-3-pro-openrouter` | OpenRouter | 有 reviewed capability/pricing alias，Mock 通路已测 |
| `flow-veo-3.1-internal` | Google Flow | 浏览器 Worker 路径，未配置时语义仍有不一致 |
| `flow-narwhal-image-internal` | Google Flow | NARWHAL image 执行目标的持久 enabled/live-enabled 硬开关；无 IMAGE 业务角色绑定 |
| `seedance-2.5-official` | Ark/Seedance | 官方 Adapter 存在，未执行 live |
| `veo-3.1-quality-official` | Veo official | Provider 仍是 `NotConfiguredProvider` stub |
| `grok-video-official` | Grok official | Provider 仍是 `NotConfiguredProvider` stub |
| `wan-2.7-official` | Wan | T2V 路径部分配置，I2V/R2V 映射未收口 |

当前不能把“逻辑定义存在”表述为“真实模型已部署”。

配置文件中 30 个 binding 的精确快照（`ALL` 表示无专属套餐范围）：

| Role | Plan | Logical model | Priority |
| --- | --- | --- | ---: |
| `DIRECTOR` | ALL | `gpt-5.6-sol-openrouter` | 0 |
| `ASSISTANT_DIRECTOR` | ALL | `claude-sonnet-5-openrouter` | 0 |
| `CINEMATOGRAPHY_REASONING` | ALL | `gpt-5.6-sol-openrouter` | 0 |
| `PROMPT_COMPILER` | ALL | `gpt-5.6-sol-openrouter` | 0 |
| `PROMPT_REFINER` | ALL | `gpt-5.6-sol-openrouter` | 0 |
| `PROMPT_REFINER_LOW_COST` | ALL | `runapi-prompt-refiner-edge` | 0 |
| `PROMPT_REFINER_FALLBACK` | ALL | `gpt-5.6-sol-openrouter` | 0 |
| `NARRATIVE_COMPILER` | ALL | `claude-sonnet-5-openrouter` | 0 |
| `NARRATIVE_COMPILER` | ALL | `gpt-5.6-sol-openrouter` | 10 |
| `CONTINUITY_REASONER` | ALL | `gpt-5.6-sol-openrouter` | 0 |
| `GENERATION_POLICY_REASONER` | ALL | `gpt-5.6-sol-openrouter` | 0 |
| `VLM_REVIEWER` | ALL | `gpt-5.6-sol-openrouter` | 0 |
| `MULTIMODAL_EMBEDDING` | ALL | `voyage-multimodal-3.5-openrouter` | 0 |
| `VIDEO_KLING_STANDARD` | ALL | `kling-3-standard-openrouter` | 0 |
| `VIDEO_KLING_PRO` | ALL | `kling-3-pro-openrouter` | 0 |
| `VIDEO_FLOW` | ALL | `flow-veo-3.1-internal` | 0 |
| `VIDEO_SEEDANCE` | ALL | `seedance-2.5-official` | 0 |
| `VIDEO_VEO` | ALL | `veo-3.1-quality-official` | 0 |
| `VIDEO_GROK` | ALL | `grok-video-official` | 0 |
| `VIDEO_WAN` | ALL | `wan-2.7-official` | 0 |
| `DIRECTOR` | FREE | `doubao-free-reasoner` | 0 |
| `ASSISTANT_DIRECTOR` | FREE | `doubao-free-reasoner` | 0 |
| `CINEMATOGRAPHY_REASONING` | FREE | `doubao-free-reasoner` | 0 |
| `PROMPT_COMPILER` | FREE | `doubao-free-reasoner` | 0 |
| `PROMPT_REFINER` | FREE | `doubao-free-reasoner` | 0 |
| `NARRATIVE_COMPILER` | FREE | `doubao-free-reasoner` | 0 |
| `CONTINUITY_REASONER` | FREE | `doubao-free-reasoner` | 0 |
| `GENERATION_POLICY_REASONER` | FREE | `doubao-free-reasoner` | 0 |
| `VLM_REVIEWER` | FREE | `doubao-free-reasoner` | 0 |
| `VIDEO_SEEDANCE` | FREE | `seedance-2.5-official` | 0 |

注意：这 9 个 Free reasoning binding 都指向默认 disabled/占位 ID 的 Doubao 定义。因此 binding 行存在不意味 Free reasoning 可执行。所有 13 个 model definition 的冻结默认 `live_enabled` 都是 `false`。

### 5.2 Provider SDK 和 Adapter

新 Provider SDK 位于 `packages/provider-sdk/provider_sdk/`：

- `transport.py`：Mock / recorded / live transport 和三重 live gate。
- `http.py`：Provider HTTP 封装。
- `capabilities.py`：能力目录和执行表面。
- `budget.py`：Provider Budget 协议。
- `edge.py`：RunAPI EdgeTaskPolicy / FactLock / 预算契约。
- `trust.py`：ProviderTrustLevel / AssetCriticality 和硬兼容规则。

新增/扩展 Adapter：

- `providers/openrouter/`：chat、responses、embeddings、video 统一客户端表面。
- `providers/seedance/seedance_provider/adapter.py`：Ark 豆包 chat + Seedance 异步 video task。
- `providers/wan/`：OpenAI-compatible chat + DashScope async video。
- `providers/runapi/`：Edge chat/image/video + budget/fact-lock。
- `providers/deepseek/`：DeepSeek-compatible chat；无默认业务角色绑定。
- `providers/google-flow/`：既有 BrowserRuntime 适配器补上三重 live gate。

`services/generation-gateway/generation_gateway/direct.py` 为官方 HTTP Provider 创建 synthetic account/worker 调度资源，不把 Provider secret 写入业务数据表。

### 5.3 Provider 信任与资产重要性

Provider 信任等级：

```text
CANONICAL > PRODUCTION > STANDARD > EDGE > TEST_ONLY
```

资产重要性：

```text
CANONICAL, HERO, IMPORTANT, STANDARD, EDGE, TEMPORARY
```

已落地的硬边界：

- Gateway 在创建 job 前检查 Provider trust 是否满足 AssetCriticality。
- RunAPI/EDGE 不能直接生成 canonical/hero/important 资产。
- 逻辑资产 promotion 和 Candidate commit 会再次检查不可变 provenance。
- 把 EDGE 结果包装成“用户上传版本”不能洗成 canonical。

这一硬边界已被审计确认。RunAPI 的任务身份、估价、live 付费边界和人工对账已改为服务端事实；当前主要剩余问题是产品 prompt 路径尚未接入、FactLock 仍是有界词法护栏，以及自动账单采集/验真与运营 UI 未完成。

### 5.4 RunAPI 持久预算

`0022_free_plan_provider_budget` 和 `core/provider-budget/` 引入 ProviderBudget 与 usage ledger，Container 为 RunAPI 初始化 10 USD 上限，并且不覆盖已有支出。预算仓库具备 reserve/settle 幂等和并发回归。

当前 Adapter 只接受进程内的强类型 `EdgeTask`，公开 metadata/dict 不能构造任务身份。Prompt refinement 的 task ID 由 workspace/project/role/prompt hash/fact locks/pricing version 确定性派生，当前估价固定为服务端 pricing snapshot 的 0.01 USD。FactLock 同时校验模型 echo、candidate 字面/locked spans、人数证据与有界中英文否定极性，无法证明保真时 fail closed/fallback；这是确定性词法护栏，不是通用语义蕴含模型。Provider 没有返回实际成本时记为 `UNCERTAIN` 并继续冻结 estimate，不再冒充 actual cost。

`UNCERTAIN` 预算已可通过 `PLATFORM_API_KEY` 保护的内部路由进行人工对账：只能依账单证据结算实际 USD，或明确证实远端未收费后释放；强制显式确认、证据引用、幂等键和 DecisionRecord，且与 workspace credits 分账。仍未完成：这个 fact-locked primitive 还没有接入用户可见 prompt 产品路径；外部账单自动采集/验真和运营 UI 未实现。

### 5.5 工作空间生成积分生命周期

`0023_workspace_credit_wallet` 建立余额与旧扣费记录；`0024_workspace_credit_lifecycle` 将其升级为生成专用的聚合与事件账本：

- `WorkspaceCreditEntry` 以 Generation Job 唯一标识一次服务端估价的预占，请求幂等范围为 project。
- `WorkspaceCreditEvent` 记录预占、跨过 Provider 边界、确认、结算、退回与对账决策。
- Job 保存 `workspace_credit_required` 和 `quoted_credits` 不可变计费事实，不会因 workspace 套餐后续变更而误分类。
- Reserve 立即减少可用余额；生成成功结算原估价；只有明确的提交前终态/取消才自动退回。
- `SENT_UNCONFIRMED`、已提交后失败/取消或其他付费结果不确定时进入 `RECONCILIATION_REQUIRED`，余额继续冻结。
- 内部对账只接收“Provider 已接受”或“Provider 未创建”证据决策，原子全额结算/退回，强制幂等键、显式确认、审计事件和 `DecisionRecord`。
- `CostRecord`、Provider invoice/USD、Flow 账号积分和 RunAPI 预算均不是 workspace wallet 事实源。

未包含在这一里程碑的是积分购买、周期 grant、过期和管理员调账。已知产品矛盾仍在：当前 Seedance 静态估价约 87 积分，Free 新用户的 50 积分买不起默认 8 秒任务。钱包会在创建 Job/Provider 调用前 fail closed，但赠送额、价格、默认时长或补贴规则仍需产品决策。

### 5.6 Narrative Compiler v2

版本：`narrative-rules-v2`。

已实现：

- 识别常见中英文场景头。
- 仅在显式标点/顺序词边界拆分独立动作。
- 对话行作为一个 `speak` 主动作。
- 每个 Shot 默认只有一个 `primary_action`。
- Character/Location/Prop 复用项目级 SQL 实体 UUID。
- Action/Dialogue/NarrativeFact/Relationship 使用稳定 UUID5。
- Event 记录 `pre_state` / actor / action / target / object / dialogue / effects / `post_state`。
- 同一 script hash + compiler version 幂等。
- Episode 已含 committed shot 时禁止重编译。

边界：这是确定性规则编译器，不是完整中文语义解析器。当前不能声称完成共指消解、隐含动作、复杂并行动作或叙事关系推理。

### 5.7 SQL 权威时间线

版本：`sql-timeline-propagation-v2`。

正常 Candidate commit 路径的当前规则：

```text
Shot N 已采纳 SHOT_OUTPUT
→ 深拷贝 state_json
→ Shot N+1 未采纳 SHOT_INPUT
→ previous_state_id = Shot N output state id
→ 计算 Shot N+1 旧 input → 旧 planned output 的结构化 delta
→ 把 delta 重放到新 input，重基 planned SHOT_OUTPUT
```

以下边界停止传播：`SCENE_CHANGE`、`TIMELINE_JUMP`、`FLASHBACK`、`MONTAGE`、`EXPLICIT_RESET`。每次传播写 `TIMELINE_PROPAGATION` DecisionRecord，包含 `target_output_state_id` 和 `output_rebased`。Engine 自身强制 source Shot 必须 `COMMITTED`，target 只允许 `DRAFT/PLANNED/READY`；项目归属、state kind、输出归属或活动/终态围栏不满足时，任何 input/output 写入之前 fail closed。

Autopilot 在准备前后比对服务端 `AuthoritativeTimelineFence`（Shot status、input/output state ID、previous state ID 和稳定 JSON hash）。Gateway 又在同一事务内锁定并复核这些事实，然后才创建 Job/Candidate/CostRecord/积分预占并把 Shot 改为 `QUEUED`。时间线被前镜 Commit 重基或状态已变时，过期计划在任何写入前冲突退回；同请求同幂等键仍可正确 replay，不重复扣费。

边界：目前只沿 `next_shot_id` 传一跳；旧数据只有 `previous_shot_id` 时不会自动反查；reset 类型仍在 JSON 中；不会递归重算整条已采纳时间线。SQL state 是权威事实源，向量检索不得反向覆盖它。

### 5.8 Continuity v2

风险公式：

```text
0.10 camera_angle_delta
+ 0.16 camera_axis_delta
+ 0.05 shot_scale_delta
+ 0.08 pose_delta
+ 0.08 orientation_delta
+ 0.05 occlusion
+ 0.08 blocking_delta
+ 0.14 scene_delta
+ 0.12 timeline_delta
+ 0.08 identity_risk
+ 0.04 * (1 - action_continuity)
+ 0.02 * (1 - previous_frame_quality)
```

非有限值直接拒绝，所有特征限定在 `[0,1]`。

强制 `RE_ANCHOR` 条件：反打、camera axis delta ≥ 0.65、场景/时间跳变、闪回、蒙太奇、显式重置、上一尾帧缺失或质量 < 0.35、identity risk ≥ 0.75。

`HARD_CONTINUITY` 需要：综合风险 ≤ 0.24、同场景/同时间线/同动作链、非反打、axis delta < 0.25、action continuity ≥ 0.7、上一帧质量 ≥ 0.65 且可用。其余为 `HYBRID`。

边界：这些特征目前主要由调用方填写，还没有从视频帧、镜头规格和摄影轴数据中自动提取。

### 5.9 Generation Policy v2

确定性映射：

| 连续性类型/输入 | 策略 |
| --- | --- |
| `HARD_CONTINUITY` + previous end frame | `CONTINUE_I2V` |
| `HYBRID` + end frame + character ref | `HYBRID_REFERENCE` |
| `HYBRID` 缺上一帧但有 character + scene canonical refs | `REANCHOR_FULL` |
| `RE_ANCHOR` | `REANCHOR_FULL` |
| 显式 start + end frame | `START_END_FRAME` |
| 只有 start frame | `IMAGE_TO_VIDEO` |
| 有通用参考图 | `REFERENCE_TO_VIDEO` |
| 无连续性资产依赖 | `TEXT_TO_VIDEO` |

必需输入缺失时 fail closed。只有 `HARD_CONTINUITY` 把上镜尾帧作为强 start frame；`HYBRID` 会清空强 start，仅把上镜尾帧作为软 reference/context；反打 `RE_ANCHOR` 同样清空 start，避免把上镜尾帧当成新机位的起点。

边界：当前只把“需要匹配角度的人物参考”写入 required context，没有真正从 front/profile/three-quarter 资产中选角度；Policy 本身没有完整成本预算输入；部分 Shot 更新和 DecisionRecord 仍需收口事务边界。

### 5.10 Capability Resolver

先根据任务需要的能力做硬过滤，再结合偏好、单个已接受镜头的成本、可靠性、延迟和任务质量先验评分。Close-up/Dialogue 更看重身份，Action 更看重动作与机位，其他镜头综合 render/camera/action。

只有 `START_END_FRAME` 在 Provider 不支持时允许降级为 I2V，其他关键策略不再无条件降级为 T2V。

当前存在三个不完全一致的能力事实源：持久化 `ModelInfrastructureService`、视频 JSON `ModelCapabilityRegistry`、算法侧 legacy `ProviderCapabilityRegistry`。后续必须合并。

### 5.11 Dynamic Identity QA 接口

已落地指标：average/minimum/p10 identity、drift slope、appearance/costume/hair similarity、reacquisition score、low-score fraction、usable/invalid samples。规则抽样位置是 `0, 0.2, 0.4, 0.6, 0.8, 0.98`，可附加 motion spikes。

当前关键阈值：

- minimum identity < 0.62：`IDENTITY_DRIFT`。
- slope ≤ -0.045 且 minimum < 0.72：持续漂移。
- average identity < 0.5：`WRONG_CHARACTER`。
- hair/costume < 0.65：对应漂移。
- 默认至少 6 个 identity samples，或受信内部 QC 显式声明不适用。
- overall ≥ 0.78 且 identity minimum ≥ 0.72 才能 PASS；overall ≥ 0.62 为 SOFT_FAIL；证据不完整为 USER_REVIEW_REQUIRED。

重要：这是规则接口，不是已部署的视觉质检系统。没有真实抽帧器、Person Tracker、View Classifier、Face Identity Embedding 或 VLM Reviewer。
正常 Worker 在完成 Candidate 后没有可信视觉 evidence producer，因此当前实际默认结果通常是 `USER_REVIEW_REQUIRED`，然后走显式人工复核。

## 6. Phase II 十六项实际交付报告

这一节按 Phase II 工程简报的交付问题顺序回答，避免“代码存在”和“产品已上线”混为一谈。

### 6.1 已注册的模型

`config/model-registry/defaults.json` 有 13 个逻辑定义、30 个角色绑定。包括 OpenRouter GPT-5.6 Sol / Claude Sonnet 5 / Voyage / Kling std+pro、Flow Veo + NARWHAL image、Seedance 2.5、Veo official、Grok、Wan 2.7，以及 disabled 的 Doubao/RunAPI 占位。这是配置注册，不是 live deployment 清单。

### 6.2 已配置的 Provider

代码层有 Google Flow、OpenRouter、Ark/Seedance、Wan、RunAPI、DeepSeek 适配器或运行路径。当前快照不能声称任何一个已用有效密钥进行真实配置或 live smoke。缺密钥应为 `NOT_CONFIGURED`，但 Seedance raw resolver 和 Flow `configured/capability_configured` 仍有契约不一致。

### 6.3 仅 Mock 或 Stub 的 Provider

- 本次所有新适配器都只有 Mock/单元验证。
- Veo official、Grok official、Kling direct、Omni、Runway 仍是诚实的 `NotConfiguredProvider` slot/stub。
- Kling 经 OpenRouter 是单独路径，不等于 Kling direct Provider 已部署。

### 6.4 RunAPI 允许的任务

只允许 `EDGE/TEMPORARY` 低价值任务，如 prompt 草稿重写/翻译、negative prompt/风格词建议、metadata caption、搜索 query rewrite 和低价值分类。媒体生成只允许策略中的 `NON_CANONICAL_TEST_GENERATION`、`TEMPORARY_PLACEHOLDER_ASSET`、`PROVIDER_INTEGRATION_SMOKE`。不得用于正式人物/场景/商品主资产、重要关键帧、对话/特写正式镜头、身份 QA 或 production commit。

### 6.5 实际 Provider 支出

本次开发没有开启 live gate，没有执行付费 Provider 调用。因此本次已知开发调用支出为 0。不能从本地 ProviderBudget 行推断外部 Provider 账户总余额或历史账单；这需要各 Provider 控制台/API 对账。

### 6.6 OpenRouter 映射

已建立 GPT/Claude/Kling/Voyage 逻辑定义，Kling std/pro 有评审过的能力/价格 alias。`ModelRoleRuntime` 可执行 chat/embedding/refine primitive，但产品只真正使用了角色列表和 Passenger resolve；导演/副导演/摄影/提示词工作流没有调用这些 execution methods。Memory 仍直连 Voyage client，不走 OpenRouter embedding role。

### 6.7 Google Flow 账号池

已有 ProviderAccount、BrowserWorker、ProviderProject/Binding、账号/工作器容量 CAS、Worker 专用凭据、WebSocket ticket、上传/poll/download 和 live gate。但是：

- 首次项目不会自动建立 Flow Provider Project 与持久 affinity。
- 目前主要依赖受保护的手工 binding 路由。
- submit 会把 binding project 写入 Job 的持久 provider request，poll 优先复用同一个 project context。
- 缺受限的 account/project migration service 及完整迁移审计。
- 未使用真实 Flow 账号做这个快照的 smoke test。

### 6.8 Narrative Compiler

确定性 v2 编译核心已实现并有专项测试，见 5.6。它不依赖真实 LLM，也不是完整语义理解系统。

### 6.9 Timeline

SQL 权威状态的一跳传播、source/target 围栏、next-shot planned output 重基、reset boundary 和 DecisionRecord 已实现，见 5.7。Autopilot preparation 生成 shot status + input/output state ID/hash 围栏；Gateway 在创建 Job、Candidate、CostRecord、积分预占和 QUEUED 状态的同一交易中锁行复核，过期计划在任何写入前返回冲突。整条 timeline 递归重算和正式 transition enum/schema 尚未完成。

### 6.10 Continuity

Continuity v2 可根据有界特征向量返回 `HARD_CONTINUITY/HYBRID/RE_ANCHOR`、原因码和 required context。特征自动提取未实现。

### 6.11 Generation Policy

与 Provider 无关的确定性策略已实现，并且重要参考缺失时 fail closed。真正的人物角度参考排名和完整 cost/quality 输入尚未完成。

### 6.12 Router

当前同时存在持久逻辑 Model Registry、视频 JSON 能力先验和算法侧 Capability Registry。所有已认证公开生成入口已统一经 Admission 执行 plan/role/deployment/trust/pricing/credit 门禁；Gateway 还在调度前和付费边界核对 persisted `enabled/live_enabled`。但 ModelRoleRuntime 的 chat/embedding/refine execution methods 仍未接入导演、摄影、记忆或用户 prompt 的产品链，三套 capability 事实源也未合并。

### 6.13 QA

规则 QA、不完整证据 fail closed、动态身份指标接口、人工复核和并发安全 commit 已实现。真实视觉 QA 技术栈未实现。

### 6.14 三镜离线 Fixture E2E

保留了原先明确断言 0 Job 的算法/状态测试，同时新增真正的离线 Fixture 服务闭环：Script → 3 Candidates/Jobs → Provider submit/poll COMPLETED → 本地有效 MP4 注册 → 受信合成 QA PASS → 3 commits → FFmpeg END_FRAME → snapshots/timeline propagation/3 accepted CostRecords。Shot 2 实际请求为 `HARD_CONTINUITY/CONTINUE_I2V` 且 start=上镜 committed end frame；Shot 3 为 `RE_ANCHOR/REANCHOR_FULL`、start 为空并携带 canonical character/scene refs。全程无网络/付费，不是真实 Provider 或真实视觉 QA 证据。

### 6.15 测试现状

当前完整计数以本文顶部的 2026-08-21 更新为准。已增加积分生命周期、并发终态 fencing、内部对账、迁移往返、模型/Provider 信任边界和三镜 Fixture 回归。`tests/live/README.md`、`live_provider` marker 与 `--run-live-provider` 已建立；普通套件会强制 mock/关闭 gates。目前没有可执行 live case，也没有执行付费 Provider 调用。

### 6.16 未解决项

以下第 7–9 节包含历史问题和实施顺序，已解决项目以 2026-08-21 标注为准。剩余核心项是：已暴露密钥轮换、PostgreSQL/Compose 实机验证、Seedance/Flow/Wan configured 语义、Flow 自动 affinity/迁移、三套能力事实源、ModelRole 产品接线、真实视觉 QA、真实 Provider fixtures 与商业钱包/定价收尾。

## 7. 紧急阻断与风险清单

### 7.1 P0：下一次开发必须先修

#### P0-1 Free/套餐/积分可被绕过（2026-08-21 已解决）

`GenerationAdmissionService` 已覆盖公开生成入口，Gateway 对 Free 缺服务端 quote/reservation 时 fail closed。下文保留原始复现与修复要求作为历史证据。

已经 Mock 端到端复现：新注册 Free 用户可向 `/v1/generations` 提交具体 `openrouter:kwaivgi/kling-v3.0-pro`，得到 `202 NEW`。

原因：

- `/v1/generations`、`/v1/images/generations`、`/v1/videos/generations` 仍接收 raw provider/model。
- Shot Candidate 与 legacy fallback 仍可携带/硬编码 Provider。
- 只有 `/api/passenger/generate` 的 Free video 分支强制 `VIDEO_SEEDANCE`。
- `WorkspaceCreditService.charge_generation()` 全仓没有产品调用方。
- `GenerationGateway.create()` 没有套餐/余额/扣费 hook。

必须建立一个 server-owned 的统一入口，在同一个原子事务中完成：

```text
workspace + plan
→ business role/capability
→ deployment availability
→ provider trust / asset criticality
→ server pricing
→ credit reservation/charge
→ generation candidate/job/idempotency/trace
```

用户端显示逻辑模型选项，但不能提交一个能绕过套餐的 Provider ID。

#### P0-2 RunAPI 任务身份和估价不可信（2026-08-21 已解决）

现在 prompt refinement 的身份/估价由服务端派生，Adapter 只接受强类型内部 `EdgeTask`，缺 actual cost 保持 `UNCERTAIN` 冻结。下文保留原始问题与修复要求作为历史证据。

公开 `GenerationRequest.metadata` 可填写 `edge_task.task_id` 和 `estimated_cost_usd`。RunAPI Adapter 直接据此预留/结算。用户可声明极小成本绕过 10 USD 预算意图。

修复必须满足：

- `task_id` 由服务端生成，并与 workspace/project/job/idempotency 绑定。
- 角色、关键性、单价、数量、时长/尺寸和估价由服务端 pricing catalog 计算。
- 如需将 EdgeTask 传到 Adapter，使用服务端签名或内部类型，不从公开 metadata 解析。
- 实际 Provider cost 不可用客户端声明值 fallback。
- 预算归零时 Provider 自动停止被路由。

#### P0-3 `0023` 历史数据升级阻断（2026-08-21 已解决）

`0023` 已安全处理支持的 recovery snapshot；`0024` 另增带历史账本数据的升/降/再升往返回归，并拒绝升级一个没有 reservation 的非终态 Free Job。

`0023_workspace_credit_wallet` 在不检查 `workspaces` 是否存在的情况下 batch alter，导致 assetless/legacy recovery snapshot 失败。

修复选项：

- 如果项目继续承诺这些 recovery snapshot，`0023` 必须像既有防护迁移一样在表缺失时安全跳过，但在表集部分存在时 fail closed。
- 或者在发布契约中正式删除这两类恢复路径，并修改历史测试/文档。不能静默破坏。

必须同时验证：空 SQLite、两种 recovery snapshot、新鲜 PostgreSQL+pgvector、从 `0020/0021/0022` 升级、`alembic check`。

#### P0-4 已在对话暴露的 Provider 凭据

用户曾在对话中粘贴 Ark/Seedance、Wan/Alibaba、OpenRouter、RunAPI 和 DeepSeek 真实 API Key。仓库扫描没有发现这些值被写入代码、测试或文档，但对话本身已经是暴露渠道。

下一次 live 之前必须：

1. 在每个 Provider 控制台撤销旧 Key。
2. 生成新的最小权限 Key。
3. 只通过本地 secret manager 或 `scripts/configure_provider_secrets.py` 配置。
4. 不得在 issue、PR、日志、截图或新对话中重新粘贴。

### 7.2 P1：上线前必须完成

1. **业务抽象仍需收口。** 公开入口已统一经过 Admission，但外部合同和部分内部对象仍暴露 raw provider/model；应继续收口为友好逻辑 role/capability，不能移除已有服务端重解析门禁。
2. **ModelRole 执行未进入产品。** `execute_chat/execute_embeddings/refine_prompt` 只有测试调用；Free Doubao 不得宣称已用于导演、编剧或提示词流程。
3. **configured 语义不一致。** 原始 Seedance resolver 会在无 Adapter 配置时返回 Free binding；Flow 的 `capability_configured` 与 Gateway `configured` 不同步，可能创建后必然失败的 job。
4. **Veo/Grok 官方 Provider 不完整。** 当前只有 stub，不得在 UI/API 宣称可用。
5. **OpenRouter/Voyage 策略未完整。** Memory 还是 Voyage 官方直连；OpenRouter 无 live model listing/schema smoke。Voyage 只能做跨模态检索/排序，不能当人脸身份验证系统。
6. **Wan 模式 ID 未收口。** I2V/R2V env IDs 没有完整进入 role/pricing/router；request model 优先语义可能让 I2V/R2V 误用 T2V ID。
   另外，用户提供的 Alibaba workspace-specific API host 目前作为非密钥默认值出现在配置辅助代码中。它必须被当成环境配置，而不是全局官方端点事实，部署前要独立验证/替换。
7. **Flow project affinity/migration 不完整。** submit/poll 已复用同一持久 project context，但仍需首次项目自动绑定固定账号/Provider Project、限制性迁移与完整审计。当前还没有强制每个本地项目/provider 只能有一个 READY binding，也未阻止同一远端 project 被多个本地项目复用；provider job ID 冲突时的 poll 定位也需更强的组合标识。
8. **Fact-locked refinement 未接入产品。** candidate 文本/locked spans 的服务端验证已实现并 fail closed，但用户可见 prompt path 还是本地 corrector，没有调用这个 primitive。
9. **三套能力事实源会漂移。** 需合并 persisted model definitions、video capability JSON 和 legacy capability registry。
10. **真实视觉 QA 缺失。** 在抽帧/跟踪/视角/身份 embedding/VLM 未接入前，自动高风险开关必须保持关闭，人工复核必须保留。
11. **Compose 不是生产部署。** 当前使用开发 Postgres/MinIO 凭据，新 Provider env 未完整传入，未 build/up/health。
12. **UI 摄影控件未接线。** 需改为有版本的 Shot revision 交易，否则隐藏，不可给用户假功能。
13. **商业 Auth 仍缺公网能力。** 邮箱验证、找回密码、MFA、邀请/成员管理、限流/风控、安全事件、Cookie/CSRF 仍需实现。

### 7.3 P2：不阻断修复开发，但不得遗忘

- 已有本地 Fixture 三镜 Candidate/Job/Output/QA/Commit E2E，但不包含真实 Provider 或真实视觉 evidence producer。
- 已有 `tests/live/` 合同、`live_provider` marker 和显式 CLI 开关，但没有可执行 live cases/CI job。
- 没有 Phase II PostgreSQL `0021`–`0024` 实机验证。
- 重载前端幂等 intent 已进入 `sessionStorage`，但没有耐久的服务端 outbox。
- 项目级总存储配额没有接入套餐/账本。
- 自适应 Router 缺真实样本和 benchmark，feature flag 应继续关闭。
- Hook/R3 实验平台、回归、显著性/置信区间/交叉验证和动态样本量未实现。

## 8. 数据库迁移地图

| Revision | 主要目的 | 当前备注 |
| --- | --- | --- |
| `0001_platform_v1` | V1 基础表 | 历史基线 |
| `0002_director_platform` | Director 平台模型 | 历史快照迁移 |
| `0003_prompt_revisions` | Prompt revision 历史 | 对 dynamic V1 metadata 做了兼容 |
| `0004_unified_asset_registry` | Asset/Version/Promotion | 逻辑资产基础 |
| `0005_visual_runtime` | Memory/Evaluation/Metrics/Trace | Visual Runtime |
| `0006_runtime_data_integrity` | 数据完整性修复 | 包含 trace/index 修复 |
| `0007_commercial_auth` | User/Workspace/Membership/Session | 商业 Auth |
| `0008_asset_registry_invariants` | Asset 不可变触发器/约束 | 支持 assetless recovery skip |
| `0009_generation_job_claim_lease` | Generation claim lease | 付费任务并发所有权 |
| `0010_shot_lineage_invariants` | Shot lineage | 阻断跨项目前镜关系 |
| `0011_legacy_workspace_backfill` | Legacy workspace 隔离 | 后续认领已改为显式内部转移 |
| `0012_project_scoped_idempotency` | 项目级幂等 | 并扩展 Alembic version 长度以兼容 PG |
| `0013_generation_reservation_ownership` | 每 Job 调度预约所有权 | cancel/recover/reconcile 容量幂等 |
| `0014_worker_scoped_credentials` | Worker 专用凭据/ticket | 不再分发 platform key |
| `0015_cost_record_job_idempotency` | 每 Job 唯一成本 | 防止并发双记账 |
| `0016_worker_command_claim_binding` | Command 绑定 connection claim | 防止付费命令重复投递 |
| `0017_explicit_legacy_workspace_claims` | 显式 Legacy 转移审计 | 普通注册者不能接管旧数据 |
| `0018_postgres_timeline_vectors` | PostgreSQL vector(16) 转换 | 已在 PG17+pgvector 验证历史 JSON 数据保留 |
| `0019_media_asset_lineage_identity` | 分离字节去重和来源链 | 相同内容不再污染镜头/候选 lineage |
| `0020_provider_media_upload_claim` | Provider media 上传 claim/reconcile | 到此的 PG17+pgvector 实机验证通过 |
| `0021_unified_model_registry` | ModelDefinition/RoleBinding | Phase II WIP |
| `0022_free_plan_provider_budget` | `plan_tier` + Provider budget | RunAPI 10 USD 预算 + `UNCERTAIN` 内部对账；真实账单自动对接待完成 |
| `0023_workspace_credit_wallet` | starter credits + legacy workspace charge | SQLite fresh/historical recovery 已修复 |
| `0024_workspace_credit_lifecycle` | reserve/settle/refund/reconcile + append-only events | SQLite populated round trip 已验证；真实 PostgreSQL 待验证 |

新迁移不得继续编号，直到 `0023` 的语义、回填、降级与历史 snapshot 契约被修复并验证。

### 8.1 本地开发库的特别风险

已读验的 `data/platform.db` 并不在 migration head：

- `alembic_version` 仍是 `0020_provider_media_upload_claim`。
- `workspaces` 表没有 `0022` 的 `plan_tier` 和 `0023` 的 `credit_balance`。
- `model_definitions`、`model_role_bindings`、`provider_budgets`、`provider_budget_usages`、`workspace_credit_entries` 却已经存在，这些表可能由 ORM `create_all` 提前创建。

这是一个“版本号停在 0020，部分 0021–0023 schema 已出现”的混合漂移库。它不能作为 migration 正确性证据，也不应被盲目升级或回滚。
`Settings` 默认数据库 URL 就指向这个文件，当前代码读取 `Workspace.plan_tier/credit_balance` 时可直接出现 `no such column`。因此默认本地启动也不应在未备份/修复前被当成健康开发环境。

接手时：

1. 先复制备份并记录 checksum。
2. 用全新临时 SQLite/PostgreSQL 做迁移测试，不要用这个开发库证明 fresh install。
3. 如需保留开发数据，写一个显式的 schema audit/repair runbook，不要手工改 `alembic_version`。

## 9. 后续开发的严格顺序

下面的顺序是为了先消除可产生真实费用、数据漂移或越权的风险，再增加模型与智能。

### Phase A：保全现场与撤销密钥

1. 在 Provider 端撤销/轮换对话中出现过的全部 Key。
2. 对 dirty tree 做本地可恢复备份，但不包含 `.env`、数据库凭据或 Provider secret。
3. 记录 `git status --short`、`git diff --stat`、数据库 checksum 和 Alembic 版本。
4. 保持 `PROVIDER_MODE=mock`、`ALLOW_LIVE_PROVIDER_CALLS=false`。

验收：旧 Key 在 Provider 控制台失效；仓库与日志扫描无真实 Key；工作树可恢复。

### Phase B：修复迁移和基础门禁

1. 修复 `0023` 对支持的 recovery snapshot 的兼容。
2. 修复 4 个 Ruff format 文件，只做机械格式化，审查 diff 确认无语义变更。
3. 在全新 SQLite、两种恢复快照、PostgreSQL+pgvector 上执行 migration matrix。
4. 解决本地 `platform.db` 的混合 schema，但必须先备份并使用专门修复方案。

验收：`pytest`、Ruff format/lint、Mypy、Node syntax、fresh + historical migrations 全绿；`alembic heads` 单头；PostgreSQL `alembic check` 无 drift。

### Phase C：统一生成 Admission Gate 和积分（生成生命周期已完成）

1. 定义一个公开生成意图合同，用业务 role/capability/quality option，不相信 provider/model。
2. Passenger、generic、OpenAI-compatible image/video、Shot Candidate 和所有 fallback 共用同一个服务端 Admission Service。
3. 服务端原子执行 workspace status + membership + plan + role + deployment availability + trust + criticality + pricing + credit reservation + job/candidate/idempotency/trace。
4. Passenger 和 Autopilot 均在 Job 创建交易内建立唯一 `CostRecord` 和工作空间积分预占。Worker 完成更新在未传 credits 时保留原服务端估价，不再清零 Passenger 或 Candidate 成本记录。
5. Credit reservation/settlement/refund/reconcile 已完成：pre-submit terminal 退回，成功结算，provider accepted/uncertain/failure/cancel 在无未计费证据时保持冻结并要求内部对账。
6. 先决定“50 积分 vs 87 积分默认 Seedance”，再开放钱包。

验收：Free 用户无法通过任何公开入口选择付费高阶 Provider；余额不足时在任何 Job/Provider 调用前失败；并发同键只扣一次；所有路径都有成本/积分账本。

### Phase D：修复 RunAPI 信任边界

1. 删除公开 metadata 对 EdgeTask 的控制权。
2. 用服务端 catalog 估价并绑定 job/workspace/project。
3. 对 task envelope 签名或使用不可从 HTTP body 构造的内部对象。
4. 补充预算用完禁用路由、实际成本结算和差异审计。
5. 将 FactLock 从“比较模型自报 echo”升级为对 candidate 内容/结构化 locked spans 的服务端验证。

验收：客户端伪造 cost/task/role/criticality 全部被忽略或 4xx；并发任务不超预算；失败 FactLock 必然 fallback/拒绝。

### Phase E：合并 Model/Capability 事实源

1. 为“部署可用”定义单一语义：逻辑 enabled + runtime ID + credential/config + adapter health + live gate 分层表达。
2. 统一 Seedance `enabled/configured`、Flow `configured/capability_configured`。
3. 合并 3 套 capability registry，保留版本化、已评审、可追溯的质量/价格先验。
4. 解决逻辑 registry 的 Wan 2.7 与 `config/video-models/wan.json` 的 experimental Wan 3.0 漂移。
5. 将 Wan T2V/I2V/R2V 的 runtime ID、capability、pricing 和 role 完整对齐。

验收：无凭据/无部署 ID 的模型不会出现在用户列表，不会创建 job；同一模型在角色列表、估价、路由、Adapter 中使用同一 runtime ID。

### Phase F：把 ModelRoleRuntime 接入真实产品链

1. Director/Assistant Director/Cinematography/Prompt Refiner/VLM/Embedding 均从业务 role 解析，不硬编码 Provider。
2. Free reasoning 才在实际产品路径调用配置后的 Doubao。
3. 新增 Free image 逻辑角色并把豆包/Ark 图像路径接入；当前没有 `IMAGE_*` role，Ark image surface 也没有进入 Free UI/API。
4. Prompt 产品路径实际调用 fact-locked refinement；高风险事实仍由确定性编译器与服务端校验掌控。
5. 将 embedding 策略收口；Voyage 只用于检索/排序，不作为人脸身份裁决。

验收：DecisionRecord 记录 role/model/config version/fallback，不记 prompt 原文或 secret；Free/Paid 路由有产品 E2E。

### Phase G：Flow affinity 和真实 Provider Fixture

1. 首次项目选择一个可用 account，创建/绑定固定 Provider Project，之后优先复用。
2. submit/upload/poll/download 必须使用同一 affinity 上下文。
3. 实现受限 migration：只在账号不可用或显式运维决策时迁移，记录 from/to/reason/actor/time/affected assets/jobs。
4. 先为每个 Provider 建 recorded fixture 和错误映射测试，再建隔离的 `tests/live/` + marker。

验收：Mock/recorded 端到端先全绿；live 必须开发者显式单次批准，不进默认 CI；Flow 不绕过 CAPTCHA、登录、风控和访问控制。

### Phase H：真实视觉 QA 和完整三镜 E2E

1. 实现自适应抽帧、Person Tracking、View Classification、Identity Embedding 和受信 VLM Reviewer。
2. 视觉 evidence 只允许内部评审主体写入，普通用户不能伪装自动 PASS。
3. 用 Fixture Provider 完成 Script → 3 Shots → Jobs → Outputs → Frame extraction → QA → Human fallback → Commit → Timeline propagation 的真正 E2E。
4. 保留当前状态算法测试，不用新 E2E 替代它。

### Phase I：工作台与商业化收尾

1. 将景别/角度/运镜/灯光控件写入有版本的 ShotSpec/Revision，产生审计 diff；未接线前隐藏。
2. 补资产版本对比和 promotion 审计历史页。
3. 补邮箱验证、找回密码、MFA、邀请/成员管理、限流、安全事件和 Cookie/CSRF。
4. 设计套餐页、积分余额/明细/估价/失败退回/对账 UI。
5. 定义“工作台同步”的产品合同：哪些状态实时同步、数据库是否唯一事实源、多窗口竞争如何处理；不要未经决策就默认 WebSocket。

### Phase J：发布门禁

1. 全套静态检查/测试/迁移矩阵/安全回归全绿。
2. Compose 完整 build/up/health，生产使用外部 secret manager、受管 Postgres/对象存储、HTTPS 和最小权限。
3. 完成备份/恢复演练、Provider 成本上限、监控/告警/审计与回滚方案。
4. 审查 dirty tree，按功能分组提交，然后才更新版本和 tag。

## 10. 本地运行、密钥与验证 Runbook

### 10.1 正常安全默认

```env
PROVIDER_MODE=mock
ALLOW_LIVE_PROVIDER_CALLS=false
```

任何普通 live 调用必须同时满足：

```env
PROVIDER_MODE=live
ALLOW_LIVE_PROVIDER_CALLS=true
LIVE_PROVIDER_CONFIRMATION=I_UNDERSTAND_THIS_COSTS_MONEY
```

RunAPI 还需额外满足：

```env
ALLOW_RUNAPI_EDGE_CALLS=true
```

并且任务只能是 Edge/Temporary、Provider 策略允许、持久预算有余额。

在 P0 未修复之前，即使持有新 Key 也不得开启 live gate。

### 10.2 只记录环境变量名，不记录值

| Provider | 环境变量名 |
| --- | --- |
| OpenRouter | `OPENROUTER_API_KEY`, `OPENROUTER_BASE_URL` |
| Ark/Doubao/Seedance | `ARK_API_KEY`, `ARK_BASE_URL`, `DOUBAO_MODEL_ID`, `SEEDANCE_MODEL_ID` |
| Wan/Alibaba | `WAN_API_KEY`, `WAN_OPENAI_BASE_URL`, `WAN_DASHSCOPE_BASE_URL`, `WAN_CHAT_MODEL_ID`, `WAN2_7_T2V_MODEL_ID`, `WAN2_7_I2V_MODEL_ID`, `WAN2_7_R2V_MODEL_ID` |
| RunAPI | `RUNAPI_API_KEY`, `RUNAPI_BASE_URL`, `RUNAPI_MODEL_ID`, `RUNAPI_CHAT_PATH`, `RUNAPI_IMAGE_PATH`, `RUNAPI_VIDEO_PATH`, `RUNAPI_BUDGET_USD`, `ALLOW_RUNAPI_EDGE_CALLS` |
| DeepSeek | `DEEPSEEK_API_KEY`, `DEEPSEEK_BASE_URL`, `DEEPSEEK_MODEL_ID` |
| Google Flow | `FLOW_API_BASE`, `FLOW_API_KEY`, `FLOW_PROJECT_ID` 及浏览器 Worker 账号/项目绑定 |
| Voyage | `VOYAGE_API_KEY`, `VOYAGE_MULTIMODAL_MODEL` |

`scripts/configure_provider_secrets.py` 是当前本地交互式辅助工具。它不应回显 secret、不应提交 `.env`、不应自动打开 live gate。
工具中当前的 Wan OpenAI/DashScope base URL 是用户 workspace-specific 的非密钥默认值，不是已验证的全局官方默认值；应在实际环境明确覆盖并做 Provider schema/smoke 验证。

### 10.3 安全的开发检查顺序

不要直接对当前漂移的 `data/platform.db` 开始 migration 验证。优先使用临时数据库。

```bash
git status --short
git diff --stat
uv run ruff format --check . --exclude references
uv run ruff check . --exclude references
uv run mypy
node --check apps/web/app.js
uv run pytest -q
uv run alembic heads
```

SQLite fresh migration 必须在临时路径上运行，然后：

```bash
uv run alembic upgrade head
uv run alembic check
```

PostgreSQL 门禁必须使用实际 PostgreSQL + pgvector，不能用 SQLite 代替类型、标识符长度、constraint/index 和并发语义验证。

### 10.4 Docker 现状

Compose 定义 Web、API、Worker、PostgreSQL/pgvector、MinIO 和 bucket init。现在只证明在提供必需占位内部 secret 时 `docker compose config -q` 能通过。当前 Compose：

- 仍使用开发 Postgres/MinIO 凭据。
- 没有完整传入 Phase II Provider 环境变量。
- 没有在本快照执行 build/up/health。
- 不是生产发布证据。

## 11. 不可破坏的系统约束

### 11.1 创意与连续性

- 最新明确用户指令最高优先。
- 不得静默改已批准故事事实。
- 一镜一个主动作、明确开始/结束状态、明确 gaze target、一个主运镜。
- 角色不看镜头，除非已明确批准。
- 上镜结束状态与下镜开始状态对齐，除非有显式 reset/transition。
- 反打重锚不得盲目继承上一镜尾帧。

### 11.2 数据、资产与审计

- SQL `TimelineState` 永远是权威时间线；Vector/LLM Memory 只是检索辅助。
- Canonical Asset 只能通过显式 promotion 切换，历史版本不得改写。
- 不得混淆“相同字节”与“相同镜头/候选来源”。
- 人工复核、正式采纳、Legacy 转移、Provider media reconcile 都必须记录服务端派生 actor/reason。
- 不得让用户自报系统 source、自动 QA 身份或 Provider cost。

### 11.3 付费、并发与路由

- 不得在未知付费边界后盲目重试、取消或退款。
- 不得绕过 job/candidate/idempotency/trace 的原子性。
- 并发 claim 必须用 DB CAS/租约/fencing，不能 SELECT 后内存改状态。
- Passenger 和 Autopilot 必须共享 Gateway、Media、Asset、存储、租户和审计基础设施；当前 Passenger CostRecord 缺口应被修复，不应为它建第二套账本。
- Provider trust/AssetCriticality 是硬边界，低成本不能覆盖它。
- “配置存在”、“Mock 通过”、“Payload 编译成功”不等于“Provider 已部署”。

### 11.4 密钥与真实调用

- 不得把真实 Key 写入源码、测试、文档、提交、日志或截图。
- 不得为了“验证一下”绕过三重 live gate。
- 不得绕过 Google/Flow 的 CAPTCHA、登录、风控、授权与访问控制。
- 所有 live smoke 需单次主动批准、低成本样本、严格预算和完整审计。

## 12. 高冲突文件与协作边界

下列文件在 Phase II 被多个子系统同时修改，开发前必须先读完 diff，避免覆盖其他未提交工作：

- `apps/api/video_platform_api/container.py`
- `apps/api/video_platform_api/main.py`
- `apps/api/video_platform_api/runtime_routes.py`
- `packages/domain/production_domain/models.py`
- `packages/shared/platform_shared/config.py`
- `services/generation-gateway/generation_gateway/gateway.py`
- `services/generation-gateway/generation_gateway/providers.py`
- `services/production-engine/production_engine/runtime.py`
- `core/production/director_production/pipeline.py`
- `core/model-registry/model_registry_core/`
- `config/model-registry/defaults.json`
- `config/video-models/*.json`
- `apps/web/app.js`
- `migrations/versions/0021_*` 至 `0024_*`

建议单一负责人先修迁移/Admission/credits，其他 Provider 或 UI 工作在独立文件范围进行。新迁移号必须先协调，不要并发占用同一 revision。

## 13. 当前未决产品问题

这些问题会改变实现语义，不应由开发者静默假设：

1. Free 的 50 积分是一次性 grant、月度 grant 还是营销体验额？
2. 当默认 Seedance 8 秒需 87 积分时，是提高赠送、降低默认时长、更改换算或由平台补贴？
3. 是否未来支持 Provider 实际用量与 quote 的差额/部分结算？当前政策已固定为 pre-submit 全额 reserve、生成完成按原 quote settle、明确未提交才 refund、未知付费结果全额冻结待对账。
4. Free 出图的“豆包”对应哪一个官方部署 ID、图像能力和限额？当前没有 IMAGE role。
5. “工作台同步”指任务进度、资产版本、多窗口状态还是多人协作？哪些必须实时？
6. “限制使用视觉模型”的精确限额、角色和触发条件是什么？当前不能自行填入阈值。
7. B 端流量/R3 数据的导入 schema、数据所有权、隐私和保留期如何定义？已明确不建议抓取消费者平台。
8. Pro Director 最终是 Claude Opus 5 还是当前默认配置 GPT-5.6 Sol？当前只有配置，无产品执行证据。

## 14. 交接完成的最终验收清单

只有全部完成才能宣称 Phase II 可发布：

- [ ] 已暴露的所有 Provider Key 已撤销并轮换。
- [ ] 仓库、Git 历史、日志和构建产物无真实 secret。
- [ ] `0023`–`0024` 支持 fresh + historical + populated round trip，PG+pgvector 升级/check 通过（SQLite 已通过，PG 待验证）。
- [ ] 本地漂移库已备份并按审计方案恢复，不再依赖 `create_all` 弥补迁移。
- [x] 所有公开生成入口经过统一 Admission Gate。
- [x] Free/Paid 套餐和 workspace credits 服务端强制，Reserve/Settle/Refund/Reconcile 并发幂等。
- [x] Passenger 与 Autopilot 都进入统一成本/积分账本。
- [x] RunAPI task/cost 服务端派生，FactLock 校验 candidate 而不是只信 echo；`UNCERTAIN` 有内部幂等审计对账（产品 prompt 接线、自动账单验真待完成）。
- [ ] Seedance/Flow/Wan 的 enabled/configured/runtime ID/pricing/capability 一致。
- [ ] 三套 capability truth 已合并或有单一权威源。
- [ ] ModelRoleRuntime 已进入导演/摄影/提示词/Embedding 产品路径。
- [ ] Flow 首次 affinity 与受限迁移待完成；submit/poll 项目上下文已一致。
- [ ] 真实视觉 QA 或明确人工审核运营流程已交付。
- [x] Fixture Provider 的三镜 Candidate → Job/Output → 合成 QA → Commit E2E 通过（不是真实视觉 QA/live Provider）。
- [ ] UI 所有可见控件均接线或被隐藏。
- [ ] Auth 商业加固和项目级存储配额已完成。
- [ ] Compose 在新环境 build/up/health，密钥、DB、Storage、HTTPS 符合生产要求。
- [ ] Ruff format/lint、Mypy、Node syntax、pytest、migration matrix、security regression 全绿。
- [ ] 代码已分组审查并提交，版本/tag 与验收证据一致。

## 15. 附录：当前关键路径

| 领域 | 路径 |
| --- | --- |
| API/Container | `apps/api/video_platform_api/` |
| Web | `apps/web/` |
| Browser Worker | `apps/browser-worker-extension/`, `services/browser-runtime/` |
| Domain Models | `packages/domain/production_domain/models.py` |
| Contracts | `packages/contracts/platform_contracts/` |
| Provider SDK | `packages/provider-sdk/provider_sdk/` |
| Model Registry | `core/model-registry/`, `config/model-registry/` |
| Entitlements/Credits | `core/entitlements/` |
| Provider Budget | `core/provider-budget/` |
| Narrative | `core/narrative/` |
| Continuity | `core/continuity/` |
| Generation Policy | `core/generation-policy/` |
| Candidate/Commit | `core/production/director_production/` |
| QA/Evaluation | `core/qa/`, `core/evaluation/` |
| Generation Gateway | `services/generation-gateway/` |
| Media/Assets | `services/media-service/`, `core/asset-registry/` |
| Visual Runtime | `services/production-engine/` |
| Providers | `providers/` |
| Skills | `skills/` |
| Migrations | `migrations/versions/` |
| Tests | `tests/` |

## 16. 本快照不声称的事

- 不声称 Phase II 已完成或 production-ready。
- 不声称 Free 用户已真正使用豆包导演/出图或 Seedance 免费视频。
- 不声称 50 starter credits 足够购买当前默认约 87 积分的 8 秒 Seedance 任务；钱包生成生命周期已可执行，但购买、周期 grant、expiry 和管理员调账未实现。
- 不声称 Veo/Grok/Omni/Kling direct/Runway 已部署。
- 不声称 OpenRouter、RunAPI、Flow、Ark/Seedance、Wan 已通过真实 Provider smoke。
- 不声称 Dynamic Identity QA 已拥有真实视觉模型。
- 不声称三镜测试调用过真实 Provider 或完成过真实视觉 QA；它只生成并处理本地 Fixture MP4。
- 不声称所有 GitHub Skill 被直接拉取进仓库；当前是研究公开方法/许可证后编写本地 Skill，没有 vendored 上游 Skill body/runtime dependency。
- 不声称 Prompt Compiler 由 Qwen/Claude/OpenRouter 在线生成；现有 compiler 是项目自写的确定性逻辑。
- 不声称 Hook/R3 学习、平台数据导入、岭回归权重更新或动态样本量已实现。

本文档应与当前代码快照一起使用。任何后续变更都应同步更新“验证证据”、“阻断”和“不声称的事”，避免文档再次变成设计愿望清单。

## 17. 附录：交付时 dirty-tree manifest

交付文档写入后的快照是 75 个路径（47 个已跟踪修改、28 个 untracked）。下列目录项表示其内可能有多个文件。

```text
 M .env.example
 M CURRENT_ARCHITECTURE.md
 M README.md
 M apps/api/video_platform_api/auth.py
 M apps/api/video_platform_api/container.py
 M apps/api/video_platform_api/main.py
 M apps/api/video_platform_api/runtime_routes.py
 M apps/web/app.js
 M config/video-models/google-flow.json
 M config/video-models/grok.json
 M config/video-models/kling.json
 M config/video-models/seedance.json
 M config/video-models/veo.json
 M config/video-models/wan.json
 M core/asset-registry/asset_registry_core/service.py
 M core/continuity/continuity_core/engine.py
 M core/generation-policy/generation_policy_core/__init__.py
 M core/generation-policy/generation_policy_core/engine.py
 M core/memory/memory_core/embedding.py
 M core/model-registry/model_registry_core/__init__.py
 M core/model-registry/model_registry_core/router.py
 M core/model-registry/model_registry_core/schemas.py
 M core/narrative/narrative_core/__init__.py
 M core/narrative/narrative_core/compiler.py
 M core/production/director_production/orchestrator.py
 M core/production/director_production/pipeline.py
 M core/qa/qa_core/__init__.py
 M core/qa/qa_core/pipeline.py
 M docs/VISUAL_RUNTIME_IMPLEMENTATION.md
 M packages/contracts/platform_contracts/generation.py
 M packages/contracts/platform_contracts/shot.py
 M packages/domain/production_domain/models.py
 M packages/provider-sdk/provider_sdk/__init__.py
 M packages/shared/platform_shared/config.py
 M providers/google-flow/google_flow_provider/adapter.py
 M providers/seedance/seedance_provider/__init__.py
 M pyproject.toml
 M services/generation-gateway/generation_gateway/__init__.py
 M services/generation-gateway/generation_gateway/gateway.py
 M services/generation-gateway/generation_gateway/providers.py
 M services/production-engine/production_engine/runtime.py
 M tests/test_asset_registry.py
 M tests/test_auth_tenancy.py
 M tests/test_flow_adapter.py
 M tests/test_model_router.py
 M tests/test_policy_qa_cost.py
 M tests/test_provider_gateway.py
?? config/model-registry/
?? core/entitlements/
?? core/model-registry/model_registry_core/infrastructure.py
?? core/narrative/narrative_core/timeline.py
?? core/provider-budget/
?? docs/DEVELOPMENT_HANDOFF_2026-08-20.md
?? docs/PRODUCT_REQUIREMENTS_LEDGER.md
?? docs/README.md
?? migrations/versions/0021_unified_model_registry.py
?? migrations/versions/0022_free_plan_provider_budget.py
?? migrations/versions/0023_workspace_credit_wallet.py
?? packages/provider-sdk/provider_sdk/budget.py
?? packages/provider-sdk/provider_sdk/capabilities.py
?? packages/provider-sdk/provider_sdk/edge.py
?? packages/provider-sdk/provider_sdk/http.py
?? packages/provider-sdk/provider_sdk/transport.py
?? packages/provider-sdk/provider_sdk/trust.py
?? providers/deepseek/
?? providers/openrouter/
?? providers/runapi/
?? providers/seedance/seedance_provider/adapter.py
?? providers/wan/
?? scripts/
?? services/generation-gateway/generation_gateway/direct.py
?? tests/test_director_algorithm_core.py
?? tests/test_model_infrastructure.py
?? tests/test_phase2_providers.py
?? tests/test_plan_provider_budget.py
```

这个 manifest 是交接证据，不是建议一次性提交的分组。
