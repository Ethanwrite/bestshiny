# AI Director Platform V1

A working platform for AI short-drama and commercial visual production. Two entry points share one set of asset, memory, routing, generation-gateway, cost and evaluation infrastructure:

- **Passenger Seat**: the user picks image or video, model, duration, resolution and reference assets directly. An image prompt can be corrected and undone separately before submission.
- **Autopilot**: the system compiles a script into `Project → Episode → Scene → Shot → Candidate`, assembles per-shot context, selects a model and runs the quality gates. Internal video prompts stay collapsed by default.

Before taking over development, read [the documentation index](docs/README.md)、[the current handoff](HANDOFF.md)、[the open-issues list](docs/OPEN_ISSUES.md)、[the architecture](CURRENT_ARCHITECTURE.md)、[the production evidence report](docs/PRODUCTION_EVIDENCE.md)、[the readiness checklist](docs/PRODUCTION_READINESS_CHECKLIST.md) and [the requirements ledger](docs/PRODUCT_REQUIREMENTS_LEDGER.md). Source and Skill audits are in [docs/source-audit.md](docs/source-audit.md) and [docs/skill-research.md](docs/skill-research.md).

> **The Phase III offline checkpoint is still not releasable.** The offline algorithm core is frozen at commit `0a74d31`, tag `v0.2.0-algorithm-core-offline`; Phase III was implemented at `99f9c60` with evidence snapshot tag `v0.3.0-production-evidence-core-offline`. The Phase III whole-repository gate was `406 passed, 57 warnings in 71.58s`, and PostgreSQL 17.10 + pgvector 0.8.6 plus a Docker Compose production-like smoke passed on real machines. No live Provider canary may be inferred from code: real Provider calls this round were **0**, known development-caused Provider spend is **USD 0**, and the single-video canary is **NOT EXECUTED**.

The current 2026-08-23 development checkpoint (commit `ea9d042`, no remote) adds migrations `0028_persistent_character_state` through
`0037_direct_uploads`, narrows the public payment flow to a single fixed 30 USDC DePay shared link, adds the
series narrative ledger, and adds the first working image-generation path — `openai/gpt-image-2` through the
OpenRouter Image API, bound as the `IMAGE_GENERATION` role. No video Provider was added and none was called.
The `406 passed` figure above is historical evidence from the previously tagged checkpoint. The current
working-tree gate measures **`612 passed, 2 skipped, 61 warnings`** — the two skipped are opt-in live image
tests — with Ruff check, Mypy (133 source files), the Web production build and npm audit all green. This is
still not an endorsement of real visual-review accuracy, of on-chain payment operations, or of a production
launch.

## What is implemented

- Commercial Auth：邮箱注册/登录/退出、PBKDF2-SHA256 强哈希密码、HttpOnly 会话 Cookie（生产 Secure、SameSite=Lax）、double-submit CSRF、持久登录限流、一次性密码重置、工作空间 `OWNER / ADMIN / EDITOR / VIEWER` 权限与项目/素材/生成路由租户隔离。
- Narrative Compiler：场景、对白/动作事件、单一主动作镜头、输入/输出状态与前后镜头链。
- Passenger/Autopilot 共用 `VisualProductionRuntime`、`GenerationGateway`、`MediaRegistry` 与持久化任务，不存在第二套生成引擎。
- Image Prompt Corrector：只服务用户可见的图片提示词，记录原文、修改结果、变化说明与撤销数据；不参与 Autopilot 视频编译。
- Video Prompt Runtime：`PromptCompilerInput → PromptCompilerService → PromptCompilerOutput → ModelAdapter`；Skill 以内容哈希版本参与审计，Kling、Veo、Seedance、Grok、Wan 分别生成模型专用提示词与 payload。画风锁由 Compiler 自身从 `ProjectStyleService` 解析，任何调用方都不会静默丢失锁定画风。
- Image Generation：`openai/gpt-image-2`（OpenRouter Image API，`POST /images`）是项目的图片模型，绑定为 `IMAGE_GENERATION` 角色的 PRIMARY，fallback 为 Seedream 5.0 与 Flow NARWHAL。支持文生图与图片编辑，单次最多 16 张参考图、10 张出图，400K 上下文。该接口同步返回 base64，因此 Gateway 增加了内联结果通道：`ProviderSubmission.result` 携带终态结果，`MediaRegistry.register_provider_bytes()` 用与下载素材相同的内容校验落库。该结果**不驻留在进程内**：它与「确认提交」同一个事务写入 `provider_synchronous_results`，与「作业终态」同一个事务删除——读取不消费，否则致命窗口只是从「轮询之前」挪到「读取与完成提交之间」。每个输出带 SHA-256，读回时校验；不匹配以 `submitted=True` 失败，进入 `RECONCILIATION_REQUIRED`，而不是把损坏产物当作已付费结果发布。超出模型已审阅执行范围的请求在付费调用之前本地拒绝。
- Batch Candidates：`image_count`（API 层的 `n`）是显式选项，整批在调用前一次性计价与预占积分；返回的每一张图各自成为该镜头的一个 `GenerationCandidate`，供用户挑选，而不是一个结果加几个散落素材。
- Media Plane：用户原图**永不**被重编码或降采样——人物面部、商品标签、织物纹理只存在于上传时的分辨率。Provider 通过 `GenerationProvider.reference_constraints` 声明可接受的像素/字节/格式上限，`RenditionResolver` 按需生成并缓存派生副本（`media_renditions`），以约束摘要为键，上限变化即生成新副本而非复用旧的。参考图 URL 由**对象存储**签发短时效 presigned URL，Provider 直接从对象存储拉取，API 不参与大文件传输；本地磁盘无法签发时**直接失败**而不是退回代理转发。
- Direct Upload：写入同样绕开 API。`POST /v1/assets/uploads` 校验租户与配额后签发 presigned PUT（并把 SHA-256 绑进请求，由对象存储强制校验），客户端自行上传；`POST /v1/assets/uploads/{id}/complete` 通过 `HEAD` 取权威大小、读取 64KB 头部做格式与像素校验后落库。完整解码交给首次使用时的 `RenditionResolver`。未配置对象存储时该接口返回 `501`，仍可使用原有的 `POST /v1/assets` 分片上传。
- Style Lock 双层校验：第一层为本地确定性 64 维描述子（色彩/明度/饱和度/边缘/明显漂移）；第二层为 `google/gemini-embedding-2`（复用现有 OpenRouter 凭据），负责材质、笔触、摄影语言与整体视觉语义。两层各自记录结论，取**更差**的一个，评估时第二层不可用判为 `REVIEW_REQUIRED` 而非放行。**向量带着自己的空间**：`EmbeddingSpaceIdentity`（provider / model / model_revision / input_schema_version / dimension / normalization / distance_metric）随每条 `StyleEmbedding` 落库，比较任何相似度之前先比空间——因为失败模式是「不失败」：跨空间做余弦不会报错，只会返回一个看着挺合理的数。不匹配时落锁被拒、候选判 `REVIEW_REQUIRED` 且**不给分**（不是给低分），reason code 里点名是哪几个字段变了。**落锁则 fail-closed**：`FEATURE_SEMANTIC_STYLE_LOCK=true` 时，若第二层的参考产不出来，落锁直接被拒绝且不写任何东西——模型不可达返回 `503`（等待有意义），参考素材读不出返回 `409`（等待没意义）；锁是 append-only 且触发器禁止重锁，所以一次降级就是永久降级。开关关闭时单层锁仍是预期结果，并记录 `SEMANTIC_EMBEDDER_NOT_CONFIGURED`。
- External Evidence Registry：`config/external-evidence/registry-v1.json`（`external-evidence-v1`，冻结于 2026-08-25）。版本化、来源可追、场景可映射：每条证据保留原始量表与样本量，引用 source_id，并在绑定到本平台模型时声明 `version_match`。只有 A/B 级来源 + `EXACT`/`EXACT_VERSION_UNSPECIFIED_REVISION` + mapping_confidence 非 LOW 才允许影响路由分；一条记录的等级取它引用的**最弱**来源。近似版本的证据（Wan 2.1 之于 2.7、Seedance 2.0 之于 2.5、Veo 3.1 Fast 之于 3.1、GPT-4o 之于 GPT Image 2）**故意保留并标为 mismatch**——删掉只会让人半年后从同一个公开来源重新推导一遍，然后悄悄挂错模型。不做跨来源归一/平均/总榜。目前 12 个生成模型里只有 `veo-3.1-fast`（OSCBench 精确 variant）与 `gpt-image-2` 有逐维证据，其余只有 holistic Arena 偏好；`FEATURE_EXTERNAL_PRIOR` 默认 **false**，通过 `GET /internal/models/external-evidence` 只读查询。
- Model Registry/Router：逻辑模型、provider model ID、信任等级与套餐角色绑定持久化到数据库；持久的 `ModelCapabilityProfile` 是 UI、Policy、Router、Cost 与 Adapter 的单一能力/质量先验事实源，旧 `config/video-models/*.json` 多头配置已移除，Wan 对齐 2.7。路由先执行硬门禁再评分：`modality == video` 与 `video_generation ∈ supported_operations` 排在最前（registry 一张表容纳全部模态，不判模态就会把 embedding/chat 模型排进视频候选），其后是能力、时长、分辨率与 `AssetCriticality`。被拒绝的模型不会从列表里消失，而是进入 `RouterDecision.rejected` 并带上 `reason_codes`，事后可审计。真实观测以每次调用传入的不可变 `RoutingEvidence` 参与评分，不写在共享 router 实例上；低样本真实观测不会覆盖人工先验；`EDGE` 永远不能处理 canonical/hero/important 资产。
- Model live kill switch：Gateway 在排队前与付费调用边界都核对持久模型身份和 `enabled/live_enabled`，最终检查与 Job CAS 同事务；重启不会覆盖管理员禁用状态，请求 metadata 也无法解锁。
- Asset Registry：逻辑资产与不可变版本覆盖人物、场景、商品、道具等素材；只有显式提升才会改变 canonical 版本，数据库触发器防止跨资产链接、未记录切换和历史改写。
- Project Style Lock：用户先把 `STYLE` 版本显式提升为 Canonical，再通过真实账号确认一次性锁定到项目。锁定版本拥有不可变的 64 维本地 Style Embedding 与 hash；后续素材库 Canonical 变更不会改写项目锁。所有 Autopilot 镜头自动继承锁定参考图、Prompt 约束及 Adapter style payload，生成结果逐帧计算相似度、低分比例和漂移斜率；缺证据、低相似度或持续漂移不能通过 Candidate Commit 门禁。
- ModelRoleRuntime：业务模型调用统一按 role 解析 chat/embedding/fact-locked refinement，每次成功或失败都写 `ModelExecutionRecord`；真实模式还必须匹配未过期的 `LiveCanaryPermit`。
- Memory/Context：L0/L1/L2 记忆、metadata-first 检索、文本/图片/视频预算装配。Narrative Memory 的 Voyage 路径已收口到 `ModelRoleRuntime → MULTIMODAL_EMBEDDING`，保存维度/输入/向量哈希而不保存审计向量全文；不可用时降级到 SQL 结构化时间线并记录 `MEMORY_VECTOR_DEGRADED`。
- Evaluation/Retry：结构化质量维度、关键失败、`ACCEPT / RETRY_SAME_MODEL / RETRY_REWRITE_PROMPT / SWITCH_MODEL / REJECT` 与有界重试计划。
- Metrics/Benchmark/Trace：生产指标、基准测试结果与镜头级生产 trace；自适应路由默认关闭。
- Credits admission + lifecycle：所有已认证的公开生成入口都由服务端解析套餐/模型角色/部署可用性/信任/估价；**FREE / PRO / ENTERPRISE 一律**在同一交易中创建 Job、积分预占、CostRecord 和幂等记录。「谁扣款」由 `WorkspaceCreditBalance.billable` 一处定义：所有**套餐**都扣；无 workspace 的项目、以及 `ALL` 工作空间（关闭鉴权时的本地开发旁路，不是套餐）不扣。套餐决定额度发放、折扣与模型权限，不决定一次生成是否要花钱。余额不足返回 **402**，与套餐权限不足的 **403** 区分开——一个是充值，一个是升级。完成时结算，明确的提交前终态会原子退回，跨过付费边界的不确定结果则冻结并进入内部审计对账，不盲退、不盲重试。
- DePay/Base USDC 支付：全站只使用一条固定 30 USDC、Quantity OFF 的 DePay Link。每次点击都先生成独立 PaymentIntent，并注入 `order_ref` 与只保存 hash 的短期 checkout token。签名回调必须匹配订单、Base Mainnet、Circle Native USDC、Treasury 和精确 30 USDC，才会在同一事务中为 FREE 工作空间永久升级 `PRO` 并追加 3,000 Credits；已是 PRO 则只追加 3,000 Credits。无订阅、自动续费或钱包二次扣款。Alchemy 保留为链上重组/对账证据源。
- Candidate + QA + Commit：一个镜头可有多个候选；自动证据不足时可由有写权限的真实用户填写理由并显式确认，形成独立审计记录，再单独采用；采用后原子写入唯一正式候选、时间线快照、尾帧与成本记录。
- Persistent Narrative Character State：已将不可变的角色 identity 与可随剧情变化的伤口、衣物破损/污渍/湿润、道具、位置、时间和灯光状态硬隔离。每个候选以显式 JSON Patch 提议 delta，先过确定性 policy，再校验与候选输出绑定的可视证据；只有采用候选时才追加新版本、commit 记录，通过 branch-aware head CAS 前移并传播给下一镜。旧版本、delta、验证与 commit 全部保留，保留审计/比较所需事实并拒绝过期冲突。
- 状态提议只能在 Candidate 仍为 `CREATED`、生成尚未 dispatch 时，于 Candidate/Generation Job 分配事务内写入。全部提议的 proposal-set hash 同时绑定 Candidate 与 Generation Job，并在 validate/commit 再校验，阻断生成后偷换 delta。显式 `branch_key` 可从 input TimelineState 选定的不可变状态版本创建独立 scope v1/head，不推进 main head。
- 输入/目标状态 JSON 在服务边界限制为最大 256 KiB、5,000 个节点、12 层深度和 200 条 continuity constraints。baseline initialize 只更新 authoritative TimelineState 中的有类型状态引用并传播，不额外写入第二个无类型 `ShotStateSnapshot`。
- Timeline v3 + 三镜 Fixture：`TimelineTransition` 以九种显式类型控制传播、分支、空间重置与 reconciliation；修改前镜状态只会标记下游 `RECOMPUTE_REQUIRED`，规划重算不会改写已提交成片。离线回归已走过 3 Candidates/Jobs/MP4 outputs/QA/commits/end frames/snapshots/accepted costs，但不等于真实 Provider。
- Character Evidence V1：本地 FFmpeg 抽帧、可注入检测/跟踪/人脸与外观 encoder、视角感知参考选择、可见度/清晰度/检测/跟踪置信加权、时序汇总和版本化阈值已接入 QA。当前证据来自自生成非用户 MP4 + 确定性推理替身；生产检测/跟踪/编码模型尚未部署，hair/costume 诚实为 `UNAVAILABLE`。
- Production Evidence：`ProviderBillingEvidence` 分离 verified/estimated/manual/unknown，Provider 无可信金额时 `actual_cost = null`；accepted-shot cost 包含失败与 repair attempts，`DecisionOutcomeRecord` 串联镜头特征、决策、Provider/模型、QA、用户结果和成本来源。
- Flow Affinity：首次自动分配、sticky account/project、本地 active 唯一、远端 ID 跨全部历史状态永久唯一、显式迁移计划与 local job/account/project/provider job 四元 poll 标识已实现；默认 provisioner fail closed，本轮未调用真实 Flow。
- 不可洗白来源：Provider 参考素材上传只写独立 binding，不覆盖 `MediaAsset` 生成来源；角色 identity、canonical promotion 与 candidate commit 都在终点复核 Provider trust。
- Media：内容字节按 SHA-256 共享，镜头/候选来源链保持独立；供应商媒体上传带并发 claim、租约和付费边界 fencing，本地与 S3/R2/MinIO 存储共用同一注册表。工作空间存储已按真实 `size_bytes` 执行原子 reserve/settle/release，不确定结果保留 hold 而不盲目释放。
- Workbench：注册/登录遮罩、自主创作/智能导演双模式、中文画面描述优化、人物主参考 v1/v2 重新提交、场景/产品/道具通用版本上传及指定镜头重做入口；界面只显示通俗说法，内部合同和模型指令默认收起。

## One-command start (Docker)

> 下列命令是 MVP 的运行方式。`0023` 的 SQLite recovery 阻断已修复，但仍不要直接对现有混合 schema 开发库执行；应先读交接文档、备份并在临时库验证。

需要 Docker Desktop 或兼容的 Docker Engine：

```bash
cp .env.example .env
export CREDENTIAL_ENCRYPTION_KEY="$(python3 -c 'import base64,secrets; print(base64.urlsafe_b64encode(secrets.token_bytes(32)).decode())')"
export PLATFORM_API_KEY="$(python3 -c 'import secrets; print(secrets.token_urlsafe(48))')"
docker compose up --build
```

- 工作台：<http://localhost:3000>
- API 文档：<http://localhost:8080/docs>
- MinIO 控制台：<http://localhost:9001>

Compose 包含 Web、FastAPI、后台 worker、PostgreSQL + pgvector、MinIO 及自动建桶任务。首次启动或升级旧数据库前，应先备份数据库并确认迁移：

```bash
uv run alembic current
uv run alembic upgrade head
```

当前迁移代码链为单 head `0034_narrative_ledger`。`0030` 增加 Alchemy Delivery、Base USDC 付款事实与追加式购买积分 Ledger；`0031` 增加通用链上 PaymentIntent；`0032` 增加 DePay checkout session 与不可改写的签名回调收据；`0033` 把 DePay checkout 绑定到独立 PaymentIntent，并将 FREE→PRO 与 3,000 Credits 入账收口到同一事务。PostgreSQL 实机证据仍只到 `0032`，上线前必须在临时 PostgreSQL 验证 `0033`；历史 Compose 证据仍只背书到 `0027`。

默认本地 `data/platform.db` 的 Alembic stamp 仍为 `0020`，同时已有部分 `0021`–`0023` 新表，但 `workspaces` 缺少新列，属于混合 schema。必须先备份和审计，不要手工 stamp 或盲目升级。旧版 `local@ai-director.invalid` 工作空间保持隔离，普通注册不会自动认领；如需转移，必须调用下文说明的受保护内部接口。

## Local development

> 2026-08-25 起：schema 唯一权威是 alembic，启动不再建表而是校验 stamp。历史上那份混合 schema 的 `data/platform.db` 已不再是运行时目标——本地 `DATABASE_URL` 指向 compose 的 PostgreSQL。

需要 Python 3.12+、`uv`、FFmpeg，以及一个 PostgreSQL。SQLite 不再是受支持的运行时：pysqlite 下
`begin_nested()` 的 savepoint 不会随外层事务回滚，而有七处调用点依赖这次回滚；`DEPLOYMENT_ENVIRONMENT=production`
时启动会直接拒绝非 PostgreSQL 的 `DATABASE_URL`。

```bash
cp .env.example .env
docker compose up -d postgres
uv sync --extra dev
uv run alembic upgrade head
uv run uvicorn video_platform_api.main:app --reload --port 18080
```

Schema 的唯一权威是 alembic。应用启动**不再**建表，只校验数据库 stamp 是否等于
`REQUIRED_SCHEMA_REVISION`，不等就拒绝启动并提示 `alembic upgrade head`。

另一个终端运行 worker：

```bash
uv run video-platform-worker
```

再启动静态 Web：

```bash
python3 -m http.server 18081 --directory apps/web
```

- 本地 Web：<http://127.0.0.1:18081>
- 本地 API 文档：<http://127.0.0.1:18080/docs>

静态开发页默认访问 `http://127.0.0.1:18080`；Docker 中由 Nginx 把 `/api` 转发到 8080。

## Key environment variables

完整模板见 [.env.example](.env.example)。

| Variable | Default / purpose | Production guidance |
| --- | --- | --- |
| `DATABASE_URL` | PostgreSQL（`docker compose up -d postgres`，已发布到 `127.0.0.1:5432`） | 使用受管数据库并先备份再迁移；production 下非 PostgreSQL 会被拒绝启动 |
| `STORAGE_BACKEND` | `local`，也支持 S3 兼容存储 | 配置私有 bucket、最小权限密钥 |
| `MAX_UPLOAD_BYTES` | 单文件默认最多 100 MiB，流式超限即中止并清理临时文件 | 与工作空间 `max/used/reserved_storage_bytes` 原子配额一起监控 |
| `PUBLIC_BASE_URL` | `http://localhost:8080` | 设置为实际 HTTPS API 地址 |
| `MODEL_CONFIG_ROOT` | 历史兼容变量，旧 `config/video-models/*.json` 事实源已移除 | 不要用它创建第二套 Capability 真相 |
| `MODEL_INFRASTRUCTURE_CONFIG` | `./config/model-registry/defaults.json` | 版本化发布逻辑模型、信任等级与套餐角色默认绑定；运行时管理员配置存入数据库 |
| `PLATFORM_API_KEY` | 仅供内部/管理员路由的服务密钥；空值时 fail-closed | 必须设置高熵密钥；不可交给浏览器 Worker |
| `DEPLOYMENT_ENVIRONMENT` | 未设置时按 `production` 处理 | 生产保持 `production`；只有本地才用 `development` |
| `AUTH_REQUIRED` | 默认 `true`，要求用户登录 | 生产必须为 `true`；显式关闭会拒绝启动 |
| `AUTH_SESSION_TTL_DAYS` | 默认 30 天 | 根据企业安全策略调整（最多 90 天） |
| `CREDENTIAL_ENCRYPTION_KEY` | 开发态空值使用进程级随机临时密钥 | 生产必须使用高熵 Fernet key，缺失或弱密钥会拒绝启动 |
| `WEB_ORIGINS` | 仅本地开发来源 | 只列出可信 Web Origin |
| `VOYAGE_API_KEY` | 空值不调用 Voyage | 与 `FEATURE_VOYAGE_MEMORY` 一起灰度启用 |
| `VOYAGE_MULTIMODAL_MODEL` | `voyage-multimodal-3.5` | 更换模型前验证 embedding 兼容性 |
| `MEMORY_EMBEDDING_DIMENSION` | `512` | 允许 256/512/1024/2048；变更需重建对应向量 |
| `MEMORY_MAX_*` | 上下文字符、token、图片、视频预算 | 按供应商限制压测后调整 |
| `MAX_AUTO_RETRIES` | `2` | 结合成本上限与人工升级策略设置 |
| `GENERATION_POLL_INTERVAL_SECONDS` | `2` 秒，远程生成任务的轮询间隔 | 过低会对 provider 造成忙轮询；过高会延迟完成检测 |
| `GENERATION_CLAIM_LEASE_SECONDS` | `300` 秒，生成提交/轮询/完成落库的数据库租约 | 必须大于单次轮询与媒体下载超时；不得用于绕过未知付费请求保护 |
| `FLOW_API_BASE/FLOW_API_KEY/FLOW_PROJECT_ID` | Google Flow 运行配置 | 仅使用用户合法账号、项目与主动授权 |
| `PROVIDER_MODE` / `ALLOW_LIVE_PROVIDER_CALLS` / `LIVE_PROVIDER_CONFIRMATION` | 默认 Mock/关闭 | 即使三重门开启，真实边界仍必须匹配持久 `LiveCanaryPermit` |
| `ALCHEMY_WEBHOOK_SIGNING_KEY` / `ALCHEMY_WEBHOOK_ID` | 校验 Alchemy Delivery 的签名与可选 Webhook 身份 | 只放 Secret Manager；签名必须覆盖未经 JSON 重排的原始 body |
| `ALCHEMY_NETWORK` / `ALCHEMY_TREASURY_ADDRESS` | `BASE_MAINNET` 与平台 USDC 收款地址 | 地址需与 Alchemy Address Activity 跟踪目标和前端付款目标一致 |
| `ALCHEMY_CREDITING_ENABLED` / `ALCHEMY_USDC_MICROUNITS_PER_CREDIT` | 默认关闭；默认 10,000 微单位（0.01 USDC）兑换 1 credit | 真实小额回归和对账 Runbook 通过前保持关闭 |
| `LEGACY_WALLET_PAYMENTS_ENABLED` | 默认 `false` | 旧的自定义金额/钱包绑定 API 仅作兼容代码；公开商业流程不得开启 |
| `DEPAY_PAYMENT_LINK_URL` / `DEPAY_LINK_ID` | DePay 共享 Base Native USDC Payment Link 及其 ID | 链接必须仅收 Base Native USDC，并与 Treasury 地址一致 |
| `DEPAY_CALLBACK_PUBLIC_KEY` | DePay 在启用 callback 后生成的 RSA 公钥 | 用于验证原始 body 的 `x-signature`；不要用任意未签名请求入账 |
| `DEPAY_OFFER_AMOUNT_USDC` / `DEPAY_OFFER_CREDITS` / `DEPAY_OFFER_UPGRADE_PLAN` | 默认 `30` / `3000` / `PRO` | 服务端单一 Offer 事实源；修改时必须同步 DePay 固定金额 Link |

### Object-storage browser CORS

`POST /v1/assets/uploads` 返回的预签名 PUT 由浏览器直接请求对象存储，因此 API 的
`WEB_ORIGINS` 配置并不能代替 bucket CORS。对象存储必须为 `WEB_ORIGINS` 中每一个实际
前端 Origin（协议、域名和端口必须完全一致）允许：

- 方法：`PUT`、`GET`、`HEAD`；
- 请求头：至少 `content-type`、`x-amz-checksum-sha256`，或受控的 `x-amz-*`；
- 响应头：建议暴露 `ETag` 和请求追踪 ID；
- 不要在使用 Cookie 的 API CORS 中用 `*`，bucket 也优先列出精确生产 Origin。

上线前运行 `uv run python scripts/verify_object_storage.py`。它会从每个 `WEB_ORIGINS`
来源模拟浏览器 OPTIONS 预检，并在 CORS、预签名上传、校验、HEAD、Range GET 或
Provider 可取的 HTTPS URL 任一环节失败时返回非零。

## Feature flags

所有高风险自动化默认关闭。环境变量提供全局默认值，数据库可设置全局或项目级 override；项目级优先。

| Flag | Environment variable | Behaviour when enabled |
| --- | --- | --- |
| `voyage_memory` | `FEATURE_VOYAGE_MEMORY` | Autopilot 自动检索/写入项目记忆；远程 embedding 统一经 `ModelRoleRuntime`，失败则记录降级并仅使用结构化时间线 |
| `auto_evaluation` | `FEATURE_AUTO_EVALUATION` | 生成完成后触发结构化评估 |
| `auto_retry` | `FEATURE_AUTO_RETRY` | 根据评估结果执行最多 `MAX_AUTO_RETRIES` 次修复；不会绕过未知付费请求保护 |
| `adaptive_router` | `FEATURE_ADAPTIVE_ROUTER` | 把已有生产指标与 benchmark 调整加入模型评分 |

运行时查询与修改：`GET /internal/feature-flags`、`PUT /internal/feature-flags/{name}`。这些接口与业务 API 使用相同的 Bearer API Key 防护。

## Main API

API 的完整请求/响应 schema 以 `/docs` 为准。普通用户使用登录会话 token；`/internal/*`与供应商账号管理使用 `PLATFORM_API_KEY`；浏览器 Worker 只使用管理员发行的、绑定 worker/account/provider 的可撤销专用凭证。三者均不可混用。

### Users and generation

| Method | Path | Description |
| --- | --- | --- |
| `GET` | `/health` | 健康检查 |
| `POST` | `/api/auth/register`、`/api/auth/login` | 创建账号/工作空间或登录；成功后设置 HttpOnly 会话 Cookie |
| `GET/POST` | `/api/auth/me`、`/api/auth/logout` | 查看当前账号或撤销当前会话 |
| `POST` | `/api/auth/password-reset/request`、`/api/auth/password-reset/confirm` | 申请/消费一次性密码重置 token，成功后撤销旧会话 |
| `POST` | `/api/prompt/correct` | 图片提示词整理与可撤销修订 |
| `POST` | `/api/pricing/estimate` | 生成前成本与积分估算 |
| `GET` | `/api/workspaces/{workspace_id}/credits` | 查询余额、冻结额、生命周期聚合与最近事件 |
| `POST` | `/v1/webhooks/alchemy` | 公开但必须通过 Alchemy HMAC；接收 Base Native USDC Address Activity Delivery |
| `POST` | `/v1/webhooks/depay` | 公开但必须通过 DePay RSA-PSS `x-signature`；验证付款并追加积分 Ledger |
| `GET` | `/v1/payments/config`、`/v1/workspaces/{workspace_id}/billing` | 读取 DePay/Base 配置和工作空间积分 |
| `POST` | `/v1/workspaces/{workspace_id}/depay-checkouts` | 为已登录工作空间创建 DePay 充值会话与带上下文的共享链接 |
| `GET` | `/v1/workspaces/{workspace_id}/depay-checkouts/{checkout_id}` | 查询充值会话状态供 Web 轮询 |
| `POST` | `/api/passenger/generate` | 乘客模式提交图片/视频任务 |
| `POST` | `/v1/shots/{shot_id}/generate` | 自动导演生成镜头候选 |
| `GET` | `/v1/generations/{job_id}` | 查询生成任务 |
| `POST` | `/api/generations/{job_id}/promote` | 把完成结果新增为资产版本，并可显式提升为 canonical |
| `POST` | `/v1/shots/{shot_id}/candidates/{candidate_id}/human-review` | 对需要人工确认且没有硬失败的候选填写理由并显式确认；仍需另行采用 |
| `POST` | `/v1/characters/{character_id}/narrative-state/initialize` | 从已采用镜头建立人工显式确认的角色叙事状态 v1 |
| `GET` | `/v1/projects/{project_id}/characters/{character_id}/narrative-state` | 读取指定 timeline scope 的当前状态 head 和不可变 identity 绑定 |
| `GET` | `/v1/shots/{shot_id}/candidates/{candidate_id}/state-transitions` | 读取 candidate 的 delta、分阶段验证和已提交版本审计视图 |

### Assets

| Method | Path | Description |
| --- | --- | --- |
| `POST` | `/api/assets` | 创建逻辑资产 |
| `GET` | `/api/projects/{project_id}/assets` | 按项目/类型列资产 |
| `GET` | `/api/assets/{asset_id}` | 查看资产与版本 |
| `POST` | `/api/assets/{asset_id}/versions` | 新增不可变版本 |
| `POST` | `/api/assets/{asset_id}/versions/{version_id}/promote` | 显式切换 canonical 版本 |
| `GET/POST` | `/api/projects/{project_id}/style-lock` | 查询或由真实用户明确确认一次性锁定项目画风；锁定后不可替换 |

### Internal runtime and observability

| Method | Path | Description |
| --- | --- | --- |
| `POST` | `/internal/router/video` | 获取可解释的视频模型排名 |
| `POST` | `/internal/memory/index`、`/internal/memory/search` | 写入/检索多层记忆 |
| `GET` | `/internal/memory/characters/{entity_id}`、`/internal/memory/scenes/{scene_id}`、`/internal/memory/state` | 查询人物、场景与当前状态 |
| `POST` | `/internal/evaluate/video`、`/internal/generations/{job_id}/evaluate` | 评估规格或已生成任务 |
| `POST` | `/internal/retry/plan` | 返回有界重试/换模型计划 |
| `POST` | `/internal/models/metrics` | 写入追加式生产指标 |
| `GET/POST` | `/internal/benchmarks`、`/internal/benchmarks/results` | 读取 benchmark 清单/写入结果 |
| `GET` | `/internal/shots/{shot_id}/traces` | 查询镜头生产 trace |
| `POST` | `/internal/workers/credentials` | 管理员发行绑定 worker/account/provider 的专用凭证（明文只返回一次） |
| `POST` | `/internal/workers/credentials/{credential_id}/revoke` | 撤销 Worker 凭证，已连接 WebSocket 也会在下一次循环被关闭 |
| `POST` | `/internal/auth/legacy-workspaces/claim` | 将隔离的 V1 本地工作空间显式转移给已存在用户，要求幂等键并写入审计记录 |
| `POST` | `/internal/provider-media-bindings/{binding_id}/reconcile` | 对结果不确定的供应商素材上传执行“验证已有 ID”或“确认远端未创建”，全程审计且禁止盲目重传 |
| `POST` | `/internal/generations/{job_id}/credit-reconcile` | 仅 `PLATFORM_API_KEY`；对不确定的原预占确认 Provider 已接受或未创建，金额由服务端派生并强制幂等/审计 |
| `POST` | `/internal/provider-budget-reservations/{reservation_id}/reconcile` | 仅 `PLATFORM_API_KEY`；根据账单证据结算或释放 `UNCERTAIN` Provider USD 预算，强制显式确认、证据引用与幂等键 |
| `POST/GET` | `/internal/live-canary-permits` | 仅 `PLATFORM_API_KEY`；在显式确认+幂等键后创建有效期/请求数/成本上限的持久 Permit，或查看脱敏使用状态；创建 Permit 不会自动发起调用 |
| `GET` | `/internal/production-evidence?project_id=...` | 仅 `PLATFORM_API_KEY`；按 project/job/shot 查看脱敏的 model execution、Provider job/Flow binding、QA、decision、cost/billing 与 timeline evidence |
| `POST` | `/v1/workers/{worker_id}/socket-ticket` | Worker 用专用 Bearer 凭证换取短期一次性 WebSocket ticket |

## Security and degradation principles

- `PLATFORM_API_KEY` 为空时，内部/管理员 HTTP 路由返回 503；生产环境中的弱 key 也会拒绝启动。Worker 凭证只能注册、心跳、领取命令与回传结果；不能访问内部 QC/管理路由。WebSocket 使用一次性短期 ticket 或 Authorization header，不在 URL query 传密钥。
- 密码经 PBKDF2-SHA256（60 万轮）加随机 salt 存储，会话仅保存 SHA-256 token 哈希并带过期/撤销时间；浏览器使用 HttpOnly Cookie，unsafe cookie 请求使用 double-submit CSRF，Bearer/internal 路径保持兼容且不放弱权限边界。
- 普通注册永远只创建新的空工作空间，不会自动认领升级前的本地数据。Legacy 项目默认属于隔离账号且对新用户不可见；只能通过 `PLATFORM_API_KEY` 保护的内部路由、指定已存在的活跃用户并提供幂等键后转移，结果会持久化审计。
- 生产环境缺少合法高熵 `CREDENTIAL_ENCRYPTION_KEY` 时拒绝启动。本地开发的空 key 降级为每进程随机临时 key，重启后旧凭据不可解密，不能用于共享或生产。
- 用户上传仅允许验证过的 PNG/JPEG/WebP/MP4/MOV/WebM；网关在 multipart 解析前和流式接收时限制大小，下载设置 `nosniff` 与安全 Content-Disposition。供应商返回的媒体 URL 只允许 HTTPS/已配置主机，逐跳校验 DNS/重定向并拒绝私网、loopback 与云元数据地址。
- Voyage 不再由业务路径直连；Narrative Memory 通过 `ModelRoleRuntime` 请求 embedding。`voyage-multimodal-3.5` 的结果在类型和持久层都只能是 `ADVISORY`，用于跨模态检索、相似度辅助或证据帧排序；不得输出 identity verdict、状态事实、delta 批准或 commit 授权。不可用时记录 `MEMORY_VECTOR_DEGRADED` 并继续使用结构化 SQL 时间线。
- 角色状态可视证据只有在同项目、同候选输出素材上由成功的 `VLM_REVIEWER` 执行生成，并显式声明 `CHARACTER_STATE_FACT_OBSERVATION` provenance 时，才可作为 `FACT_OBSERVATION`。Voyage、缺执行记录、绑定了其他素材或低置信证据均 fail closed 到人工复核；高置信不匹配则拒绝。
- 真实模式下，三重 live 环境门之外还要求 Provider+模型精确匹配的持久 `LiveCanaryPermit`；到期、请求数或成本触顶都是硬停。跨过远程边界后未返回可信账单的用量保持 `UNCERTAIN`。
- 专用视觉评审器缺失或证据不足时，不会伪装成高置信通过；需要人工确认或返回修复/拒绝决策。
- 当前 Style Embedding 是可审计的本地确定性 64 维颜色/明度/饱和度/边缘描述符，适合离线约束和回归，不等于已校准的生产视觉模型。候选视频用本地 FFmpeg 抽帧后计算相似度；生产上线前仍需以批准数据校准阈值或接入版本化的学习型风格 encoder。
- Gateway 对状态未知的付费提交不盲目重发；自动重试使用新幂等键并受次数上限控制。
- 多 worker 通过数据库 CAS claim token 与可过期租约串行化付费提交、供应商轮询和完成落库；跨过付费边界后失联必须先人工对账，不会自动再提交。
- 供应商参考素材首次上传按素材、Provider、账号原子领取；付费边界后的未知结果保持 `NEEDS_RECONCILIATION`，只能通过受保护接口验证远端 ID，或由管理员明确确认远端未创建后再释放重传资格，所有操作写审计记录。
- Google Flow Worker 只在用户已登录且主动授权的浏览器上下文工作，不绕过 CAPTCHA、登录、风控或平台访问控制。
- 模型 Adapter 只负责编译模型专用 prompt/payload，不等于供应商 transport 已接通。

## Quality gates

```bash
uv run ruff check . --exclude references
uv run mypy
uv run pytest -q                                        # SQLite —— 快，算法层
POSTGRES_PASSWORD=... uv run pytest -q --database=postgres   # PostgreSQL —— 事务/并发/触发器
docker compose config -q
```

测试矩阵有两半。SQLite 那半足够覆盖纯算法与简单服务；事务、savepoint、锁、CAS、幂等、账本与触发器行为
只有 PostgreSQL 那半能真正回答——它把共用的 `container` fixture 接到 `video_platform_test` 库里的一次性
schema 上。自建 `Settings`、硬编码 SQLite URL 的测试模块（约十五个）不会被改道。

两半都必须绿。已知的引擎差异如果是**被测代码**的缺陷而非测试的缺陷，登记在 `POSTGRES_KNOWN_DIVERGENCES`
里作为 strict xfail，修好那天 PostgreSQL 这半会失败，直到把条目删掉为止。

### Stable baseline before Phase II

截至 2026-08-20，完整测试套件为 **205 passed**。新增回归覆盖注册/登录/会话撤销、工作空间角色与跨租户阻断、生产认证不可关闭、Worker 专用凭证绑定/撤销/过期与一次性 WebSocket ticket、上传主动内容/伪装 MIME/请求大小阻断、媒体下载 SSRF/私网 DNS/重定向/流式限额、受保护媒体访问、中文图片描述事实保持/撤销、模型路由、五种视频 Adapter、canonical 资产与数据库不变量、人工复核审计、记忆检索与预算、评估/重试、指标/benchmark、Passenger/Autopilot 共用运行时，以及多 worker 提交/轮询/完成竞态、候选正式采纳竞态、成本记录幂等、相同字节独立来源链、供应商媒体并发上传/人工恢复、取消/重启容量释放、历史预约迁移与终态不可回退。

真实浏览器已在 1440px、1024px 与 390px 验证注册、退出/重新登录、双模式切换、中文画面描述优化/恢复原文、人物主参考 v1→v2、场景资产 v1→v2 与显式正式版切换；三个断点无横向溢出，控制台无 error/warning。

### Phase III tag gates and current working-tree gates

离线基线的历史冻结结果是 **348 passed, 39 warnings**。Phase III tag 历史完整套件为 **406 passed, 57 warnings in 71.58s**；Mypy（121 source files）、Ruff lint、Ruff format（226 files already formatted）、Node syntax 与 `git diff --check` 通过。57 个 warning 主要来自已知的 Alembic/SQLite/Starlette 弃用警告与 SQLAlchemy FK cycle warning。

2026-08-22 当前未发布工作树实测为 **468 passed, 61 warnings in 139.29s**；Ruff check、Mypy（132 source files）、Web 生产构建、npm audit 与 Alembic 单 head 全绿。PostgreSQL 17 fresh `0032`/`alembic check` 是前一迁移的证据，当前 `0033` 仍需 PostgreSQL 验证；历史 Compose smoke 只运行到 `0027`。

PostgreSQL 17.10 + pgvector 0.8.6 已在临时实机数据库验证 fresh/populated migration、`vector(16)`、关键索引/唯一性/外键、积分事务、生成 enqueue 事务和 head `0027`。Docker Desktop 29.5.3 上 Compose config、API/worker/Web 镜像 build、up、PostgreSQL/MinIO/API health、Web/Worker 运行、宿主 HTTP 200 smoke 和容器内 Alembic head/check 均通过；使用纯假 development smoke 凭据，无 Provider key/live call，并在结束后不删卷 shutdown。完整证据见 [the production evidence report](docs/PRODUCTION_EVIDENCE.md) 和 [the current handoff](HANDOFF.md)。

## Incomplete, or needing real-environment verification

- Seedance、官方 Veo、Grok、Omni、Kling、Runway 目前主要是持久能力配置、Adapter 或诚实的未配置 provider slot；本轮无真实调用，不能把 payload/Mock 测试等同于供应商端到端。
- Wan 2.7 只有 **T2V** 经过真实调用验证（2026-08-25，一段 5s 720P）。请把这个结论读窄：T2V 是唯一没有 `media` 数组的模式，且那次 canary 未设 negative prompt、未带音频，所以它没有触碰任何 I2V/R2V 线上协议字段。2026-08-25 依据官方 T2V/I2V/R2V API 参考重新校正了请求体（`media.type` 直接携带语义角色而非 image/video/audio；`negative_prompt` 属于 `input`；音频分为 T2V `input.audio_url`、I2V `driving_audio`、R2V 参考素材内嵌 `reference_voice`；T2V/I2V 均不接受 reference image；时长 2–15 秒，R2V 带参考视频时上限 10 秒），细节见 HANDOFF §12d。**I2V 与 R2V 已于同日完成真实调用验证**：两段 2 秒 720P，任务 `fb7cf016`（I2V，`media.type=first_frame`）与 `57ba09a0`（R2V，`media.type=reference_image`），均 `COMPLETED` 且产物经 range 取回确认为真实 MP4 容器（`ftyp`/`isom`，约 400 KB）。参考图由阿里侧从 OSS 预签名 GET 抓取，`FETCHABLE_URL` 链路首次端到端跑通；同时确认 `input.negative_prompt`、`duration: 2`（校正后的下限）与 R2V 的 `parameters.ratio` 均被接受。累计已知消费：3 段片（T2V 5s、I2V 2s、R2V 2s）。**仍未验证**：三条音频通道（T2V `audio_url`、I2V `driving_audio`、R2V `reference_voice`）都需要可抓取的音频素材，本轮未跑。
- Voyage Multimodal 已经 `ModelRoleRuntime` 接入并可安全降级，但实际 text/image/multimodal embedding canary 都是 **NOT EXECUTED**；上线前需验证真实维度、检索质量、延迟、账单与数据合规。
- 生成积分的 Reserve → Generate → Settle / Refund → Reconcile 已闭环。固定 30 USDC PaymentIntent、DePay 共享链接、checkout token、签名 callback、FREE→PRO 与追加式入账已实现，但真实小额支付、运营对账 UI 和 PostgreSQL/Compose `0033` 证据尚未完成。签名 callback 公钥与 Treasury 未配置时，Web 付款按钮会 fail closed。
- Google Flow 已实现首次自动 affinity、sticky account/project、双向唯一数据库约束、限制性 migration plan 和四元 poll 标识；默认 provisioner 在无可用真实部署时 fail closed。无真实 Flow account/project canary，仍不可开启商用 live。
- Web 已接通生成结果确认、选择已有/新建逻辑资产、人物/场景/产品修改图重新提交与显式 canonical 提升；完整版本对比和 promotion 审计历史的专用页仍待完善。
- HttpOnly/Secure/SameSite Cookie、CSRF、找回密码和持久登录限流已完成；邮箱验证、MFA、成员邀请/移除、设备会话和完整安全事件仍需在公网商用前完成。
- 单文件流式限制与工作空间级存储 reserve/settle/release 已接入；生产套餐配额值、保留期和删除运营政策仍待审核。
- CharacterEvidence V1 已对自生成本地 MP4 执行真实抽帧与证据聚合，但检测/跟踪/人脸/外观模型仍是可注入边界+测试替身，不是已部署的生产视觉 AI。
- Persistent Narrative Character State 已用“米拉镜头 12 基线 → 镜头 13 伤口血迹/信号弹位置/站位 delta → policy + 可视证据 → commit v2 → 镜头 14 继承”离线回归覆盖；这证明事务与传播合同，不证明生产 VLM 的视觉判定精度。
- Project Style Lock 已覆盖版本绑定 embedding、一次性锁定、锁后资产库 Canonical 变化隔离、Prompt/参考图/Adapter payload 自动继承、相似度/漂移证据持久化和 Commit 硬门禁；尚未对真实用户作品校准 descriptor/阈值，也未执行任何 Provider 端 style-control canary。
- Docker 本地生产类似 smoke 已通过，但五个真实 Provider canary 仍未执行；单视频 canary 明确为 **NOT EXECUTED**。
- Adaptive router 需要真实样本与 benchmark 结果才能超过静态先验；低样本期应保持 feature flag 关闭并人工审阅。
