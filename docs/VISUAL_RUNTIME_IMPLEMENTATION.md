# Visual Runtime Implementation Record

更新时间：2026-08-20

本文记录 Passenger Seat / Autopilot Visual Runtime 升级的实际落地范围。它是实施记录，不覆盖 [CURRENT_ARCHITECTURE.md](../CURRENT_ARCHITECTURE.md)：后者是升级前的审计快照，应继续用于对照迁移前基线。

## 总体结论与不变量

- Passenger Seat 与 Autopilot 已接入同一个 `VisualProductionRuntime`，并继续共用 `GenerationGateway`、provider registry、账号调度、媒体存储、成本记录与付费幂等保护。
- 用户可见的图片提示词整理与内部视频镜头编译已经分离。Autopilot 不调用 Image Prompt Corrector，视频 Adapter 不复用一条通用字符串冒充模型专用提示词。
- 逻辑资产 `Asset` 与不可变 `AssetVersion` 建在原 `MediaAsset` 之上；生成结果不会自动覆盖 canonical，必须显式 promote，并记录 promotion 历史。
- 记忆、自动评估、自适应路由、Wan 实验候选和自动重试均由默认关闭的 feature flag 控制。
- 当前 credits 是可解释的报价估算，不是用户钱包；当前模型 Adapter 是 payload 编译器，不代表每个真实 provider transport 已完成。

运行主链：

```text
Passenger Seat ─┐
                ├─ VisualProductionRuntime ── GenerationGateway ── Provider
Autopilot ──────┘          │       │                    │
                           │       ├─ Asset/Memory      ├─ durable job/event
                           │       ├─ Router/Adapter    └─ paid-request safety
                           │       └─ Trace/Cost
                           └─ Evaluation ── bounded Retry/Switch
```

## 2026-08-22 增量 — Project Style Lock 与全片画风漂移门禁

- 迁移 `0029_project_style_lock` 新增项目级精确版本指针以及不可变
  `StyleEmbedding / ProjectStyleLock / CandidateStyleEvaluation`。
- STYLE 仍通过通用 Asset Registry 管理多版本和显式 Canonical promotion；真实用户确认后，项目只能
  一次性锁定当前 Canonical READY 版本。素材库后续换 Canonical 不会改变已锁项目。
- `ProjectStyleService` 从版本媒体自动提取 64-D 归一化本地 descriptor，持久化
  algorithm/model/dimension/vector hash、源媒体 ID/hash 与 `DETERMINISTIC_LOCAL` provenance。
- `VisualProductionRuntime` 优先装配锁定画风参考媒体；`CanonicalShotSpec`、中性 Prompt、模型 Prompt、
  Generation Job metadata 和 Adapter payload 共用同一 lock/version/embedding provenance。
- 图片候选直接评估；视频候选用 FFmpeg 在 `0/0.2/0.4/0.6/0.8/0.98` 抽帧。结果记录
  average/minimum/p10 similarity、drift slope、low-score fraction 与冻结阈值。
- QAPipeline 将风格 FAIL 作为硬失败、缺证据作为 review；Candidate Commit 在非规范副作用发生前和
  CAS 采用事务内再次核对 PASS evaluation 与 candidate output/style lock/version/embedding。
- `GET/POST /api/projects/{project_id}/style-lock` 和 Web“锁定为整部作品画风”确认已接通；开发 bypass
  不能伪造真实用户锁定来源。

当前 descriptor 是无需网络的确定性颜色、明度、饱和度、边缘与粗空间统计，不是已校准学习型
encoder。Adapter `style_control` 是平台内部适配合同；真实 Provider 是否支持原生 embedding 字段仍须
按官方合同逐个映射并执行 canary。当前实际外部控制是 Prompt 与锁定参考媒体；`0029` 也尚无
PostgreSQL/Compose populated upgrade 证据。

## Feature flag 清单

| Flag | Env default | 默认 | 影响范围 | 安全降级 |
| --- | --- | --- | --- | --- |
| `voyage_memory` | `FEATURE_VOYAGE_MEMORY` | off | Autopilot 自动检索；生成结果 promote 后可写记忆 | 无 key 时使用本地确定性 embedding；不宣称多模态质量 |
| `auto_evaluation` | `FEATURE_AUTO_EVALUATION` | off | 候选生成完成后的结构化评估 | 证据不足不伪装为通过 |
| `auto_retry` | `FEATURE_AUTO_RETRY` | off | 根据评估计划自动修复/重试/换模型 | `MAX_AUTO_RETRIES` 默认 2；保留 Gateway 未知付费状态保护 |
| `adaptive_router` | `FEATURE_ADAPTIVE_ROUTER` | off | 把生产指标和 benchmark 调整写入模型评分 | 关闭时只用版本化静态配置与先验 |
| `wan3` | `FEATURE_WAN3` | off | 允许 Wan 实验配置进入候选集合 | 没有注册 provider transport 时仍会被排除 |

Flag 支持环境默认、数据库全局 override 与项目 override，项目级优先。运行时 API 为 `GET /internal/feature-flags` 与 `PUT /internal/feature-flags/{name}`。

---

## Phase 1 — 现状审计与增量边界

### Changes

- 固化升级前模块边界、直接生成路径、导演镜头路径、可复用数据库表及缺口。
- 明确不新建第二套 Gateway、媒体存储或生产引擎；后续阶段只能增量接入。
- 明确原 `MediaAsset`、`CharacterIdentityVersion`、`TimelineState`、候选、QA、成本与事件表继续保留。

### Files Changed

- `CURRENT_ARCHITECTURE.md`：升级前审计快照（本次文档收尾未改写）。
- `docs/source-audit.md`：来源与边界审计。
- `docs/skill-research.md`：摄影、运镜、构图、灯光、商业图、人物一致性和提示词 Skill 调研记录。

### Migration Notes

- 本阶段没有数据库迁移。
- `CURRENT_ARCHITECTURE.md` 中的“Required change”描述的是当时缺口，不应被解释为当前未实现清单；当前状态以本文和代码为准。

### API Changes

- 无。

### Tests

- 审计建立在升级前自动化基线之上；后续阶段保留原测试并追加专项回归。

### Risks / Next

- 审计快照会随实现推进而显得过时，因此只作为历史证据，不作为运行手册。
- 后续架构变更必须持续验证“单 Gateway、单媒体注册表、canonical 显式提升”三项不变量。

---

## Phase 2 — 模型能力注册表、路由与视频 Adapter

### Changes

- 新增 JSON 配置的 `ModelCapabilityRegistry`，记录 provider/model/version、能力、成本、延迟、adapter、静态质量先验与已知失败先验。
- 新增持久化 `ModelDefinition`、`ModelRoleBinding` 与 `ModelInfrastructureService`：业务角色只绑定逻辑模型；`ALL/FREE/PRO/ENTERPRISE` 等套餐作用域可配置，套餐专属绑定存在时不会静默继承通用付费模型。
- 新增无数据库依赖的 `ProviderTrustLevel`、`AssetCriticality` 与硬兼容性检查；`EDGE` provider 无论成本分多低都不能进入 canonical、hero 或 important 任务。
- 新增确定性 `VideoModelRouter`：先做硬能力过滤，再输出排序、总分、能力得分、成本/延迟/失败扣分、缺失能力与可解释原因。
- 新增 typed `VideoModelAdapter` 接口和 registry；实现 Kling、Veo、Seedance、Grok、Wan 的模型专用 prompt/payload 映射。
- Adapter 映射参考图、起始帧、结束帧、时长、清晰度、音频等字段；Grok 只在镜头未批准直视摄影机时加强结尾视线约束，显式批准的正面直视镜头不会被适配器擅自改写或被评估器惩罚。
- 旧 provider capability/resolver 路径保留为兼容层，实际 provider 执行仍只经过 GenerationGateway。

### Files Changed

- `core/model-registry/model_registry_core/{schemas.py,registry.py,router.py,infrastructure.py,__init__.py}`
- `packages/provider-sdk/provider_sdk/trust.py`
- `config/model-registry/defaults.json`
- `config/video-models/{google-flow,grok,kling,seedance,veo,wan}.json`
- `core/adapters/video_adapter_core/{base.py,adapters.py,registry.py,__init__.py}`
- `skills/model-prompting/SKILL.md`
- `tests/test_model_router.py`
- `tests/test_video_adapters.py`

### Migration Notes

- 视频质量/失败先验继续使用版本控制 JSON；业务角色、provider model ID、信任等级与套餐绑定由版本化默认配置首次写入数据库，之后不会在启动时覆盖管理员修改。
- `0021_unified_model_registry` 新增模型定义与套餐可选角色绑定；默认全部 `live_enabled=false`，配置 API key 本身不会打开真实付费调用。
- 生产指标与 benchmark 的持久化由 Phase 8 的 `0005_visual_runtime` 提供。
- 自定义部署如移动 `MODEL_CONFIG_ROOT`，必须保证全部 JSON profile 一起发布。

### API Changes

- `POST /internal/router/video`：返回版本化模型候选、分数、扣分、缺失能力和选择理由。
- 无 provider transport API 直出；Adapter 只在运行时内部生成请求 payload。

### Tests

- `tests/test_model_router.py`：能力/信任等级硬过滤、确定性排序、已知失败扣分、Grok 后视/正视镜头风险、自适应样本门槛。
- `tests/test_model_infrastructure.py`：默认配置、幂等持久化、管理员覆盖保护、套餐作用域、FREE 未配置时 fail-closed，以及 canonical/hero 禁止 EDGE。
- `tests/test_video_adapters.py`：五类 Adapter 注册、模型专用 prompt、不共享通用 payload、参考/首尾帧/时长/清晰度/音频字段、Grok gaze 约束。

### Risks / Next

- JSON 中的成本和成功率是版本化先验，不是实时供应商价格；应定期审计并通过 metrics/benchmark 校准。
- Adapter 编译通过不等于远程 transport 已接通。Kling、Veo、Seedance、Grok、Wan 上线前仍需各自凭据、配额、错误映射和真实媒体回传验收。
- provider 与 model 必须原子选择；任何兼容 fallback 都不能只换 provider 而保留不兼容的 model id。

---

## Phase 3 — 图片提示词整理与 Passenger Seat

### Changes

- 将用户可见的图片提示词整理提取为独立 `ImagePromptCorrector`，进行任务类型识别、事实/人物/商品/文字约束保持、结构化修改说明和 undo 数据输出。
- 新增图片提示词知识库 Skill；图片整理只在 Passenger Seat 图片路径由用户主动触发。
- Autopilot 的提示词区域改为“高级设置/原始镜头要求”，不再调用图片提示词整理；内部编译结果默认收起。
- Web Passenger Seat 提供图片/视频切换、模型选择、时长/清晰度、参考素材、成本预估、整理/撤销/生成/结果与确认入口。
- Passenger 提交沿用 `VisualProductionRuntime.submit_passenger()` 与共享 Gateway，不绕过幂等、媒体与 trace。

### Files Changed

- `core/image-prompt/image_prompt_core/{schemas.py,corrector.py,__init__.py}`
- `skills/image-prompt-corrector/`
- `core/skills/skill_core/{compiler.py,__init__.py}`（兼容 facade 与记录边界）
- `apps/web/{index.html,styles.css,app.js}`
- `apps/api/video_platform_api/runtime_routes.py`
- `packages/contracts/platform_contracts/shot.py`
- `migrations/versions/0003_prompt_revisions.py`
- `tests/test_image_prompt_corrector.py`
- `tests/test_visual_runtime.py`

### Migration Notes

- `0003_prompt_revisions` 新增追加式 `prompt_revisions`，记录 original/corrected、detected type、preserved constraints、editable variables、changes、corrector version。
- 迁移不修改旧 `prompt_compilations`；旧 `/v1/prompts/refine` 兼容路径继续存在，但底层调用图片 Corrector。
- 升级命令：`uv run alembic upgrade head`。

### API Changes

- `POST /api/prompt/correct`：图片提示词整理并持久化 revision。
- `POST /api/passenger/generate`：用户指定 `media_type/provider/model/prompt`，视频可带 duration、首尾帧与 references；响应包含估算成本、积分和 pricing version。
- `POST /api/pricing/estimate`：生成前显示成本与积分拆分。
- `/v1/generations`、`/v1/images/generations`、`/v1/videos/generations` 保留兼容。

### Tests

- `tests/test_image_prompt_corrector.py`：类型识别、事实与精确文字保持、修改差异、undo、revision 持久化。
- `tests/test_visual_runtime.py`：Passenger 指定模型不被自动路由覆盖，共用 Gateway，生成前写 trace。
- Web 语法检查使用 `node --check apps/web/app.js`；响应式与真实点击流程仍应在浏览器做最终验收。

### Risks / Next

- Corrector 当前以可解释确定性规则和 Skill 知识为主；未来接 LLM 时仍必须先锁定不变量，并保留原文/差异/撤销。
- Web 的“确认结果”已调用 `/api/generations/{job_id}/promote`，支持新建/选择逻辑资产和显式 canonical；通用“素材版本”卡片另行支持人物、场景、产品、道具和服装的用户上传 v1/v2。完整版本差异与 promotion 审计展示仍待完善。
- 图片模型暂用透明的临时单次成本基准；接入 image model registry/live price 后再替换，不能静默改变报价。

---

## Phase 4 — 统一资产注册表与 canonical 版本

### Changes

- 在内容寻址 `MediaAsset` 之上新增逻辑 `Asset` 与不可变 `AssetVersion`，版本可引用主媒体和有角色标记的多个参考媒体。
- 支持人物、场景、商品、道具、服装、车辆、生物、声音、风格、参考图等逻辑类型；类型值由服务统一校验。
- canonical 只通过显式 `promote()` 改变；每次切换记录 from/to、操作者、原因和 metadata。
- 已锁定的 `CharacterIdentityVersion` 不被新系统覆盖；Autopilot 组合 canonical 资产和既有人物身份参考。
- 完成的 Passenger 结果可作为新逻辑资产或现有资产的新版本；是否 canonical 由请求明确指定。

### Files Changed

- `core/asset-registry/asset_registry_core/{service.py,__init__.py}`
- `packages/domain/production_domain/models.py`
- `apps/api/video_platform_api/runtime_routes.py`
- `migrations/versions/0004_unified_asset_registry.py`
- `tests/test_asset_registry.py`
- `tests/test_visual_runtime.py`

### Migration Notes

- `0004_unified_asset_registry` 追加 `assets`、`asset_versions`、`asset_version_media`、`asset_canonical_promotions`。
- `0008_asset_registry_invariants` 预检旧数据并安装 SQLite/PostgreSQL 约束触发器：canonical、父版本和 promotion 必须属于同一资产，canonical 切换必须先写当次 promotion，版本/版本媒体/promotion 历史为数据库级只追加。
- 所有新表链接原 `projects`、`users` 与 `media_assets`；没有复制或删除已有媒体。
- `AssetVersion` 的 `(asset_id, version)` 唯一；媒体外键采用 `RESTRICT`，避免被误删。
- 旧 `Location/Prop/CharacterIdentityVersion` 仍有效；没有自动回填 canonical。迁移后应由用户审核并显式提升需要的版本。

### API Changes

- `POST /api/assets`
- `GET /api/projects/{project_id}/assets`
- `GET /api/assets/{asset_id}`
- `POST /api/assets/{asset_id}/versions`
- `POST /api/assets/{asset_id}/versions/{version_id}/promote`
- `POST /api/generations/{job_id}/promote`

### Tests

- `tests/test_asset_registry.py`：版本递增、版本不可变语义、无效媒体、不可 promote 状态、canonical 显式切换与 promotion 审计。
- 同一套测试另覆盖直接 SQL 绕过服务层时的跨资产 canonical/父版本/promotion 拒绝、未记录 canonical 切换拒绝、历史更新/删除拒绝及旧库迁移。
- `tests/test_visual_runtime.py`：生成结果转资产版本及 canonical 使用链。

### Risks / Next

- 业务上“删除版本”需要单独的保留/归档策略；当前关系优先保护来源链，不应直接级联删除媒体。
- Web 仍需完整呈现逻辑资产、版本差异和 canonical promotion 历史。
- 对旧项目做批量回填前，应先设计人工审批与可回滚映射，不能按最新媒体自动猜 canonical。

---

## Phase 5 — 多层记忆、Voyage 与上下文预算

### Changes

- 新增 L0/L1/L2 记忆层：当前状态、episodic shot/asset memory、canonical long-term memory。
- 新增 `EmbeddingProvider` 接口、`VoyageMultimodalEmbeddingProvider` 和 `LocalTestEmbeddingProvider`。
- 检索先按 project/entity/scene/layer/type/canonical metadata 过滤，再计算相似度，并结合 canonical、时间邻近与新近度重排。
- `ContextAssembler` 以明确字符、token、图片和视频预算组合 canonical assets、时间状态、镜头要求、历史记忆、世界规则和上一镜尾帧。
- Autopilot 只有在 `voyage_memory` flag 打开时自动检索；显式内部 memory API 保持可调用，便于运维与测试。
- Voyage 不作为人物身份一致性的唯一判定器。

### Files Changed

- `core/memory/memory_core/{schemas.py,embedding.py,engine.py,context.py,__init__.py}`
- `core/runtime-control/runtime_control_core/{feature_flags.py,__init__.py}`
- `packages/shared/platform_shared/config.py`
- `.env.example`
- `services/production-engine/production_engine/runtime.py`
- `apps/api/video_platform_api/{container.py,runtime_routes.py}`
- `migrations/versions/0005_visual_runtime_memory_evaluation.py`
- `tests/test_memory_context.py`
- `tests/test_visual_runtime.py`

### Migration Notes

- `0005_visual_runtime` 新增 `shot_memories` 与 `feature_flags`（同时包含 Phase 7/8 的 additive tables）。
- `shot_memories` 持久化 embedding provider/model/dimension，便于未来重建和版本隔离。
- `MEMORY_EMBEDDING_DIMENSION` 默认 512，允许 256/512/1024/2048。更改维度时应重建对应记录，不应混合比较不同维度。
- feature flag 默认来自环境，数据库 override 不要求预先 seed。

### API Changes

- `POST /internal/memory/index`
- `POST /internal/memory/search`
- `GET /internal/memory/characters/{entity_id}`
- `GET /internal/memory/scenes/{scene_id}`
- `GET /internal/memory/state`
- `GET /internal/feature-flags`
- `PUT /internal/feature-flags/{name}`

### Tests

- `tests/test_memory_context.py`：index/search、metadata-first 过滤、canonical 与 recency 排序、L0 当前状态、上下文字符/图片/视频预算、本地 embedding 稳定性。
- `tests/test_visual_runtime.py`：flag 关闭时不自动检索，打开时把 memory ids 写入 trace/context。

### Risks / Next

- 没有 `VOYAGE_API_KEY` 时使用的是 deterministic test fallback，不是生产级多模态语义检索；上线验收必须覆盖真实 Voyage 请求、限流、超时、错误和数据驻留。
- 远程图片/视频 URL 必须可被 embedding provider 安全访问；私有对象需短期签名 URL 或受控上传机制。
- 当前向量保存在 JSON 以兼容 SQLite 测试；大规模生产需评估 pgvector 索引、分区、回填吞吐与删除合规。

---

## Phase 6 — CanonicalShotSpec、视频编译与共享运行时

### Changes

- 新增 typed `CanonicalShotSpec`，要求每个主体有明确 gaze/eyeline target，并要求每镜一个 dominant camera movement。
- `VideoShotPromptCompiler` 只生成模型中立镜头规格与审计记录；模型措辞、reference/start/end/duration/resolution/audio 映射留在 Adapter。
- `VisualProductionRuntime.prepare_autopilot()` 读取镜头/时间状态、canonical 资产、可选记忆，装配 bounded context，路由模型并由所选 Adapter 编译请求。
- `CandidatePipeline` 优先使用共享 runtime；保留旧编译/能力解析逻辑作为兼容 fallback，避免一次性破坏已有调用方。
- Passenger 明确选择的 provider/model 不进入自动路由，但继续经过共享 Gateway、成本与 trace。

### Files Changed

- `packages/contracts/platform_contracts/shot.py`
- `packages/contracts/platform_contracts/__init__.py`
- `core/video-prompt/video_prompt_core/{compiler.py,__init__.py}`
- `core/adapters/video_adapter_core/`
- `services/production-engine/production_engine/{runtime.py,__init__.py}`
- `core/production/director_production/pipeline.py`
- `apps/api/video_platform_api/{container.py,runtime_routes.py}`
- `tests/test_video_adapters.py`
- `tests/test_visual_runtime.py`
- `tests/test_director_api.py`

### Migration Notes

- CanonicalShotSpec 是运行时 contract，不替换 `shots` 表；编译后的规格随 GenerationRequest metadata/ProductionTrace 记录。
- `production_traces` 由 `0005_visual_runtime` 提供；旧 `PromptCompilation` 与 Candidate 记录继续保留。
- 兼容 fallback 是迁移保护，不应演化成第二条长期独立路径；待真实数据验证后再逐步收缩。

### API Changes

- `POST /api/passenger/generate` 进入共享 runtime。
- `POST /v1/shots/{shot_id}/generate` 的外部路径保持兼容，内部优先使用 canonical spec/router/adapter。
- `POST /internal/router/video` 可在生成前独立解释候选选择。
- `GET /internal/shots/{shot_id}/traces` 提供 prompt version、context assets、memory ids、router scores 与成本链。

### Tests

- `tests/test_video_adapters.py`：canonical-like 输入到五类模型 payload 的字段保持与模型差异。
- `tests/test_visual_runtime.py`：Passenger/Autopilot 共用 Gateway、手选模型保持、Autopilot 路由/上下文/Adapter/trace、canonical 参考注入。
- `tests/test_director_api.py`：原导演生成 API 与新 runtime 的兼容集成。

### Risks / Next

- 旧兼容编译器仍存在，任何新功能应只加到 canonical pipeline，避免两条路径再次漂移。
- 单一 dominant action、主体站位、gaze 和相邻镜头状态仍需要生成结果评估；合同正确不能保证模型完全服从。
- 真实 provider transport 回传的 duration/resolution/audio 能力应与配置 profile 做合约测试，不能只依赖静态声明。

---

## Phase 7 — 结构化评估与有界重试

### Changes

- 新增 `GenerationEvaluator`，比较 expectation 与 evidence，输出维度分、checks、critical failure、证据完整性、decision、retry reasons 与 retry patch。
- 决策枚举为 `ACCEPT`、`RETRY_SAME_MODEL`、`RETRY_REWRITE_PROMPT`、`SWITCH_MODEL`、`REJECT`。
- gaze、服装、道具、screen direction 等关键不一致可触发硬门槛；证据不足不会被高分均值掩盖。
- `RetryEngine` 把评估结果转为补参考、改提示词、同模型重试或换模型计划；次数达到上限即拒绝/转人工。
- 自动评估与自动重试分别由 `auto_evaluation`、`auto_retry` 控制；重试任务使用新幂等键，但不绕过 Gateway 对未知付费状态的保护。

### Files Changed

- `core/evaluation/evaluation_core/{schemas.py,evaluator.py,retry.py,__init__.py}`
- `services/production-engine/production_engine/runtime.py`
- `core/production/director_production/pipeline.py`
- `apps/api/video_platform_api/{container.py,runtime_routes.py}`
- `packages/domain/production_domain/models.py`
- `migrations/versions/0005_visual_runtime_memory_evaluation.py`
- `tests/test_evaluation_retry.py`
- `tests/test_visual_runtime.py`

### Migration Notes

- `0005_visual_runtime` 追加 `evaluation_results`，关联 project/shot/generation job/generated media，并记录 evaluator/judge/model/attempt 版本信息。
- Gateway 原来的基础设施重试和 unknown-paid-request 安全语义不变；Generation Retry 是更高层的创意修复决策，两者不能混为同一个 retry counter。

### API Changes

- `POST /internal/evaluate/video`
- `POST /internal/generations/{job_id}/evaluate`
- `POST /internal/retry/plan`

### Tests

- `tests/test_evaluation_retry.py`：完整证据接受、关键失败改写、证据缺失拒绝/修复、同模型/补参考/换模型计划、最大重试次数。
- `tests/test_visual_runtime.py`：对已生成 job 评估、flag 控制、自动 retry 新任务与 trace 更新。

### Risks / Next

- 未配置专用视觉 judge 时，评估只能使用调用方提供的结构化 evidence 或安全降级；不能宣称自动看懂成片。
- 自动换模型可能改变运动风格或人物表现，仍需 canonical references、prompt patch 与人工抽检。
- 上线前应为每个 provider 定义成本上限、429/5xx/审核拒绝策略和“转人工”告警，不要只依赖次数上限。

---

## Phase 8 — 指标、Benchmark、Trace 与积分估算

### Changes

- 新增 append-only `ModelMetricsService`，记录 generation success、user accept/regenerate、auto retry、身份/场景/道具/对白/运镜/gaze/物理失败、延迟和成本。
- 新增版本化 benchmark manifest/result 存储；结果可转换为路由调整，但只有 `adaptive_router` 打开时参与评分。
- 新增 `ProductionTrace`，关联 mode、project/shot/job、provider/model、prompt version、context assets、retrieved memories、router scores、成本、evaluation 与 retry。
- 新增 `CreditPricingEngine`：视频使用 model profile 的每秒成本，乘以时长、分辨率、参考图与服务倍率；图片在 image registry 接入前使用公开临时基准。默认 `$0.01/credit`、服务倍率 `1.20`，版本 `credit-pricing-v1`。
- 原 `CostEngine` 继续记录实际/估算/重试/浪费成本和 adopted shot 成本；credits estimate 不替代 CostRecord。

### Files Changed

- `core/model-metrics/model_metrics_core/{service.py,benchmark.py,__init__.py}`
- `core/cost/cost_core/{service.py,__init__.py}`
- `core/model-registry/model_registry_core/router.py`
- `services/production-engine/production_engine/runtime.py`
- `apps/api/video_platform_api/{container.py,runtime_routes.py}`
- `packages/domain/production_domain/models.py`
- `migrations/versions/0005_visual_runtime_memory_evaluation.py`
- `tests/test_metrics_benchmark.py`
- `tests/test_visual_runtime.py`

### Migration Notes

- `0005_visual_runtime` 追加 `model_metrics`、`model_benchmark_results`、`production_traces`。
- Metrics 是 append-only evidence；不要就地覆盖历史样本。Benchmark 记录带 suite/model/version/case key，避免新旧结果混算。
- Credits 没有新增 wallet 表。现有 `CostRecord.credits` 是任务成本记录字段，不构成复式账本、余额或退款凭证。

### API Changes

- `POST /api/pricing/estimate`
- `POST /internal/models/metrics`
- `GET /internal/benchmarks`
- `POST /internal/benchmarks/results`
- `GET /internal/shots/{shot_id}/traces`
- `POST /api/passenger/generate` 响应增加 `estimated_cost`、`estimated_credits`、`credit_pricing_version`。

### Tests

- `tests/test_metrics_benchmark.py`：允许的 metric、重复事件幂等入口、样本聚合、失败维度调整、benchmark manifest/result/adjustment。
- `tests/test_visual_runtime.py`：trace 字段、Passenger 成本估算、feature flag 与路由调整集成。
- 原成本测试继续覆盖任务成本、重试成本、浪费成本与已采用镜头成本。

### Risks / Next

- 必须另建真正的钱包域：余额、充值、赠送、预占、结算、退款、过期、对账、管理员调整、幂等账务事件和审计；在此之前 UI 只能称“预计积分”。
- 图片 `$0.04` 临时 provider 成本和视频 JSON 先验都需要实时价目版本、汇率/税费策略及供应商账单对账。
- Adaptive router 应设置最小样本、置信区间、时间衰减和异常值保护；当前 flag 默认关闭是正确上线姿势。
- Benchmark 需要真实生成媒体和人工/视觉 QC 才有意义；仅记录空清单或合成分数不能作为模型采购结论。

---

## 商业化加固 — 账号、租户边界与数据完整性

### Changes

- 新增邮箱注册、登录、当前账号、退出/会话撤销；密码使用 PBKDF2-SHA256 60 万轮加随机 salt，数据库仅保存会话 token 的 SHA-256 哈希。
- 新增 `OWNER / ADMIN / EDITOR / VIEWER` 工作空间成员关系；所有普通项目、镜头、人物、媒体、逻辑资产、提示词修订和生成任务路由均校验项目归属与角色权限。
- 用户路由与 `/internal/*`、供应商账号和 worker 路由拆分；内部 HTTP/Worker WebSocket 在 `PLATFORM_API_KEY` 为空时安全拒绝。
- 媒体 ID、人物 ID、shot lineage、prompt reference 与相同 storage key 均经跨项目阻断；本地媒体预览改为携会话取 Blob URL，不再依赖公开存储 URL。
- Workbench 增加注册/登录/退出、通俗用语、积分估算说明、人物主参考重新上传、通用资产版本管理和单镜头重做入口。

### Files Changed

- `apps/api/video_platform_api/{auth.py,main.py,runtime_routes.py}`
- `packages/domain/production_domain/models.py`
- `packages/shared/platform_shared/config.py`
- `migrations/versions/{0006_runtime_data_integrity.py,0007_commercial_auth.py,0008_asset_registry_invariants.py}`
- `apps/web/{index.html,styles.css,app.js}`
- `tests/{test_auth_tenancy.py,test_runtime_data_integrity.py,test_asset_registry.py,test_migration_history.py}`

### Migration Notes

- `0006_runtime_data_integrity` 对 feature-flag scope、model metric 幂等键与 trace 唯一索引做兼容清理与约束。
- `0007_commercial_auth` 新增 users 密码哈希字段、workspace memberships 和过期/可撤销哈希会话；只有没有其他真实用户时，首个注册账号才能在加锁后安全认领 legacy local 工作空间。
- `0008_asset_registry_invariants` 在数据库层保护资产链路与只追加历史；无 Asset Registry 表的合法恢复快照可安全跳过，只存在部分资产表的损坏 schema 会明确中止。

### API Changes

- `POST /api/auth/register`、`POST /api/auth/login`、`GET /api/auth/me`、`POST /api/auth/logout`
- 普通 `/api/*` 与 `/v1/*` 资源路由使用用户 Bearer 会话；内部/管理路由仅接受独立 `PLATFORM_API_KEY`。
- `/v1/storage/{storage_key}` 需登录，并要求该 storage key 至少有一个 MediaAsset 属于当前用户可访问的项目。

### Tests

- 完整套件 104 项，覆盖注册/重复邮箱、登录/退出/会话撤销、过期、角色读写、跨租户项目/媒体/存储/人物/提示词参考/镜头注入拒绝以及内部路由 fail-closed。
- 全新 SQLite `alembic upgrade head` 与 `alembic check` 纳入迁移回归；通用 Asset Registry 触发器已在 SQLite 执行，PostgreSQL DDL 因本机无可用 PostgreSQL/Docker daemon 尚未做真实 PG 执行。
- 真实浏览器在 1440px、1024px、390px 通过注册、登录/退出、双模式、中文描述事实保持/恢复、人物 v1→v2、场景 v1→v2 和显式 canonical 切换，无横向溢出或控制台错误。

### Risks / Next

- 邮箱验证、找回密码、MFA、成员邀请/移除 UI、登录限流/异常风控、安全审计事件和设备会话管理仍需在公网商用前完成。
- Web 会话目前放在 `sessionStorage`；上线应切换为 Secure + HttpOnly + SameSite Cookie，同时完成 CSRF 防护。
- Credits 仍是透明估算与成本记录，不是余额、充值、预占、结算、退款与对账的财务账本。

---

## 发布与回滚说明

1. 备份生产数据库与对象存储索引。
2. 发布代码与 `config/video-models/*.json`，执行 `uv run alembic upgrade head`。
3. 保持五个 feature flag 全部关闭，先验证旧 Autopilot、Passenger 手动提交、job 查询、媒体入库与 canonical 显式 promote。
4. 配置 `PLATFORM_API_KEY`、`CREDENTIAL_ENCRYPTION_KEY`、受限 CORS 与 provider 凭据；不得使用开发默认密钥对外服务。
5. 先项目级打开 `voyage_memory`，验证真实 Voyage/本地降级标记、预算和 trace；再分别灰度 `auto_evaluation`、`auto_retry`。
6. 只有收集足够真实 metrics/benchmark 后再打开 `adaptive_router`；Wan transport 未验收前保持 `wan3` 关闭。

发生问题时优先关闭对应 flag，而不是删除新表或回滚已写入的数据。Alembic downgrade 会删除 Phase 3–8 的追加表，可能丢失 prompt revision、canonical promotion、memory、evaluation、metrics、benchmark 与 trace，因此生产回滚应采用“旧代码兼容新表 + flag off”，数据库 downgrade 只作为已备份且确认无新数据的最后手段。

## 下一轮验收清单

- 用真实 Passenger image/video 任务验证 provider/model 选择、成本展示、结果预览、失败文案与 promote-to-canonical 全链。
- 用至少一个真实 Autopilot scene 验证 canonical reference、上一镜尾帧、gaze、单一动作、路由理由、Adapter payload 和 trace。
- 用受控 Voyage 项目验证多模态检索质量、延迟、配额、私有 URL、删除合规；确认 identity verdict 仍由独立证据完成。
- 配置真实视觉 judge 后验证 critical gates，并模拟 retry same/rewrite/switch/limit/unknown-paid-state。
- 建立钱包账本和供应商账单对账后，才把“预计积分”升级为可扣费积分。
- 浏览器回归继续保留 1440px、1024px 与 390px 三个断点，覆盖账号、双模式、移动导航、图片描述优化、费用/积分、素材版本和无横向溢出。

## 本次验证快照

- `uv run pytest -q -p no:cacheprovider`：104 passed；仅有 Starlette/httpx2、Alembic 路径配置和既有循环外键警告。
- `uv run ruff format --check . --exclude references`、`uv run ruff check . --exclude references`、`uv run mypy`：格式、静态规则与 85 个 Python 源文件类型检查通过。
- `node --check apps/web/app.js`、`docker compose config -q`、`git diff --check`：Web 语法、Compose 配置与补丁空白检查通过。
- 产品浏览器验收截图保存在 `output/product-audit/`。
- 该快照只证明仓库自动化和静态语法通过；不等于 Voyage、Google Flow 或其他付费 provider 已做真实消费测试。
